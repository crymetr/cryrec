"""CryRec: record a chosen input (mic) and a chosen output (speaker loopback) together.

Each source is written to its own WAV while recording. On stop, ffmpeg mixes them
into one MP3. The separate tracks are kept as FLAC (me / them) for later transcripts.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyaudiowpatch as pyaudio

APP = "CryRec"
VERSION = "0.1.0"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUT = Path.home() / "Documents" / "Recordings"
NO_DEVICE = "(none)"
CHUNK_MS = 50
GAP_PAD_SEC = 0.25  # loopback sends nothing during silence; pad gaps larger than this


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


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = Path(__file__).with_name("ffmpeg.exe")
    return str(local) if local.exists() else None


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

        root.title(f"{APP} v{VERSION}")
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

        self.load_devices()
        self.tick()

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
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return f"Saved WAV files (ffmpeg not found, no MP3): {session_dir}"
    mp3 = session_dir / "meeting.mp3"
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for w in wavs:
        cmd += ["-i", str(w)]
    if len(wavs) > 1:
        cmd += ["-filter_complex", f"amix=inputs={len(wavs)}:duration=longest:normalize=0,alimiter=limit=0.95"]
    cmd += ["-ac", "2", "-b:a", "128k", str(mp3)]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=no_window)
    if r.returncode != 0:
        return f"MP3 mix failed, WAV files kept: {r.stderr.strip()[:200]}"
    for w in wavs:
        flac = w.with_suffix(".flac")
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(w), str(flac)],
                           capture_output=True, creationflags=no_window)
        if r.returncode == 0 and flac.exists():
            w.unlink()
    return f"Saved {mp3}"


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
