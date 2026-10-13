"""Meeting notes (summary, decisions, action items) from a CryRec transcript.

Backends:
    ClaudeNotes   Anthropic API, one request (1M context)
    OpenAINotes   any OpenAI-compatible /chat/completions API
    OllamaNotes   a local Ollama model (native API, so the context size can be set)
Small-context backends split long transcripts into chunks, take notes per chunk, then merge.
"""

import json
import urllib.error
import urllib.request

NOTES_LANGUAGES = {"English": "English", "Turkish": "Turkish", "Same as meeting": None}
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "gemma3:4b"


class Cancelled(Exception):
    pass


def _system(my_name, language):
    me = (f'"Me" is {my_name}, who recorded the meeting. In the notes, write "{my_name}" instead of "Me".'
          if my_name else '"Me" is the person who recorded the meeting.')
    lang = f"Write the notes in {language}." if language else "Write the notes in the main language of the meeting."
    return f"""You turn meeting transcripts into short, accurate meeting notes.

The transcript has lines like "[hh:mm:ss] Me: ..." and "[hh:mm:ss] Them: ...". {me}
"Them" is everyone on the other side of the call; it can be several people. Use real names only when the transcript says them; otherwise write "Them".
The transcript comes from speech recognition, so expect some misheard words. Do not invent facts, numbers, owners or dates that are not in the transcript.
{lang} Use short sentences and everyday words.

Answer with Markdown only, using exactly these sections:

## Summary
3 to 6 bullet points: what the meeting was about and what came out of it.

## Decisions
One bullet per decision. "None." if there were none.

## Action items
One checkbox line per task someone agreed to do, including small ones (for example "tell the customer").
The owner is the person who said they would do it. Lines start with "- [ ] " and nothing else, for example:
- [ ] Alex: send the test results to the team
- [ ] Sam: book the meeting room
If a day or date was said for a task, add it at the end as "(due: <what was said>)"; otherwise add nothing. "None." if there were none.

## Open questions
Things raised but not settled. "None." if there were none."""


def _user(transcript):
    return f"Transcript:\n\n<transcript>\n{transcript.strip()}\n</transcript>"


MERGE_PROMPT = ("Below are meeting notes for consecutive parts of one meeting. Merge them into one set of notes "
                "for the whole meeting with the same four sections. Remove duplicates, keep every distinct "
                "decision and action item.\n\n")


def chunk_transcript(text, max_chars):
    """Split on line boundaries so no transcript line is cut."""
    chunks, cur = [], []
    size = 0
    for line in text.splitlines():
        if cur and size + len(line) > max_chars:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


class _ChunkedNotes:
    """Shared map-reduce for backends with a small context window."""
    max_chars = 24000

    def _ask(self, system, user, cancel):
        raise NotImplementedError

    def notes(self, transcript, my_name, language, progress, cancel):
        system = _system(my_name, language)
        chunks = chunk_transcript(transcript, self.max_chars)
        if len(chunks) == 1:
            progress(0.1, "Writing notes...")
            return self._ask(system, _user(transcript), cancel)
        parts = []
        for i, c in enumerate(chunks):
            if cancel():
                raise Cancelled()
            progress(i / (len(chunks) + 1), f"Writing notes, part {i + 1} of {len(chunks)}...")
            parts.append(self._ask(system, _user(c), cancel))
        progress(len(chunks) / (len(chunks) + 1), "Merging notes...")
        joined = "\n\n".join(f"### Part {i + 1}\n{p}" for i, p in enumerate(parts))
        return self._ask(system, MERGE_PROMPT + joined, cancel)


class ClaudeNotes:
    def __init__(self, key, model=DEFAULT_CLAUDE_MODEL):
        self.key = key
        self.model = model or DEFAULT_CLAUDE_MODEL

    def notes(self, transcript, my_name, language, progress, cancel):
        import anthropic
        client = anthropic.Anthropic(api_key=self.key, max_retries=3)
        req = dict(model=self.model, max_tokens=16000, system=_system(my_name, language),
                   messages=[{"role": "user", "content": _user(transcript)}],
                   output_config={"effort": "medium"})
        progress(0.1, f"Writing notes with {self.model}...")
        try:
            if self.model.startswith("claude-haiku"):  # Haiku has no server-side fallback
                stream = client.messages.stream(**req)
            else:  # on a safety false positive, the API re-runs the request on its recommended fallback model
                stream = client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)
            with stream as s:
                for _ in s:
                    if cancel():
                        raise Cancelled()
                msg = s.get_final_message()
        except anthropic.AuthenticationError:
            raise RuntimeError("Claude rejected the API key.") from None
        except anthropic.NotFoundError:
            raise RuntimeError(f"Claude model '{self.model}' not found.") from None
        except anthropic.RateLimitError:
            raise RuntimeError("Claude rate limit or credit limit reached. Try again later.") from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("Could not reach the Claude API.") from None
        if msg.stop_reason == "refusal":
            raise RuntimeError("Claude declined to summarize this transcript.")
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason == "max_tokens":
            text += "\n\n(Notes were cut off at the length limit.)"
        return text


class OpenAINotes(_ChunkedNotes):
    def __init__(self, base_url, key, model, max_chars=24000):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.max_chars = max_chars

    def _ask(self, system, user, cancel):
        body = json.dumps({"model": self.model, "temperature": 0.2,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "CryRec"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API error {e.code}: {e.read()[:300].decode(errors='replace')}") from None


class OllamaNotes(_ChunkedNotes):
    """Native Ollama API: lets us set num_ctx (the OpenAI-compatible route can't, and silently truncates)."""

    def __init__(self, url=DEFAULT_OLLAMA_URL, model=DEFAULT_OLLAMA_MODEL, num_ctx=8192):
        self.url = (url or DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.num_ctx = num_ctx
        # leave room for the system prompt and the answer; ~3.5 chars per token for TR/EN text
        self.max_chars = int((num_ctx - 2500) * 3.5)

    def _ask(self, system, user, cancel):
        body = json.dumps({"model": self.model, "stream": False,
                           "options": {"num_ctx": self.num_ctx, "temperature": 0.2},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(f"{self.url}/api/chat", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.load(r)["message"]["content"].strip()
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach Ollama at {self.url}: {e.reason}") from None


def remote_notes(url, key, transcript, backend, language, my_name, progress, cancel):
    """Ask a CryRec server for notes. backend: "local" (its Ollama) or "claude" (its Anthropic key)."""
    import time
    import urllib.parse
    from transcribe import _server_call
    url = url.rstrip("/")
    q = urllib.parse.urlencode({"backend": backend, "language": language or "", "my_name": my_name or ""})
    job_id = _server_call(f"{url}/notes?{q}", key, data=transcript.encode("utf-8"),
                          content_type="text/plain; charset=utf-8")["id"]
    while True:
        if cancel():
            try:
                _server_call(f"{url}/jobs/{job_id}", key, method="DELETE")
            except Exception:
                pass
            raise Cancelled()
        st = _server_call(f"{url}/jobs/{job_id}", key)
        if st["status"] == "done":
            return st["md"]
        if st["status"] == "failed":
            raise RuntimeError(f"Server: {st.get('error')}")
        progress(st.get("progress", 0.0), "Server: " + (st.get("text") or st["status"]))
        time.sleep(2)


def write_notes(session_dir, notes_md):
    from pathlib import Path
    session_dir = Path(session_dir)
    out = session_dir / "notes.md"
    out.write_text(f"# Meeting notes: {session_dir.name.replace('_', ' ')}\n\n{notes_md.strip()}\n", encoding="utf-8")
    return out
