"""CryRec: record a chosen input (mic) and a chosen output (speaker loopback) together.

Each source is written to its own WAV while recording. On stop, the tracks are mixed
into one MP3 and kept as FLAC (me / them) for later transcripts.

Portable mode: if a file named "portable" sits next to the exe, settings live there too.
"""

import json
import os
import queue
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
import dictate
import summarize as sm
import transcribe as tr

APP = "CryRec"
VERSION = "0.7.0"
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
CLEAN_ENGINES = {"off": "Off", "claude": "Claude", "server_claude": "My server (Claude)"}
POLISH_MODES = {"auto": "Auto", "off": "Off", "claude": "Claude", "server_claude": "My server (Claude)"}
NOTES_ENGINES = {"off": "Off", "claude": "Claude", "openai": "OpenAI-compatible", "ollama": "Ollama (local)",
                 "server_local": "My server (local)", "server_claude": "My server (Claude)"}
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
        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.configure(bg="#1f2430")
        self.msg = tk.StringVar()
        tk.Label(w, textvariable=self.msg, bg="#1f2430", fg="#ffffff", font=("Segoe UI", 11),
                 padx=18, pady=10).pack()
        w.withdraw()
        self.hwnd = None
        self.restart()

    def restart(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        cfg = self.app.cfg
        if cfg.get("dictation_enabled", True):
            self.hotkey = cfg.get("dictation_hotkey", "Ctrl+Win")
            self.watcher = dictate.HotkeyWatcher(self.hotkey, self._start, self._mode, self._stop, self._cancel)

    # overlay (UI thread only)
    def _show(self, text, hide_after=None):
        def do():
            self.msg.set(text)
            w = self.win
            if self.hwnd is None:
                w.deiconify()
                self.hwnd = dictate.make_no_activate(w)
            w.update_idletasks()
            x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
            y = w.winfo_screenheight() - w.winfo_reqheight() - 110
            w.geometry(f"+{x}+{y}")
            w.attributes("-topmost", True)
            dictate.show_no_activate(self.hwnd, True)
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
            self._show("●  Listening...")
        except Exception as e:
            self.rec = None
            self._show(f"Dictation: no microphone ({e})", hide_after=3)

    def _mode(self, mode):
        if not self.rec:
            return
        self._show(f"●  Listening. Press {self.hotkey} to finish, Esc to cancel." if mode == "tap"
                   else "●  Listening. Release to finish.")

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
        self._show("✎  Writing...")
        threading.Thread(target=self._process, args=(audio, self.target), daemon=True).start()

    def _process(self, audio, target):
        try:
            text = run_dictation(dict(self.app.cfg), audio, self.cache)
        except Exception as e:
            self._show(f"Dictation failed: {e}"[:120], hide_after=5)
            return
        if not text:
            self._show("Nothing heard.", hide_after=2)
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
    """Small always-on-top box in the bottom-right corner, with buttons and an optional countdown."""

    def __init__(self, root, title, message, buttons, countdown=None, on_timeout=None, countdown_text="{s} s"):
        self.root, self.on_timeout, self.left = root, on_timeout, countdown
        self.countdown_text = countdown_text
        w = self.win = tk.Toplevel(root)
        w.title(title)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        try:
            w.iconbitmap(str(RES_DIR / "cryrec.ico"))
        except tk.TclError:
            pass
        f = ttk.Frame(w, padding=14)
        f.grid()
        ttk.Label(f, text=message, wraplength=320, font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        self.count = tk.StringVar()
        ttk.Label(f, textvariable=self.count, foreground="#666").grid(row=1, column=0, sticky="w", pady=(4, 0))
        b = ttk.Frame(f)
        b.grid(row=2, column=0, sticky="e", pady=(10, 0))
        for i, (label, fn) in enumerate(buttons):
            ttk.Button(b, text=label, command=lambda fn=fn: self._click(fn)).grid(row=0, column=i, padx=(6 if i else 0, 0))
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_reqwidth() - 24
        y = w.winfo_screenheight() - w.winfo_reqheight() - 80
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

    def __init__(self, parent):
        w = self.win = tk.Toplevel(parent)
        w.title(f"{APP} vocabulary")
        w.transient(parent)
        w.grab_set()
        f = ttk.Frame(w, padding=10)
        f.grid(sticky="nsew")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="One name or term per line. To fix a word that keeps coming out wrong: wrong -> right",
                  foreground="#666").grid(row=0, column=0, sticky="w")
        self.text = tk.Text(f, width=60, height=20, font=("Consolas", 10), undo=True)
        self.text.grid(row=1, column=0, sticky="nsew", pady=6)
        sb = ttk.Scrollbar(f, command=self.text.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=6)
        self.text.config(yscrollcommand=sb.set)
        self.text.insert("1.0", load_vocabulary() or VOCAB_TEMPLATE)
        b = ttk.Frame(f)
        b.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(b, text="Save", command=self.save).grid(row=0, column=0, padx=4)
        ttk.Button(b, text="Cancel", command=w.destroy).grid(row=0, column=1)

    def save(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            VOCAB_FILE.write_text(self.text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        except OSError as e:
            messagebox.showerror(APP, f"Could not save:\n{e}")
            return
        self.win.destroy()


class SettingsDialog:
    """Where transcripts are made: GPU use for This PC, your own server, or an online API."""

    def __init__(self, parent, cfg, on_save=None):
        self.cfg = cfg
        self.on_save = on_save
        w = self.win = tk.Toplevel(parent)
        w.title(f"{APP} settings")
        w.resizable(False, False)
        w.transient(parent)
        w.grab_set()
        outer = ttk.Frame(w, padding=10)
        outer.grid()
        nb = ttk.Notebook(outer)
        nb.grid(row=0, column=0)
        f = ttk.Frame(nb, padding=10)      # transcript
        fn = ttk.Frame(nb, padding=10)     # notes and Claude
        fc = ttk.Frame(nb, padding=10)     # calls and dictation
        nb.add(f, text="Transcript")
        nb.add(fn, text="Notes & Claude")
        nb.add(fc, text="Calls & dictation")
        self.calls_tab = fc
        pad = {"padx": 6, "pady": 3}

        ttk.Label(f, text="This PC", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        self.gpu = tk.BooleanVar(value=cfg.get("use_gpu", True))
        ttk.Checkbutton(f, text="Use the NVIDIA GPU when CUDA is available (falls back to CPU)",
                        variable=self.gpu).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(f, text="My server", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="A CryRec server you run yourself (see server/ in the repo).", foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w", padx=6)
        self.s_url = self._row(f, 4, "Address", cfg.get("server_url", ""), "https://...")
        self.s_key = self._row(f, 5, "Key", secret_get(cfg, "server_key"), show="•")
        self.s_res = tk.StringVar()
        ttk.Button(f, text="Test", command=self.test_server).grid(row=6, column=2, sticky="e", **pad)
        ttk.Label(f, textvariable=self.s_res, foreground="#666").grid(row=6, column=0, columnspan=2, sticky="w", padx=6)

        ttk.Label(f, text="Online API", font=("Segoe UI", 10, "bold")).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="Any OpenAI-compatible Whisper API. Audio is sent to that company.", foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", padx=6)
        self.a_url = self._row(f, 9, "Base URL", cfg.get("api_url") or DEFAULT_API_URL)
        self.a_key = self._row(f, 10, "API key", secret_get(cfg, "api_key"), show="•")
        self.a_model = self._row(f, 11, "Model", cfg.get("api_model") or DEFAULT_API_MODEL)
        self.a_res = tk.StringVar()
        ttk.Button(f, text="Test", command=self.test_api).grid(row=12, column=2, sticky="e", **pad)
        ttk.Label(f, textvariable=self.a_res, foreground="#666").grid(row=12, column=0, columnspan=2, sticky="w", padx=6)

        ttk.Label(f, text="Transcript quality", font=("Segoe UI", 10, "bold")).grid(row=13, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="Clean up").grid(row=14, column=0, sticky="w", padx=6, pady=3)
        self.clean_var = tk.StringVar(value=CLEAN_ENGINES.get(cfg.get("clean_engine"), CLEAN_ENGINES["off"]))
        ttk.Combobox(f, textvariable=self.clean_var, values=list(CLEAN_ENGINES.values()), state="readonly", width=20).grid(row=14, column=1, sticky="w", padx=6)
        ttk.Button(f, text="Vocabulary...", command=lambda: VocabularyEditor(self.win)).grid(row=14, column=2, sticky="e", padx=6)
        ttk.Label(f, text="Clean up: Claude fixes capitals, punctuation and misheard terms (original kept).",
                  foreground="#666").grid(row=15, column=0, columnspan=3, sticky="w", padx=6)

        ttk.Label(fn, text="Notes", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(fn, text="Summary, decisions and action items from the transcript.", foreground="#666").grid(row=1, column=0, columnspan=3, sticky="w", padx=6)
        self.my_name = self._row(fn, 2, "Your name", cfg.get("my_name", ""))
        self.n_model = self._row(fn, 3, "API model", cfg.get("notes_model") or "llama-3.3-70b-versatile")
        self.o_url = self._row(fn, 4, "Ollama URL", cfg.get("ollama_url") or sm.DEFAULT_OLLAMA_URL)
        self.o_model = self._row(fn, 5, "Ollama model", cfg.get("ollama_model") or sm.DEFAULT_OLLAMA_MODEL)
        ttk.Label(fn, text="\"API model\" uses the Online API address and key (Transcript tab).", foreground="#666").grid(row=6, column=0, columnspan=3, sticky="w", padx=6)
        ttk.Label(fn, text="Claude", font=("Segoe UI", 10, "bold")).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(fn, text="Used for notes, transcript cleanup and dictation polish when Claude is picked.", foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", padx=6)
        self.c_key = self._row(fn, 9, "Claude key", secret_get(cfg, "claude_key"), show="•")
        self.c_model = self._row(fn, 10, "Claude model", cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL)

        ttk.Label(fc, text="Calls", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        self.watch = tk.BooleanVar(value=cfg.get("watch_calls", True))
        ttk.Checkbutton(fc, text="Notice Teams, Zoom and Webex calls and offer to record them",
                        variable=self.watch).grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(fc, text="Start CryRec with Windows (in the tray)",
                        variable=self.autostart).grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fc, text="Silence warning after").grid(row=3, column=0, sticky="w", **pad)
        sil = ttk.Frame(fc)
        sil.grid(row=3, column=1, columnspan=2, sticky="w", padx=6)
        self.silence = tk.StringVar(value=str(cfg.get("silence_minutes", 5)))
        ttk.Spinbox(sil, from_=0, to=60, width=5, textvariable=self.silence).grid(row=0, column=0)
        ttk.Label(sil, text="minutes (0 = off)").grid(row=0, column=1, padx=6)
        ttk.Label(fc, text="When a call ends or nothing is heard for that long, CryRec asks and then stops\n"
                           "by itself after 30 seconds. It never records without asking.",
                  foreground="#666").grid(row=4, column=0, columnspan=3, sticky="w", padx=6)

        ttk.Label(fc, text="Dictation", font=("Segoe UI", 10, "bold")).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.dict_on = tk.BooleanVar(value=cfg.get("dictation_enabled", True))
        ttk.Checkbutton(fc, text="Dictation shortcut: speak, and the text is typed where the cursor is",
                        variable=self.dict_on).grid(row=6, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fc, text="Shortcut").grid(row=7, column=0, sticky="w", **pad)
        self.dict_key = tk.StringVar(value=cfg.get("dictation_hotkey", "Ctrl+Win"))
        ttk.Combobox(fc, textvariable=self.dict_key, values=list(dictate.HOTKEYS), state="readonly", width=14).grid(row=7, column=1, sticky="w", padx=6)
        ttk.Label(fc, text="Polish").grid(row=8, column=0, sticky="w", **pad)
        self.polish = tk.StringVar(value=POLISH_MODES.get(cfg.get("dictation_polish"), POLISH_MODES["auto"]))
        ttk.Combobox(fc, textvariable=self.polish, values=list(POLISH_MODES.values()), state="readonly", width=20).grid(row=8, column=1, sticky="w", padx=6)
        self.polish_model = self._row(fc, 10, "Polish model", cfg.get("polish_model") or cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL)
        ttk.Label(fc, text="Tap the shortcut to start and again to stop (Esc cancels), or hold it while you talk.\n"
                           "Polish: Claude removes \"um\"s and fixes punctuation (Auto: via your server when\n"
                           "the transcript engine is My server, else with your Claude key). Faster models\n"
                           "(claude-haiku-5-5) add about 1 s, Opus about 2 s. Uses the mic\n"
                           "picked in the main window, or the Windows default.",
                  foreground="#666").grid(row=11, column=0, columnspan=3, sticky="w", padx=6)

        b = ttk.Frame(outer)
        b.grid(row=1, column=0, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Save", command=self.save).grid(row=0, column=0, padx=4)
        ttk.Button(b, text="Cancel", command=w.destroy).grid(row=0, column=1)

    def _row(self, f, r, label, value, hint=None, show=None):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=3)
        var = tk.StringVar(value=value)
        ttk.Entry(f, textvariable=var, width=46, show=show or "").grid(row=r, column=1, columnspan=2, sticky="w", padx=6, pady=3)
        return var

    def _bg(self, fn, out):
        out.set("Testing...")

        def run():
            try:
                msg = fn()
            except Exception as e:
                msg = f"Failed: {e}"
            self.win.after(0, lambda: out.set(msg[:70]))
        threading.Thread(target=run, daemon=True).start()

    def test_server(self):
        url, key = self.s_url.get().strip(), self.s_key.get().strip()

        def fn():
            h = tr.server_health(url, key)
            return f"OK, server {h.get('version')}, model {h.get('model')}"
        self._bg(fn, self.s_res)

    def test_api(self):
        url, key = self.a_url.get().strip().rstrip("/"), self.a_key.get().strip()

        def fn():
            req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}", "User-Agent": APP})
            with urllib.request.urlopen(req, timeout=20) as r:
                ids = [m.get("id") for m in json.load(r).get("data", [])]
            want = self.a_model.get().strip()
            return "OK, model found" if want in ids else f"Key OK, but model '{want}' not listed"
        self._bg(fn, self.a_res)

    def save(self):
        self.cfg.update(use_gpu=self.gpu.get(), server_url=self.s_url.get().strip(),
                        api_url=self.a_url.get().strip(), api_model=self.a_model.get().strip(),
                        my_name=self.my_name.get().strip(), claude_model=self.c_model.get().strip(),
                        notes_model=self.n_model.get().strip(), ollama_url=self.o_url.get().strip(),
                        ollama_model=self.o_model.get().strip(),
                        clean_engine=next((k for k, v in CLEAN_ENGINES.items() if v == self.clean_var.get()), "off"),
                        watch_calls=self.watch.get(), silence_minutes=_int(self.silence.get(), 5, 0, 60),
                        dictation_enabled=self.dict_on.get(), dictation_hotkey=self.dict_key.get(),
                        dictation_polish=next((k for k, v in POLISH_MODES.items() if v == self.polish.get()), "auto"),
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


class App:
    def __init__(self, root, hidden=False):
        self.hidden = hidden
        self.root = root
        self.cfg = load_config()
        self.pa = pyaudio.PyAudio()
        self.inputs, self.outputs = {}, {}
        self.tracks = []
        self.keepalive = None
        self.session_dir = None
        self.t0 = None
        self.recording = False

        root.title(f"{APP} v{VERSION}" + (" (portable)" if PORTABLE else ""))
        try:
            root.iconbitmap(str(RES_DIR / "cryrec.ico"))
        except tk.TclError:
            pass
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(root, padding=10)
        frm.grid()

        ttk.Label(frm, text="Input (mic)").grid(row=0, column=0, sticky="w", **pad)
        self.in_var = tk.StringVar()
        self.in_box = ttk.Combobox(frm, textvariable=self.in_var, state="readonly", width=48)
        self.in_box.grid(row=0, column=1, columnspan=2, **pad)

        ttk.Label(frm, text="Output (speaker)").grid(row=1, column=0, sticky="w", **pad)
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(frm, textvariable=self.out_var, state="readonly", width=48)
        self.out_box.grid(row=1, column=1, columnspan=2, **pad)

        ttk.Label(frm, text="Mic level").grid(row=2, column=0, sticky="w", **pad)
        self.in_meter = ttk.Progressbar(frm, length=330, maximum=1.0)
        self.in_meter.grid(row=2, column=1, columnspan=2, **pad)
        ttk.Label(frm, text="Speaker level").grid(row=3, column=0, sticky="w", **pad)
        self.out_meter = ttk.Progressbar(frm, length=330, maximum=1.0)
        self.out_meter.grid(row=3, column=1, columnspan=2, **pad)

        ttk.Label(frm, text="Save to").grid(row=4, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(value=self.cfg.get("out_dir", str(DEFAULT_OUT)))
        ttk.Entry(frm, textvariable=self.dir_var, width=38).grid(row=4, column=1, sticky="w", **pad)
        ttk.Button(frm, text="Browse", command=self.pick_dir).grid(row=4, column=2, sticky="e", **pad)

        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, columnspan=3, pady=(10, 0))
        self.rec_btn = ttk.Button(bar, text="● Record", width=14, command=self.toggle)
        self.rec_btn.grid(row=0, column=0, padx=6)
        ttk.Button(bar, text="Refresh devices", command=self.load_devices).grid(row=0, column=1, padx=6)
        ttk.Button(bar, text="Open folder", command=self.open_folder).grid(row=0, column=2, padx=6)

        self.status = tk.StringVar(value="Ready")
        self.timer = tk.StringVar(value="00:00:00")
        ttk.Label(frm, textvariable=self.timer, font=("Segoe UI", 16, "bold")).grid(row=6, column=0, pady=(10, 0), sticky="w", padx=10)
        ttk.Label(frm, textvariable=self.status, wraplength=330).grid(row=6, column=1, columnspan=2, pady=(10, 0), sticky="w", padx=10)

        # transcript
        ttk.Separator(frm).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ttk.Label(frm, text="Transcript").grid(row=8, column=0, sticky="w", **pad)
        opts = ttk.Frame(frm)
        opts.grid(row=8, column=1, columnspan=2, sticky="w", padx=10)
        self.engine_var = tk.StringVar(value=ENGINES.get(self.cfg.get("engine"), ENGINES["local"]))
        eng_box = ttk.Combobox(opts, textvariable=self.engine_var, values=list(ENGINES.values()), state="readonly", width=12)
        eng_box.grid(row=0, column=0)
        eng_box.bind("<<ComboboxSelected>>", lambda e: self._engine_changed())
        self.model_labels = {v["label"]: k for k, v in tr.MODELS.items()}
        self.model_var = tk.StringVar(value=tr.MODELS.get(self.cfg.get("model"), tr.MODELS["turbo"])["label"])
        self.model_box = ttk.Combobox(opts, textvariable=self.model_var, values=list(self.model_labels), state="readonly", width=22)
        self.model_box.grid(row=0, column=1, padx=(6, 0))
        lang = self.cfg.get("language")
        self.lang_var = tk.StringVar(value=lang if lang in tr.LANGUAGES else "Auto")
        ttk.Combobox(opts, textvariable=self.lang_var, values=list(tr.LANGUAGES), state="readonly", width=12).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(frm, text="Notes").grid(row=9, column=0, sticky="w", **pad)
        nopts = ttk.Frame(frm)
        nopts.grid(row=9, column=1, columnspan=2, sticky="w", padx=10)
        self.notes_var = tk.StringVar(value=NOTES_ENGINES.get(self.cfg.get("notes_engine"), NOTES_ENGINES["off"]))
        ttk.Combobox(nopts, textvariable=self.notes_var, values=list(NOTES_ENGINES.values()), state="readonly", width=20).grid(row=0, column=0)
        nl = self.cfg.get("notes_language")
        self.notes_lang_var = tk.StringVar(value=nl if nl in sm.NOTES_LANGUAGES else "English")
        ttk.Combobox(nopts, textvariable=self.notes_lang_var, values=list(sm.NOTES_LANGUAGES), state="readonly", width=16).grid(row=0, column=1, padx=(6, 0))
        self.auto_var = tk.BooleanVar(value=self.cfg.get("auto_transcribe", True))
        ttk.Checkbutton(frm, text="Transcribe (and write notes) after recording", variable=self.auto_var).grid(row=10, column=1, columnspan=2, sticky="w", padx=10)
        tbtns = ttk.Frame(frm)
        tbtns.grid(row=11, column=0, columnspan=3, sticky="e", padx=10, pady=(4, 0))
        ttk.Button(tbtns, text="Settings", command=self.open_settings).grid(row=0, column=0, padx=(0, 6))
        self.notes_btn = ttk.Button(tbtns, text="Notes...", command=self.notes_clicked)
        self.notes_btn.grid(row=0, column=1, padx=(0, 6))
        self.tr_btn = ttk.Button(tbtns, text="Transcribe...", command=self.transcribe_clicked)
        self.tr_btn.grid(row=0, column=2)
        self._engine_changed()
        self.t_status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.t_status, foreground="#666", wraplength=430).grid(row=12, column=0, columnspan=3, sticky="w", padx=10)
        self.t_queue = queue.Queue()
        self.t_cancel = threading.Event()
        self.t_busy = False
        threading.Thread(target=self._transcribe_worker, daemon=True).start()

        ttk.Separator(frm).grid(row=13, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.upd_status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.upd_status, foreground="#666").grid(row=14, column=0, columnspan=2, sticky="w", padx=10)
        self.upd_btn = ttk.Button(frm, text="Check for updates", command=self.update_clicked)
        self.upd_btn.grid(row=14, column=2, sticky="e", padx=10)

        self.updater = Updater(lambda v: root.after(0, lambda: self.update_ready(v)))
        self.updater.start_background()

        self.load_devices()
        self.tick()

        # calls, silence, tray
        self.calls = callwatch.CallWatcher(self.on_call_start, self.on_call_end)
        self.call_app = None
        self.start_popup = self.end_popup = None
        self.last_sound = time.monotonic()
        self.tray = None
        self._setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.hide_or_quit)
        root.after(2000, self._poll_calls)
        root.after(1000, self._poll_show_flag)
        self.dictation = DictationUI(self)
        if self.hidden:
            root.withdraw()
        elif not self.cfg.get("asked_autostart"):
            root.after(800, self._ask_autostart)

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
    def update_ready(self, version):
        self.upd_status.set(f"Version {version} is ready to install.")
        self.upd_btn.config(text=f"Install update {version}", state="normal")

    def update_clicked(self):
        if self.updater.path:
            self.install_update()
            return
        self.upd_btn.config(state="disabled")
        self.upd_status.set("Checking...")

        def run():
            res = self.updater.check()
            msg = {"latest": f"You're on the latest version ({VERSION}).",
                   "busy": "Already checking, one moment.",
                   "dev": "Dev build: updates only work in the exe.",
                   "newer": None}.get(res, "Couldn't reach GitHub. Try again later.")
            if msg:
                self.root.after(0, lambda: (self.upd_status.set(msg), self.upd_btn.config(state="normal")))
        threading.Thread(target=run, daemon=True).start()

    def install_update(self):
        if self.recording or str(self.rec_btn["state"]) == "disabled":
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
    def _engine(self):
        return next((k for k, v in ENGINES.items() if v == self.engine_var.get()), "local")

    def _engine_changed(self):
        self.model_box.config(state="readonly" if self._engine() == "local" else "disabled")

    def _transcript_settings(self):
        model = self.model_labels.get(self.model_var.get(), "turbo")
        notes = next((k for k, v in NOTES_ENGINES.items() if v == self.notes_var.get()), "off")
        self.cfg.update(engine=self._engine(), model=model, language=self.lang_var.get(),
                        auto_transcribe=self.auto_var.get(), notes_engine=notes,
                        notes_language=self.notes_lang_var.get())
        save_config(self.cfg)
        return dict(self.cfg)

    def _notes_ready(self, cfg):
        """UI thread: True if the chosen notes engine has what it needs (asks the user otherwise)."""
        need = {"claude": ("claude_key", "your Claude API key"), "openai": ("api_key", "your API key"),
                "server_local": ("server_key", "your server address and key"),
                "server_claude": ("server_key", "your server address and key")}.get(cfg.get("notes_engine"))
        if need and not secret_get(cfg, need[0]):
            messagebox.showinfo(APP, f"Notes: set {need[1]} in Settings first.")
            return False
        return True

    def notes_clicked(self):
        if self.t_busy:
            return
        cfg = self._transcript_settings()
        if cfg["notes_engine"] == "off":
            messagebox.showinfo(APP, "Pick a notes engine next to \"Notes\" first.")
            return
        start = self.session_dir if self.session_dir and self.session_dir.exists() else Path(self.dir_var.get())
        d = filedialog.askdirectory(initialdir=str(start), title="Pick a recording folder with a transcript")
        if not d:
            return
        if not (Path(d) / "transcript.txt").exists():
            messagebox.showwarning(APP, "This folder has no transcript.txt yet. Use Transcribe... first.")
            return
        if self._notes_ready(cfg):
            self.t_queue.put((Path(d), cfg, "notes"))

    def open_settings(self):
        SettingsDialog(self.root, self.cfg, on_save=lambda: self.dictation.restart())

    def transcribe_clicked(self):
        if self.t_busy:
            self.t_cancel.set()
            self.t_status.set("Cancelling...")
            return
        start = self.session_dir if self.session_dir and self.session_dir.exists() else Path(self.dir_var.get())
        d = filedialog.askdirectory(initialdir=str(start), title="Pick a recording folder")
        if d:
            self.enqueue_transcript(Path(d))

    def enqueue_transcript(self, session_dir):
        """UI thread only: checks the model is there (asks to download once) and queues the job."""
        cfg = self._transcript_settings()
        if not tr.find_tracks(session_dir):
            messagebox.showwarning(APP, f"No recording found in:\n{session_dir}")
            return
        engine, model = cfg["engine"], cfg["model"]
        if engine == "local" and not tr.model_ready(MODELS_DIR, model):
            info = tr.MODELS[model]
            if not messagebox.askyesno(APP, f"Transcripts need the {info['label'].split(' (')[0]} speech model "
                                            f"({info['size_gb']} GB, one-time download).\n\n"
                                            "It runs on this computer. Audio never leaves it.\n\nDownload now?"):
                self.t_status.set("Transcript skipped (no model).")
                return
        if engine == "server" and not (cfg.get("server_url") and secret_get(cfg, "server_key")):
            messagebox.showinfo(APP, "Set your server address and key in Settings first.")
            return
        if engine == "api" and not secret_get(cfg, "api_key"):
            messagebox.showinfo(APP, "Set your API key in Settings first.")
            return
        if cfg["notes_engine"] != "off" and not self._notes_ready(cfg):
            cfg["notes_engine"] = "off"  # still make the transcript
        clean = cfg.get("clean_engine", "off")
        if (clean == "claude" and not secret_get(cfg, "claude_key")) or \
                (clean == "server_claude" and not secret_get(cfg, "server_key")):
            messagebox.showinfo(APP, "Clean up: set the Claude key or server key in Settings first.")
            cfg["clean_engine"] = "off"
        self.t_queue.put((session_dir, cfg, "all"))
        if not self.t_busy:
            self.t_status.set("Transcript queued...")

    def _transcribe_worker(self):
        loaded = {}
        while True:
            session_dir, cfg, mode = self.t_queue.get()
            self.t_cancel.clear()
            self._t_ui(busy=True)

            def progress(frac, text, name=session_dir.name):
                self._t_ui(text=f"{name}: {text} ({frac * 100:.0f}%)")
            try:
                if mode == "all":
                    out = run_transcript(cfg, session_dir, progress, self.t_cancel.is_set, loaded)
                    msg = f"Transcript saved: {out}"
                    if cfg.get("clean_engine", "off") != "off":
                        try:
                            kept = run_clean(cfg, session_dir, progress, self.t_cancel.is_set)
                            msg = "Transcript saved and cleaned" + (f" ({kept} lines left as heard)" if kept else "")
                        except (tr.Cancelled, sm.Cancelled):
                            raise
                        except Exception as e:
                            msg = f"Transcript saved, cleanup failed: {e}"
                if cfg.get("notes_engine", "off") != "off":
                    try:
                        notes = run_notes(cfg, session_dir, progress, self.t_cancel.is_set)
                        msg = f"Transcript and notes saved: {notes.parent}" if mode == "all" else f"Notes saved: {notes}"
                    except (tr.Cancelled, sm.Cancelled):
                        raise
                    except Exception as e:
                        msg = (f"Transcript saved, notes failed: {e}" if mode == "all" else f"Notes failed: {e}")
            except (tr.Cancelled, sm.Cancelled):
                msg = "Transcript cancelled."
                while not self.t_queue.empty():
                    self.t_queue.get_nowait()
            except Exception as e:
                msg = f"Transcript failed: {e}"
            if self.t_queue.empty():
                loaded.clear()  # free ~1 GB of RAM until the next job
            self._t_ui(busy=not self.t_queue.empty(), text=msg)

    def _t_ui(self, busy=None, text=None):
        def apply():
            if busy is not None:
                self.t_busy = busy
                self.tr_btn.config(text="Cancel transcript" if busy else "Transcribe...")
            if text is not None:
                self.t_status.set(text)
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
        else:
            self.start()

    def start(self):
        mic = self.inputs.get(self.in_var.get())
        spk = self.outputs.get(self.out_var.get())
        if not mic and not spk:
            messagebox.showwarning(APP, "Pick at least one input or output.")
            return
        out_root = Path(self.dir_var.get()).expanduser()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = out_root / stamp
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP, f"Cannot create folder:\n{e}")
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
        self.rec_btn.config(text="■ Stop")
        self.in_box.config(state="disabled")
        self.out_box.config(state="disabled")
        self.status.set(f"Recording to {self.session_dir}")
        self.last_sound = time.monotonic()
        if self.start_popup and self.start_popup.alive:
            self.start_popup.close()
        self._tray_title()

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
        self.rec_btn.config(text="● Record", state="disabled")
        self.in_box.config(state="readonly")
        self.out_box.config(state="readonly")
        errors = [f"{t.role}: {t.error}" for t in self.tracks if t.error]
        self.status.set("Saving MP3...")
        threading.Thread(target=self._finish, args=(list(self.tracks), self.session_dir, errors), daemon=True).start()

    def _finish(self, tracks, session_dir, errors):
        msg = finalize(tracks, session_dir)
        if errors:
            msg += "\nErrors: " + "; ".join(errors)

        def done():
            self.status.set(msg)
            self.rec_btn.config(state="normal")
            if self.auto_var.get() and msg.startswith("Saved"):
                self.enqueue_transcript(session_dir)
        self.root.after(0, done)

    # ---------- ui ----------
    def tick(self):
        if self.recording:
            secs = int(time.monotonic() - self.t0)
            self.timer.set(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")
            lv = {t.role: t.level for t in self.tracks}
            self.in_meter["value"] = lv.get("mic", 0)
            self.out_meter["value"] = lv.get("spk", 0)
            self._check_silence(lv.values())
        else:
            self.in_meter["value"] = 0
            self.out_meter["value"] = 0
        self.root.after(100, self.tick)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def open_folder(self):
        target = self.session_dir if self.session_dir and self.session_dir.exists() else Path(self.dir_var.get())
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def quit(self):
        if self.recording:
            self.show()
        if self.recording and not messagebox.askyesno(APP, "Recording is running. Stop and save before closing?"):
            return
        if not self.recording and self.t_busy and not messagebox.askyesno(
                APP, "A transcript is running. Close anyway? You can run it again later with Transcribe..."):
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
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, hidden="--hidden" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
