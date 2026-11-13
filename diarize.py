"""Who said what on one track (the speaker loopback, or the phone's single track).

No extra speech detection: the transcript's own segments are the speech. Each segment is cut into ~3 s windows,
each window gets a speaker embedding (WeSpeaker ResNet34 through sherpa-onnx, CPU), windows are grouped by
average-linkage clustering on cosine similarity, and every segment takes the speaker that holds most of its
windows. Groups with very little speech are folded into the nearest big one. Measured on the AMI ES2004a
meeting squeezed through 24 kbps Opus (Teams-like): 94% of the transcribed speech gets the right speaker,
one person sometimes splits into two groups (naming both the same merges them), ~2 s CPU per minute of speech.

Voiceprints: the mean embedding of a named speaker is kept in voices.json (per PC, never uploaded) and used to
suggest the name next time. A suggestion is always shown for confirmation, never applied silently.
"""
import json
import re
import urllib.request
from pathlib import Path

import numpy as np

MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34_LM.onnx")
MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_BYTES = 26530550
RATE = 16000
WIN = 3.0          # seconds per embedding window
THRESHOLD = 0.5    # average cosine similarity needed to join two groups
MIN_SPEECH = 20.0  # seconds; smaller groups join the nearest big one
VOICE_MATCH = 0.75  # cosine to suggest a saved name; same person across real Teams calls 0.86-0.95, other voices up to ~0.89 (unverified), so it is only a suggestion


def model_path(models_root):
    return Path(models_root) / "speakers" / MODEL_NAME


def model_ready(models_root):
    p = model_path(models_root)
    return p.exists() and p.stat().st_size == MODEL_BYTES


def download_model(models_root, progress, cancel):
    p = model_path(models_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
        got = 0
        while True:
            if cancel():
                raise RuntimeError("cancelled")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            progress(got / MODEL_BYTES, "downloading speaker model...")
    if tmp.stat().st_size != MODEL_BYTES:
        tmp.unlink()
        raise RuntimeError("speaker model download incomplete")
    tmp.replace(p)


class Embedder:
    def __init__(self, models_root, threads=4):
        import sherpa_onnx
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path(models_root)), num_threads=threads))

    def __call__(self, x):
        if len(x) < RATE // 2:  # too short for a stable embedding: repeat it
            x = np.tile(x, int(np.ceil(RATE / 2 / max(1, len(x)))))[:RATE // 2]
        st = self.ex.create_stream()
        st.accept_waveform(RATE, x)
        st.input_finished()
        v = np.asarray(self.ex.compute(st), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)


def windows(segments):
    """[(segment index, start, end)] ~WIN s windows covering each segment."""
    out = []
    for i, s in enumerate(segments):
        dur = s["end"] - s["start"]
        if dur <= 0.2:
            continue
        n = max(1, round(dur / WIN))
        step = dur / n
        out += [(i, s["start"] + k * step, s["start"] + (k + 1) * step) for k in range(n)]
    return out


def cluster(V, threshold=THRESHOLD):
    """Average-linkage agglomerative clustering on cosine similarity. Returns a group id per row."""
    n = len(V)
    if n == 1:
        return np.zeros(1, int)
    sums = (V @ V.T).astype(np.float64)
    size = np.ones(n)
    alive = np.ones(n, bool)
    S = sums.copy()
    np.fill_diagonal(S, -np.inf)
    members = [[i] for i in range(n)]
    while True:
        i, j = np.unravel_index(np.argmax(S), S.shape)
        if S[i, j] < threshold:
            break
        members[i] += members[j]
        members[j] = []
        sums[i, :] += sums[j, :]
        sums[:, i] = sums[i, :]
        size[i] += size[j]
        alive[j] = False
        S[j, :] = -np.inf
        S[:, j] = -np.inf
        S[i, :] = np.where(alive, sums[i, :] / (size[i] * size), -np.inf)
        S[:, i] = S[i, :]
        S[i, i] = -np.inf
    label = np.zeros(n, int)
    for g, m in enumerate(x for x in members if x):
        label[m] = g
    return label


def diarize(audio, segments, embed, progress=lambda f, t: None, cancel=lambda: False):
    """audio: float32 mono 16 kHz of the track. segments: [{start, end, ...}] of that track.
    Returns (speaker index per segment, groups) where groups[k] = {"vec", "seconds", "sample"} sorted by most
    speech first, so Speaker 1 talks most."""
    wins = windows(segments)
    if not wins:
        return [0] * len(segments), []
    vecs = []
    for n, (_, a, b) in enumerate(wins):
        if cancel():
            raise RuntimeError("cancelled")
        vecs.append(embed(audio[int(a * RATE):int(b * RATE)]))
        if n % 20 == 0:
            progress(n / len(wins), "telling speakers apart...")
    V = np.array(vecs)
    w = np.array([b - a for _, a, b in wins])
    lab = cluster(V)
    groups = sorted(set(lab), key=lambda g: -w[lab == g].sum())
    big = [g for g in groups if w[lab == g].sum() >= MIN_SPEECH] or groups[:1]
    cent = {g: V[lab == g].mean(0) for g in groups}
    for g in groups:
        if g not in big:
            lab[lab == g] = max(big, key=lambda b: float(cent[g] @ cent[b]))
    # each segment: the group holding most of its windows (by duration)
    votes = {}
    for (i, a, b), g in zip(wins, lab):
        votes.setdefault(i, {}).setdefault(g, 0.0)
        votes[i][g] += b - a
    rank = {g: k for k, g in enumerate(big)}
    seg_spk = []
    for i in range(len(segments)):
        v = votes.get(i)
        seg_spk.append(rank[max(v, key=v.get)] if v else 0)
    out = []
    for g in big:
        m = lab == g
        vec = V[m].mean(0)
        idx = [i for i, s in enumerate(seg_spk) if s == rank[g]]
        best = max(idx, key=lambda i: segments[i]["end"] - segments[i]["start"]) if idx else None
        out.append({"vec": vec / (np.linalg.norm(vec) + 1e-9), "seconds": float(w[m].sum()),
                    "sample": segments[best]["start"] if best is not None else 0.0})
    return seg_spk, out


# ---------- voiceprints ----------
def load_voices(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def suggest(voices, vec, taken=()):
    """(name, similarity) of the closest saved voice above VOICE_MATCH, else (None, best similarity)."""
    best, best_s = None, -1.0
    for name, v in voices.items():
        if name in taken:
            continue
        s = float(np.asarray(v["vec"]) @ vec)
        if s > best_s:
            best, best_s = name, s
    return (best, best_s) if best_s >= VOICE_MATCH else (None, best_s)


def remember(path, name, vec, seconds):
    """Fold a confirmed speaker into the saved voice (weighted by speech time, capped so it keeps adapting)."""
    voices = load_voices(path)
    old = voices.get(name)
    vec = np.asarray(vec, dtype=np.float64)
    if old:
        w_old = min(float(old.get("seconds", 0)), 1800.0)
        v = np.asarray(old["vec"]) * w_old + vec * seconds
        v /= np.linalg.norm(v) + 1e-9
        voices[name] = {"vec": [round(float(x), 5) for x in v], "seconds": w_old + seconds,
                        "meetings": old.get("meetings", 0) + 1}
    else:
        voices[name] = {"vec": [round(float(x), 5) for x in vec], "seconds": seconds, "meetings": 1}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(voices), encoding="utf-8")


# ---------- recording folders ----------
RESERVED = re.compile(r"^(Me|Them|Speaker( \d+)?)$", re.IGNORECASE)


class Cancelled(Exception):
    pass


def _write_all(files):
    """{path: text}: write every file to a temp first, then swap them in, so a failure (disk full, a file
    open in another program) leaves the old versions instead of half-written ones."""
    tmps = {}
    try:
        for path, text in files.items():
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmps[path] = tmp
        for path, tmp in tmps.items():
            tmp.replace(path)
    finally:
        for tmp in tmps.values():
            if tmp.exists():
                tmp.unlink()


def speaker_track(session_dir, find_tracks):
    """(label in the transcript, audio path) of the track to split: the speaker loopback, or the only track."""
    tracks = find_tracks(Path(session_dir))
    for label, path in tracks:
        if label == "Them":
            return label, path
    return tracks[0] if len(tracks) == 1 else (None, None)


def run_session(session_dir, models_root, voices_path, tr, progress, cancel):
    """Split the 'Them' (or single) track into Speaker 1..N in transcript.srt/.txt and write speakers.json.
    Returns the number of speakers found (0 = nothing to split). Raises Cancelled."""
    d = Path(session_dir)
    label, path = speaker_track(d, tr.find_tracks)
    srt = d / "transcript.srt"
    if not path or not srt.exists():
        return 0
    segs = tr.read_srt(srt)
    # the loopback track: everything that is not the mic ("Them", or Speaker N / names from an earlier run);
    # a single track (phone, or mic only): every line
    mine = [s for s in segs if s["who"] != "Me"] if label == "Them" else list(segs)
    if not mine:
        return 0
    try:
        if not model_ready(models_root):
            download_model(models_root, progress, cancel)
        progress(0.0, "telling speakers apart...")
        audio = tr.load_audio(Path(path))  # 16 kHz mono float32; decodes m4a/mp3 too
        spk, groups = diarize(audio, mine, Embedder(models_root), progress, cancel)
    except RuntimeError as e:
        if cancel():
            raise Cancelled() from e
        raise
    if cancel():
        raise Cancelled()
    for s, k in zip(mine, spk):
        s["who"] = f"Speaker {k + 1}"
    voices, taken, info = load_voices(voices_path), set(), {}
    for k, g in enumerate(groups):
        name, score = suggest(voices, g["vec"], taken)
        if name:
            taken.add(name)
        info[f"Speaker {k + 1}"] = {"name": None, "suggest": name, "score": round(score, 3),
                                    "seconds": round(g["seconds"], 1), "sample": g["sample"],
                                    "vec": [round(float(x), 5) for x in g["vec"]]}
    files = {}
    if (d / "transcript.raw.txt").exists():  # cleaned text: keep it, only change who says each line
        # txt lines are blocks that start at a segment; walk them in order, per whole-second timestamp
        queue = {}
        for s in sorted(mine, key=lambda x: x["start"]):
            queue.setdefault(tr.fmt_ts(s["start"]), []).append(s["who"])
        line = re.compile(r"^\[(\d\d:\d\d:\d\d)\] ([^:]{1,40}): ")
        for name in ("transcript.txt", "transcript.raw.txt"):
            q = {k: list(v) for k, v in queue.items()}
            out = []
            for ln in (d / name).read_text(encoding="utf-8").split("\n"):
                m = line.match(ln)
                if m and (label != "Them" or m.group(2) != "Me") and q.get(m.group(1)):
                    ln = f"[{m.group(1)}] {q[m.group(1)].pop(0)}: " + ln[m.end():]
                out.append(ln)
            files[d / name] = "\n".join(out)
    segs.sort(key=lambda s: (s["start"], s["who"]))
    if d / "transcript.txt" not in files:
        labels = sorted({s["who"] for s in segs}, key=lambda w: (w != "Me", w))
        duration = max((s["end"] for s in segs), default=0.0)
        files[d / "transcript.txt"] = tr.render_txt(d, segs, duration, labels)
    files[srt] = tr.render_srt(segs)
    files[d / "speakers.json"] = json.dumps({"track": label, "speakers": info}, indent=1)
    _write_all(files)
    return len(groups)


def load_speakers(session_dir):
    try:
        return json.loads((Path(session_dir) / "speakers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def current_label(label, info):
    return info.get("name") or label


def clean_name(name):
    name = re.sub(r"\s+", " ", (name or "").replace(":", " ")).strip()[:40]
    return None if not name or RESERVED.match(name) else name


def apply_names(session_dir, names, voices_path):
    """names: {"Speaker 2": "Mohammad", ...} (empty = back to "Speaker 2"). Renames the speaker in
    transcript.txt, transcript.raw.txt and transcript.srt, saves speakers.json and teaches voices.json.
    Two speakers given the same name become one (the second group is folded into the first), so every name
    on the page belongs to exactly one group and renames stay unambiguous. Returns {old shown name: new}."""
    d = Path(session_dir)
    data = load_speakers(d)
    if not data:
        return {}
    sp = data["speakers"]
    final = {}  # label -> shown name after this save
    for label, info in sp.items():
        new = clean_name(names[label]) if label in names else info.get("name")
        final[label] = new or label
    renames, owner = {}, {}
    for label in list(sp):
        shown = final[label]
        old = current_label(label, sp[label])
        if shown != old:
            renames[old] = shown
        if shown in owner and shown != label:  # same name as an earlier group: fold this group into it
            keep = sp[owner[shown]]
            g = sp.pop(label)
            tot = keep["seconds"] + g["seconds"]
            v = np.asarray(keep["vec"]) * keep["seconds"] + np.asarray(g["vec"]) * g["seconds"]
            keep["vec"] = [round(float(x), 5) for x in v / (np.linalg.norm(v) + 1e-9)]
            keep["seconds"] = round(tot, 1)
            continue
        owner[shown] = label
        sp[label]["name"] = shown if shown != label else None
    files = {}
    if renames:
        alt = "|".join(map(re.escape, sorted(renames, key=len, reverse=True)))
        line = re.compile(r"^(\[\d\d:\d\d:\d\d\] )(" + alt + r"): ")
        srt_line = re.compile(r"^(" + alt + r"): ")
        for name in ("transcript.txt", "transcript.raw.txt"):
            p = d / name
            if p.exists():
                files[p] = "\n".join(line.sub(lambda m: m.group(1) + renames[m.group(2)] + ": ", ln)
                                     for ln in p.read_text(encoding="utf-8").split("\n"))
        p = d / "transcript.srt"
        if p.exists():
            files[p] = "\n".join(srt_line.sub(lambda m: renames[m.group(1)] + ": ", ln)
                                 for ln in p.read_text(encoding="utf-8").split("\n"))
    files[d / "speakers.json"] = json.dumps(data, indent=1)
    _write_all(files)
    for info in sp.values():  # teach only voices that got a (new) name in this save
        if info.get("name") and info["name"] in renames.values():
            remember(voices_path, info["name"], info["vec"], info["seconds"])
    return renames
