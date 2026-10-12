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

import lameenc
import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf
import soxr

APP = "CryRec"
VERSION = "0.1.2"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
PORTABLE = (APP_DIR / "portable").exists()
CONFIG_DIR = APP_DIR if PORTABLE else Path(os.environ.get("APPDATA", Path.home())) / APP
CONFIG_FILE = CONFIG_DIR / "cryrec.json"
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

        ttk.Separator(frm).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.upd_status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.upd_status, foreground="#666").grid(row=8, column=0, columnspan=2, sticky="w", padx=10)
        self.upd_btn = ttk.Button(frm, text="Check for updates", command=self.update_clicked)
        self.upd_btn.grid(row=8, column=2, sticky="e", padx=10)

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
        try:
            self.updater.apply()
        except Exception as e:
            messagebox.showerror(APP, f"Could not start the update:\n{e}")
            return
        self.pa.terminate()
        self.root.destroy()
        os._exit(0)  # the installer / new exe needs this process gone

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
        self.root.after(0, lambda: (self.status.set(msg), self.rec_btn.config(state="normal")))

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
