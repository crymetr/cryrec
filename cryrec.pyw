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

import summarize as sm
import transcribe as tr

APP = "CryRec"
VERSION = "0.5.0"
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
    """Named mutex the installer waits on, so it never replaces files while CryRec runs."""
    try:
        import ctypes
        return ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except Exception:
        return None


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

    def __init__(self, parent, cfg):
        self.cfg = cfg
        w = self.win = tk.Toplevel(parent)
        w.title(f"{APP} settings")
        w.resizable(False, False)
        w.transient(parent)
        w.grab_set()
        f = ttk.Frame(w, padding=12)
        f.grid()
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

        ttk.Label(f, text="Notes", font=("Segoe UI", 10, "bold")).grid(row=13, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="Summary, decisions and action items from the transcript.", foreground="#666").grid(row=14, column=0, columnspan=3, sticky="w", padx=6)
        self.my_name = self._row(f, 15, "Your name", cfg.get("my_name", ""))
        self.c_key = self._row(f, 16, "Claude key", secret_get(cfg, "claude_key"), show="•")
        self.c_model = self._row(f, 17, "Claude model", cfg.get("claude_model") or sm.DEFAULT_CLAUDE_MODEL)
        self.n_model = self._row(f, 18, "API model", cfg.get("notes_model") or "llama-3.3-70b-versatile")
        self.o_url = self._row(f, 19, "Ollama URL", cfg.get("ollama_url") or sm.DEFAULT_OLLAMA_URL)
        self.o_model = self._row(f, 20, "Ollama model", cfg.get("ollama_model") or sm.DEFAULT_OLLAMA_MODEL)
        ttk.Label(f, text="\"API model\" is used with the Online API address and key above.", foreground="#666").grid(row=21, column=0, columnspan=3, sticky="w", padx=6)

        ttk.Label(f, text="Transcript quality", font=("Segoe UI", 10, "bold")).grid(row=22, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="Clean up").grid(row=23, column=0, sticky="w", padx=6, pady=3)
        self.clean_var = tk.StringVar(value=CLEAN_ENGINES.get(cfg.get("clean_engine"), CLEAN_ENGINES["off"]))
        ttk.Combobox(f, textvariable=self.clean_var, values=list(CLEAN_ENGINES.values()), state="readonly", width=20).grid(row=23, column=1, sticky="w", padx=6)
        ttk.Button(f, text="Vocabulary...", command=lambda: VocabularyEditor(self.win)).grid(row=23, column=2, sticky="e", padx=6)
        ttk.Label(f, text="Clean up: Claude fixes capitals, punctuation and misheard terms (original kept).",
                  foreground="#666").grid(row=24, column=0, columnspan=3, sticky="w", padx=6)

        b = ttk.Frame(f)
        b.grid(row=25, column=0, columnspan=3, sticky="e", pady=(12, 0))
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
                        clean_engine=next((k for k, v in CLEAN_ENGINES.items() if v == self.clean_var.get()), "off"))
        secret_set(self.cfg, "server_key", self.s_key.get().strip())
        secret_set(self.cfg, "api_key", self.a_key.get().strip())
        secret_set(self.cfg, "claude_key", self.c_key.get().strip())
        save_config(self.cfg)
        self.win.destroy()


class App:
    def __init__(self, root):
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
        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        SettingsDialog(self.root, self.cfg)

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

    def _teardown(self):
        for t in self.tracks:
            t.stop()
        if self.keepalive:
            self.keepalive.stop()
            self.keepalive = None

    def stop(self):
        self._teardown()
        self.recording = False
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

    def on_close(self):
        if self.recording and not messagebox.askyesno(APP, "Recording is running. Stop and save before closing?"):
            return
        if not self.recording and self.t_busy and not messagebox.askyesno(
                APP, "A transcript is running. Close anyway? You can run it again later with Transcribe..."):
            return
        self.t_cancel.set()
        if self.recording:
            self._teardown()
            finalize(self.tracks, self.session_dir)
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
    _mutex = hold_mutex()  # noqa: F841  (kept alive for the process lifetime)
    cleanup_old_update()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
