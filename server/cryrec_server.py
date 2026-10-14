"""CryRec transcription server: run it on a machine with a GPU, point CryRec on other PCs at it.

    python cryrec_server.py --init            # writes a config with a random key, prints it
    python cryrec_server.py                   # serves on 127.0.0.1:8765

Put it behind HTTPS (Cloudflare Tunnel, Tailscale, a reverse proxy) before exposing it.
Config: %APPDATA%\\CryRec\\server.json (or --config PATH). Keys: key, host, port, model,
device (auto|cuda|cpu), models_dir, jobs_dir, keep_hours, log_file.

API (all need "Authorization: Bearer <key>"):
    GET    /health            {ok, version, model, device}
    POST   /jobs?language=&name=   body = zip with me.mp3 / them.mp3 (or meeting.mp3) -> {id}
    POST   /notes             JSON {transcript, backend: local|claude, language, my_name, vocabulary} -> {id}
    POST   /clean             JSON {transcript, vocabulary} -> {id}   (Claude fixes casing/punctuation/terms)
    POST   /dictate           JSON {audio_b64, ext, language, polish, model, vocabulary} -> {text, raw}
                              (answers on the same request; jumps the queue; keeps the model warm
                               keep_warm_minutes after use)
    POST   /warm              load the model now (sent when the dictation key goes down)
    GET    /jobs/<id>         {status: queued|running|done|failed, progress, text, error,
                               txt+srt (transcript) | md (notes) | txt+kept (clean)}
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
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import summarize as sm  # noqa: E402
import transcribe as tr  # noqa: E402

VERSION = "0.7.0"
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
    "anthropic_api_key": "",
    "claude_model": sm.DEFAULT_CLAUDE_MODEL,
    # terms and "wrong -> right" fixes used for every job, merged with the client's own list
    "vocabulary_file": str(APPDATA / "vocabulary.txt"),
}
MAX_VOCAB = 64 * 1024
MAX_DICTATE_BODY = 12 * 1024 * 1024
MAX_NOTES_BODY = 5 * 1024 * 1024
MAX_BODY = 300 * 1024 * 1024
TRACK_NAME = re.compile(r"^(me|them|meeting)\.(mp3|flac|wav|ogg|opus|m4a)$")


LOG_FILE = None


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

    def add(self, body, language, name):
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

    def _load_engine(self, engine, job):
        if not tr.model_ready(self.cfg["models_dir"], self.cfg["model"]):
            tr.download_model(self.cfg["models_dir"], self.cfg["model"],
                              lambda f, t: job.update(text=t), job["cancel"].is_set)
        if engine is None:
            engine = tr.LocalEngine(self.cfg["models_dir"], self.cfg["model"], self.cfg["device"])
            self.device = engine.device
            log(f"model {self.cfg['model']} loaded on {engine.device}")
        return engine

    def _worker(self):
        engine = None
        while True:
            try:
                _, _, job_id = self.q.get(timeout=30)
            except queue.Empty:
                engine = self._maybe_unload(engine)
                continue
            job = self.get(job_id)
            if job and job.get("kind") == "warm":  # someone pressed the dictation key: load now
                try:
                    engine = self._load_engine(engine, job)
                except Exception as e:
                    log(f"warm-up failed: {e!r}")
                self.last_dictate = time.time()
                continue
            if job and job.get("kind") == "dictate":
                t0 = time.time()
                try:
                    engine = self._load_engine(engine, job)
                    self._run_dictate(job, engine)
                    job["status"] = "done"
                    log(f"dictate {job_id[:8]} done in {time.time() - t0:.1f}s ({len(job['result'])} chars"
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
                    engine = None  # free Whisper's GPU memory for the local LLM
                    self.device = "idle"
                    gc.collect()
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
                return self._send(200, {"ok": True, "version": VERSION, "model": cfg["model"], "device": jobs.device,
                                        "notes": {"local": bool(cfg.get("ollama_url")),
                                                  "claude": bool(cfg.get("anthropic_api_key"))}})
            job = jobs.get(self._job_id() or "")
            if not job:
                return self._send(404, {"error": "not found"})
            out = {k: job.get(k) for k in ("id", "status", "progress", "text", "error")}
            if job["status"] == "done":
                if job.get("kind") == "notes":
                    out["md"] = job["md"]
                elif job.get("kind") == "clean":
                    out.update(txt=job["txt"], kept=job["kept"])
                else:
                    out.update(txt=job["txt"], srt=job["srt"])
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
                ext = p.get("ext") if p.get("ext") in ("mp3", "wav", "flac") else "mp3"
                job = jobs.add_dictate(audio, ext, p.get("language"), bool(p.get("polish")), p.get("model"),
                                       str(p.get("vocabulary", "")))
                if not job["done"].wait(90):  # stay under proxy timeouts (Cloudflare: 100 s)
                    job["cancel"].set()
                    return self._send(504, {"error": "timed out"})
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
            if u.path != "/jobs":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            body = self.rfile.read(n)
            try:
                job_id = jobs.add(body, q.get("language", ["Auto"])[0], q.get("name", ["meeting"])[0])
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
    global LOG_FILE
    LOG_FILE = cfg.get("log_file") or None
    jobs = Jobs(cfg)
    srv = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), make_handler(cfg, jobs))
    log(f"CryRec server {VERSION} on http://{cfg['host']}:{cfg['port']} (model {cfg['model']}, device {cfg['device']})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
