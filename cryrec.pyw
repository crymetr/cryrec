"""CryRec: record a chosen input (mic) and a chosen output (speaker loopback) together.

Each source is written to its own WAV while recording. On stop, the tracks are mixed
into one MP3 and kept as FLAC (me / them) for later transcripts.

Portable mode: if a file named "portable" sits next to the exe, settings live there too.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # trust the Windows certificate store, so HTTPS works behind TLS-inspecting proxies (Zscaler etc.)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import lameenc
import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf
import soxr

import callwatch
from ui_theme import (THEME_MODES, LevelMeter, PillButton, Theme, Tooltip, enable_dpi_awareness,
                      work_area)
import dictate
import summarize as sm
import transcribe as tr

APP = "CryRec"
VERSION = "0.9.2"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
PORTABLE = (APP_DIR / "portable").exists()
CONFIG_DIR = APP_DIR if PORTABLE else Path(os.environ.get("APPDATA", Path.home())) / APP
CONFIG_FILE = CONFIG_DIR / "cryrec.json"
VOCAB_FILE = CONFIG_DIR / "vocabulary.txt"
VOCAB_TEMPLATE = """# CryRec vocabulary: helps the transcript spell your names and terms right.
# One term per line, e.g.:   Power BI
# Fix a word that keeps coming out wrong:   power pi -> Power BI
# Lines starting with # are ignored.
"""
DEFAULT_OUT = (APP_DIR / "Recordings") if PORTABLE else Path.home() / "Documents" / "Recordings"
NO_DEVICE = "(none)"
CHUNK_MS = 50
GAP_PAD_SEC = 0.25  # loopback sends nothing during silence; pad gaps larger than this
MIX_RATE = 48000
MIX_BLOCK_SEC = 10
REPO = "crymetr/cryrec"
UPDATE_EVERY = 3600  # seconds; unauthenticated GitHub API allows 60/hour per IP
UPDATE_DIR = (APP_DIR / "updates") if PORTABLE else Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP / "updates"
MUTEX_NAME = "CryRec-singleton-A38983BB-4076-4045-A6FA-5273C2036256"
MODELS_DIR = (APP_DIR / "models") if PORTABLE else Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP / "models"
ENGINES = {"local": "This PC", "server": "My server", "api": "Online API"}
CLEAN_ENGINES = {"off": "Off", "claude": "Cloud AI", "server_claude": "My server (cloud AI)"}
POLISH_MODES = {"auto": "Auto", "off": "Off", "claude": "Cloud AI", "server_claude": "My server (cloud AI)"}
NOTES_ENGINES = {"off": "Off", "claude": "Cloud AI", "openai": "OpenAI-compatible API",
                 "ollama": "Ollama (local)", "server_local": "My server (local AI)",
                 "server_claude": "My server (cloud AI)"}
DEFAULT_API_URL = "https://api.groq.com/openai/v1"
DEFAULT_API_MODEL = "whisper-large-v3-turbo"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


class Updater:
    """Auto-update from GitHub Releases (frozen builds only).

    Checks on startup and hourly. A newer release is downloaded in the background,
    then on_ready(version) fires once. apply() installs it: the installed build runs
    the setup silently, the portable build swaps CryRec.exe in place. Every failure
    is swallowed; an update check must never crash or block recording.
    """

    def __init__(self, on_ready):
        self.on_ready = on_ready
        self.version = None
        self.path = None
        self._lock = threading.Lock()

    @staticmethod
    def _ver(s):
        parts = []
        for p in str(s).lstrip("v").split("."):
            try:
                parts.append(int(p))
            except ValueError:
                break
        return tuple(parts)

    def check(self):
        """Returns "newer", "latest", "busy", "dev" or "failed"."""
        if not FROZEN:
            return "dev"
        if not self._lock.acquire(blocking=False):
            return "busy"
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"User-Agent": f"{APP}-updater", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            tag = (data.get("tag_name") or "").lstrip("v")
            if not tag or self._ver(tag) <= self._ver(VERSION):
                return "latest"
            if tag == self.version:
                return "newer"
            suffix = "-portable.zip" if PORTABLE else "-setup.exe"
            asset = next((a for a in data.get("assets", [])
                          if a["name"].lower().startswith("cryrec-") and a["name"].lower().endswith(suffix)), None)
            if not asset:
                return "latest"  # release still building
            UPDATE_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPDATE_DIR / asset["name"]
            if not (dest.exists() and dest.stat().st_size == asset.get("size")):
                tmp = dest.with_name(dest.name + ".part")
                dl = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": f"{APP}-updater"})
                with urllib.request.urlopen(dl, timeout=120) as resp, open(tmp, "wb") as fh:
                    while chunk := resp.read(1 << 16):
                        fh.write(chunk)
                os.replace(tmp, dest)
            if PORTABLE:
                with zipfile.ZipFile(dest) as z:
                    member = next(n for n in z.namelist() if n.lower().endswith("cryrec.exe"))
                    new_exe = UPDATE_DIR / f"CryRec-{tag}.exe"
                    with z.open(member) as src, open(new_exe, "wb") as out:
                        out.write(src.read())
                dest = new_exe
            self.version, self.path = tag, dest
            self.on_ready(tag)
            return "newer"
        except Exception:
            return "failed"
        finally:
            self._lock.release()

    def start_background(self):
        def loop():
            while True:
                self.check()
                time.sleep(UPDATE_EVERY)
        threading.Thread(target=loop, daemon=True).start()

    def apply(self):
        """Launch the new version. The caller must exit right after."""
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        try:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)  # don't leak PyInstaller's DLL path
        except Exception:
            pass
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if PORTABLE:
            exe = Path(sys.executable)
            old = exe.with_name("CryRec.old.exe")
            old.unlink(missing_ok=True)
            os.replace(exe, old)  # Windows allows renaming a running exe
            try:
                os.replace(self.path, exe)
            except Exception:
                os.replace(old, exe)
                raise
            subprocess.Popen([str(exe)], env=env, close_fds=True, creationflags=flags)
        else:
            log = UPDATE_DIR / "setup.log"
            subprocess.Popen([str(self.path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"],
                             env=env, close_fds=True, creationflags=flags)


def cleanup_old_update():
    """Remove leftovers from a previous portable swap."""
    if not (FROZEN and PORTABLE):
        return
    old = Path(sys.executable).with_name("CryRec.old.exe")

    def run():
        for _ in range(20):  # the old process may still be exiting
            try:
                old.unlink(missing_ok=True)
                return
            except Exception:
                time.sleep(1)
    threading.Thread(target=run, daemon=True).start()


def hold_mutex():
    """Named mutex the installer waits on, so it never replaces files while CryRec runs.
    Returns (handle, already_running)."""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        return h, ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return None, False


class Track:
    """One capture stream written to a WAV file, with gap padding and a level meter."""

    def __init__(self, pa, device, path, pad_gaps):
        self.pa = pa
        self.device = device
        self.path = path
        self.pad_gaps = pad_gaps
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, min(2, int(device["maxInputChannels"])))
        self.frames_per_chunk = int(self.rate * CHUNK_MS / 1000)
        self.q = queue.Queue()
        self.level = 0.0
        self.frames_written = 0
        self.error = None
        self._stop = threading.Event()
        self._stream = None
        self._writer = None
        self._wav = None
        self._t0 = None

    def _callback(self, data, frame_count, time_info, status):
        self.q.put(data)
        return (None, pyaudio.paContinue)

    def start(self, t0):
        self._t0 = t0
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(self.channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self.rate)
        self._stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=self.frames_per_chunk,
            stream_callback=self._callback,
        )
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def _write_silence(self, frames):
        self._wav.writeframes(b"\x00\x00" * self.channels * frames)
        self.frames_written += frames

    def _write_loop(self):
        try:
            while not self._stop.is_set() or not self.q.empty():
                try:
                    data = self.q.get(timeout=0.1)
                except queue.Empty:
                    data = None
                if self.pad_gaps:
                    expected = int((time.monotonic() - self._t0) * self.rate)
                    pending = 0 if data is None else len(data) // (2 * self.channels)
                    gap = expected - self.frames_written - pending
                    if gap > self.rate * GAP_PAD_SEC:
                        self._write_silence(gap)
                if data is None:
                    if self.pad_gaps:
                        self.level = 0.0
                    continue
                self._wav.writeframes(data)
                self.frames_written += len(data) // (2 * self.channels)
                samples = np.frombuffer(data, dtype=np.int16)
                if samples.size:
                    peak = np.abs(samples.astype(np.int32)).max() / 32768.0
                    self.level = max(peak, self.level * 0.6)
        except Exception as e:  # surfaced in the UI on stop
            self.error = e

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=5)
        if self._wav is not None:
            self._wav.close()


class KeepAlive:
    """Plays silence on the output so WASAPI loopback keeps delivering frames."""

    def __init__(self, pa, out_device):
        self.stream = None
        try:
            rate = int(out_device["defaultSampleRate"])
            ch = max(1, min(2, int(out_device["maxOutputChannels"])))
            silence = b"\x00\x00" * ch * 1024
            self.stream = pa.open(
                format=pyaudio.paInt16, channels=ch, rate=rate, output=True,
                output_device_index=out_device["index"], frames_per_buffer=1024,
                stream_callback=lambda *a: (silence, pyaudio.paContinue),
            )
        except Exception:
            self.stream = None  # gap padding still covers us

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass


def _dpapi(data, protect):
    """Encrypt/decrypt bytes for the current Windows user (keys never sit in the config as plain text)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def secret_get(cfg, name):
    import base64
    try:
        return _dpapi(base64.b64decode(cfg.get(name + "_enc", "")), False).decode()
    except Exception:
        return ""  # missing, or encrypted by another user/PC (portable moved): ask again


def secret_set(cfg, name, value):
    import base64
    if value:
        cfg[name + "_enc"] = base64.b64encode(_dpapi(value.encode(), True)).decode()
    else:
        cfg.pop(name + "_enc", None)


def load_vocabulary():
    try:
        return VOCAB_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_transcript(cfg, session_dir, progress, cancel, cache):
    """Transcribe one recording folder with the engine chosen in cfg. cache keeps a loaded local model."""
    engine, language = cfg.get("engine", "local"), cfg.get("language", "Auto")
    vocab = load_vocabulary()
    if engine == "server":
        return tr.remote_transcribe(session_dir, cfg.get("server_url", ""), secret_get(cfg, "server_key"),
                                    language, progress, cancel, vocab)
    if engine == "api":
        eng = tr.ApiEngine(cfg.get("api_url") or DEFAULT_API_URL, secret_get(cfg, "api_key"),
                           cfg.get("api_model") or DEFAULT_API_MODEL)
        return tr.transcribe_session(session_dir, eng, language, progress, cancel, vocab)
    model = cfg.get("model", "turbo")
    device = "auto" if cfg.get("use_gpu", True) else "cpu"
    if not tr.model_ready(MODELS_DIR, model):
        tr.download_model(MODELS_DIR, model, progress, cancel)
    if (model, device) not in cache:
        cache.clear()
        progress(0.0, "loading speech model...")
        cache[(model, device)] = tr.LocalEngine(MODELS_DIR, model, device)
    return tr.transcribe_session(session_dir, cache[(model, device)], language, progress, cancel, vocab)


def run_clean(cfg, session_dir, progress, cancel):
    """Claude fixes casing, punctuation and misheard terms in transcript.txt (the original is kept as
    transcript.raw.txt). Returns how many lines were left unchanged because an answer didn't line up."""
    session_dir = Path(session_dir)
    txt = session_dir / "transcript.txt"
    raw = txt.read_text(encoding="utf-8")
    vocab = load_vocabulary()
    if cfg.get("clean_engine") == "server_claude":
        cleaned, kept = sm.remote_clean(cfg.get("server_url", ""), secret_get(cfg, "server_key"), raw,
                                        progress, cancel, vocab)
    else:
        claude = sm.ClaudeNotes(secret_get(cfg, "claude_key"), cfg.get("claude_model"))
        glossary, _ = tr.parse_vocabulary(vocab)
        cleaned, kept = sm.clean_transcript(raw, lambda s, u: claude.ask(s, u, cancel, effort="low")[0],
                                            glossary, progress, cancel)
    (session_dir / "transcript.raw.txt").write_text(raw, encoding="utf-8")
    txt.write_text(cleaned, encoding="utf-8")
    return kept


def run_notes(cfg, session_dir, progress, cancel):
    """Write notes.md from transcript.txt with the notes engine chosen in cfg."""
    transcript = (Path(session_dir) / "transcript.txt").read_text(encoding="utf-8")
    engine = cfg.get("notes_engine", "off")
    language = sm.NOTES_LANGUAGES.get(cfg.get("notes_language", "English"), "English")
    my_name = cfg.get("my_name", "")
    vocab = load_vocabulary()
    glossary, _ = tr.parse_vocabulary(vocab)
    if engine in ("server_local", "server_claude"):
        md = sm.remote_notes(cfg.get("server_url", ""), secret_get(cfg, "server_key"), transcript,
                             "local" if engine == "server_local" else "claude", language, my_name, progress, cancel,
                             vocab)
    else:
        backend = {
            "claude": lambda: sm.ClaudeNotes(secret_get(cfg, "claude_key"), cfg.get("claude_model")),
            "openai": lambda: sm.OpenAINotes(cfg.get("api_url") or DEFAULT_API_URL, secret_get(cfg, "api_key"),
                                             cfg.get("notes_model") or "llama-3.3-70b-versatile"),
            "ollama": lambda: sm.OllamaNotes(cfg.get("ollama_url"), cfg.get("ollama_model")),
        }[engine]()
        md = backend.notes(transcript, my_name, language, progress, cancel, glossary)
    return sm.write_notes(session_dir, md)


DICTATION_LOG = CONFIG_DIR / "dictation.txt"


def run_dictation(cfg, audio, cache):
    """16 kHz clip -> finished text, with the transcript engine and polish chosen in cfg."""
    vocab = load_vocabulary()
    terms, fixes = tr.parse_vocabulary(vocab)
    language = cfg.get("language", "Auto")
    polish = cfg.get("dictation_polish", "auto")
    model = cfg.get("polish_model") or cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL
    claude_key = secret_get(cfg, "claude_key")
    if cfg.get("engine") == "server":
        r = tr.remote_dictate(cfg.get("server_url", ""), secret_get(cfg, "server_key"), audio, language,
                              polish in ("auto", "server_claude"), model, vocab)
        if polish == "claude" and claude_key and r.get("raw"):
            return sm.polish_dictation(r["raw"], sm.ClaudeNotes(claude_key, model), terms)
        return r.get("text", "")
    if cfg.get("engine") == "api":
        eng = tr.ApiEngine(cfg.get("api_url") or DEFAULT_API_URL, secret_get(cfg, "api_key"),
                           cfg.get("api_model") or DEFAULT_API_MODEL)
    else:
        key = (cfg.get("model", "turbo"), "auto" if cfg.get("use_gpu", True) else "cpu")
        if key not in cache:
            cache.clear()
            cache[key] = tr.LocalEngine(MODELS_DIR, key[0], key[1])
        eng = cache[key]
    lang = tr.LANGUAGES.get(language, language)
    if hasattr(eng, "quick_text"):
        raw = eng.quick_text(audio, lang, terms)
    else:
        raw = " ".join(t.strip() for _, _, t in eng.segments(audio, lang, lambda t: None, lambda: False, terms=terms))
    raw = tr.apply_fixes(tr.collapse_repeats(raw), fixes).strip()
    if raw and polish in ("auto", "claude") and claude_key:
        return sm.polish_dictation(raw, sm.ClaudeNotes(claude_key, model), terms)
    return raw


class DictationUI:
    """Shortcut -> record -> transcribe (+ polish) -> paste, with a small overlay that never takes focus."""

    def __init__(self, app):
        self.app, self.root = app, app.root
        self.watcher = self.rec = None
        self.target = None
        self.cache = {}
        self.last_text = ""
        # rounded pill: the window's own background is a colour key made fully transparent
        key = "#010203"
        w = self.win = tk.Toplevel(self.root)
        w.withdraw()
        w.overrideredirect(True)
        w.configure(bg=key)
        w.attributes("-transparentcolor", key)
        w.attributes("-topmost", True)
        self.canvas = tk.Canvas(w, bg=key, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.msg = tk.StringVar()
        self.kind = "listen"
        # set "never activate" before the window is ever shown, so even the first show can't take focus
        self.hwnd = dictate.make_no_activate(w)
        app.theme.on_change(lambda: self.msg.get() and self._draw())
        self.restart()

    def _draw(self):
        t = self.app.theme
        px = t.px
        text = self.msg.get()
        f = t.fonts["body"]
        h = px(42)
        w = f.measure(text) + px(60)
        c = self.canvas
        c.delete("all")
        c.config(width=w, height=h)
        fill = t.c("overlay")
        r = h // 2
        c.create_oval(0, 0, h, h, fill=fill, outline=fill)
        c.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
        c.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        dot = {"listen": t.c("record"), "busy": t.c("accent"), "error": t.c("warn")}[self.kind]
        d = px(10)
        c.create_oval(px(18), (h - d) // 2, px(18) + d, (h + d) // 2, fill=dot, outline=dot)
        c.create_text(px(38), h // 2, text=text, anchor="w", fill=t.c("overlay_text"), font=f)
        return w, h

    def restart(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        cfg = self.app.cfg
        if cfg.get("dictation_enabled", True):
            self.hotkey = cfg.get("dictation_hotkey", "Ctrl+Win")
            self.watcher = dictate.HotkeyWatcher(self.hotkey, self._start, self._mode, self._stop, self._cancel)

    # overlay (UI thread only)
    def _show(self, text, hide_after=None, kind="listen"):
        def do():
            self.msg.set(text)
            self.kind = kind
            w, h = self._draw()
            px = self.app.theme.px
            area = work_area() or (0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight())
            x = area[0] + (area[2] - area[0] - w) // 2
            y = area[3] - h - px(40)
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.update_idletasks()
            dictate.place_no_activate(self.hwnd, x, y, w, h)
            if hide_after:
                self.root.after(int(hide_after * 1000), lambda: self._hide_if(text))
        self.root.after(0, do)

    def _hide(self):
        self.root.after(0, lambda: self.hwnd and dictate.show_no_activate(self.hwnd, False))

    def _hide_if(self, text):
        if self.msg.get() == text and self.hwnd:
            dictate.show_no_activate(self.hwnd, False)

    # watcher callbacks (watcher thread)
    def _start(self):
        self.target = dictate.foreground_window()
        cfg = self.app.cfg
        if cfg.get("engine") == "server" and cfg.get("server_url"):
            threading.Thread(target=tr.server_warm, args=(cfg["server_url"], secret_get(cfg, "server_key")),
                             daemon=True).start()
        try:
            self.rec = dictate.Recorder(self.app.pa, self.app.dictation_device())
            self._show("Listening...")
        except Exception:
            self.rec = None
            self._show("No microphone found for dictation.", hide_after=3, kind="error")

    def _mode(self, mode):
        if not self.rec:
            return
        self._show(f"Listening.  {self.hotkey} to finish, Esc to cancel" if mode == "tap"
                   else "Listening.  Let go to finish")

    def _cancel(self):
        if self.rec:
            self.rec.stop()
            self.rec = None
        self._hide()

    def _stop(self):
        if not self.rec:
            return
        audio, self.rec = self.rec.stop(), None
        if len(audio) < dictate.MIN_SEC * 16000 or float(np.abs(audio).max(initial=0)) < 0.01:
            self._hide()
            return
        self._show("Writing...", kind="busy")
        threading.Thread(target=self._process, args=(audio, self.target), daemon=True).start()

    def _process(self, audio, target):
        try:
            text = run_dictation(dict(self.app.cfg), audio, self.cache)
        except Exception as e:
            self._show(f"Dictation failed: {e}"[:110], hide_after=5, kind="error")
            return
        if not text:
            self._show("Nothing heard.", hide_after=2, kind="error")
            return
        self.last_text = text
        try:
            with open(DICTATION_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text}\n")
        except OSError:
            pass
        self._hide()
        dictate.paste_text(text, target)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHOW_FLAG = CONFIG_DIR / "show.flag"  # a second launch drops this so the running copy shows its window
SILENCE_LEVEL = 0.02  # peak level under which a track counts as silent


def _int(text, default, lo, hi):
    try:
        return max(lo, min(hi, int(str(text).strip())))
    except ValueError:
        return default


def _launch_command(hidden=True):
    if FROZEN:
        cmd = f'"{sys.executable}"'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd = f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}"'
    return cmd + (" --hidden" if hidden else "")


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, _launch_command(hidden=True))
            else:
                winreg.DeleteValue(k, APP)
    except OSError:
        pass


class Popup:
    """Small always-on-top box above the taskbar, bottom right, with buttons and an optional countdown."""

    def __init__(self, root, title, message, buttons, countdown=None, on_timeout=None, countdown_text="{s} s"):
        self.root, self.on_timeout, self.left = root, on_timeout, countdown
        self.countdown_text = countdown_text
        theme = getattr(root, "cryrec_app", None) and root.cryrec_app.theme
        px = theme.px if theme else (lambda n: n)
        w = self.win = tk.Toplevel(root)
        w.title(title)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        try:
            w.iconbitmap(str(RES_DIR / "cryrec.ico"))
        except tk.TclError:
            pass
        if theme:
            theme.register(w)
        f = ttk.Frame(w, padding=(px(18), px(14)))
        f.grid()
        ttk.Label(f, text=message, wraplength=px(320), justify="left",
                  style="Strong.TLabel").grid(row=0, column=0, sticky="w")
        self.count = tk.StringVar()
        ttk.Label(f, textvariable=self.count, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        b = ttk.Frame(f)
        b.grid(row=2, column=0, sticky="e", pady=(px(12), 0))
        for i, (label, fn) in enumerate(buttons):
            ttk.Button(b, text=label, style="Accent.TButton" if i == 0 else "TButton",
                       command=lambda fn=fn: self._click(fn)).grid(row=0, column=i, padx=(px(8) if i else 0, 0))
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.update_idletasks()
        area = work_area()
        if area:
            x = area[2] - w.winfo_reqwidth() - px(16)
            y = area[3] - w.winfo_reqheight() - px(16) - px(32)  # leave room for the caption bar
        else:
            x = w.winfo_screenwidth() - w.winfo_reqwidth() - px(24)
            y = w.winfo_screenheight() - w.winfo_reqheight() - px(80)
        w.geometry(f"+{x}+{y}")
        w.bell()
        self.alive = True
        if countdown:
            self._tick()

    def _tick(self):
        if not self.alive:
            return
        if self.left <= 0:
            self.close()
            if self.on_timeout:
                self.on_timeout()
            return
        self.count.set(self.countdown_text.format(s=self.left))
        self.left -= 1
        self.win.after(1000, self._tick)

    def _click(self, fn):
        self.close()
        fn()

    def close(self):
        if self.alive:
            self.alive = False
            self.win.destroy()


class VocabularyEditor:
    """Edit vocabulary.txt: names and terms, and "wrong -> right" fixes."""

    def __init__(self, parent, theme):
        px = theme.px
        w = self.win = tk.Toplevel(parent)
        w.title(f"{APP} vocabulary")
        w.transient(parent)
        theme.register(w)
        f = ttk.Frame(w, padding=px(12))
        f.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="One name or term per line. Fix a word that keeps coming out wrong with:  wrong -> right",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.text = tk.Text(f, width=60, height=18, font=("Consolas", 10), undo=True, relief="flat",
                            bg=theme.c("field"), fg=theme.c("text"), insertbackground=theme.c("text"),
                            selectbackground=theme.c("accent"), padx=px(8), pady=px(6),
                            highlightthickness=1, highlightbackground=theme.c("border"),
                            highlightcolor=theme.c("accent"))
        self.text.grid(row=1, column=0, sticky="nsew", pady=px(8))
        sb = ttk.Scrollbar(f, command=self.text.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=px(8))
        self.text.config(yscrollcommand=sb.set)
        self.text.insert("1.0", load_vocabulary() or VOCAB_TEMPLATE)

        def recolor(t=theme):
            if self.text.winfo_exists():
                self.text.config(bg=t.c("field"), fg=t.c("text"), insertbackground=t.c("text"),
                                 selectbackground=t.c("accent"), highlightbackground=t.c("border"),
                                 highlightcolor=t.c("accent"))
        theme.on_change(recolor)
        b = ttk.Frame(f)
        b.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(b, text="Save", style="Accent.TButton", command=self.save).grid(row=0, column=0, padx=(0, px(6)))
        ttk.Button(b, text="Cancel", command=w.destroy).grid(row=0, column=1)
        w.grab_set()

    def save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            VOCAB_FILE.write_text(self.text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        except OSError as e:
            messagebox.showerror(APP, f"Could not save:\n{e}", parent=self.win)
            return
        self.win.destroy()


class SettingsDialog:
    """Four tabs: General, Transcript, Notes, Calls & dictation. Engine-specific fields show only when used."""

    def __init__(self, parent, cfg, theme, on_save=None):
        self.cfg, self.theme, self.on_save = cfg, theme, on_save
        px = theme.px
        w = self.win = tk.Toplevel(parent)
        w.title(f"{APP} settings")
        w.resizable(False, False)
        w.transient(parent)
        try:
            w.iconbitmap(str(RES_DIR / "cryrec.ico"))
        except tk.TclError:
            pass
        theme.register(w)
        outer = ttk.Frame(w, padding=px(12))
        outer.grid(sticky="nsew")
        nb = self.nb = ttk.Notebook(outer)
        nb.grid(row=0, column=0, sticky="nsew")
        tabs = {}
        for key, title in (("general", "General"), ("transcript", "Transcript"), ("notes", "Notes"),
                           ("calls", "Calls & dictation")):
            f = ttk.Frame(nb, padding=(px(14), px(12)))
            f.columnconfigure(1, weight=1)
            nb.add(f, text=title)
            tabs[key] = f
        self.rows = {k: 0 for k in tabs}

        # ---- General
        g = tabs["general"]
        self._section(g, "general", "Recordings")
        self.out_dir = tk.StringVar(value=cfg.get("out_dir", str(DEFAULT_OUT)))
        row = self._next("general")
        ttk.Label(g, text="Save to").grid(row=row, column=0, sticky="w", pady=px(3))
        pick = ttk.Frame(g)
        pick.grid(row=row, column=1, sticky="ew", padx=(px(10), 0))
        pick.columnconfigure(0, weight=1)
        ttk.Entry(pick, textvariable=self.out_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(pick, text="Browse", command=self._browse).grid(row=0, column=1, padx=(px(6), 0))
        self._section(g, "general", "Appearance")
        self.theme_var = self._combo(g, "general", "Theme", THEME_MODES, cfg.get("theme", "system"))
        self._section(g, "general", "Start-up")
        self.autostart = self._check(g, "general", "Start CryRec with Windows (in the tray)", autostart_enabled())
        self._section(g, "general", "Updates")
        row = self._next("general")
        ttk.Label(g, text=f"Version {VERSION}" + (" (portable)" if PORTABLE else "")).grid(row=row, column=0, sticky="w")
        self.upd_msg = tk.StringVar()
        ttk.Label(g, textvariable=self.upd_msg, style="Muted.TLabel").grid(row=row, column=1, sticky="w", padx=(px(10), 0))
        self.upd_btn = ttk.Button(g, text="Check now", command=self._check_updates)
        self.upd_btn.grid(row=row, column=2, sticky="e")

        # ---- Transcript
        t = tabs["transcript"]
        self._section(t, "transcript", "Engine")
        self.engine = self._combo(t, "transcript", "Transcribe with", ENGINES, cfg.get("engine", "local"),
                                  on_change=self._engine_changed)
        self.language = self._combo(t, "transcript", "Language", {k: k for k in tr.LANGUAGES},
                                    cfg.get("language", "Auto"))
        self.auto = self._check(t, "transcript", "Transcribe right after each recording",
                                cfg.get("auto_transcribe", True))
        # engine-specific groups
        self.groups = {}
        start = self._next("transcript")
        loc = self.groups["local"] = ttk.Frame(t)
        loc.columnconfigure(1, weight=1)
        self.model = self._combo(loc, None, "Speech model", {k: v["label"] for k, v in tr.MODELS.items()},
                                 cfg.get("model", "turbo"), row=0)
        self.gpu = tk.BooleanVar(value=cfg.get("use_gpu", True))
        ttk.Checkbutton(loc, text="Use an NVIDIA GPU when available", variable=self.gpu).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=px(3))
        srv = self.groups["server"] = ttk.Frame(t)
        srv.columnconfigure(1, weight=1)
        self.s_url = self._entry(srv, None, "Server address", cfg.get("server_url", ""), row=0)
        self.s_key = self._entry(srv, None, "Server key", secret_get(cfg, "server_key"), row=1, show="•")
        self.s_res = self._test_row(srv, 2, self.test_server)
        api = self.groups["api"] = ttk.Frame(t)
        api.columnconfigure(1, weight=1)
        self.a_url = self._entry(api, None, "API address", cfg.get("api_url") or DEFAULT_API_URL, row=0)
        self.a_key = self._entry(api, None, "API key", secret_get(cfg, "api_key"), row=1, show="•")
        self.a_model = self._entry(api, None, "Model", cfg.get("api_model") or DEFAULT_API_MODEL, row=2)
        self.a_res = self._test_row(api, 3, self.test_api)
        ttk.Label(api, text="Audio is sent to this service.", style="Muted.TLabel").grid(
            row=4, column=0, columnspan=3, sticky="w")
        for fr in self.groups.values():
            fr.grid(row=start, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))
        self.rows["transcript"] = start + 1
        self._section(t, "transcript", "Quality")
        self.clean = self._combo(t, "transcript", "Clean up", CLEAN_ENGINES, cfg.get("clean_engine", "off"))
        self._hint(t, "transcript", "Fixes capitals, punctuation and misheard terms. The original is kept.")
        row = self._next("transcript")
        ttk.Label(t, text="Vocabulary").grid(row=row, column=0, sticky="w", pady=px(3))
        ttk.Button(t, text="Edit names and terms...", command=lambda: VocabularyEditor(self.win, self.theme)).grid(
            row=row, column=1, sticky="w", padx=(px(10), 0))
        self._engine_changed()

        # ---- Notes
        n = tabs["notes"]
        self._section(n, "notes", "Meeting notes")
        self.notes_engine = self._combo(n, "notes", "Write notes with", NOTES_ENGINES, cfg.get("notes_engine", "off"))
        self._hint(n, "notes", "Default for every recording. Recent > ⋯ > Write notes with uses another one once.")
        self.notes_lang = self._combo(n, "notes", "Language", {k: k for k in sm.NOTES_LANGUAGES},
                                      cfg.get("notes_language", "English"))
        self.my_name = self._entry(n, "notes", "Your name", cfg.get("my_name", ""))
        self._hint(n, "notes", "Used instead of \"Me\" in action items.")
        self._section(n, "notes", "Cloud AI")
        self.c_key = self._entry(n, "notes", "API key", secret_get(cfg, "claude_key"), show="•")
        self._hint(n, "notes", "Only needed for \"Cloud AI\" on this PC. \"My server\" options use the server's key.")
        self.adv = tk.BooleanVar(value=False)
        ttk.Checkbutton(n, text="Show advanced options", variable=self.adv, command=self._toggle_adv).grid(
            row=self._next("notes"), column=0, columnspan=3, sticky="w", pady=(px(10), 0))
        adv = self.adv_frame = ttk.Frame(n)
        adv.columnconfigure(1, weight=1)
        self.c_model = self._entry(adv, None, "Cloud model", cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL, row=0)
        self.n_model = self._entry(adv, None, "API notes model", cfg.get("notes_model") or "llama-3.3-70b-versatile", row=1)
        self.o_url = self._entry(adv, None, "Ollama address", cfg.get("ollama_url") or sm.DEFAULT_OLLAMA_URL, row=2)
        self.o_model = self._entry(adv, None, "Ollama model", cfg.get("ollama_model") or sm.DEFAULT_OLLAMA_MODEL, row=3)
        self.polish_model = self._entry(adv, None, "Dictation model",
                                        cfg.get("polish_model") or cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL,
                                        row=4)
        self.adv_row = self._next("notes")

        # ---- Calls & dictation
        c = tabs["calls"]
        self._section(c, "calls", "Calls")
        self.watch = self._check(c, "calls", "Offer to record Teams, Zoom and Webex calls", cfg.get("watch_calls", True))
        row = self._next("calls")
        ttk.Label(c, text="Silence warning").grid(row=row, column=0, sticky="w", pady=px(3))
        sil = ttk.Frame(c)
        sil.grid(row=row, column=1, sticky="w", padx=(px(10), 0))
        self.silence = tk.StringVar(value=str(cfg.get("silence_minutes", 5)))
        ttk.Spinbox(sil, from_=0, to=60, width=5, textvariable=self.silence).grid(row=0, column=0)
        ttk.Label(sil, text="minutes (0 = off)", style="Muted.TLabel").grid(row=0, column=1, padx=px(8))
        self._hint(c, "calls", "Asks before recording. Stops by itself 30 s after a call ends or goes quiet.")
        self._section(c, "calls", "Dictation")
        self.dict_on = self._check(c, "calls", "Dictation shortcut", cfg.get("dictation_enabled", True))
        self.dict_key = self._combo(c, "calls", "Shortcut", {k: k for k in dictate.HOTKEYS},
                                    cfg.get("dictation_hotkey", "Ctrl+Win"))
        self.polish = self._combo(c, "calls", "Polish", POLISH_MODES, cfg.get("dictation_polish", "auto"))
        self._hint(c, "calls", "Tap to start and stop (Esc cancels), or hold while you talk.\n"
                               "Polish removes \"um\"s and fixes punctuation.")

        # ---- buttons
        self.err = tk.StringVar()
        bar = ttk.Frame(outer)
        bar.grid(row=1, column=0, sticky="ew", pady=(px(12), 0))
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self.err, style="Error.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).grid(row=0, column=1, padx=(0, px(6)))
        ttk.Button(bar, text="Cancel", command=w.destroy).grid(row=0, column=2)
        w.bind("<Escape>", lambda e: w.destroy())
        w.grab_set()

    # ---- layout helpers
    def _next(self, tab):
        r = self.rows[tab]
        self.rows[tab] += 1
        return r

    def _section(self, f, tab, text):
        r = self._next(tab)
        ttk.Label(f, text=text, style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w",
                                                             pady=(self.theme.px(10 if r else 0), self.theme.px(2)))

    def _hint(self, f, tab, text):
        ttk.Label(f, text=text, style="Muted.TLabel", justify="left").grid(
            row=self._next(tab), column=0, columnspan=3, sticky="w", pady=(0, self.theme.px(2)))

    def _entry(self, f, tab, label, value, row=None, show=None):
        r = self._next(tab) if row is None else row
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self.theme.px(3))
        var = tk.StringVar(value=value)
        ttk.Entry(f, textvariable=var, width=40, show=show or "").grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(self.theme.px(10), 0), pady=self.theme.px(3))
        return var

    def _combo(self, f, tab, label, options, current, row=None, on_change=None):
        """options: {key: label}. Returns a getter-friendly StringVar holding the label; read with _key()."""
        r = self._next(tab) if row is None else row
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self.theme.px(3))
        var = tk.StringVar(value=options.get(current, next(iter(options.values()))))
        var.options = options
        box = ttk.Combobox(f, textvariable=var, values=list(options.values()), state="readonly", width=26)
        box.grid(row=r, column=1, sticky="w", padx=(self.theme.px(10), 0), pady=self.theme.px(3))
        if on_change:
            box.bind("<<ComboboxSelected>>", lambda e: on_change())
        return var

    @staticmethod
    def _key(var):
        return next((k for k, v in var.options.items() if v == var.get()), next(iter(var.options)))

    def _check(self, f, tab, text, value):
        var = tk.BooleanVar(value=value)
        ttk.Checkbutton(f, text=text, variable=var).grid(row=self._next(tab), column=0, columnspan=3, sticky="w",
                                                         pady=self.theme.px(3))
        return var

    def _test_row(self, f, row, fn):
        res = tk.StringVar()
        ttk.Label(f, textvariable=res, style="Muted.TLabel").grid(row=row, column=1, sticky="w", padx=(self.theme.px(10), 0))
        ttk.Button(f, text="Test", command=fn).grid(row=row, column=2, sticky="e", pady=self.theme.px(3))
        return res

    def _engine_changed(self):
        cur = self._key(self.engine)
        for k, fr in self.groups.items():
            if k == cur:
                fr.grid()
            else:
                fr.grid_remove()

    def _toggle_adv(self):
        if self.adv.get():
            self.adv_frame.grid(row=self.adv_row, column=0, columnspan=3, sticky="ew", pady=(self.theme.px(4), 0))
        else:
            self.adv_frame.grid_remove()

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get(), parent=self.win)
        if d:
            self.out_dir.set(d)

    def _bg(self, fn, out):
        out.set("Testing...")

        def run():
            try:
                msg = fn()
            except Exception as e:
                msg = f"Failed: {e}"
            self.win.after(0, lambda: out.set(msg[:60]))
        threading.Thread(target=run, daemon=True).start()

    def test_server(self):
        url, key = self.s_url.get().strip(), self.s_key.get().strip()

        def fn():
            h = tr.server_health(url, key)
            return f"OK, server {h.get('version')}"
        self._bg(fn, self.s_res)

    def test_api(self):
        url, key = self.a_url.get().strip().rstrip("/"), self.a_key.get().strip()

        def fn():
            req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}", "User-Agent": APP})
            with urllib.request.urlopen(req, timeout=20) as r:
                ids = [m.get("id") for m in json.load(r).get("data", [])]
            want = self.a_model.get().strip()
            return "OK, model found" if want in ids else f"Key OK, model '{want}' not listed"
        self._bg(fn, self.a_res)

    def _check_updates(self):
        app = getattr(self.win.master, "cryrec_app", None)
        if not app:
            return
        self.upd_btn.config(state="disabled")
        self.upd_msg.set("Checking...")

        def run():
            res = app.updater.check()
            msg = {"latest": "You have the latest version.", "busy": "Already checking.",
                   "dev": "Updates work in the installed app only.",
                   "newer": f"Version {app.updater.version} is ready, see the main window."}.get(
                res, "Couldn't reach GitHub.")
            self.win.after(0, lambda: (self.upd_msg.set(msg), self.upd_btn.config(state="normal")))
        threading.Thread(target=run, daemon=True).start()

    def save(self):
        engine = self._key(self.engine)
        url = self.s_url.get().strip()
        if engine == "server" and not url.lower().startswith(("http://", "https://")):
            self.nb.select(1)
            self.err.set("Server address must start with https://")
            return
        if not self.out_dir.get().strip():
            self.nb.select(0)
            self.err.set("Pick a folder for recordings.")
            return
        self.cfg.update(
            out_dir=self.out_dir.get().strip(), theme=self._key(self.theme_var),
            engine=engine, language=self._key(self.language), auto_transcribe=self.auto.get(),
            model=self._key(self.model), use_gpu=self.gpu.get(), server_url=url,
            api_url=self.a_url.get().strip(), api_model=self.a_model.get().strip(),
            clean_engine=self._key(self.clean), notes_engine=self._key(self.notes_engine),
            notes_language=self._key(self.notes_lang),
            my_name=self.my_name.get().strip(), claude_model=self.c_model.get().strip(),
            notes_model=self.n_model.get().strip(), ollama_url=self.o_url.get().strip(),
            ollama_model=self.o_model.get().strip(), watch_calls=self.watch.get(),
            silence_minutes=_int(self.silence.get(), 5, 0, 60), dictation_enabled=self.dict_on.get(),
            dictation_hotkey=self._key(self.dict_key), dictation_polish=self._key(self.polish),
            polish_model=self.polish_model.get().strip())
        if self.autostart.get() != autostart_enabled():
            set_autostart(self.autostart.get())
        secret_set(self.cfg, "server_key", self.s_key.get().strip())
        secret_set(self.cfg, "api_key", self.a_key.get().strip())
        secret_set(self.cfg, "claude_key", self.c_key.get().strip())
        save_config(self.cfg)
        self.win.destroy()
        if self.on_save:
            self.on_save()


SESSION_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
RECENT_COUNT = 5


def session_when(name):
    """'2026-10-16_10-00-00' -> 'Today 10:00' / 'Yesterday 09:00' / '14 Oct 16:00'."""
    try:
        dt = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return name
    days = (datetime.now().date() - dt.date()).days
    day = "Today" if days == 0 else "Yesterday" if days == 1 else dt.strftime("%d %b").lstrip("0")
    return f"{day} {dt:%H:%M}"


class App:
    def __init__(self, root, hidden=False):
        self.hidden = hidden
        self.root = root
        root.cryrec_app = self
        self.cfg = load_config()
        self.theme = Theme(root, self.cfg.get("theme", "system"))
        self.pa = pyaudio.PyAudio()
        self.inputs, self.outputs = {}, {}
        self.tracks = []
        self.keepalive = None
        self.session_dir = None
        self.saving = None
        self.t0 = None
        self.recording = False
        self.jobs = {}  # session folder -> {"stage", "progress", "errors": {stage: message}}; Tk thread only
        self.skip = set()  # folders cancelled while queued
        self.running = None  # folder the worker is on
        self.cancel_all = False
        self.pending_msg = None  # job message that arrived while recording
        px = self.theme.px

        root.title(APP + (" (portable)" if PORTABLE else ""))
        try:
            root.iconbitmap(str(RES_DIR / "cryrec.ico"))
        except tk.TclError:
            pass
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.theme.register(root)

        frm = ttk.Frame(root, padding=(px(18), px(14), px(18), px(14)))
        frm.grid(sticky="nsew")
        frm.columnconfigure(0, weight=1)
        width = px(380)

        # header
        head = ttk.Frame(frm)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text=APP, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text=f"v{VERSION}", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=px(6))
        self.menu_btn = ttk.Button(head, text="", style="Icon.TButton", width=3, command=self.show_menu)
        self.menu_btn.grid(row=0, column=2)
        Tooltip(self.menu_btn, self.theme, "More")
        gear = ttk.Button(head, text="", style="Icon.TButton", width=3, command=self.open_settings)
        gear.grid(row=0, column=3, padx=(px(4), 0))
        Tooltip(gear, self.theme, "Settings")

        # timer, status, record button
        self.timer = tk.StringVar(value="00:00:00")
        ttk.Label(frm, textvariable=self.timer, style="Timer.TLabel", anchor="center").grid(
            row=1, column=0, sticky="ew", pady=(px(14), 0))
        self.status = tk.StringVar(value="Ready")
        self.status_lbl = ttk.Label(frm, textvariable=self.status, style="Muted.TLabel", anchor="center",
                                    width=1)  # width=1 + sticky ew: long text never widens the window
        self.status_lbl.grid(row=2, column=0, sticky="ew")
        self.status_full = ""
        Tooltip(self.status_lbl, self.theme, lambda: self.status_full)
        self.rec_btn = PillButton(frm, self.theme, "●  Record", self.toggle)
        self.rec_btn.grid(row=3, column=0, pady=(px(12), px(16)))

        # devices
        dev = ttk.Frame(frm)
        dev.grid(row=4, column=0, sticky="ew")
        dev.columnconfigure(1, weight=1)
        ttk.Label(dev, text="Microphone", style="Strong.TLabel").grid(row=0, column=0, sticky="w")
        ref = ttk.Button(dev, text="", style="Icon.TButton", width=3, command=self.load_devices)
        ref.grid(row=0, column=2, sticky="e")
        Tooltip(ref, self.theme, "Refresh devices")
        self.in_var = tk.StringVar()
        self.in_box = ttk.Combobox(dev, textvariable=self.in_var, state="readonly", width=1)
        self.in_box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(4), px(6)))
        self.in_meter = LevelMeter(dev, self.theme, width=360)
        self.in_meter.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(dev, text="Speaker (the other side)", style="Strong.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(px(14), 0))
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(dev, textvariable=self.out_var, state="readonly", width=1)
        self.out_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(px(4), px(6)))
        self.out_meter = LevelMeter(dev, self.theme, width=360)
        self.out_meter.grid(row=5, column=0, columnspan=3, sticky="ew")

        # a picked device stays highlighted in a readonly ttk combobox; drop the highlight and the focus
        for box in (self.in_box, self.out_box):
            box.bind("<<ComboboxSelected>>", lambda e: (e.widget.selection_clear(), self.root.focus_set()))

        # recent recordings
        ttk.Separator(frm).grid(row=6, column=0, sticky="ew", pady=(px(16), px(10)))
        rh = ttk.Frame(frm)
        rh.grid(row=7, column=0, sticky="ew")
        rh.columnconfigure(0, weight=1)
        ttk.Label(rh, text="Recent", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(rh, text="Open folder", style="Link.TButton", command=self.open_folder).grid(row=0, column=1)
        self.recent = ttk.Frame(frm)
        self.recent.grid(row=8, column=0, sticky="ew", pady=(px(4), 0))
        self.recent.columnconfigure(0, weight=1)
        self._recent_sig = None

        # update bar (shown only when an update is ready)
        self.upd_frame = ttk.Frame(frm)
        self.upd_frame.columnconfigure(0, weight=1)
        self.upd_text = tk.StringVar()
        ttk.Label(self.upd_frame, textvariable=self.upd_text, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(self.upd_frame, text="Install", style="Accent.TButton", command=self.install_update).grid(
            row=0, column=1)

        frm.grid_propagate(True)
        root.update_idletasks()
        root.minsize(width, 0)

        self.t_queue = queue.Queue()
        self.t_cancel = threading.Event()
        self.t_busy = False
        threading.Thread(target=self._transcribe_worker, daemon=True).start()
        self.updater = Updater(lambda v: root.after(0, lambda: self.update_ready(v)))
        self.updater.start_background()

        self.load_devices()
        self.refresh_recent(force=True)
        self.tick()

        # calls, silence, tray, dictation
        self.calls = callwatch.CallWatcher(self.on_call_start, self.on_call_end)
        self.call_app = None
        self.start_popup = self.end_popup = None
        self.last_sound = time.monotonic()
        self.tray = None
        self._setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.hide_or_quit)
        root.after(2000, self._poll_calls)
        root.after(1000, self._poll_show_flag)
        root.after(3000, self._poll_recent)
        self.dictation = DictationUI(self)
        if self.hidden:
            root.withdraw()
        elif not self.cfg.get("asked_autostart"):
            root.after(800, self._ask_autostart)

    # ---------- status line ----------
    def set_status(self, text):
        self.status_full = text
        self.status.set(text)

    # ---------- recent recordings ----------
    def _recent_folders(self):
        root = Path(self.cfg.get("out_dir", str(DEFAULT_OUT))).expanduser()
        try:
            dirs = [p for p in root.iterdir() if p.is_dir() and SESSION_NAME.match(p.name)]
        except OSError:
            return []
        return sorted(dirs, key=lambda p: p.name, reverse=True)[:RECENT_COUNT]

    def _session_state(self, d):
        """(line, kind, open_target) for one recording folder; kind is ok/busy/error/plain."""
        job = self.jobs.get(str(d))
        if self.recording and self.session_dir == d:
            return "Recording...", "busy", None
        if self.saving == d:
            return "Saving audio...", "busy", None
        if job and job.get("stage"):
            pct = f" {job['progress'] * 100:.0f}%" if job.get("progress") else ""
            return {"transcript": "Transcribing", "clean": "Cleaning up", "notes": "Writing notes",
                    "queued": "Waiting"}.get(job["stage"], job["stage"]) + pct, "busy", None
        has_t, has_n = (d / "transcript.txt").exists(), (d / "notes.md").exists()
        errors = (job or {}).get("errors", {})
        parts = []
        if has_t and "transcript" not in errors:
            parts.append("✓ Transcript")
        if has_n and "notes" not in errors:
            parts.append("✓ Notes")
        for stage in errors:
            parts.append(f"✗ {stage.capitalize()} failed (hover for details)" if len(errors) == 1
                         else f"✗ {stage.capitalize()}")
        if not parts:
            parts.append("Audio only" if (d / "meeting.mp3").exists() else "Empty")
        target = d / "notes.md" if has_n else d / "transcript.txt" if has_t else d
        kind = "error" if errors else "ok" if has_t else "plain"
        return "  ".join(parts), kind, target

    def refresh_recent(self, force=False):
        folders = self._recent_folders()
        states = [(d, self._session_state(d)) for d in folders]
        sig = tuple((str(d), s[0], s[1]) for d, s in states)
        if sig == self._recent_sig and not force:
            return
        self._recent_sig = sig
        for w in self.recent.winfo_children():
            w.destroy()
        px = self.theme.px
        if not states:
            ttk.Label(self.recent, text="No recordings yet. Press Record when a meeting starts.",
                      style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=px(4))
            return
        for i, (d, (line, kind, target)) in enumerate(states):
            row = ttk.Frame(self.recent)
            row.grid(row=i, column=0, sticky="ew", pady=px(3))
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=session_when(d.name), style="Strong.TLabel").grid(row=0, column=0, sticky="w")
            style = {"error": "Error.TLabel", "ok": "Ok.TLabel"}.get(kind, "Muted.TLabel")
            lbl = ttk.Label(row, text=line, style=style, width=1)
            lbl.grid(row=1, column=0, sticky="ew")
            errs = (self.jobs.get(str(d)) or {}).get("errors", {})
            if errs:
                Tooltip(lbl, self.theme, "\n".join(f"{k}: {v}" for k, v in errs.items()))
            if target:
                ttk.Button(row, text="Open", style="Link.TButton",
                           command=lambda t=target: os.startfile(t)).grid(row=0, column=1, rowspan=2, padx=(px(6), 0))
            more = ttk.Button(row, text="", style="Icon.TButton", width=3)
            more.config(command=lambda d=d, b=more: self.row_menu(d, b))
            more.grid(row=0, column=2, rowspan=2, padx=(px(4), 0))

    def _poll_recent(self):
        if self.root.state() != "withdrawn":
            self.refresh_recent()
        self.root.after(3000, self._poll_recent)

    def row_menu(self, d, button):
        m = tk.Menu(self.root, tearoff=False)
        busy_here = (self.jobs.get(str(d)) or {}).get("stage")
        if busy_here:
            m.add_command(label="Cancel", command=lambda: self.cancel_one(d))
        else:
            m.add_command(label="Transcribe again", command=lambda: self.enqueue_transcript(d))
            notes_menu = tk.Menu(m, tearoff=False)
            for key, label in NOTES_ENGINES.items():
                if key != "off":
                    notes_menu.add_command(label=label + ("  (default)" if key == self.cfg.get("notes_engine") else ""),
                                           command=lambda k=key: self.enqueue_notes(d, engine=k))
            m.add_cascade(label="Write notes with", menu=notes_menu,
                          state="normal" if (d / "transcript.txt").exists() else "disabled")
        m.add_separator()
        m.add_command(label="Open folder", command=lambda: os.startfile(d))
        m.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def show_menu(self):
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label="Transcribe a folder...", command=self.transcribe_clicked)
        m.add_command(label="Write notes for a folder...", command=self.notes_clicked)
        m.add_command(label="Open recordings folder", command=self.open_folder)
        m.add_separator()
        if self.t_busy:
            m.add_command(label="Cancel all transcripts", command=self.cancel_jobs)
        m.add_command(label="Copy last dictation", command=self.copy_last_dictation)
        m.add_separator()
        m.add_command(label="Quit CryRec", command=self.quit)
        b = self.menu_btn
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def cancel_jobs(self):
        """Stop the running job and drop everything queued."""
        self.cancel_all = True
        self.t_cancel.set()
        self.set_status("Cancelling...")

    def cancel_one(self, d):
        """Cancel just this recording: drop it from the queue, or stop it if it is the one running."""
        key = str(d)
        if key == self.running:
            self.t_cancel.set()
            self.set_status("Cancelling...")
        else:
            self.skip.add(key)
            self.jobs.pop(key, None)
            self.refresh_recent(force=True)

    # ---------- calls and silence ----------
    def _poll_calls(self):
        if self.cfg.get("watch_calls", True):
            try:
                self.calls.poll()
            except Exception:
                pass
        self.root.after(3000, self._poll_calls)

    def on_call_start(self, app):
        if self.recording:
            self.call_app = self.call_app or app
            return
        if self.start_popup and self.start_popup.alive:
            return

        def record():
            self.call_app = app
            self.start()
        self.start_popup = Popup(self.root, APP, f"{app} call started. Record it?\n"
                                 "Tell the others that you are recording.",
                                 [("Record", record), ("Ignore", lambda: None)], countdown=120,
                                 countdown_text="This closes in {s} s")

    def on_call_end(self, app):
        if self.start_popup and self.start_popup.alive:
            self.start_popup.close()
        if self.recording and self.call_app in (None, app):
            self._ask_stop(f"{app} call ended. Stop and save the recording?")

    def _check_silence(self, levels):
        mins = self.cfg.get("silence_minutes", 5)
        now = time.monotonic()
        if max(levels, default=0) > SILENCE_LEVEL:
            self.last_sound = now
        elif mins and now - self.last_sound > mins * 60:
            self._ask_stop(f"No sound for {mins} minutes. Is the meeting over?")
            self.last_sound = now

    def _ask_stop(self, message):
        if self.end_popup and self.end_popup.alive:
            return

        def keep():
            self.last_sound = time.monotonic()
        self.end_popup = Popup(self.root, APP, message, [("Stop now", self.stop_if_recording),
                                                         ("Keep recording", keep)],
                               countdown=30, on_timeout=self.stop_if_recording,
                               countdown_text="Stopping by itself in {s} s")

    def stop_if_recording(self):
        if self.recording:
            self.stop()

    def dictation_device(self):
        """The mic picked in the main window, or the Windows default microphone."""
        dev = self.inputs.get(self.in_var.get())
        if dev:
            return dev
        return self.pa.get_default_wasapi_device(d_in=True)

    def copy_last_dictation(self):
        if self.dictation.last_text:
            dictate.set_clipboard_text(self.dictation.last_text)

    # ---------- window and tray ----------
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image
            img = Image.open(RES_DIR / "cryrec.ico")
        except Exception:
            return  # no tray: closing the window quits, as before

        def on_ui(fn):
            return lambda icon, item: self.root.after(0, fn)
        menu = pystray.Menu(
            pystray.MenuItem("Open CryRec", on_ui(self.show), default=True),
            pystray.MenuItem(lambda item: "Stop recording" if self.recording else "Start recording",
                             on_ui(self.toggle)),
            pystray.MenuItem("Copy last dictation", on_ui(self.copy_last_dictation)),
            pystray.MenuItem("Quit", on_ui(self.quit)))
        self.tray = pystray.Icon(APP, img, APP, menu)
        self.tray.run_detached()

    def _tray_title(self):
        if self.tray:
            self.tray.title = f"{APP}: recording" if self.recording else APP
            self.tray.update_menu()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_or_quit(self):
        if not self.tray:
            return self.quit()
        self.root.withdraw()
        if not self.cfg.get("told_tray"):
            self.cfg["told_tray"] = True
            save_config(self.cfg)
            try:
                self.tray.notify("CryRec keeps running here and will notice calls. Right-click > Quit to exit.", APP)
            except Exception:
                pass

    def _poll_show_flag(self):
        if SHOW_FLAG.exists():
            try:
                SHOW_FLAG.unlink()
            except OSError:
                pass
            self.show()
        self.root.after(1000, self._poll_show_flag)

    def _ask_autostart(self):
        self.cfg["asked_autostart"] = True
        save_config(self.cfg)
        if messagebox.askyesno(APP, "Start CryRec with Windows?\n\nIt then waits in the tray and offers to record "
                                    "when a Teams, Zoom or Webex call starts. You can change this in Settings."):
            set_autostart(True)

    # ---------- updates ----------

    # ---------- updates ----------
    def update_ready(self, version):
        self.upd_text.set(f"Version {version} is ready.")
        self.upd_frame.grid(row=9, column=0, sticky="ew", pady=(self.theme.px(12), 0))

    def install_update(self):
        if self.recording or self.saving:
            messagebox.showinfo(APP, "Wait until the recording is stopped and saved, then install the update.")
            return
        if self.t_busy and not messagebox.askyesno(APP, "A transcript is running. Cancel it and install the update?"):
            return
        try:
            self.updater.apply()
        except Exception as e:
            messagebox.showerror(APP, f"Could not start the update:\n{e}")
            return
        self.pa.terminate()
        self.root.destroy()
        os._exit(0)  # the installer / new exe needs this process gone

    # ---------- transcripts ----------
    def _transcript_settings(self):
        return dict(self.cfg)

    def _notes_ready(self, cfg):
        """UI thread: True if the chosen notes engine has what it needs (asks the user otherwise)."""
        need = {"claude": ("claude_key", "the cloud AI key (Settings > Notes)"),
                "openai": ("api_key", "the API key (Settings > Transcript)"),
                "server_local": ("server_key", "your server address and key (Settings > Transcript)"),
                "server_claude": ("server_key", "your server address and key (Settings > Transcript)")}.get(
            cfg.get("notes_engine"))
        if need and not secret_get(cfg, need[0]):
            messagebox.showinfo(APP, f"Notes: set {need[1]} first.")
            return False
        return True

    def _pick_folder(self, title):
        start = self.session_dir if self.session_dir and self.session_dir.exists() else \
            Path(self.cfg.get("out_dir", str(DEFAULT_OUT)))
        d = filedialog.askdirectory(initialdir=str(start), title=title)
        return Path(d) if d else None

    def notes_clicked(self):
        d = self._pick_folder("Pick a recording folder with a transcript")
        if d:
            self.enqueue_notes(d)

    def enqueue_notes(self, d, engine=None):
        """engine overrides the default notes engine for this one recording (Recent > Write notes with)."""
        cfg = self._transcript_settings()
        if engine:
            cfg["notes_engine"] = engine
        if cfg.get("notes_engine", "off") == "off":
            messagebox.showinfo(APP, "Pick a notes engine in Settings > Notes first.")
            return
        if not (d / "transcript.txt").exists():
            messagebox.showwarning(APP, "This folder has no transcript yet. Transcribe it first.")
            return
        if self._notes_ready(cfg):
            self._queue(d, cfg, "notes")

    def open_settings(self):
        def saved():
            self.theme.set_mode(self.cfg.get("theme", "system"))
            self.dictation.restart()
            self.refresh_recent(force=True)
        SettingsDialog(self.root, self.cfg, self.theme, on_save=saved)

    def transcribe_clicked(self):
        d = self._pick_folder("Pick a recording folder")
        if d:
            self.enqueue_transcript(d)

    def enqueue_transcript(self, session_dir):
        """UI thread only: checks the model is there (asks to download once) and queues the job."""
        cfg = self._transcript_settings()
        if not tr.find_tracks(session_dir):
            messagebox.showwarning(APP, f"No recording found in:\n{session_dir}")
            return
        engine, model = cfg.get("engine", "local"), cfg.get("model", "turbo")
        if engine == "local" and not tr.model_ready(MODELS_DIR, model):
            info = tr.MODELS[model]
            if not messagebox.askyesno(APP, f"Transcripts need the {info['label'].split(' (')[0]} speech model "
                                            f"({info['size_gb']} GB, one-time download).\n\n"
                                            "It runs on this computer. Audio never leaves it.\n\nDownload now?"):
                self.set_status("Transcript skipped (no speech model).")
                return
        if engine == "server" and not (cfg.get("server_url") and secret_get(cfg, "server_key")):
            messagebox.showinfo(APP, "Set your server address and key in Settings > Transcript first.")
            return
        if engine == "api" and not secret_get(cfg, "api_key"):
            messagebox.showinfo(APP, "Set your API key in Settings > Transcript first.")
            return
        if cfg.get("notes_engine", "off") != "off" and not self._notes_ready(cfg):
            cfg["notes_engine"] = "off"  # still make the transcript
        clean = cfg.get("clean_engine", "off")
        if (clean == "claude" and not secret_get(cfg, "claude_key")) or \
                (clean == "server_claude" and not secret_get(cfg, "server_key")):
            messagebox.showinfo(APP, "Clean up: set the cloud AI key or the server key in Settings first.")
            cfg["clean_engine"] = "off"
        self._queue(session_dir, cfg, "all")

    def _queue(self, d, cfg, mode):
        self.jobs[str(d)] = {"stage": "queued", "progress": 0, "errors": {}}
        self.t_queue.put((d, cfg, mode))
        self.refresh_recent(force=True)

    def _transcribe_worker(self):
        """Runs jobs one by one. It never touches self.jobs or widgets directly: every change goes
        through _t_ui, which applies it on the Tk thread."""
        loaded = {}
        while True:
            session_dir, cfg, mode = self.t_queue.get()
            key = str(session_dir)
            if key in self.skip:  # cancelled while still waiting in the queue
                self.skip.discard(key)
                self._t_ui(busy=not self.t_queue.empty())
                continue
            self.running = key
            self.t_cancel.clear()
            errors = {}
            self._t_ui(busy=True, job=(key, {"stage": "transcript" if mode == "all" else "notes", "progress": 0,
                                             "errors": {}}))

            def stage(name, key=key):
                self._t_ui(job=(key, {"stage": name, "progress": 0}))

            def progress(frac, text, key=key):
                self._t_ui(text=text, job=(key, {"progress": frac}))
            msg = ""
            try:
                if mode == "all":
                    run_transcript(cfg, session_dir, progress, self.t_cancel.is_set, loaded)
                    msg = "Transcript saved."
                    if cfg.get("clean_engine", "off") != "off":
                        stage("clean")
                        try:
                            kept = run_clean(cfg, session_dir, progress, self.t_cancel.is_set)
                            if kept:
                                errors["cleanup"] = f"{kept} lines were left as heard"
                        except (tr.Cancelled, sm.Cancelled):
                            raise
                        except Exception as e:
                            errors["cleanup"] = str(e)
                if cfg.get("notes_engine", "off") != "off":
                    stage("notes")
                    try:
                        run_notes(cfg, session_dir, progress, self.t_cancel.is_set)
                        msg = "Transcript and notes saved." if mode == "all" else "Notes saved."
                    except (tr.Cancelled, sm.Cancelled):
                        raise
                    except Exception as e:
                        errors["notes"] = str(e)
                if errors:
                    msg = "Done, with problems: " + "; ".join(f"{k} {v}" for k, v in errors.items())
            except (tr.Cancelled, sm.Cancelled):
                msg = "Cancelled."
                if self.cancel_all:
                    dropped = []
                    while not self.t_queue.empty():
                        dropped.append(str(self.t_queue.get_nowait()[0]))
                    self._t_ui(drop=dropped)
            except Exception as e:
                errors["transcript"] = str(e)
                msg = f"Transcript failed: {e}"
            self.cancel_all = False
            self.running = None
            if self.t_queue.empty():
                loaded.clear()  # free ~1 GB of RAM until the next job
            name = session_when(session_dir.name) if SESSION_NAME.match(session_dir.name) else session_dir.name
            self._t_ui(busy=not self.t_queue.empty(), text=f"{name}: {msg}",
                       job=(key, {"stage": None, "errors": dict(errors)}))

    def _t_ui(self, busy=None, text=None, job=None, drop=()):
        def apply():
            if busy is not None:
                self.t_busy = busy
            if job:
                self.jobs.setdefault(job[0], {"errors": {}}).update(job[1])
            for k in drop:
                self.jobs.pop(k, None)
            if text is not None:
                if self.recording:
                    self.pending_msg = text  # shown once the recording is saved
                else:
                    self.set_status(text)
            self.refresh_recent()
        self.root.after(0, apply)

    # ---------- devices ----------
    def load_devices(self):
        if self.recording:
            return
        try:
            self.pa.terminate()
        except Exception:
            pass
        self.pa = pyaudio.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        self.inputs, self.outputs = {NO_DEVICE: None}, {NO_DEVICE: None}
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d["hostApi"] != wasapi:
                continue
            if d.get("isLoopbackDevice"):
                self.outputs[d["name"].replace(" [Loopback]", "")] = d
            elif d["maxInputChannels"] > 0:
                self.inputs[d["name"]] = d
        self.in_box["values"] = list(self.inputs)
        self.out_box["values"] = list(self.outputs)
        self.in_var.set(self._pick(self.inputs, "input", self._default_input_name()))
        self.out_var.set(self._pick(self.outputs, "output", self._default_output_name()))

    def _default_input_name(self):
        try:
            return self.pa.get_default_wasapi_device(d_in=True)["name"]
        except Exception:
            return None

    def _default_output_name(self):
        try:
            return self.pa.get_default_wasapi_loopback()["name"].replace(" [Loopback]", "")
        except Exception:
            return None

    def _pick(self, devices, key, default):
        for name in (self.cfg.get(key), default):
            if name in devices:
                return name
        names = [n for n in devices if n != NO_DEVICE]
        return names[0] if names else NO_DEVICE

    def _output_device_for(self, name):
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d["name"] == name and d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice"):
                return d
        return None

    # ---------- recording ----------
    def toggle(self):
        if self.recording:
            self.stop()
        elif not self.saving:
            self.start()

    def start(self):
        mic = self.inputs.get(self.in_var.get())
        spk = self.outputs.get(self.out_var.get())
        if not mic and not spk:
            messagebox.showwarning(APP, "Pick a microphone or a speaker first.")
            return
        out_root = Path(self.cfg.get("out_dir", str(DEFAULT_OUT))).expanduser()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = out_root / stamp
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP, f"Cannot create the recordings folder:\n{e}\n\nPick another one in Settings > General.")
            return

        self.cfg.update(input=self.in_var.get(), output=self.out_var.get(), out_dir=str(out_root))
        save_config(self.cfg)

        self.tracks = []
        try:
            if spk:
                out_dev = self._output_device_for(self.out_var.get())
                if out_dev:
                    self.keepalive = KeepAlive(self.pa, out_dev)
            self.t0 = time.monotonic()
            if mic:
                t = Track(self.pa, mic, self.session_dir / "me.wav", pad_gaps=False)
                t.role = "mic"
                self.tracks.append(t)
            if spk:
                t = Track(self.pa, spk, self.session_dir / "them.wav", pad_gaps=True)
                t.role = "spk"
                self.tracks.append(t)
            for t in self.tracks:
                t.start(self.t0)
        except Exception as e:
            self._teardown()
            messagebox.showerror(APP, f"Could not start recording:\n{e}")
            return

        self.recording = True
        self.rec_btn.configure_button(text="\u25A0  Stop", kind="stop")
        self.in_box.config(state="disabled")
        self.out_box.config(state="disabled")
        sides = " and ".join(x for x, on in (("your mic", mic), ("the other side", spk)) if on)
        self.set_status(f"Recording {sides}")
        self.status_full = f"Recording to {self.session_dir}"
        self.last_sound = time.monotonic()
        if self.start_popup and self.start_popup.alive:
            self.start_popup.close()
        self._tray_title()
        self.refresh_recent(force=True)

    def _teardown(self):
        for t in self.tracks:
            t.stop()
        if self.keepalive:
            self.keepalive.stop()
            self.keepalive = None

    def stop(self):
        self._teardown()
        self.recording = False
        self.call_app = None
        if self.end_popup and self.end_popup.alive:
            self.end_popup.close()
        self._tray_title()
        self.saving = self.session_dir
        self.rec_btn.configure_button(text="Saving...", kind="stop", enabled=False)
        self.in_box.config(state="readonly")
        self.out_box.config(state="readonly")
        errors = [f"{t.role}: {t.error}" for t in self.tracks if t.error]
        self.set_status("Saving audio...")
        self.refresh_recent(force=True)
        threading.Thread(target=self._finish, args=(list(self.tracks), self.session_dir, errors), daemon=True).start()

    def _finish(self, tracks, session_dir, errors):
        msg = finalize(tracks, session_dir)
        if errors:
            msg += "  Errors: " + "; ".join(errors)

        def done():
            self.saving = None
            self.rec_btn.configure_button(text="\u25CF  Record", kind="record", enabled=True)
            self.timer.set("00:00:00")
            self.set_status("Saved." if msg.startswith("Saved") else msg)
            self.status_full = msg
            if self.pending_msg:
                pending, self.pending_msg = self.pending_msg, None
                self.root.after(4000, lambda: not self.recording and self.set_status(pending))
            if self.cfg.get("auto_transcribe", True) and msg.startswith("Saved"):
                self.enqueue_transcript(session_dir)
            self.refresh_recent(force=True)
        self.root.after(0, done)

    # ---------- ui ----------
    def tick(self):
        if self.recording:
            secs = int(time.monotonic() - self.t0)
            self.timer.set(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")
            lv = {t.role: t.level for t in self.tracks}
            self.in_meter.set(lv.get("mic", 0))
            self.out_meter.set(lv.get("spk", 0))
            self._check_silence(lv.values())
        else:
            self.in_meter.set(0)
            self.out_meter.set(0)
        self.root.after(100, self.tick)

    def open_folder(self):
        target = Path(self.cfg.get("out_dir", str(DEFAULT_OUT))).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def quit(self):
        if self.recording:
            self.show()
        if self.recording and not messagebox.askyesno(APP, "Recording is running. Stop and save before closing?"):
            return
        if not self.recording and self.t_busy and not messagebox.askyesno(
                APP, "A transcript is running. Close anyway? You can run it again later from Recent."):
            return
        self.t_cancel.set()
        if self.recording:
            self._teardown()
            finalize(self.tracks, self.session_dir)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.pa.terminate()
        self.root.destroy()



def finalize(tracks, session_dir):
    """Mix the WAV tracks into meeting.mp3 and keep each track as FLAC."""
    wavs = [t.path for t in tracks if t.path.exists() and t.frames_written > 0]
    if not wavs:
        return "Nothing was recorded."
    mp3 = session_dir / "meeting.mp3"
    try:
        mix_to_mp3(wavs, mp3)
    except Exception as e:
        return f"MP3 mix failed, WAV files kept: {e}"
    for w in wavs:
        try:
            wav_to_flac(w)
        except Exception:
            pass  # WAV stays if FLAC fails
    return f"Saved {mp3}"


def _read_block(wf, frames):
    """Read up to `frames` from an open wave file as float32 stereo, or None at EOF."""
    raw = wf.readframes(frames)
    if not raw:
        return None
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    x = x.reshape(-1, wf.getnchannels())
    return np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x[:, :2]


def mix_to_mp3(wavs, mp3_path):
    """Resample every track to MIX_RATE, sum them, and stream-encode one stereo MP3."""
    files = [wave.open(str(w), "rb") for w in wavs]
    try:
        resamplers = [soxr.ResampleStream(f.getframerate(), MIX_RATE, 2, dtype="float32") for f in files]
        bufs = [np.zeros((0, 2), dtype=np.float32) for _ in files]
        done = [False] * len(files)
        enc = lameenc.Encoder()
        enc.set_bit_rate(128)
        enc.set_in_sample_rate(MIX_RATE)
        enc.set_channels(2)
        enc.set_quality(2)
        with open(mp3_path, "wb") as out:
            while not all(done):
                for i, f in enumerate(files):
                    if done[i]:
                        continue
                    blk = _read_block(f, f.getframerate() * MIX_BLOCK_SEC)
                    last = blk is None
                    y = resamplers[i].resample_chunk(blk if blk is not None else np.zeros((0, 2), np.float32), last=last)
                    bufs[i] = np.concatenate([bufs[i], y])
                    done[i] = last
                # mix what every live track has; finished tracks count as silence
                n = min((len(b) for b, d in zip(bufs, done) if not d), default=max(len(b) for b in bufs))
                if n == 0:
                    continue
                mixed = np.zeros((n, 2), dtype=np.float32)
                for i in range(len(bufs)):
                    part = bufs[i][:n]
                    mixed[: len(part)] += part
                    bufs[i] = bufs[i][n:]
                pcm = (np.clip(mixed, -1.0, 1.0) * 32767).astype(np.int16)
                out.write(enc.encode(pcm.tobytes()))
            out.write(enc.flush())
    finally:
        for f in files:
            f.close()


def wav_to_flac(wav_path):
    flac = wav_path.with_suffix(".flac")
    with wave.open(str(wav_path), "rb") as wf, sf.SoundFile(
        str(flac), "w", samplerate=wf.getframerate(), channels=wf.getnchannels(), subtype="PCM_16", format="FLAC"
    ) as out:
        block = wf.getframerate() * MIX_BLOCK_SEC
        while True:
            raw = wf.readframes(block)
            if not raw:
                break
            out.write(np.frombuffer(raw, dtype=np.int16).reshape(-1, wf.getnchannels()))
    wav_path.unlink()


def selftest(out_dir):
    """Record 3 s of speaker loopback headless and save it; result goes to selftest.txt."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pa = pyaudio.PyAudio()
        lb = pa.get_default_wasapi_loopback()
        t = Track(pa, lb, out_dir / "them.wav", pad_gaps=True)
        t.role = "spk"
        t.start(time.monotonic())
        time.sleep(3)
        t.stop()
        msg = finalize([t], out_dir)
        pa.terminate()
    except Exception as e:
        msg = f"FAIL {e!r}"
    (out_dir / "selftest.txt").write_text(msg, encoding="utf-8")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return
    if len(sys.argv) > 2 and sys.argv[1] == "--transcribe":
        # headless: transcribe one recording folder with the saved model/language
        cfg = load_config()
        out = Path(sys.argv[2]) / "transcript.log"
        try:
            res = f"OK {run_transcript(cfg, Path(sys.argv[2]), lambda f, t: None, lambda: False, {})}"
            if cfg.get("clean_engine", "off") != "off":
                res += f"\nOK cleaned, {run_clean(cfg, Path(sys.argv[2]), lambda f, t: None, lambda: False)} lines left as heard"
            if cfg.get("notes_engine", "off") != "off":
                res += f"\nOK {run_notes(cfg, Path(sys.argv[2]), lambda f, t: None, lambda: False)}"
        except Exception as e:
            res = f"FAIL {e!r}"
        out.write_text(res, encoding="utf-8")
        return
    if len(sys.argv) > 2 and sys.argv[1] == "--notes":
        # headless: notes.md from an existing transcript.txt with the saved notes engine
        cfg = load_config()
        out = Path(sys.argv[2]) / "notes.log"
        try:
            res = f"OK {run_notes(cfg, Path(sys.argv[2]), lambda f, t: None, lambda: False)}"
        except Exception as e:
            res = f"FAIL {e!r}"
        out.write_text(res, encoding="utf-8")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        # headless: install the newest release if there is one, then exit
        up = Updater(lambda v: None)
        if up.check() == "newer" and up.path:
            up.apply()
        return
    _mutex, already = hold_mutex()  # handle kept alive for the process lifetime
    if already:  # one CryRec at a time: ask the running one to show itself
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            SHOW_FLAG.write_text("show")
        except OSError:
            pass
        return
    cleanup_old_update()
    enable_dpi_awareness()
    root = tk.Tk()
    App(root, hidden="--hidden" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
