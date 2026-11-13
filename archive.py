"""Search across every recording: an SQLite FTS5 index of transcript lines, notes and bookmarks.

The index lives next to the config (not in the recordings folder, which may be a synced OneDrive folder
where SQLite files get corrupted). It only reads transcript.txt / notes.md / bookmarks.json / meeting.json
and is rebuilt per folder when one of those files changes, so it can always be deleted safely.
"""
import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

LINE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] ([^:]{1,40}): (.*)$")
INDEX_VERSION = 4
FILES = ("transcript.txt", "notes.md", "bookmarks.json", "meeting.json", "handwritten.md")
SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY, sig TEXT, title TEXT, tags TEXT);
CREATE TABLE IF NOT EXISTS vecs (path TEXT, model TEXT, tsig TEXT, t INTEGER, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS vecs_path ON vecs (path, model);
CREATE VIRTUAL TABLE IF NOT EXISTS lines USING fts5(
    norm, text UNINDEXED, who UNINDEXED, path UNINDEXED, t UNINDEXED, kind UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2");
"""


FOLD = str.maketrans("ıİIâîû", "iiiaiu")


def fold(text):
    """Same length as text: dotless i and the circumflex vowels FTS5's unicode61 leaves alone are mapped,
    so 'kisa' finds 'kısa'. Case and the other accents are folded by the tokenizer."""
    return text.translate(FOLD)


def parse_ts(h, m, s):
    return int(h) * 3600 + int(m) * 60 + int(s)


def transcript_lines(path):
    """[(seconds, who, text)] from a transcript.txt ('[hh:mm:ss] Who: text' lines; header lines skipped)."""
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if m:
            out.append((parse_ts(*m.group(1, 2, 3)), m.group(4), m.group(5)))
    return out


def load_bookmarks(folder):
    try:
        data = json.loads((Path(folder) / "bookmarks.json").read_text(encoding="utf-8", errors="replace"))
        return [b for b in data if isinstance(b, dict) and "t" in b]
    except (OSError, ValueError):
        return []


def add_bookmark(folder, t, label="Marked"):
    marks = load_bookmarks(folder)
    marks.append({"t": round(t, 1), "label": label})
    (Path(folder) / "bookmarks.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")


def folder_title(folder):
    try:
        title = json.loads((Path(folder) / "meeting.json").read_text(encoding="utf-8", errors="replace")).get("title")
    except (OSError, ValueError):
        title = None
    return title or Path(folder).name


def _sig(folder):
    parts = []
    for name in FILES:
        try:
            st = (Path(folder) / name).stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            pass
    return "|".join(parts)


class Archive:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        if self.db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:  # older layout: rebuild
            self.db.executescript("DROP TABLE IF EXISTS lines; DROP TABLE IF EXISTS folders; DROP TABLE IF EXISTS vecs;")
            self.db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()  # one connection shared by search windows and their index threads

    def close(self):
        self.db.close()

    def update(self, folders):
        """Re-index folders whose files changed and drop folders that are gone. Returns how many changed."""
        with self.lock:
            return self._update([Path(f) for f in folders])

    def _update(self, folders):
        known = dict(self.db.execute("SELECT path, sig FROM folders"))
        changed = 0
        with self.db:
            for path in set(known) - {str(f) for f in folders}:
                self._drop(path)
                changed += 1
            for f in folders:
                sig = _sig(f)
                if known.get(str(f), "") == sig:
                    continue
                self._drop(str(f))
                if sig:  # a folder whose text files are all gone just leaves the index
                    self._add(f, sig)
                changed += 1
        return changed

    def _drop(self, path):
        self.db.execute("DELETE FROM lines WHERE path = ?", (path,))
        self.db.execute("DELETE FROM folders WHERE path = ?", (path,))

    def _add(self, f, sig):
        p = str(f)
        title = folder_title(f)
        rows = [(text, who, p, t, "line") for t, who, text in transcript_lines(f / "transcript.txt")]
        try:
            notes = (f / "notes.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            notes = ""
        for line in notes.splitlines():
            line = line.strip().lstrip("#-*> ").strip()
            if line:
                rows.append((line, "Notes", p, -1, "note"))
        for b in load_bookmarks(f):
            rows.append((b.get("label") or "Marked", "Bookmark", p, int(b["t"]), "mark"))
        rows.append((title, "Title", p, -1, "title"))
        self.db.executemany("INSERT INTO lines (norm, text, who, path, t, kind) VALUES (?, ?, ?, ?, ?, ?)",
                            [(fold(r[0]),) + r for r in rows])
        try:
            hand = (f / "handwritten.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            hand = ""
        more = [(ln.strip().lstrip("#-*> ").strip(), "Handwritten", p, -1, "note") for ln in hand.splitlines()]
        self.db.executemany("INSERT INTO lines (norm, text, who, path, t, kind) VALUES (?, ?, ?, ?, ?, ?)",
                            [(fold(r[0]),) + r for r in more if r[0]])
        import recall  # lazy: recall imports this module
        self.db.execute("INSERT INTO folders (path, sig, title, tags) VALUES (?, ?, ?, ?)",
                        (p, sig, title, " ".join(recall.folder_tags(f))))

    # ---- meaning search: one vector per passage (3 transcript lines), made by a local embedding model ----
    def embed_missing(self, embed, model, progress=lambda t: None, cancel=lambda: False, budget=45.0):
        """Embed recordings whose transcript has no vectors for this model yet, newest first, for at most
        `budget` seconds: a big archive is learned over several questions instead of blocking the first one.
        A recording that fails is skipped (tried again next time). Returns how many are still waiting."""
        import time
        import numpy as np
        with self.lock:
            paths = [r[0] for r in self.db.execute("SELECT path FROM folders")]
            have = {(r[0], r[1]) for r in self.db.execute("SELECT DISTINCT path, tsig FROM vecs WHERE model = ?", (model,))}
            # vectors of recordings that are gone
            self.db.execute("DELETE FROM vecs WHERE path NOT IN (SELECT path FROM folders)")
            self.db.commit()
        todo = [(p, _tsig(p)) for p in sorted(paths, key=lambda x: Path(x).name, reverse=True)]
        todo = [(p, sig) for p, sig in todo if (p, sig) not in have]
        end, left, changed = time.monotonic() + budget, len(todo), False
        for k, (p, sig) in enumerate(todo):
            if cancel() or time.monotonic() > end:
                break
            progress(f"Learning recording {k + 1} of {len(todo)} (once per recording)...")
            try:
                ch = passages_of(p)
                rows = []
                if ch:
                    vecs, _ = embed([c[1] for c in ch], "d")
                    for (t, text), v in zip(ch, vecs):
                        v = np.asarray(v, dtype=np.float32)
                        rows.append((p, model, sig, t, text, (v / (np.linalg.norm(v) + 1e-9)).tobytes()))
                else:
                    rows.append((p, model, sig, -1, "", b""))  # nothing to embed: remember that
            except Exception:
                continue
            with self.lock, self.db:
                self.db.execute("DELETE FROM vecs WHERE path = ? AND model = ?", (p, model))
                self.db.executemany("INSERT INTO vecs (path, model, tsig, t, text, vec) VALUES (?, ?, ?, ?, ?, ?)", rows)
                self._vec_gen = getattr(self, "_vec_gen", 0) + 1
            left -= 1
        return left

    def nearest(self, qvec, model, limit=6):
        """Passages closest in meaning to the question vector: [{path, title, t, text, score}]."""
        import numpy as np
        q = np.asarray(qvec, dtype=np.float32)
        gen = getattr(self, "_vec_gen", 0)
        cache = getattr(self, "_vec_cache", None)
        if not cache or cache[0] != (model, gen):
            with self.lock:
                rows = self.db.execute(
                    "SELECT v.path, f.title, v.t, v.text, v.vec FROM vecs v JOIN folders f ON f.path = v.path "
                    "WHERE v.model = ? AND v.t >= 0", (model,)).fetchall()
            rows = [r for r in rows if len(r[4]) == q.shape[0] * 4]  # only vectors of the question's size
            mat = np.frombuffer(b"".join(r[4] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else None
            cache = self._vec_cache = ((model, gen), rows, mat)
        _, rows, mat = cache
        if mat is None or mat.shape[1] != q.shape[0]:
            return []
        score = mat @ (q / (np.linalg.norm(q) + 1e-9))
        return [{"path": rows[i][0], "title": rows[i][1], "t": rows[i][2], "text": rows[i][3], "score": float(score[i])}
                for i in np.argsort(-score)[:limit]]

    def tags(self, path):
        with self.lock:
            r = self.db.execute("SELECT tags FROM folders WHERE path = ?", (str(path),)).fetchone()
        return (r[0] or "").split() if r else []

    def search_any(self, words, limit=60):
        """Lines matching ANY of the words or phrases, best first (for answering questions)."""
        terms = []
        for w in words:
            toks = [fold(t) for t in re.findall(r"\w[\w.\-/]*", w, re.UNICODE)]
            if toks:
                terms.append('"' + " ".join(toks).replace('"', "") + '"' + ("*" if len(toks) == 1 else ""))
        if not terms:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT l.path, f.title, l.t, l.who, l.kind, l.text FROM lines l JOIN folders f ON f.path = l.path "
                "WHERE lines MATCH ? AND l.kind != 'title' ORDER BY rank LIMIT ?",
                (" OR ".join(terms), limit)).fetchall()
        return [{"path": r[0], "title": r[1], "t": r[2], "who": r[3], "kind": r[4], "text": r[5]} for r in rows]

    def search(self, query, limit=200):
        """[{path, title, t, who, kind, text, snippet}] best match first. Every word must match (prefix match).
        snippet marks the hits with ⁅ ... ⁆."""
        words = [fold(w) for w in re.findall(r"\w[\w.\-/]*", query, re.UNICODE)]
        if not words:
            return []
        fts = " ".join('"' + w.replace('"', "") + '"*' for w in words)
        with self.lock:
            rows = self.db.execute(
                "SELECT l.path, f.title, l.t, l.who, l.kind, l.text FROM lines l JOIN folders f ON f.path = l.path "
                "WHERE lines MATCH ? ORDER BY rank LIMIT ?", (fts, limit)).fetchall()
        return [{"path": r[0], "title": r[1], "t": r[2], "who": r[3], "kind": r[4], "text": r[5],
                 "snippet": snippet(r[5], words)} for r in rows]


def _tsig(folder):
    """Changes when the transcript changes (the only file the vectors are made from)."""
    try:
        st = (Path(folder) / "transcript.txt").stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


def passages_of(folder, size=3, step=2, limit=900):
    """[(seconds, text)]: overlapping windows of transcript lines, the unit of meaning search."""
    lines = transcript_lines(Path(folder) / "transcript.txt")
    return [(lines[i][0], "\n".join(f"{w}: {t}" for _, w, t in lines[i:i + size])[:limit])
            for i in range(0, len(lines), step)]


def fmt_clock(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    return f"{h:02d}:{rem // 60:02d}:{rem % 60:02d}"


def _plain(c):
    return unicodedata.normalize("NFKD", c)[0].lower() if c else c


def snippet(text, words, width=110):
    """Cut ~width chars around the first hit and mark word starts that begin with a query word."""
    plain = "".join(_plain(c) for c in fold(text))  # same length as text
    pats = [re.compile(r"(?<!\w)" + re.escape("".join(_plain(c) for c in w)) + r"\w*") for w in words]
    hits = sorted((m.start(), m.end()) for p in pats for m in p.finditer(plain))
    first = hits[0][0] if hits else 0
    a = max(0, first - width // 3)
    b = min(len(text), a + width)
    if a > 0:
        a = text.find(" ", a) + 1 or a
    out, pos = [], a
    for s, e in hits:
        if s < pos or e > b:
            continue
        out += [text[pos:s], "⁅", text[s:e], "⁆"]
        pos = e
    out.append(text[pos:b])
    return ("… " if a > 0 else "") + "".join(out) + (" …" if b < len(text) else "")
