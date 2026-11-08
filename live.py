"""Transcript while recording: every few minutes the new audio is cut at a quiet moment into
live/partNNN/ (me.flac + them.flac, 16 kHz) and transcribed in the background. After Stop only
the last few minutes are left, so the transcript is ready seconds after the meeting instead of
minutes. transcribe.merge_parts() joins the parts; a part that failed is redone after Stop."""

import json
import threading
import time

import numpy as np
import soundfile as sf
import soxr

import transcribe as tr

PART_SEC = 300     # cut a part every 5 minutes
SEARCH_SEC = 20    # ...at the quietest moment in its last 20 s, so no word is cut in half
QUIET_WIN = 0.5    # length of the quiet window we look for
SR = tr.SAMPLE_RATE


class LiveTranscript:
    """tracks: [(stem, Track)]; transcribe_part(part_dir) does one part (raises on failure,
    tr.Cancelled stops live work for this recording: the rest is done after Stop)."""

    def __init__(self, session_dir, tracks, transcribe_part):
        self.dir = session_dir / "live"
        self.dir.mkdir(exist_ok=True)
        self.transcribe_part = transcribe_part
        self.tracks = tracks
        self.resamplers = {}
        self.pending = {stem: np.zeros(0, np.float32) for stem, _ in tracks}
        self.parts = []          # {"name", "start", "end"}
        self.todo = []           # part names not transcribed yet
        self.offset = 0.0        # seconds already cut into parts
        self.active = True       # False once live transcription gave up (CPU only, cancelled...)
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.done = threading.Event()
        for _, t in tracks:
            t.tap = []
        threading.Thread(target=self._loop, daemon=True).start()

    def finish(self):
        """Call after the tracks have stopped: cuts the last part and finishes the rest."""
        self.stopping.set()

    def cancel(self):
        self.cancelled.set()
        self.stopping.set()

    # ---------- worker ----------
    def _loop(self):
        try:
            while not self.stopping.is_set():
                self._drain()
                if min(len(p) for p in self.pending.values()) >= (PART_SEC + 1) * SR:
                    self._cut(self._quiet_point())
                elif self.todo and self.active:
                    self._run(self.todo[0])
                else:
                    self.stopping.wait(2)
            self._drain(final=True)
            while min(len(p) for p in self.pending.values()) >= (PART_SEC + 1) * SR:
                self._cut(self._quiet_point())  # a backlog (slow GPU): keep the parts small
            longest = max(len(p) for p in self.pending.values())
            if longest > 0:
                self._cut(longest)
            while self.todo and self.active and not self.cancelled.is_set():
                self._run(self.todo[0])
        except Exception:
            self.active = False  # whatever is left is transcribed after Stop
        finally:
            self._save(complete=True)
            self.done.set()

    def _drain(self, final=False):
        for stem, t in self.tracks:
            with t.tap_lock:
                chunks, t.tap = t.tap, ([] if not final else None)
            if not chunks:
                continue
            x = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
            x = x.reshape(-1, t.channels).mean(axis=1)
            if t.rate != SR:
                rs = self.resamplers.get(stem)
                if rs is None:
                    rs = self.resamplers[stem] = soxr.ResampleStream(t.rate, SR, 1, dtype="float32")
                x = rs.resample_chunk(x, last=final)
            self.pending[stem] = np.concatenate([self.pending[stem], x.astype(np.float32)])

    def _quiet_point(self):
        """Sample index near PART_SEC where both sides are quietest."""
        lo, hi, win = (PART_SEC - SEARCH_SEC) * SR, PART_SEC * SR, int(QUIET_WIN * SR)
        energy = np.zeros(hi - lo, np.float32)
        for p in self.pending.values():
            energy += np.abs(p[lo:hi])
        rolling = np.convolve(energy, np.ones(win, np.float32), mode="valid")
        return lo + int(np.argmin(rolling)) + win // 2

    def _cut(self, n):
        name = f"part{len(self.parts) + 1:03d}"
        d = self.dir / name
        d.mkdir(exist_ok=True)
        for stem, p in self.pending.items():
            chunk = p[:n]
            if len(chunk) < n:  # a track that stopped a little early
                chunk = np.concatenate([chunk, np.zeros(n - len(chunk), np.float32)])
            sf.write(str(d / f"{stem}.flac"), chunk, SR, subtype="PCM_16")
            self.pending[stem] = p[n:]
        self.parts.append({"name": name, "start": round(self.offset, 3), "end": round(self.offset + n / SR, 3)})
        self.offset += n / SR
        self.todo.append(name)
        self._save()

    def _run(self, name):
        try:
            self.transcribe_part(self.dir / name)
            self.todo.remove(name)
        except tr.Cancelled:
            self.active = False
        except Exception:
            self.todo.remove(name)  # leave it for the run after Stop; keep going with the next part
        self._save()

    def _save(self, complete=False):
        data = {"parts": self.parts, "complete": complete, "updated": time.time()}
        tmp = self.dir / "parts.json.tmp"
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "parts.json")
