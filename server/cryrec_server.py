"""CryRec transcription server: run it on a machine with a GPU, point CryRec on other PCs at it.

    python cryrec_server.py --init            # writes a config with a random key, prints it
    python cryrec_server.py                   # serves on 127.0.0.1:8765

Put it behind HTTPS (Cloudflare Tunnel, Tailscale, a reverse proxy) before exposing it.
Config: %APPDATA%\\CryRec\\server.json (or --config PATH). Keys: key, host, port, model,
device (auto|cuda|cpu), models_dir, jobs_dir, keep_hours, log_file.

API (all need "Authorization: Bearer <key>"):
    GET    /health            {ok, version, model, device}
    POST   /jobs?language=&name=   body = zip with me.mp3 / them.mp3 (or meeting.mp3) -> {id}
    POST   /notes?backend=local|claude&language=&my_name=   body = transcript text -> {id}
    GET    /jobs/<id>         {status: queued|running|done|failed, progress, text, txt, srt | md, error}
    DELETE /jobs/<id>         cancel
Audio is deleted as soon as a job finishes; transcripts are kept keep_hours for the client to fetch.
"""

import argparse
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

VERSION = "0.4.0"
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
}
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
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.device = "idle"
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._janitor, daemon=True).start()

    def add(self, body, language, name):
        job_id = uuid.uuid4().hex
        safe = re.sub(r"[^\w\- ]", "_", name or "meeting")[:80] or "meeting"
        d = self.root / job_id / safe
        d.mkdir(parents=True)
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                names = [n for n in z.namelist() if TRACK_NAME.match(n)]
                if not names:
                    raise ValueError("zip has no me/them/meeting audio")
                for n in names:  # exact allow-listed names only, no paths
                    (d / n).write_bytes(z.read(n))
        except Exception:
            shutil.rmtree(d.parent, ignore_errors=True)
            raise
        job = {"id": job_id, "dir": d, "language": language or "Auto", "status": "queued",
               "progress": 0.0, "text": "waiting", "created": time.time(), "cancel": threading.Event()}
        with self.lock:
            self.jobs[job_id] = job
        self.q.put(job_id)
        log(f"job {job_id[:8]} queued ({safe}, {sum((d / n).stat().st_size for n in names) / 1e6:.1f} MB)")
        return job_id

    def add_notes(self, transcript, backend, language, my_name):
        if backend not in ("local", "claude"):
            raise ValueError("backend must be local or claude")
        if backend == "claude" and not self.cfg.get("anthropic_api_key"):
            raise ValueError("this server has no Anthropic API key")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "kind": "notes", "transcript": transcript, "backend": backend, "language": language,
               "my_name": my_name, "status": "queued", "progress": 0.0, "text": "waiting",
               "created": time.time(), "cancel": threading.Event()}
        with self.lock:
            self.jobs[job_id] = job
        self.q.put(job_id)
        log(f"notes {job_id[:8]} queued ({backend}, {len(transcript) / 1000:.0f}k chars)")
        return job_id

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def _run_notes(self, job):
        if job["backend"] == "local":
            eng = sm.OllamaNotes(self.cfg["ollama_url"], self.cfg["ollama_model"], int(self.cfg["ollama_ctx"]))
        else:
            eng = sm.ClaudeNotes(self.cfg["anthropic_api_key"], self.cfg["claude_model"])
        job["md"] = eng.notes(job.pop("transcript"), job["my_name"], job["language"],
                              lambda f, t: job.update(progress=f, text=t), job["cancel"].is_set)

    def _worker(self):
        engine = None
        while True:
            job_id = self.q.get()
            job = self.get(job_id)
            if not job or job["cancel"].is_set():
                continue
            t0 = time.time()
            if job.get("kind") == "notes":
                job["status"], job["text"] = "running", "writing notes"
                if job["backend"] == "local" and engine is not None:
                    engine = None  # free Whisper's GPU memory for the local LLM
                    self.device = "idle"
                    gc.collect()
                try:
                    self._run_notes(job)
                    job["status"], job["progress"] = "done", 1.0
                    log(f"notes {job_id[:8]} done in {time.time() - t0:.0f}s ({job['backend']})")
                except (tr.Cancelled, sm.Cancelled):
                    job["status"], job["error"] = "failed", "cancelled"
                except Exception as e:
                    job["status"], job["error"] = "failed", str(e)
                    log(f"notes {job_id[:8]} failed: {e!r}")
                job.pop("transcript", None)
                continue
            job["status"], job["text"] = "running", "loading speech model"
            try:
                if not tr.model_ready(self.cfg["models_dir"], self.cfg["model"]):
                    tr.download_model(self.cfg["models_dir"], self.cfg["model"],
                                      lambda f, t: job.update(text=t), job["cancel"].is_set)
                if engine is None:
                    engine = tr.LocalEngine(self.cfg["models_dir"], self.cfg["model"], self.cfg["device"])
                    self.device = engine.device
                    log(f"model {self.cfg['model']} loaded on {engine.device}")
                out = tr.transcribe_session(job["dir"], engine, job["language"],
                                            lambda f, t: job.update(progress=f, text=t), job["cancel"].is_set)
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
            if self.q.empty():
                engine = None  # give the GPU memory back to whatever else runs here
                self.device = "idle"
                gc.collect()

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
                else:
                    out.update(txt=job["txt"], srt=job["srt"])
            self._send(200, out)

        def do_POST(self):
            if not self._authed():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/notes":
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_NOTES_BODY:
                    return self._send(413, {"error": "body missing or too large"})
                text = self.rfile.read(n).decode("utf-8", errors="replace")
                try:
                    job_id = jobs.add_notes(text, q.get("backend", ["local"])[0], q.get("language", ["English"])[0] or None,
                                            q.get("my_name", [""])[0])
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
