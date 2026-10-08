"""related.py — suggest [[wikilinks]] between notes that say related things.

    cl links suggest [--tag journal] [--min 0.80] [--gap-days 7] [--per-note 3]
                     [--write] [--json]

Built 2026-10-01 for the journal: "it's great if my journal entries link to each
other when related — a helpful way to see patterns in thinking over time." A link
is like a tag: it adds a way in, it does not change what was written. So links go
where every other link goes — the trailing `## Related` footer — and nothing in
the body is touched.

HOW "RELATED" IS MEASURED. An embedding model (nomic-embed-text, ~270MB) turns
each note into a point in meaning-space; notes about the same thing land close
together. It doesn't read or judge or write — it only measures distance, which is
why a small one is enough. It runs on the CPU (num_gpu 0): on the GPU it would
compete with the pinned resident model, and ollama's answer to "doesn't fit" is to
evict the resident. Long notes are embedded in chunks and averaged. Vectors are
cached by (path, mtime), so a rerun only embeds what changed.

WHAT IS LEFT OUT, and why:
  * pairs already linked, in either direction — nothing to suggest;
  * pairs written within GAP_DAYS of each other — consecutive days' schedules
    are trivially alike, and "patterns over time" means across time;
  * machine logs saved under daily-note names (Hermes transcripts, session
    logs): they matched each other on FORMAT, not meaning, and drowned the
    first trial run. Detected by content, since they carry the journal's names.

DIRECTION. The newer note links to the older one: you are writing now, and the
footer says "this connects to what you thought then". The older note is not
edited; the link shows up there as a backlink (`<leader>b`, Surface), which
`cl links back` derives.

Dry run by default; --write appends to (or creates) the newer note's footer.
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from datetime import datetime
from pathlib import Path

import fm
import links
import stream
from paths import DATA_DIR, KB, NOTE_DIRS, WRITING, JOURNAL

OLLAMA = "http://127.0.0.1:11434/api/embed"
MODEL = "nomic-embed-text"
CACHE = DATA_DIR / "related-vectors.json"
CHUNK = 1500
MAX_CHARS = 9000
DAILY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Hermes transcripts, timestamped session logs, and conversation syntheses that
# were filed into daily notes. Heuristic on purpose — the dry run is the check.
MACHINE_LOG = re.compile(r"^# Hermes —|^- \d{4} — |^Spent a good session|"
                         r"conversation with Claude", re.M)
MIN_WORDS = 40          # an empty template with three schedule lines is not a thought


def _body(path: Path) -> str:
    _, body, _ = fm.split(path)
    body = re.sub(r"\n## Related\b.*", "", body, flags=re.S)
    return body.replace("originally written by hand", "").strip()


def _when(path: Path) -> datetime | None:
    """Date the words were put down: `written:` (transcriptions), else a daily
    note's own name, else captured/created — the same precedence as chrono."""
    meta = fm.read(path)
    v = str(meta.get("written") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return datetime.fromisoformat(v)
    # A daily note's name is its date. Its `created:` can be a migration stamp
    # (March dailies carry June), so the name wins.
    if DAILY.match(path.stem):
        return datetime.fromisoformat(path.stem)
    for key in ("captured", "created"):
        v = str(meta.get(key) or "").replace("T", " ")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return datetime.fromisoformat(v)
    return None


def _all_notes() -> list[Path]:
    return sorted(p for d in NOTE_DIRS if d.is_dir() for p in d.rglob("*.md"))


def candidates(tag: str) -> list[Path]:
    """The notes of one FOLDER (tags retired 2026-10-05; the arg keeps its old
    name for callers). "journal" is writing/journal/; another name is a folder
    under writing/ (poems, stories, ...) or under the kb root (threads, ...)."""
    tag = (tag or "journal").strip("/#")
    d = JOURNAL if tag == "journal" else (
        WRITING / tag if (WRITING / tag).is_dir() else KB / tag)
    if not d.is_dir() or d not in NOTE_DIRS and not any(d.is_relative_to(n) for n in NOTE_DIRS):
        return []
    out = []
    for md in sorted(d.rglob("*.md")):
        body = _body(md)
        prose = re.sub(r"^#.*$", "", body, flags=re.M)
        if len(prose.split()) < MIN_WORDS or MACHINE_LOG.search(body):
            continue
        out.append(md)
    return out


def _embed(texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(OLLAMA, json.dumps(
        {"model": MODEL, "input": texts, "options": {"num_gpu": 0}, "keep_alive": "5m"}
    ).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["embeddings"]


def vectors(paths: list[Path]) -> dict[Path, list[float]]:
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    out = {}
    for p in paths:
        key = f"{p.name}|{p.stat().st_mtime_ns}"
        if key not in cache:
            body = _body(p)[:MAX_CHARS]
            embs = _embed([f"search_document: {body[i:i + CHUNK]}"
                           for i in range(0, len(body), CHUNK)])
            v = [sum(c) / len(embs) for c in zip(*embs)]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            cache[key] = [x / n for x in v]
        out[p] = cache[key]
    # Keep every vector whose note still exists, not only this call's: the
    # journal run and the per-note lookup share one cache, and pruning to the
    # caller's set made each re-embed the other's notes every time.
    live = set(out_keys(paths))
    for p in _all_notes():
        try:
            live.add(f"{p.name}|{p.stat().st_mtime_ns}")
        except OSError:
            pass
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({k: v for k, v in cache.items() if k in live}))
    return out


def out_keys(paths):
    return [f"{p.name}|{p.stat().st_mtime_ns}" for p in paths]


def suggest_for(name: str, text: str, k=5, min_sim=0.55) -> list[dict]:
    """Notes related to ONE note, for the "suggest links" button.

    `text` is sent along because the note may not have reached the tower yet —
    a phone capture is minutes from its sync. Every other note is compared by
    its cached vector. Already-linked notes and the note itself are skipped."""
    paths = [p for p in _all_notes() if p.name != name
             and len(re.sub(r"^#.*$", "", _body(p), flags=re.M).split()) >= MIN_WORDS
             and not MACHINE_LOG.search(_body(p))]
    vec = vectors(paths)
    body = re.sub(r"\n## Related\b.*", "", text, flags=re.S)
    body = re.sub(r"\A---\n.*?\n---\n", "", body, flags=re.S)[:MAX_CHARS] or name
    embs = _embed([f"search_document: {body[i:i + CHUNK]}" for i in range(0, len(body), CHUNK)])
    v = [sum(c) / len(embs) for c in zip(*embs)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / n for x in v]
    linked = set(re.findall(r"\[\[([^\]|#]+)", text))
    scored = sorted(((sum(a * b for a, b in zip(v, vec[p])), p) for p in paths), key=lambda t: -t[0])
    out = []
    for s, p in scored:
        if s < min_sim or len(out) >= k:
            break
        if p.stem in linked:
            continue
        out.append({"link": p.stem, "score": round(s, 3),
                    "title": stream._title(_body(p)), "text": re.sub(r"\s+", " ", _body(p))[:100]})
    return out


def _linked(a: Path, b: Path, mapping) -> bool:
    for src, dst in ((a, b), (b, a)):
        if any(r.get("path") == str(dst) for r in links.outgoing(src, mapping)):
            return True
    return False


def suggest(tag="journal", min_sim=0.80, gap_days=7, per_note=3) -> list[dict]:
    paths = candidates(tag)
    vec = vectors(paths)
    when = {p: _when(p) for p in paths}
    mapping = links.build_map()
    pairs = []
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            s = sum(x * y for x, y in zip(vec[a], vec[b]))
            if s < min_sim:
                continue
            da, db = when[a], when[b]
            if da and db and abs((da - db).days) <= gap_days:
                continue
            newer, older = (a, b) if (da or datetime.min) >= (db or datetime.min) else (b, a)
            pairs.append((s, newer, older))
    pairs.sort(key=lambda t: -t[0])
    taken: dict[Path, int] = {}
    out = []
    for s, newer, older in pairs:
        if taken.get(newer, 0) >= per_note or _linked(newer, older, mapping):
            continue
        taken[newer] = taken.get(newer, 0) + 1
        out.append({"from": str(newer), "to": str(older), "link": older.stem,
                    "score": round(s, 3),
                    "from_date": when[newer].date().isoformat() if when[newer] else None,
                    "to_date": when[older].date().isoformat() if when[older] else None,
                    "from_text": re.sub(r"\s+", " ", _body(newer))[:80],
                    "to_text": re.sub(r"\s+", " ", _body(older))[:80]})
    return out


def write(rows: list[dict]) -> int:
    """Append [[link]] to each `from` note's trailing ## Related footer, creating
    it if absent: `[[a]] · [[b]]`, one line, the house format."""
    by_note: dict[str, list[str]] = {}
    for r in rows:
        by_note.setdefault(r["from"], []).append(r["link"])
    for path, names in by_note.items():
        p = Path(path)
        text = p.read_text()
        add = " · ".join(f"[[{n}]]" for n in names)
        m = re.search(r"\n## Related[ \t]*\n(?:[ \t]*\n)*(.*?)\s*\Z", text, flags=re.S)
        if m and m.group(1).strip():
            text = text[:m.end(1)] + " · " + add + "\n"
        elif m:
            text = text[:m.start()] + f"\n## Related\n\n{add}\n"
        else:
            text = text.rstrip("\n") + f"\n\n## Related\n\n{add}\n"
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(text)
        tmp.replace(p)
    return len(by_note)
