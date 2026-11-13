"""CryRec transcription server: run it on a machine with a GPU, point CryRec on other PCs at it.

    python cryrec_server.py --init            # writes a config with a random key, prints it
    python cryrec_server.py                   # serves on 127.0.0.1:8765

Put it behind HTTPS (Cloudflare Tunnel, Tailscale, a reverse proxy) before exposing it.
Config: %APPDATA%\\CryRec\\server.json (or --config PATH). Keys: key, host, port, model,
device (auto|cuda|cpu), models_dir, jobs_dir, keep_hours, log_file, ollama_preempt (true = dictation stops
a request a local Ollama is still running, so both don't share the GPU; needs psutil).

API (all need "Authorization: Bearer <key>"):
    GET    /health            {ok, version, model, device}
    POST   /jobs?language=&name=&speakers=1   body = zip with me.mp3 / them.mp3 (or meeting.mp3 / .m4a) -> {id}
                               speakers=1: the other side / single track is split into Speaker 1..N
    POST   /voices            JSON {name, vec, seconds}: a client named a speaker (suggested next time)
    POST   /notes             JSON {transcript, backend: local|claude, language, my_name, vocabulary} -> {id}
    POST   /clean             JSON {transcript, vocabulary} -> {id}   (Claude fixes casing/punctuation/terms)
    POST   /dictate           JSON {audio_b64, ext, language, polish, model, vocabulary} -> {text, raw}
                              (answers on the same request; jumps the queue; keeps the model warm
                               keep_warm_minutes after use). A long clip that isn't done in 80 s
                               answers 202 {id} instead; poll GET /jobs/<id> for {result, raw, polished}
    POST   /warm              load the model now (sent when the dictation key goes down)
    POST   /share             zip with share.json {text, note, subject} + files/<name> -> {id}
                              saved to share_dir/<id>/ (off while share_dir is empty); a "ready" file
                              is written last, so a watcher on that folder never sees a half-written share
    GET    /jobs/<id>         {status: queued|running|done|failed, progress, text, error,
                               txt+srt (+speakers) (transcript) | md (notes) | txt+kept (clean)}
A zip may also carry vocab.txt; it is merged with the server's vocabulary_file.
    DELETE /jobs/<id>         cancel
Audio is deleted as soon as a job finishes; transcripts are kept keep_hours for the client to fetch.
"""

import argparse
import base64
import gc
import hmac
import io
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import diarize as dz  # noqa: E402
import summarize as sm  # noqa: E402
import transcribe as tr  # noqa: E402

VERSION = "0.13.0"
APPDATA = Path(os.environ.get("APPDATA", Path.home())) / "CryRec"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CryRec"
DEFAULTS = {
    "key": "",
    "host": "127.0.0.1",
    "port": 8765,
    "model": "turbo",
    "device": "auto",
    "models_dir": str(LOCALAPPDATA / "models"),
    "jobs_dir": str(LOCALAPPDATA / "server-jobs"),
    "keep_hours": 24,
    # meeting notes: "local" = Ollama on this machine, "claude" = Anthropic API (needs a key)
    "ollama_url": sm.DEFAULT_OLLAMA_URL,
    "ollama_model": sm.DEFAULT_OLLAMA_MODEL,
    "ollama_ctx": 8192,
    # dictation first: stop a request a local Ollama is still running on the GPU (needs psutil)
    "ollama_preempt": False,
    "anthropic_api_key": "",
    "claude_model": sm.DEFAULT_CLAUDE_MODEL,
    # terms and "wrong -> right" fixes used for every job, merged with the client's own list
    "vocabulary_file": str(APPDATA / "vocabulary.txt"),
    # phone "Share to my PC": folder that receives shared text/files; empty = off
    "share_dir": "",
    # speakers told apart on request (phone recordings): names learned from clients; empty = next to server.json
    "voices_file": "",
}
MAX_VOCAB = 64 * 1024
MAX_DICTATE_BODY = 12 * 1024 * 1024
MAX_NOTES_BODY = 5 * 1024 * 1024
MAX_BODY = 300 * 1024 * 1024
SHARE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRACK_NAME = re.compile(r"^(me|them|meeting)\.(mp3|flac|wav|ogg|opus|m4a)$")


LOG_FILE = None
_gpu_cache = {"t": 0.0, "info": None}


def gpu_info():
    """NVIDIA power limit / draw / P-state for /health (watchdogs use it); None without an NVIDIA GPU."""
    if time.time() - _gpu_cache["t"] < 30:
        return _gpu_cache["info"]
    info = None
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=enforced.power.limit,power.draw,pstate,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()[0]
        limit, draw, pstate, mem_used, mem_total = (x.strip() for x in out.split(","))
        info = {"power_limit_w": float(limit), "power_w": float(draw), "pstate": pstate,
                "mem_used_mb": int(mem_used), "mem_total_mb": int(mem_total)}
    except Exception:
        pass
    _gpu_cache.update(t=time.time(), info=info)
    return info


def _ollama_call(url, path, body=None):
    """GET (no body) or POST JSON to a local Ollama; returns the first JSON object of the reply ({} if empty)."""
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        lines = r.read().decode("utf-8", errors="replace").strip().splitlines()
    return json.loads(lines[0]) if lines else {}


def _stop_ollama_runner(url, name):
    """Kill the Ollama runner process(es) serving `name`, found by its weights file on their command line.
    Ollama starts a fresh runner on the next request for that model."""
    try:
        import psutil
    except ImportError:
        log("ollama_preempt needs psutil (pip install psutil)")
        return 0
    blobs = set(re.findall(r"sha256-[0-9a-f]{64}", _ollama_call(url, "/api/show", {"model": name}).get("modelfile", "")))
    n = 0
    for p in psutil.process_iter(["name"]) if blobs else ():
        if not re.search(r"llama|ollama", p.info["name"] or "", re.I):  # reading every command line takes seconds
            continue
        try:
            if any(b in " ".join(p.cmdline()) for b in blobs):
                p.kill()
                n += 1
        except psutil.Error:
            pass
    return n


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class Jobs:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg["jobs_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.q = queue.PriorityQueue()  # (priority, seq, job_id): dictation (0) goes before long jobs (1)
        self.seq = 0
        self.lock = threading.Lock()
        self.device = "idle"
        self.last_dictate = 0.0
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._janitor, daemon=True).start()

    def _put(self, job_id, priority):
        with self.lock:
            self.seq += 1
            seq = self.seq
        self.q.put((priority, seq, job_id))

    def add_dictate(self, audio, ext, language, polish, model, vocabulary):
        """Short clip -> text, optionally polished by Claude. The caller waits on job["done"]."""
        if polish and not self.cfg.get("anthropic_api_key"):
            polish = False
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "kind": "dictate", "audio": audio, "ext": ext, "language": language or "Auto",
               "polish": polish, "model": model, "vocab": (vocabulary or "")[:MAX_VOCAB], "status": "queued",
               "progress": 0.0, "text": "waiting", "created": time.time(), "cancel": threading.Event(),
               "done": threading.Event()}
        with self.lock:
            self.jobs[job_id] = job
        self._put(job_id, 0)
        return job

    def warm(self):
        """Load the model now (if it isn't) so the dictation that follows is quick."""
        self.last_dictate = time.time()
        if self.device != "idle":
            return
        job_id = uuid.uuid4().hex
        with self.lock:
            self.jobs[job_id] = {"id": job_id, "kind": "warm", "created": time.time(), "status": "done",
                                 "cancel": threading.Event()}
        self._put(job_id, 0)

    def _run_dictate(self, job, engine):
        tmp = self.root / f"{job['id']}.{job['ext']}"
        tmp.write_bytes(job.pop("audio"))
        try:
            audio = tr.load_audio(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        vocab = self.vocabulary(job.get("vocab"))
        terms, fixes = tr.parse_vocabulary(vocab)
        lang = tr.LANGUAGES.get(job["language"], job["language"])
        job["seconds"] = len(audio) / tr.SAMPLE_RATE
        raw = tr.apply_fixes(tr.collapse_repeats(engine.quick_text(audio, lang, terms)), fixes).strip()
        text = raw
        if job["polish"] and raw:
            model = job["model"] if str(job.get("model", "")).startswith("claude-") else self.cfg["claude_model"]
            text = sm.polish_dictation(raw, sm.ClaudeNotes(self.cfg["anthropic_api_key"], model), terms)
        job["raw"], job["result"] = raw, text

    def vocabulary(self, client_vocab=""):
        """Server list first, then the client's; parse_vocabulary drops duplicate terms."""
        try:
            server_vocab = Path(self.cfg["vocabulary_file"]).read_text(encoding="utf-8")
        except (OSError, KeyError):
            server_vocab = ""
        return server_vocab + "\n" + (client_vocab or "")

    def add(self, body, language, name, speakers=False):
        job_id = uuid.uuid4().hex
        safe = re.sub(r"[^\w\- ]", "_", name or "meeting")[:80] or "meeting"
        d = self.root / job_id / safe
        d.mkdir(parents=True)
        vocab = ""
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                names = [n for n in z.namelist() if TRACK_NAME.match(n)]
                if not names:
                    raise ValueError("zip has no me/them/meeting audio")
                for n in names:  # exact allow-listed names only, no paths
                    (d / n).write_bytes(z.read(n))
                if "vocab.txt" in z.namelist():
                    vocab = z.read("vocab.txt")[:MAX_VOCAB].decode("utf-8", errors="replace")
        except Exception:
            shutil.rmtree(d.parent, ignore_errors=True)
            raise
        job = {"id": job_id, "dir": d, "language": language or "Auto", "status": "queued", "vocab": vocab,
               "speakers": speakers,
               "progress": 0.0, "text": "waiting", "created": time.time(), "cancel": threading.Event()}
        with self.lock:
            self.jobs[job_id] = job
        self._put(job_id, 1)
        log(f"job {job_id[:8]} queued ({safe}, {sum((d / n).stat().st_size for n in names) / 1e6:.1f} MB)")
        return job_id

    def add_text_job(self, kind, transcript, vocabulary="", backend="claude", language=None, my_name=""):
        """kind "notes" (backend local|claude) or "clean" (always claude)."""
        if backend not in ("local", "claude") or (kind == "clean" and backend != "claude"):
            raise ValueError("backend must be local or claude (clean: claude only)")
        if backend == "claude" and not self.cfg.get("anthropic_api_key"):
            raise ValueError("this server has no Anthropic API key")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "kind": kind, "transcript": transcript, "backend": backend, "language": language,
               "my_name": my_name, "vocab": (vocabulary or "")[:MAX_VOCAB], "status": "queued", "progress": 0.0,
               "text": "waiting", "created": time.time(), "cancel": threading.Event()}
        with self.lock:
            self.jobs[job_id] = job
        self._put(job_id, 1)
        log(f"{kind} {job_id[:8]} queued ({backend}, {len(transcript) / 1000:.0f}k chars)")
        return job_id

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def _run_text_job(self, job):
        glossary, _ = tr.parse_vocabulary(self.vocabulary(job.get("vocab")))
        prog = lambda f, t: job.update(progress=f, text=t)  # noqa: E731
        claude = sm.ClaudeNotes(self.cfg["anthropic_api_key"], self.cfg["claude_model"])
        if job["kind"] == "clean":
            job["txt"], job["kept"] = sm.clean_transcript(
                job.pop("transcript"), lambda s, u: claude.ask(s, u, job["cancel"].is_set, effort="low")[0],
                glossary, prog, job["cancel"].is_set)
            return
        if job["backend"] == "local":
            eng = sm.OllamaNotes(self.cfg["ollama_url"], self.cfg["ollama_model"], int(self.cfg["ollama_ctx"]))
        else:
            eng = claude
        job["md"] = eng.notes(job.pop("transcript"), job["my_name"], job["language"], prog,
                              job["cancel"].is_set, glossary)

    def _free_ollama(self):
        """Dictation must not wait behind a local LLM holding the GPU: unload whatever a local Ollama has loaded.
        Ollama reloads a model by itself on its next request. An unload waits for a request that is already
        running, so with ollama_preempt on, a model still loaded after that has its runner process stopped
        (that request fails; its caller has to retry)."""
        url = (self.cfg.get("ollama_url") or "").rstrip("/")
        if urlparse(url).hostname not in ("127.0.0.1", "localhost"):
            return
        # "localhost" tries IPv6 first; Ollama listens on 127.0.0.1 only, so every call waited ~2 s on Windows
        url = url.replace("://localhost", "://127.0.0.1", 1)
        try:
            names = [m["name"] for m in _ollama_call(url, "/api/ps").get("models", [])]
            for name in names:
                _ollama_call(url, "/api/generate", {"model": name, "keep_alive": 0})
                log(f"unloaded ollama model {name} for dictation")
            if not names or not self.cfg.get("ollama_preempt"):
                return
            for _ in range(4):  # an idle model is gone within a moment; a busy one stays until its request ends
                time.sleep(0.25)
                busy = [m["name"] for m in _ollama_call(url, "/api/ps").get("models", []) if m["name"] in names]
                if not busy:
                    return
            for name in busy:
                n = _stop_ollama_runner(url, name)
                log(f"stopped a running ollama request on {name} for dictation ({n} process{'es' * (n != 1)})")
        except Exception as e:
            log(f"could not free ollama: {e!r}")

    def _load_engine(self, engine, job):
        if not tr.model_ready(self.cfg["models_dir"], self.cfg["model"]):
            tr.download_model(self.cfg["models_dir"], self.cfg["model"],
                              lambda f, t: job.update(text=t), job["cancel"].is_set)
        if engine is None:
            engine = tr.LocalEngine(self.cfg["models_dir"], self.cfg["model"], self.cfg["device"])
            self.device = engine.device
            log(f"model {self.cfg['model']} loaded on {engine.device}")
        elif engine.parked:
            t0 = time.time()
            engine.resume()
            self.device = engine.device
            log(f"model back on {engine.device} in {time.time() - t0:.1f}s")
        return engine

    def _worker(self):
        engine = None
        if tr.model_ready(self.cfg["models_dir"], self.cfg["model"]):
            try:  # load once at start; it is parked in RAM after keep_warm_minutes, so no dictation waits ~25 s
                engine = self._load_engine(None, {"cancel": threading.Event()})
                t0 = time.time()  # first decode / VAD / Claude import in a fresh process cost ~30 s: pay it now
                tmp = self.root / "warm.mp3"
                tmp.write_bytes(tr.encode_mp3(tr.np.zeros(tr.SAMPLE_RATE, tr.np.float32)))
                engine.quick_text(tr.load_audio(tmp), "en", ["CryRec"])
                tmp.unlink(missing_ok=True)
                import anthropic  # noqa: F401
                log(f"dictation path warmed in {time.time() - t0:.1f}s")
            except Exception as e:
                log(f"preload failed: {e!r}")
        while True:
            try:
                _, _, job_id = self.q.get(timeout=30)
            except queue.Empty:
                engine = self._maybe_unload(engine)
                continue
            job = self.get(job_id)
            if job and job.get("kind") == "warm":  # someone pressed the dictation key: load now
                self._free_ollama()
                try:
                    engine = self._load_engine(engine, job)
                except Exception as e:
                    log(f"warm-up failed: {e!r}")
                self.last_dictate = time.time()
                continue
            if job and job.get("kind") == "dictate":
                t0 = time.time()
                self._free_ollama()
                try:
                    engine = self._load_engine(engine, job)
                    self._run_dictate(job, engine)
                    job["status"] = "done"
                    log(f"dictate {job_id[:8]} done in {time.time() - t0:.1f}s ({job['seconds']:.0f}s audio, {len(job['result'])} chars"
                        f"{', polished' if job['polish'] else ''})")
                except Exception as e:
                    job["status"], job["error"] = "failed", str(e)
                    log(f"dictate {job_id[:8]} failed: {e!r}")
                job.pop("audio", None)
                self.last_dictate = time.time()
                job["done"].set()
                continue
            if not job or job["cancel"].is_set():
                continue
            t0 = time.time()
            if job.get("kind") in ("notes", "clean"):
                job["status"], job["text"] = "running", "working on the transcript"
                if job["backend"] == "local" and engine is not None:
                    engine.park()  # free Whisper's GPU memory for the local LLM (weights stay in RAM)
                    self.device = "idle"
                try:
                    self._run_text_job(job)
                    job["status"], job["progress"] = "done", 1.0
                    log(f"{job['kind']} {job_id[:8]} done in {time.time() - t0:.0f}s ({job['backend']})")
                except (tr.Cancelled, sm.Cancelled):
                    job["status"], job["error"] = "failed", "cancelled"
                except Exception as e:
                    job["status"], job["error"] = "failed", str(e)
                    log(f"{job['kind']} {job_id[:8]} failed: {e!r}")
                job.pop("transcript", None)
                continue
            job["status"], job["text"] = "running", "loading speech model"
            try:
                engine = self._load_engine(engine, job)
                out = tr.transcribe_session(job["dir"], engine, job["language"],
                                            lambda f, t: job.update(progress=f, text=t), job["cancel"].is_set,
                                            self.vocabulary(job.get("vocab")))
                if job.get("speakers"):
                    job["text"] = "telling speakers apart"
                    try:  # optional: a failure keeps the plain transcript
                        n = dz.run_session(job["dir"], Path(self.cfg["models_dir"]), self.cfg["voices_file"], tr,
                                           lambda f, t: job.update(progress=f, text=t), job["cancel"].is_set)
                        if n:
                            sp = json.loads((job["dir"] / "speakers.json").read_text(encoding="utf-8"))
                            job["speakers_json"] = sp
                    except dz.Cancelled:
                        raise tr.Cancelled()
                    except Exception as e:
                        log(f"job {job_id[:8]} speakers failed: {e!r}")
                job["txt"] = out.read_text(encoding="utf-8")
                job["srt"] = (job["dir"] / "transcript.srt").read_text(encoding="utf-8")
                job["status"], job["progress"] = "done", 1.0
                log(f"job {job_id[:8]} done in {time.time() - t0:.0f}s on {engine.device}")
            except tr.Cancelled:
                job["status"], job["error"] = "failed", "cancelled"
                log(f"job {job_id[:8]} cancelled")
            except Exception as e:
                job["status"], job["error"] = "failed", str(e)
                log(f"job {job_id[:8]} failed: {e!r}")
            shutil.rmtree(job["dir"].parent, ignore_errors=True)  # audio never stays on disk
            engine = self._maybe_unload(engine)

    def _maybe_unload(self, engine):
        """Give the GPU memory back when idle, but stay loaded a while after dictation (it needs to be quick)."""
        warm = float(self.cfg.get("keep_warm_minutes", 10)) * 60
        if engine is not None and self.q.empty() and time.time() - self.last_dictate > warm:
            if engine.device == "gpu":
                if not engine.parked:
                    engine.park()  # VRAM back, weights stay in RAM: the next dictation starts in ~0.5 s
                    self.device = "idle"
                    log("model parked in RAM")
                return engine
            self.device = "idle"
            gc.collect()
            return None
        return engine

    def _janitor(self):
        while True:
            time.sleep(600)
            cutoff = time.time() - self.cfg["keep_hours"] * 3600
            with self.lock:
                for k in [k for k, j in self.jobs.items() if j["created"] < cutoff and j["status"] in ("done", "failed")]:
                    del self.jobs[k]


def save_share(share_dir, body):
    """Unpack a phone share into share_dir/<id>/: share.json, files/..., then "ready". Returns the id."""
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        meta = json.loads(z.read("share.json").decode("utf-8")) if "share.json" in z.namelist() else {}
        if not isinstance(meta, dict):
            raise ValueError("share.json must be an object")
        sid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        out = Path(share_dir) / sid
        (out / "files").mkdir(parents=True)
        names = []
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith("files/"):
                continue
            name = SHARE_NAME.sub("_", info.filename.rsplit("/", 1)[-1]).strip(" .") or "file"
            stem, dot, ext = name.rpartition(".")
            n = 2
            while name in names:  # two shared files with the same name
                name = f"{stem or ext} ({n}){dot}{ext if stem else ''}"
                n += 1
            with z.open(info) as src, open(out / "files" / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            names.append(name)
    meta = {k: str(meta.get(k, "")) for k in ("text", "note", "subject")} | {
        "files": names, "received": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "share.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "ready").write_text("", encoding="utf-8")
    return sid


def make_handler(cfg, jobs):
    key = cfg["key"].encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"CryRecServer/{VERSION}"

        def log_message(self, fmt, *args):
            pass  # keep logs to job events; no request lines with client IPs

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip().encode()
            if key and hmac.compare_digest(got, key):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _job_id(self):
            parts = urlparse(self.path).path.strip("/").split("/")
            return parts[1] if len(parts) == 2 and parts[0] == "jobs" else None

        def do_GET(self):
            if not self._authed():
                return
            path = urlparse(self.path).path
            if path == "/health":
                return self._send(200, {"ok": True, "version": VERSION, "model": cfg["model"], "device": jobs.device, "gpu": gpu_info(),
                                        "notes": {"local": bool(cfg.get("ollama_url")),
                                                  "claude": bool(cfg.get("anthropic_api_key"))},
                                        "share": bool(cfg.get("share_dir"))})
            job = jobs.get(self._job_id() or "")
            if not job:
                return self._send(404, {"error": "not found"})
            out = {k: job.get(k) for k in ("id", "status", "progress", "text", "error")}
            if job["status"] == "done":
                if job.get("kind") == "notes":
                    out["md"] = job["md"]
                elif job.get("kind") == "clean":
                    out.update(txt=job["txt"], kept=job["kept"])
                elif job.get("kind") == "dictate":
                    out.update(result=job["result"], raw=job["raw"], polished=job["polish"])
                else:
                    out.update(txt=job["txt"], srt=job["srt"])
                    if job.get("speakers_json"):
                        out["speakers"] = job["speakers_json"]
            self._send(200, out)

        def do_POST(self):
            if not self._authed():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/warm":
                jobs.warm()
                return self._send(200, {"ok": True, "device": jobs.device})
            if u.path == "/dictate":
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_DICTATE_BODY:
                    return self._send(413, {"error": "body missing or too large"})
                try:
                    p = json.loads(self.rfile.read(n))
                    audio = base64.b64decode(p["audio_b64"])
                except (ValueError, KeyError):
                    return self._send(400, {"error": "bad request"})
                ext = p.get("ext") if p.get("ext") in ("mp3", "wav", "flac", "m4a") else "mp3"
                job = jobs.add_dictate(audio, ext, p.get("language"), bool(p.get("polish")), p.get("model"),
                                       str(p.get("vocabulary", "")))
                if not job["done"].wait(80):  # stay under proxy timeouts (Cloudflare: 100 s); the client polls
                    return self._send(202, {"id": job["id"], "status": job["status"]})
                if job["status"] != "done":
                    return self._send(500, {"error": job.get("error", "failed")})
                return self._send(200, {"text": job["result"], "raw": job["raw"], "polished": job["polish"]})
            if u.path in ("/notes", "/clean"):
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_NOTES_BODY:
                    return self._send(413, {"error": "body missing or too large"})
                raw = self.rfile.read(n).decode("utf-8", errors="replace")
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    try:
                        p = json.loads(raw)
                    except ValueError:
                        return self._send(400, {"error": "bad JSON"})
                else:  # 0.4 clients: transcript as plain text, options in the query
                    p = {"transcript": raw, **{k: v[0] for k, v in q.items()}}
                try:
                    job_id = jobs.add_text_job(
                        u.path.strip("/"), str(p.get("transcript", "")), str(p.get("vocabulary", "")),
                        p.get("backend", "claude" if u.path == "/clean" else "local"),
                        p.get("language") or None, str(p.get("my_name", "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"id": job_id})
            if u.path == "/share":
                if not cfg.get("share_dir"):
                    return self._send(403, {"error": "Sharing is off on this server (share_dir in server.json)."})
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_BODY:
                    return self._send(413, {"error": "body missing or too large"})
                try:
                    sid = save_share(cfg["share_dir"], self.rfile.read(n))
                except (ValueError, KeyError, zipfile.BadZipFile) as e:
                    return self._send(400, {"error": f"bad share: {e}"})
                log(f"share {sid} received ({n // 1024} KB)")
                return self._send(200, {"id": sid})
            if u.path == "/voices":  # a client named a speaker: remember the voice for suggestions
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 64 * 1024:
                    return self._send(413, {"error": "body missing or too large"})
                try:
                    p = json.loads(self.rfile.read(n))
                    name = dz.clean_name(str(p["name"]))
                    vec = [float(x) for x in p["vec"][:dz.DIM + 1]]
                    if not name or len(vec) != dz.DIM or not all(abs(x) <= 1.0 for x in vec):
                        raise ValueError("bad name or voice")
                    dz.remember(cfg["voices_file"], name, vec, max(0.0, min(float(p.get("seconds", 0)), 36000.0)))
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, {"error": f"bad request: {e}"})
                log(f"voice saved: {name}")
                return self._send(200, {"ok": True})
            if u.path != "/jobs":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            body = self.rfile.read(n)
            try:
                job_id = jobs.add(body, q.get("language", ["Auto"])[0], q.get("name", ["meeting"])[0],
                                  q.get("speakers", ["0"])[0] == "1")
            except (ValueError, zipfile.BadZipFile) as e:
                return self._send(400, {"error": str(e)})
            self._send(200, {"id": job_id})

        def do_DELETE(self):
            if not self._authed():
                return
            job = jobs.get(self._job_id() or "")
            if not job:
                return self._send(404, {"error": "not found"})
            job["cancel"].set()
            if job["status"] == "queued":
                job["status"], job["error"] = "failed", "cancelled"
            self._send(200, {"ok": True})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="CryRec transcription server")
    ap.add_argument("--config", default=str(APPDATA / "server.json"))
    ap.add_argument("--init", action="store_true", help="create the config with a new random key")
    args = ap.parse_args()
    path = Path(args.config)
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if args.init or not cfg["key"]:
        if not cfg["key"]:
            cfg["key"] = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"config: {path}\nkey:    {cfg['key']}")
        if args.init:
            return
    cfg["voices_file"] = cfg.get("voices_file") or str(path.parent / "voices.json")
    global LOG_FILE
    LOG_FILE = cfg.get("log_file") or None
    jobs = Jobs(cfg)
    srv = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), make_handler(cfg, jobs))
    log(f"CryRec server {VERSION} on http://{cfg['host']}:{cfg['port']} (model {cfg['model']}, device {cfg['device']})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
