"""Local meeting transcripts for CryRec (faster-whisper, CPU, no network after model download).

Each recording folder has me.flac (mic) and them.flac (speaker). Both are transcribed
separately, so every line gets a reliable Me / Them label. Mic lines that are only the
remote voice leaking from the speakers into the mic are dropped.
"""

import difflib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
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
        for ext in (".flac", ".wav", ".mp3"):
            p = session_dir / f"{stem}{ext}"
            if p.exists():
                found.append((label, p))
                break
    if not found and (session_dir / "meeting.mp3").exists():
        found.append(("Speaker", session_dir / "meeting.mp3"))
    return found


def _add_cuda_dll_dirs():
    """Make pip-installed CUDA libs (nvidia-cublas-cu12, nvidia-cudnn-cu12) findable on Windows."""
    import site
    try:
        dirs = list(site.getsitepackages()) + [site.getusersitepackages()]
    except AttributeError:  # frozen exe: no site-packages, rely on CUDA already being on PATH
        return
    roots = [Path(p) / "nvidia" for p in dirs]
    for root in roots:
        for bin_dir in root.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except Exception:
                pass
            if str(bin_dir) not in os.environ.get("PATH", ""):
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class LocalEngine:
    """faster-whisper on this machine. device="auto" tries the GPU first and falls back to CPU."""

    def __init__(self, models_root, name, device="auto"):
        from faster_whisper import WhisperModel
        path = str(model_dir(models_root, name))
        self.device = "cpu"
        self.model = None
        if device in ("auto", "cuda"):
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    _add_cuda_dll_dirs()
                    m = WhisperModel(path, device="cuda", compute_type="int8_float16")
                    list(m.transcribe(np.zeros(SAMPLE_RATE, np.float32), vad_filter=False)[0])  # fails here if cuBLAS/cuDNN are missing
                    self.model, self.device = m, "gpu"
            except Exception:
                self.model = None
            if self.model is None and device == "cuda":
                raise RuntimeError("GPU requested but CUDA (cuBLAS 12 / cuDNN 9) is not usable.")
        if self.model is None:
            threads = max(1, (os.cpu_count() or 4) // 2)
            self.model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)

    def segments(self, audio, lang, on_progress, cancel, terms=(), mic=False):
        segs, _info = self.model.transcribe(
            audio,
            language=None if lang in (None, "mixed") else lang,
            multilingual=lang == "mixed",
            beam_size=5,
            vad_filter=True,
            # the mic also picks up the remote voice faintly (speakers, bone-conduction headsets): be stricter there
            vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.6 if mic else 0.5},
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
            word_timestamps=True,  # needed by hallucination_silence_threshold and for splitting
            hotwords=hotword_prompt(terms),  # sent with every 30 s window, unlike initial_prompt
        )
        for s in segs:
            if cancel():
                raise Cancelled()
            on_progress(s.end)
            yield from split_on_pauses(s)


class ApiEngine:
    """Any OpenAI-compatible /audio/transcriptions endpoint (Groq, OpenAI, a local server...)."""

    CHUNK_SEC = 600  # keeps each upload far below the usual 25 MB limit

    def __init__(self, base_url, key, model):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.device = "api"

    def segments(self, audio, lang, on_progress, cancel, terms=(), mic=False):
        step = self.CHUNK_SEC * SAMPLE_RATE
        for off in range(0, len(audio), step):
            if cancel():
                raise Cancelled()
            chunk = audio[off:off + step]
            if float(np.abs(chunk).max(initial=0)) < 1e-3:
                continue
            fields = {"model": self.model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
            if lang in ("tr", "en"):
                fields["language"] = lang
            if terms:
                fields["prompt"] = hotword_prompt(terms)  # Whisper APIs take vocabulary as the prompt
            data = self._post(fields, encode_mp3(chunk, 64))  # 10 min at 64 kbps is ~5 MB
            t0 = off / SAMPLE_RATE
            segs = data.get("segments") or []
            if not segs and data.get("text"):
                segs = [{"start": 0, "end": len(chunk) / SAMPLE_RATE, "text": data["text"]}]
            for s in segs:
                yield t0 + s["start"], t0 + s["end"], s["text"]
            on_progress(t0 + len(chunk) / SAMPLE_RATE)

    def _post(self, fields, mp3):
        boundary = "----cryrec" + os.urandom(8).hex()
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
                 "Content-Type: audio/mpeg\r\n\r\n").encode() + mp3 + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{self.base_url}/audio/transcriptions", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "CryRec"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API error {e.code}: {e.read()[:300].decode(errors='replace')}") from None


PAUSE_SPLIT_SEC = 1.0
MAX_HOTWORD_CHARS = 600  # Whisper's prompt holds ~220 tokens; keep room for the audio context


def parse_vocabulary(text):
    """Vocabulary lines: "Term" (spelling hint) or "wrong -> right" (fixed after transcription).
    Lines starting with # are comments. Returns (terms, [(regex, right), ...])."""
    import re
    terms, fixes = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "->" in line:
            wrong, right = (p.strip() for p in line.split("->", 1))
            if wrong and right:
                fixes.append((re.compile(rf"(?<!\w){re.escape(wrong)}(?!\w)", re.IGNORECASE), right))
                if right not in terms:
                    terms.append(right)
        elif line not in terms:
            terms.append(line)
    return terms, fixes


def hotword_prompt(terms):
    """A short, properly cased and punctuated hint: steers spelling, and a little the casing too."""
    if not terms:
        return None
    out, size = [], 0
    for t in terms:
        if size + len(t) + 2 > MAX_HOTWORD_CHARS:
            break
        out.append(t)
        size += len(t) + 2
    return "Glossary: " + ", ".join(out) + "."


def split_on_pauses(seg):
    """Whisper can glue 40+ s into one segment; cut it where the words pause so turns interleave correctly."""
    words = getattr(seg, "words", None)
    if not words:
        yield seg.start, seg.end, seg.text
        return
    cur = [words[0]]
    for w in words[1:]:
        if w.start - cur[-1].end > PAUSE_SPLIT_SEC:
            yield cur[0].start, cur[-1].end, "".join(x.word for x in cur)
            cur = []
        cur.append(w)
    yield cur[0].start, cur[-1].end, "".join(x.word for x in cur)


def collapse_repeats(text):
    """ "No problem. No problem. No problem." -> "No problem."  Mhm x14 -> "Mhm." (Whisper loops on quiet audio)."""
    import re
    # the same 1-5 word phrase three or more times in a row
    pattern = re.compile(r"(\b(?:\w+[\s,.!?-]*){1,5}?)(?:\1){2,}", re.IGNORECASE)
    text = text.strip() + " "  # so the last repeat matches like the others
    prev = None
    while prev != text:
        prev = text
        text = pattern.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def apply_fixes(text, fixes):
    for rx, right in fixes:
        text = rx.sub(right, text)
    return text


def encode_mp3(audio, kbps=32):
    """16 kHz mono float32 -> small MP3 bytes (plenty for speech recognition)."""
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(SAMPLE_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    return bytes(enc.encode(pcm) + enc.flush())


def transcribe_session(session_dir, engine, language, progress, cancel, vocabulary=""):
    """Write transcript.txt and transcript.srt into session_dir. Returns the txt path.
    vocabulary: text in the parse_vocabulary format (spelling hints and wrong -> right fixes)."""
    terms, fixes = parse_vocabulary(vocabulary)
    session_dir = Path(session_dir)
    tracks = find_tracks(session_dir)
    if not tracks:
        raise FileNotFoundError("No me/them tracks or meeting.mp3 in this folder.")
    lang = LANGUAGES.get(language, language)
    segments = []
    total_dur = 0.0
    where = {"gpu": " on GPU", "cpu": "", "api": " via API"}.get(getattr(engine, "device", ""), "")
    for ti, (label, path) in enumerate(tracks):
        audio = load_audio(path)
        dur = len(audio) / SAMPLE_RATE
        total_dur = max(total_dur, dur)
        if dur < 0.5 or float(np.abs(audio).max(initial=0)) < 1e-3:
            continue  # empty or silent track

        def on_progress(t, ti=ti, label=label, dur=dur):
            progress((ti + min(1.0, t / dur)) / len(tracks), f"Transcribing {label}{where} {fmt_ts(t)} / {fmt_ts(dur)}")
        for start, end, text in engine.segments(audio, lang, on_progress, cancel, terms=terms, mic=label == "Me"):
            text = apply_fixes(collapse_repeats(text.strip()), fixes)
            if text:
                segments.append({"start": start, "end": end, "who": label, "text": text})
    segments = drop_bleed(segments)
    segments.sort(key=lambda s: (s["start"], s["who"]))
    txt = session_dir / "transcript.txt"
    txt.write_text(render_txt(session_dir, segments, total_dur, [l for l, _ in tracks]), encoding="utf-8")
    (session_dir / "transcript.srt").write_text(render_srt(segments), encoding="utf-8")
    return txt


MAX_UPLOAD = 95 * 1024 * 1024  # Cloudflare and most proxies cap a request at 100 MB


def remote_transcribe(session_dir, url, key, language, progress, cancel, vocabulary=""):
    """Send the tracks to a CryRec server (see server/) and save the transcript it returns.
    The server merges `vocabulary` with its own list."""
    session_dir = Path(session_dir)
    tracks = find_tracks(session_dir)
    if not tracks:
        raise FileNotFoundError("No me/them tracks or meeting.mp3 in this folder.")
    url = url.rstrip("/")
    progress(0.0, "Preparing audio for the server...")
    audios = [(label, load_audio(path)) for label, path in tracks]
    total_sec = sum(len(a) for _, a in audios) / SAMPLE_RATE or 1
    # best quality that still fits one upload: 64 kbps, lower only for very long meetings
    kbps = max(16, min(64, int(MAX_UPLOAD * 0.95 * 8 / total_sec / 1000) // 8 * 8))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for label, audio in audios:
            stem = {"Me": "me", "Them": "them"}.get(label, "meeting")
            z.writestr(f"{stem}.mp3", encode_mp3(audio, kbps))
            if cancel():
                raise Cancelled()
        if vocabulary.strip():
            z.writestr("vocab.txt", vocabulary)
    del audios
    body = buf.getvalue()
    if len(body) > MAX_UPLOAD:
        raise RuntimeError(f"Recording is too long to upload ({len(body) / 1e6:.0f} MB > 95 MB).")
    q = urllib.parse.urlencode({"language": language, "name": session_dir.name})
    job = _server_call(f"{url}/jobs?{q}", key, data=body, content_type="application/zip")
    job_id = job["id"]
    try:
        while True:
            if cancel():
                raise Cancelled()
            st = _server_call(f"{url}/jobs/{job_id}", key)
            if st["status"] == "done":
                (session_dir / "transcript.txt").write_text(st["txt"], encoding="utf-8")
                (session_dir / "transcript.srt").write_text(st["srt"], encoding="utf-8")
                return session_dir / "transcript.txt"
            if st["status"] == "failed":
                raise RuntimeError(f"Server: {st.get('error')}")
            progress(st.get("progress", 0.0), "Server: " + (st.get("text") or st["status"]))
            time.sleep(2)
    except Cancelled:
        try:
            _server_call(f"{url}/jobs/{job_id}", key, method="DELETE")
        except Exception:
            pass
        raise


def server_health(url, key):
    return _server_call(url.rstrip("/") + "/health", key)


def _server_call(url, key, data=None, content_type=None, method=None):
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "CryRec"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read()[:300].decode(errors="replace")
        raise RuntimeError({401: "Server rejected the key.", 404: "Job not found on server."}.get(e.code, f"Server error {e.code}: {msg}")) from None


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
