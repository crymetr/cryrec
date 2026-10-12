"""Local meeting transcripts for CryRec (faster-whisper, CPU, no network after model download).

Each recording folder has me.flac (mic) and them.flac (speaker). Both are transcribed
separately, so every line gets a reliable Me / Them label. Mic lines that are only the
remote voice leaking from the speakers into the mic are dropped.
"""

import difflib
import json
import os
import urllib.request
from datetime import timedelta
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

MODELS = {
    "turbo": {"repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "label": "Turbo (best, 1.5 GB)", "size_gb": 1.5},
    "small": {"repo": "Systran/faster-whisper-small", "label": "Small (faster, 0.5 GB)", "size_gb": 0.5},
}
LANGUAGES = {
    "Auto": None,
    "Mixed TR/EN": "mixed",
    "Turkish": "tr",
    "English": "en",
}
TRACKS = (("me", "Me"), ("them", "Them"))
SAMPLE_RATE = 16000
MERGE_GAP_SEC = 1.5
BLEED_RATIO = 0.55


class Cancelled(Exception):
    pass


# ---------- model download ----------

def model_dir(models_root, name):
    return Path(models_root) / name


def model_ready(models_root, name):
    return (model_dir(models_root, name) / ".complete").exists()


def download_model(models_root, name, progress, cancel):
    """Download a model from Hugging Face with resume. progress(frac, text)."""
    repo = MODELS[name]["repo"]
    dest = model_dir(models_root, name)
    dest.mkdir(parents=True, exist_ok=True)
    ua = {"User-Agent": "CryRec"}
    with urllib.request.urlopen(urllib.request.Request(f"https://huggingface.co/api/models/{repo}/tree/main", headers=ua), timeout=30) as r:
        files = [f for f in json.load(r) if f["type"] == "file" and f["path"] not in (".gitattributes", "README.md")]
    total = sum(f["size"] for f in files) or 1
    done = 0
    for f in files:
        target = dest / f["path"]
        if target.exists() and target.stat().st_size == f["size"]:
            done += f["size"]
            continue
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = dict(ua)
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(f"https://huggingface.co/{repo}/resolve/main/{f['path']}", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            if have and resp.status != 206:
                have = 0  # server ignored the range, start over
            with open(part, "ab" if have else "wb") as out:
                while chunk := resp.read(1 << 20):
                    if cancel():
                        raise Cancelled()
                    out.write(chunk)
                    have += len(chunk)
                    progress((done + have) / total, f"Downloading model {(done + have) / 1e9:.2f} / {total / 1e9:.2f} GB")
        os.replace(part, target)
        done += f["size"]
    (dest / ".complete").write_text("ok")
    return dest


# ---------- transcription ----------

def load_audio(path):
    """Any FLAC/WAV/MP3 -> 16 kHz mono float32."""
    if path.suffix.lower() in (".flac", ".wav"):
        x, rate = sf.read(str(path), dtype="float32", always_2d=True)
        x = x.mean(axis=1)
        return soxr.resample(x, rate, SAMPLE_RATE).astype(np.float32) if rate != SAMPLE_RATE else x
    from faster_whisper.audio import decode_audio
    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def find_tracks(session_dir):
    """[(label, path)] for this recording. Falls back to meeting.mp3 as one track."""
    found = []
    for stem, label in TRACKS:
        for ext in (".flac", ".wav"):
            p = session_dir / f"{stem}{ext}"
            if p.exists():
                found.append((label, p))
                break
    if not found and (session_dir / "meeting.mp3").exists():
        found.append(("Speaker", session_dir / "meeting.mp3"))
    return found


def load_model(models_root, name):
    from faster_whisper import WhisperModel
    threads = max(1, (os.cpu_count() or 4) // 2)
    return WhisperModel(str(model_dir(models_root, name)), device="cpu", compute_type="int8", cpu_threads=threads)


def transcribe_session(session_dir, model, language, progress, cancel):
    """Write transcript.txt and transcript.srt into session_dir. Returns the txt path."""
    session_dir = Path(session_dir)
    tracks = find_tracks(session_dir)
    if not tracks:
        raise FileNotFoundError("No me/them tracks or meeting.mp3 in this folder.")
    lang = LANGUAGES.get(language, language)
    segments = []
    total_dur = 0.0
    for ti, (label, path) in enumerate(tracks):
        audio = load_audio(path)
        dur = len(audio) / SAMPLE_RATE
        total_dur = max(total_dur, dur)
        if dur < 0.5 or float(np.abs(audio).max(initial=0)) < 1e-3:
            continue  # empty or silent track
        segs, _info = model.transcribe(
            audio,
            language=None if lang in (None, "mixed") else lang,
            multilingual=lang == "mixed",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
            word_timestamps=True,  # needed by hallucination_silence_threshold
        )
        for s in segs:
            if cancel():
                raise Cancelled()
            text = s.text.strip()
            if text:
                segments.append({"start": s.start, "end": s.end, "who": label, "text": text})
            progress((ti + min(1.0, s.end / dur)) / len(tracks), f"Transcribing {label} {fmt_ts(s.end)} / {fmt_ts(dur)}")
    segments = drop_bleed(segments)
    segments.sort(key=lambda s: (s["start"], s["who"]))
    txt = session_dir / "transcript.txt"
    txt.write_text(render_txt(session_dir, segments, total_dur, [l for l, _ in tracks]), encoding="utf-8")
    (session_dir / "transcript.srt").write_text(render_srt(segments), encoding="utf-8")
    return txt


def drop_bleed(segments):
    """Remove Me lines that repeat what Them said at the same time (speaker leaking into mic)."""
    them = [s for s in segments if s["who"] == "Them"]
    if not them:
        return segments
    keep = []
    for s in segments:
        if s["who"] == "Me":
            near = " ".join(t["text"] for t in them if t["start"] < s["end"] + 1 and t["end"] > s["start"] - 1)
            if near and difflib.SequenceMatcher(None, s["text"].lower(), near.lower()).ratio() >= BLEED_RATIO:
                continue
            if near and s["text"].lower() in near.lower():
                continue
        keep.append(s)
    return keep


def fmt_ts(sec, srt=False):
    td = timedelta(seconds=max(0.0, sec))
    h, rem = divmod(int(td.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{int(td.microseconds / 1000):03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_txt(session_dir, segments, duration, labels):
    lines = [
        f"Meeting {session_dir.name.replace('_', ' ')}  (length {fmt_ts(duration)})",
        "Speakers: " + ", ".join({"Me": "Me (mic)", "Them": "Them (speaker)"}.get(l, l) for l in labels),
        "",
    ]
    block = None
    for s in segments:
        if block and block["who"] == s["who"] and s["start"] - block["end"] <= MERGE_GAP_SEC:
            block["text"] += " " + s["text"]
            block["end"] = s["end"]
            continue
        if block:
            lines.append(f"[{fmt_ts(block['start'])}] {block['who']}: {block['text']}")
        block = dict(s)
    if block:
        lines.append(f"[{fmt_ts(block['start'])}] {block['who']}: {block['text']}")
    if not segments:
        lines.append("(no speech found)")
    return "\n".join(lines) + "\n"


def render_srt(segments):
    out = []
    for i, s in enumerate(segments, 1):
        out.append(f"{i}\n{fmt_ts(s['start'], True)} --> {fmt_ts(s['end'], True)}\n{s['who']}: {s['text']}\n")
    return "\n".join(out)
