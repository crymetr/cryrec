"""Transcript viewer with an audio player (click a line = play from there) and search across recordings."""
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import av
import numpy as np

import archive

RATE = 48000
AUDIO_NAMES = ("meeting.mp3", "meeting.m4a")
PRE_ROLL = 2.0  # start a little before the line, so the first word is not cut


def fmt(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def find_audio(folder):
    for name in AUDIO_NAMES:
        p = Path(folder) / name
        if p.exists():
            return p
    return None


class Player:
    """Streams one audio file to the default output. Decoding runs in a thread; seek/pause are requests to it.
    Has its own PyAudio instance, so refreshing devices in the main window can't close its stream."""

    def __init__(self, path):
        self.path = str(path)
        with av.open(self.path) as c:
            s = c.streams.audio[0]
            self.duration = float(c.duration / av.time_base) if c.duration else float(s.duration * s.time_base)
        self.pos = 0.0
        self.playing = False
        self.error = None  # set by the worker if playback dies; the viewer shows it
        self._seek = None
        self._stop = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def play(self, at=None):
        with self._lock:
            if at is not None:
                self._seek = max(0.0, min(at, self.duration - 0.5))
                self.pos = self._seek
            self.playing = True
        self._wake.set()

    def pause(self):
        self.playing = False

    def seek(self, at):
        with self._lock:
            self._seek = max(0.0, min(at, self.duration - 0.5))
            self.pos = self._seek
        self._wake.set()

    def close(self):
        self._stop = True
        self.playing = False
        self._wake.set()

    def _run(self):
        pa = stream = container = None
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            stream = pa.open(format=pa.get_format_from_width(2), channels=1, rate=RATE, output=True,
                             frames_per_buffer=2048)
            container = av.open(self.path)
            self._loop(stream, container)
        except Exception as e:
            self.error = str(e) or type(e).__name__
            self.playing = False
        finally:
            if container:
                container.close()
            if stream:
                stream.stop_stream()
                stream.close()
            if pa:
                pa.terminate()

    def _loop(self, stream, container):
        astream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        frames = container.decode(astream)
        written = self.pos  # seconds of audio handed to the device since the last seek
        skip_until = None  # after a seek: drop decoded audio before this time (seek lands on an earlier frame)
        while not self._stop:
            with self._lock:
                target, self._seek = self._seek, None
            if target is not None:
                container.seek(int(target / astream.time_base), stream=astream, any_frame=False, backward=True)
                resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
                frames = container.decode(astream)
                written = skip_until = target
            if not self.playing:
                self._wake.wait(0.2)
                self._wake.clear()
                continue
            try:
                frame = next(frames)
            except (StopIteration, av.error.EOFError):
                with self._lock:
                    if self._seek is None:  # a play(at) that came in meanwhile wins over "ended"
                        self.playing = False
                        self.pos = self.duration
                continue
            for out in resampler.resample(frame):
                pcm = out.to_ndarray().reshape(-1).astype(np.int16)
                t0 = float(frame.time) if frame.time is not None else written
                if skip_until is not None:
                    if t0 < skip_until:
                        pcm = pcm[int((skip_until - t0) * RATE):]
                        t0 = skip_until
                    if len(pcm):
                        skip_until = None
                if not len(pcm):
                    continue
                stream.write(pcm.tobytes())
                written = t0 + len(pcm) / RATE
                if self._seek is None:
                    self.pos = written
                if self._stop or not self.playing or self._seek is not None:
                    break


class _TextBox:
    """Read-only themed Text with a scrollbar."""

    def __init__(self, parent, theme, row, height=22, width=90):
        px = theme.px
        self.text = tk.Text(parent, wrap="word", width=width, height=height, relief="flat", cursor="arrow",
                            font=("Segoe UI", 10), padx=px(10), pady=px(8), highlightthickness=1,
                            spacing1=px(2), spacing3=px(2))
        self.text.grid(row=row, column=0, sticky="nsew")
        sb = ttk.Scrollbar(parent, command=self.text.yview)
        sb.grid(row=row, column=1, sticky="ns")
        self.text.config(yscrollcommand=sb.set)
        self.theme = theme
        theme.on_change(self.recolor)
        self.recolor()

    def recolor(self, t=None):
        t = self.theme
        if not self.text.winfo_exists():
            return
        self.text.config(bg=t.c("field"), fg=t.c("text"), selectbackground=t.c("accent"),
                         highlightbackground=t.c("border"), highlightcolor=t.c("border"),
                         insertbackground=t.c("text"))
        self.text.tag_config("ts", foreground=t.c("accent"))
        self.text.tag_config("who", foreground=t.c("muted"), font=("Segoe UI Semibold", 10))
        self.text.tag_config("mark", foreground=t.c("warn"))
        self.text.tag_config("now", background=t.c("meter_off"))
        self.text.tag_config("hit", background=t.c("meter_mid"), foreground="#000000")
        self.text.tag_config("title", font=("Segoe UI Semibold", 10))
        self.text.tag_config("muted", foreground=t.c("muted"))


class ViewerWindow:
    """One recording: transcript lines (click = play from there), bookmarks, player bar, find, actions."""

    def __init__(self, app, folder, at=None, highlight=""):
        self.app, self.folder = app, Path(folder)
        theme, px = app.theme, app.theme.px
        w = self.win = tk.Toplevel(app.root)
        w.title(f"{archive.folder_title(folder)} · CryRec")
        theme.register(w)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        f = ttk.Frame(w, padding=px(12))
        f.grid(sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(8)))
        top.columnconfigure(3, weight=1)
        self.play_btn = ttk.Button(top, text="▶", width=3, command=self.toggle)
        self.play_btn.grid(row=0, column=0)
        ttk.Button(top, text="−10", width=4, command=lambda: self.jump(-10)).grid(row=0, column=1, padx=(px(4), 0))
        ttk.Button(top, text="+10", width=4, command=lambda: self.jump(10)).grid(row=0, column=2, padx=(px(4), 0))
        self.scale_var = tk.DoubleVar(value=0)
        self.scale = ttk.Scale(top, variable=self.scale_var, from_=0, to=1)
        self.scale.grid(row=0, column=3, sticky="ew", padx=px(8))
        self.scale.bind("<ButtonPress-1>", self._scrub)
        self.scale.bind("<ButtonRelease-1>", self._scrub_done)
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(top, textvariable=self.time_var, style="Muted.TLabel").grid(row=0, column=4)
        self.find_var = tk.StringVar(value=highlight)
        find = ttk.Entry(top, textvariable=self.find_var, width=18)
        find.grid(row=0, column=5, padx=(px(10), 0))
        find.bind("<Return>", lambda e: self.find_next())
        ttk.Button(top, text="Find", command=self.find_next).grid(row=0, column=6, padx=(px(4), 0))
        self.more_btn = ttk.Button(top, text="⋯", width=3, command=self.menu)
        self.more_btn.grid(row=0, column=7, padx=(px(4), 0))

        self.box = _TextBox(f, theme, row=1)
        self.text = self.box.text
        self.status = tk.StringVar()
        ttk.Label(f, textvariable=self.status, style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(px(6), 0))

        self.lines = []  # (seconds, text index of line start)
        self.load()
        self.player = None
        self.dragging = False
        audio = find_audio(self.folder)
        if audio:
            try:
                self.player = Player(audio)
                self.scale.config(to=self.player.duration)
            except Exception as e:  # unreadable / half-written audio: transcript still useful
                self.status.set(f"Audio can't be played: {e}")
        else:
            self.status.set("No audio in this folder (only the text).")
        if not self.player:
            self.play_btn.state(["disabled"])
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.bind("<space>", lambda e: (self.toggle(), "break")[1])
        w.bind("<Control-f>", lambda e: find.focus_set())
        w.geometry(f"{px(760)}x{px(620)}")
        if highlight:
            self.find_next(first=True)
        if at is not None:
            self.goto(at, play=bool(self.player))
        self._tick()

    def load(self):
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        self.lines = []
        marks = sorted(archive.load_bookmarks(self.folder), key=lambda b: b["t"])
        if marks:
            t.insert("end", "Bookmarks  ", "title")
            for i, b in enumerate(marks):
                tag = f"mark{i}"
                t.insert("end", f"★ {fmt(b['t'])} {b.get('label', '')}   ", ("mark", tag))
                t.tag_bind(tag, "<Button-1>", lambda e, s=b["t"]: self.goto(s, play=True))
            t.insert("end", "\n\n")
        rows = archive.transcript_lines(self.folder / "transcript.txt")
        if not rows:
            t.insert("end", "No transcript yet.", "muted")
        for i, (sec, who, line) in enumerate(rows):
            start = t.index("end-1c")
            tag = f"l{i}"
            t.insert("end", f"{fmt(sec)}  ", ("ts", tag))
            t.insert("end", f"{who}: ", ("who", tag))
            t.insert("end", line + "\n", tag)
            t.tag_bind(tag, "<Button-1>", lambda e, s=sec: self.goto(s, play=True))
            self.lines.append((sec, start))
        t.config(state="disabled")
        self.status.set(f"{len(rows)} lines · click a line to play from there · Space = play/pause")

    def goto(self, sec, play=False):
        if self.player:
            at = max(0, sec - PRE_ROLL)
            self.player.play(at) if play else self.player.seek(at)
        self._mark_line(sec, see=True)

    def _mark_line(self, sec, see=False):
        idx = None
        for s, start in self.lines:
            if s <= sec + 0.5:
                idx = start
            else:
                break
        self.text.tag_remove("now", "1.0", "end")
        if idx:
            self.text.tag_add("now", idx, f"{idx} lineend")
            if see:
                self.text.see(idx)

    def toggle(self):
        if not self.player:
            return
        if self.player.playing:
            self.player.pause()
        else:
            self.player.play(None if self.player.pos < self.player.duration - 0.5 else 0)

    def jump(self, d):
        if self.player:
            self.player.seek(self.player.pos + d)

    def _scrub(self, _v):
        self.dragging = True

    def _scrub_done(self, _e):
        self.dragging = False
        if self.player:
            self.player.seek(self.scale_var.get())
            self._mark_line(self.scale_var.get(), see=True)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        if self.player and self.player.error:
            self.status.set(f"Playback stopped: {self.player.error}")
            self.player = None
            self.play_btn.state(["disabled"])
        if self.player:
            p = self.player
            self.play_btn.config(text="⏸" if p.playing else "▶")
            if not self.dragging:
                self.scale_var.set(p.pos)
            self.time_var.set(f"{fmt(p.pos)} / {fmt(p.duration)}")
            if p.playing:
                self._mark_line(p.pos, see=False)
        self.win.after(250, self._tick)

    def find_next(self, first=False):
        q = self.find_var.get().strip()
        t = self.text
        t.tag_remove("hit", "1.0", "end")
        if not q:
            return
        start = "1.0"
        count = tk.IntVar()
        first_hit = None
        while True:
            pos = t.search(q, start, "end", nocase=True, count=count)
            if not pos:
                break
            t.tag_add("hit", pos, f"{pos}+{count.get()}c")
            first_hit = first_hit or pos
            start = f"{pos}+{count.get()}c"
        if not first_hit:
            self.status.set(f"'{q}' not found in this recording")
            return
        cur = t.index("insert") if not first else "1.0"
        nxt = t.tag_nextrange("hit", f"{cur}+1c") or t.tag_nextrange("hit", "1.0")
        t.mark_set("insert", nxt[0])
        t.see(nxt[0])

    def menu(self):
        m = tk.Menu(self.win, tearoff=False)
        self.app.add_action_items(m, self.folder)
        m.add_command(label="Open folder", command=lambda: os.startfile(self.folder))
        b = self.more_btn
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def close(self):
        if self.player:
            self.player.close()
        self.win.destroy()


class SearchWindow:
    """Search every recording's transcript, notes and bookmarks; double-click opens the moment."""

    def __init__(self, app, archive_obj, folders_fn):
        self.app, self.arch, self.folders_fn = app, archive_obj, folders_fn
        theme, px = app.theme, app.theme.px
        w = self.win = tk.Toplevel(app.root)
        w.title("Search recordings · CryRec")
        theme.register(w)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        f = ttk.Frame(w, padding=px(12))
        f.grid(sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(8)))
        top.columnconfigure(0, weight=1)
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q)
        e.grid(row=0, column=0, sticky="ew")
        e.bind("<Return>", lambda ev: self.run())
        ttk.Button(top, text="Search", style="Accent.TButton", command=self.run).grid(row=0, column=1, padx=(px(6), 0))
        self.box = _TextBox(f, theme, row=1, height=24, width=96)
        self.text = self.box.text
        self.status = tk.StringVar(value="Type words; every word must appear. Click a result to open it.")
        ttk.Label(f, textvariable=self.status, style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        w.geometry(f"{px(800)}x{px(600)}")
        e.focus_set()
        self.indexing, self.index_msg = True, None
        threading.Thread(target=self._index, daemon=True).start()
        self._index_done()

    def _index(self):  # worker thread: no Tk calls here
        try:
            n = self.arch.update(self.folders_fn())
            self.index_msg = f"Index updated ({n} recordings changed)." if n else ""
        except Exception as e:
            self.index_msg = f"Indexing failed: {e}"
        self.indexing = False

    def _index_done(self):
        if not self.win.winfo_exists():
            return
        if self.indexing:
            self.win.after(200, self._index_done)
        elif self.index_msg:
            self.status.set(self.index_msg)

    def run(self):
        q = self.q.get().strip()
        if not q:
            return
        if self.indexing:
            self.status.set("Indexing recordings, one moment...")
            self.win.after(400, self.run)
            return
        t0 = time.perf_counter()
        hits = self.arch.search(q)
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        by_folder = {}
        for h in hits:
            by_folder.setdefault(h["path"], []).append(h)
        for n, (path, rows) in enumerate(by_folder.items()):
            rows.sort(key=lambda h: h["t"])  # recordings stay in best-match order, hits inside one in time order
            head = f"h{n}"
            name = Path(path).name[:16].replace("_", " ")
            t.insert("end", f"{rows[0]['title']}", ("title", head))
            t.insert("end", f"   {name}\n", ("muted", head))
            t.tag_bind(head, "<Button-1>", lambda e, p=path: ViewerWindow(self.app, p, highlight=q))
            for i, h in enumerate(rows[:8]):
                tag = f"r{n}_{i}"
                label = {"note": "notes", "title": "title", "mark": "★"}.get(h["kind"], fmt(h["t"]))
                t.insert("end", f"   {label}  ", ("ts", tag))
                if h["kind"] == "line":
                    t.insert("end", f"{h['who']}: ", ("who", tag))
                parts = h["snippet"].replace("⁆", "⁅").split("⁅")
                for j, part in enumerate(parts):
                    t.insert("end", part, (tag, "hit") if j % 2 else tag)
                t.insert("end", "\n", tag)
                at = h["t"] if h["t"] >= 0 else None
                t.tag_bind(tag, "<Button-1>", lambda e, p=path, a=at: ViewerWindow(self.app, p, at=a, highlight=q))
            if len(rows) > 8:
                t.insert("end", f"   + {len(rows) - 8} more in this recording\n", ("muted", head))
            t.insert("end", "\n")
        if not hits:
            t.insert("end", "Nothing found.", "muted")
        t.config(state="disabled")
        self.status.set(f"{len(hits)} hits in {len(by_folder)} recordings ({(time.perf_counter() - t0) * 1000:.0f} ms)")
