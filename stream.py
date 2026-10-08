"""stream.py — `cl stream` — the view engine over ~/kb/writing/_stream.

The stream has good capture and no way to LOOK at what's in it. Tags accumulate
and nothing groups them. This is the missing half: one query engine, several
faces (terminal here, nvim buffer, Surface, Hermes) — so the grouping logic
lives in exactly one place and every surface agrees.

The design commitment: a view is a QUERY, never a file. Nothing here writes a
derived copy of your notes that could drift from them. `render` emits a
read-only artifact to outbox/ for eyeballing, and that is explicitly derived —
regenerate it, never edit it.

An agenda item is a note tagged `#todo`. Urgent is the child `#todo/sooner`.

  tags:    what it is ABOUT (home, blog/x) AND whether it is on your plate.

This used to read `status: todo` and `when:` — two frontmatter fields nothing
has written since the flatten, so the view returned an empty agenda over a kb
holding two dozen live todos while `cl triage --view todo` (reading the tag)
showed them all. Two spellings of one idea, and the one with a writer won.
There is now exactly one: the tag. `status:` and `when:` are not read here and
are not written by anything.

Two bands, not six: `#todo/sooner` and everything else under `#todo`. A date
per item was never filled in, and an unfilled date manufactured an "overdue"
that only ever meant "captured a while ago". Age still sorts the later band —
that is a rendering choice over data that exists, which the dates were not.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fm  # noqa: E402

KB = Path(os.environ.get("KB_DIR", str(Path.home() / "kb")))
from paths import STORE, TODO, WRITING, WORKING, THREADS  # noqa: E402
import todo  # noqa: E402
STREAM = STORE          # = working/ since 2026-10-05
# Tags were retired 2026-10-05: the commands that read or wrote them say so and
# exit 0, so an old caller degrades to "nothing" instead of a traceback.
RETIRED = "tags are retired (2026-10-05) — folders + todo.md now; see `cl todo`"
OUTBOX = KB / "outbox" / "reports"

# The pool is a tag, and urgent is its CHILD — not a second flat tag. Flat
# `#todo` + `#sooner` is two bits and therefore four states, one of which
# (`#sooner` alone) means nothing and is invisible to the pool forever. As a
# child there are two states and the meaningless one cannot be spelled. Same
# constants triage_tui.py works in; they must not drift apart again.
POOL_TAG = "todo"
SOONER = "todo/sooner"

_DAILY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$")
# A line that is nothing but inline tags — `#home`, `#update/daily #idea`.
# These lead a lot of older captures and make a terrible title.
_TAGLINE = re.compile(r"^\s*(?:#[\w/\-]+\s*)+$")
# Provenance that leaked into the body rather than the frontmatter. `source:`
# is the only one in the store; anchored to the line start and a short bare
# key so a real sentence containing a colon is never eaten.
_STRAY_FIELD = re.compile(r"^(?:source|captured|via):\s*\S*$", re.I)
# A transcribed notebook page says where it came from on its first line. That is
# provenance, not a title — left in, every transcription in a view reads
# "originally written by hand".
_PROVENANCE = re.compile(r"^originally (?:hand)?written(?: by hand)?:?$", re.I)
_TRAILING_TAGS = re.compile(r"(?:\s+#[\w/\-]+)+\s*$")


# ── reading ────────────────────────────────────────────────────────────────

def _norm_dt(raw):
    """Frontmatter timestamps come in three shapes across the stream's history:
    `2026-09-01T11:57:43`, `2026-09-01 11:57`, and bare `2026-09-01`. Normalise
    or the same day interleaves by FORMAT instead of by time when sorted."""
    if not raw:
        return None
    s = str(raw).strip().strip('"').strip("'").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_date(raw):
    dt = _norm_dt(raw)
    return dt.date() if dt else None


def _title(body: str) -> str:
    """First markdown heading, else the first line that carries actual words.

    Skipping pure-tag lines matters: `bondo-flashing-bear-damage.md` opens with
    a bare `#home`, and a view titled "#home" four times over is useless. So
    does skipping a leading `source:` line — the retired phone-capture chain
    put one in the BODY of a few dozen notes, and they all showed up in the
    agenda titled "source: phone-notif"."""
    for line in body.splitlines():
        s = line.strip()
        if not s or s == "---":
            continue
        m = _HEADING.match(s)
        if m:
            s = m.group(1).strip()
        elif _TAGLINE.match(s) or _STRAY_FIELD.match(s) or _PROVENANCE.match(s):
            continue
        # An inline tag trailing the one line the note has is the tag doing its
        # job, not part of the sentence: `buy music stand #todo` is titled
        # "buy music stand".
        s = _TRAILING_TAGS.sub("", s).strip()
        if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()      # a matched pair only — `"Bondo" plus …`
        return s if len(s) <= 90 else s[:87] + "…"
    return "(empty)"


def _tags(meta) -> list:
    t = meta.get("tags", [])
    if isinstance(t, str):
        t = [x.strip() for x in t.strip("[]").split(",") if x.strip()]
    return [str(x).strip().lstrip("#") for x in t if str(x).strip()]


# ── tag identity ───────────────────────────────────────────────────────────
# The whole point of tags-as-routing is that a note's placement is a WORD, and
# words drift: bridge/Bridge/bridges/bridge_ui are four spellings of one idea,
# and a vocabulary that accumulates all four stops being able to answer "what
# is this about". Nothing downstream can repair that later — `cl stream tag x`
# is an exact-or-prefix match, so a near-duplicate is simply a tag whose notes
# have gone missing.
#
# So reuse is enforced HERE, in the writer, rather than asked for in a prompt.
# A human typing a variant and a model proposing one hit the same guard, and it
# keeps working as the vocabulary grows — which is the opposite of a prompt
# that says "prefer existing tags" and degrades as the list gets longer than
# the model's attention.

def norm_tag(raw) -> str:
    """The canonical stored spelling: lowercase, dashes, no leading #."""
    t = str(raw or "").strip().lstrip("#").lower()
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-{2,}", "-", t)
    t = re.sub(r"/{2,}", "/", t)
    return t.strip("-/")


def _cmp_key(t: str) -> str:
    """The form two tags are the SAME idea in. Drops separators and a trailing
    plural per segment, so bridge/Bridge/bridges/bridge_ui vs bridge-ui all
    collapse together. Never stored — only compared."""
    segs = []
    for seg in norm_tag(t).split("/"):
        seg = seg.replace("-", "")
        # -ies before -s, or `groceries` stems to `grocerie` and never meets
        # `grocery` — the exact near-duplicate already sitting in the kb, and
        # the one that proved a bare trailing-s rule is not enough.
        if len(seg) > 4 and seg.endswith("ies"):
            seg = seg[:-3] + "y"
        elif len(seg) > 3 and seg.endswith("s") and not seg.endswith("ss"):
            seg = seg[:-1]
        segs.append(seg)
    return "/".join(segs)


def _lev(a: str, b: str) -> int:
    """Levenshtein. Small strings, called against a vocabulary of tens — a
    dependency would cost more than the twelve lines."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def vocabulary(items=None) -> dict:
    """Every frontmatter tag in the stream → how many notes carry it.

    Daily notes are INCLUDED, unlike every view in this file. They are not
    agenda items, but `journal` is a real tag with real weight, and a
    vocabulary that omitted it would report the system's most-used word as
    novel the next time anything proposed it."""
    items = items if items is not None else load(include_daily=True)
    counts = {}
    for it in items:
        for t in it["tags"]:
            t = norm_tag(t)
            if t:
                counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ── body tags ──────────────────────────────────────────────────────────────
# A `#tag` in a note's BODY is a tag, exactly as Obsidian reads it (2026-10-04).
#
# It used to be the other way round: only frontmatter counted, and three
# separate machines existed to hoist body hashtags up into it — kb-inbox at
# capture, an nvim-write save hook, and a tower sweep for notes written on the
# phone. Each was a door that could be missed (a `#dream` typed in Obsidian sat
# untagged and exposed to --prune-noise). Reading the body here retires all
# three, and makes this module agree with Obsidian's tag pane and Dataview's
# `file.tags`, so every face sees the same set.
#
# Obsidian's rules, which is also the false-positive defence that the old
# "only words the vocabulary already has" gate used to be:
#   - a tag starts at the start of a line or after whitespace (`page#section`,
#     `[[note#heading]]` and URLs are not tags)
#   - fenced code and inline code are skipped (`#define`, `#include`)
#   - it must contain a non-digit (`#1`, `#2026` are not tags)
#   - `# heading` has a space after the hash, so it is never a candidate
# What still gets through is a hex colour in prose (`#e8a34e`) — Obsidian
# counts that too, and the tag index shows it the day it happens.

_FENCED = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1[^\n]*$", re.M | re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TAG_STOP = r"\s#!@$%^&*()=+\[\]{};:'\",.<>?\\|`~"
_BODYTAG = re.compile(r"(?:(?<=\s)|^)#([^" + _TAG_STOP + r"]+)", re.M)


def _code_spans(text):
    """[(start, end)] of fenced and inline code, which hold no tags."""
    spans = [m.span() for m in _FENCED.finditer(text)]
    for m in _INLINE_CODE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return spans


def _tag_matches(text):
    """Every body-tag match outside code, as (match, normalised tag)."""
    spans = _code_spans(text)
    for m in _BODYTAG.finditer(text or ""):
        if any(a <= m.start() < b for a, b in spans):
            continue
        raw = m.group(1).rstrip("/-")
        if not re.search(r"[^\d/]", raw):
            continue                                   # #1, #2026
        t = norm_tag(raw)
        if t:
            yield m, t


def body_tags(text, vocab=None) -> list:
    """The tags in a note body, normalised, in order of first appearance.

    `vocab` given: only those it already names — the old gated reading, kept
    for callers that still want "did they mean an EXISTING tag".
    """
    known = {norm_tag(t) for t in vocab} if vocab is not None else None
    out = []
    for _, t in _tag_matches(text):
        if (known is None or t in known) and t not in out:
            out.append(t)
    return out


def note_tags(path) -> list:
    """Every tag one note file carries: frontmatter then body, normalised. The
    single-note form of what load() computes, for modules that hold a path."""
    path = Path(path)
    meta = fm.read(path)
    _, body, _ = fm.split(path)
    return list(dict.fromkeys([norm_tag(t) for t in _tags(meta)] + body_tags(body)))


def _edit_body_tags(body, fn):
    """Rewrite body tags through fn(tag) -> new tag | "" (remove) | None (keep).

    Removal is careful with prose: a tag on a line of nothing but tags, or
    opening or closing a line, is deleted with its space; a tag mid-sentence
    loses only its hash ("talked about #buddhism today" -> "talked about buddhism today"),
    because deleting the word would change what was written."""
    out, pos = [], 0
    for m, t in list(_tag_matches(body)):
        new = fn(t)
        if new is None:
            continue
        out.append(body[pos:m.start()])
        pos = m.end()
        if new:
            out.append("#" + new)
            continue
        line_start = body.rfind("\n", 0, m.start()) + 1
        line_end = body.find("\n", m.end())
        line_end = len(body) if line_end == -1 else line_end
        rest = body[m.end():line_end]
        # A tag opening a line, closing it, or on a line of only tags is a
        # LABEL ("#todo fix the sink", "buy stand #todo") and goes; one in the
        # middle of a sentence is a word and only loses its hash.
        if _TAGLINE.match(body[line_start:line_end]) or not rest.strip() \
                or _TRAILING_TAGS.fullmatch(rest) or m.start() == line_start:
            # drop it with one side's whitespace: the space before it, or —
            # when it opens the line — the space after it
            prev = "".join(out)
            if m.start() == line_start:
                out = [prev]
                while pos < len(body) and body[pos] in " \t":
                    pos += 1
            else:
                out = [prev.rstrip(" \t")]
        else:
            out.append(m.group(1))                     # unhash, keep the word
    out.append(body[pos:])
    text = "".join(out)
    # A line that removal emptied goes too (edits never add or remove newlines,
    # so the old and new lines pair up one to one). Blank lines that were
    # already blank stay.
    before, after = body.split("\n"), text.split("\n")
    if len(before) == len(after):
        text = "\n".join(a for b, a in zip(before, after) if a.strip() or not b.strip())
    return text


def _write_body(path: Path, new_body: str):
    fm_lines, _, had = fm.split(path)
    text = path.read_text()
    trailing = text.endswith("\n")
    out = ("\n".join(["---", *fm_lines, "---"]) + "\n" + new_body) if had else new_body
    if trailing and not out.endswith("\n"):
        out += "\n"
    path.write_text(out)


# Below this length an edit distance of 1 is noise, not a typo: ui/up/id are
# all one edit apart and all mean different things.
_NEAR_MIN_LEN = 4


def classify_tag(raw: str, vocab: dict) -> dict:
    """Where one proposed tag sits against the vocabulary already in use.

      exact    already a tag, spelled the same way            → apply
      variant  the same idea, spelled differently             → apply the
                                                                EXISTING spelling
      near     close enough to be a typo or a near-duplicate  → refuse, list
                                                                candidates
      new      nothing like it                                → apply, and say
                                                                it is new

    `variant` resolving to the vocabulary's spelling rather than the caller's
    is the load-bearing choice: it means the first spelling of an idea wins and
    every later caller converges on it, without anyone having to look it up.

    A hierarchical relative is NEVER near: `blog` and `blog/kids` are a parent
    and a child, and `cl stream tag blog` already returns both. Treating that
    as a collision would make the hierarchy unusable."""
    tag = norm_tag(raw)
    out = {"input": str(raw or "").strip(), "tag": tag, "verdict": "new",
           "candidates": []}
    if not tag:
        out["verdict"] = "empty"
        return out
    if tag in vocab:
        out["verdict"] = "exact"
        return out

    key = _cmp_key(tag)
    for known in vocab:
        if _cmp_key(known) == key:
            out.update(verdict="variant", tag=known, candidates=[known])
            return out

    # LEAF NAME -> the full path, when there is only one path it could mean.
    #
    # Writing `#grocery` and having it land in `#shopping/grocery` is the whole
    # ergonomic case for a hierarchy: the prefix is the part you already know
    # and the least interesting to type, and a capture door where you must
    # spell the whole path is one you stop using. So a bare word that is not
    # itself a tag resolves to the single existing tag ending in it — reported
    # as a `variant`, because that is exactly what it is: the same idea,
    # spelled shorter, applied under the vocabulary's own spelling.
    #
    # Compared on _cmp_key, so `groceries` reaches `shopping/grocery` by the
    # same stemming every other comparison here uses.
    #
    # AMBIGUITY IS A REFUSAL, not a guess. Once `errands/grocery` exists too,
    # `#grocery` stops having one answer, and picking the older or the bigger
    # one would file notes somewhere you did not choose. The candidates say
    # which paths it could have been — the same shape the near-duplicate
    # refusal already takes, so nothing new has to be learned to read it.
    if "/" not in tag:
        leaves = [k for k in vocab
                  if "/" in k and _cmp_key(k.rsplit("/", 1)[1]) == key]
        if len(leaves) == 1:
            out.update(verdict="variant", tag=leaves[0], candidates=leaves)
            return out
        if len(leaves) > 1:
            out.update(verdict="near", candidates=sorted(leaves)[:5])
            return out

    # PREFIX -> the tag it is the beginning of.
    #
    # `hear` is not a new idea when `hearth` exists, and neither is `journ`,
    # `buddh` or `zeb`. Levenshtein cannot see these: `hear`/`hearth` is
    # distance 2, and the threshold below is 1 for anything short. So typing
    # the first few letters and stopping used to MINT A TAG, which is a large
    # part of how the vocabulary reached 236 words before the 2026-09-08 cull.
    #
    # Same shape as the leaf rule above: one answer resolves, several refuse.
    # Three characters minimum, or every two-letter typo drags in half the
    # vocabulary.
    if len(tag) >= 3:
        pre = [k for k in vocab
               if k != tag and k.startswith(tag) and not k.startswith(tag + "/")]
        if len(pre) == 1:
            out.update(verdict="variant", tag=pre[0], candidates=pre)
            return out
        if len(pre) > 1:
            out.update(verdict="near", candidates=sorted(pre)[:5])
            return out

    # CONTAINS AN EXISTING TAG as a whole word-component: `ai-tooling` is `ai`
    # with a qualifier bolted on, and the qualifier belongs in the note. This
    # is the other half of the same erosion -- a new tag that quietly shadows
    # one already in use. Refused with the tag it shadows, never auto-applied:
    # sometimes the compound really is its own idea, and that is a judgement.
    parts = [x for x in re.split(r"[-/]", tag) if x]
    if len(parts) > 1:
        shadow = sorted({k for k in vocab if k in parts})
        if shadow:
            out.update(verdict="near", candidates=shadow[:5])
            return out

    near = []
    for known in vocab:
        if tag.startswith(known + "/") or known.startswith(tag + "/"):
            continue                      # parent/child, not a collision
        if max(len(tag), len(known)) < _NEAR_MIN_LEN:
            continue
        d = _lev(tag, known)
        if d <= (1 if min(len(tag), len(known)) < 6 else 2):
            near.append((d, known))
    if near:
        near.sort()
        out.update(verdict="near", candidates=[n for _d, n in near[:5]])
    return out


def resolve_tags(raws, vocab, *, allow_new=False) -> tuple[list, list, list]:
    """Run every proposed tag through the guard.

    Returns (tags-to-apply, notes-for-the-human, blockers). A non-empty
    blocker list means apply NOTHING — a partial tagging that silently dropped
    the one tag you were unsure about is worse than a refusal that says so."""
    apply, notes, blocked = [], [], []
    for raw in raws:
        c = classify_tag(raw, vocab)
        v = c["verdict"]
        if v == "empty":
            continue
        if v == "exact":
            apply.append(c["tag"])
        elif v == "variant":
            apply.append(c["tag"])
            if c["tag"] != norm_tag(raw):
                notes.append(f"'{c['input']}' → existing tag '{c['tag']}'")
        elif v == "near" and not allow_new:
            blocked.append(c)
        elif v == "near":
            apply.append(c["tag"])
            notes.append(f"'{c['tag']}' created despite {', '.join(c['candidates'])}")
        else:
            apply.append(c["tag"])
            notes.append(f"'{c['tag']}' is a NEW tag")
    return list(dict.fromkeys(apply)), notes, blocked


def load(include_daily=False, dirs=None) -> list:
    """Every stream note as a plain dict. Cheap enough to do on every call —
    the stream is a few hundred small files — which keeps every face reading
    live state instead of a cache that can be stale or wrong."""
    items = []
    today = date.today()
    paths = sorted(p for d in (dirs or (STREAM,)) if d.is_dir() for p in d.rglob("*.md"))
    for path in paths:
        if path.name.startswith((".", "_")) or path.name == "README.md":
            continue
        if not include_daily and _DAILY.match(path.stem):
            continue          # the daily note is a writing surface, not an item
        try:
            meta = fm.read(path)
            _, body, _ = fm.split(path)
        except OSError:
            continue
        # `captured:` wins over `created:` where both exist — the stream's own
        # rule, and some notes carry both with different values. `date:` is a
        # fourth spelling three hand-written notes use; read-tolerant here,
        # because the alternative is that they sort to the top of every view
        # with no age forever. Nothing WRITES it — kb-inbox emits `created:`.
        created = _norm_dt(meta.get("captured") or meta.get("created")
                           or meta.get("date"))
        status = str(meta.get("status", "") or "").strip().lower()
        when = _norm_date(meta.get("when"))
        # `written:` is when the words were first put down, for a note that
        # reached the store later — a notebook page transcribed years on. Only
        # `chrono` sorts on it; every recency view stays on `created`, because
        # "what came in lately" is still a question about the store.
        written = _norm_date(meta.get("written"))
        items.append({
            "slug": path.stem,
            "path": str(path),
            "relpath": str(path.relative_to(KB)),
            "title": _title(body),
            # A tag is a tag wherever it is written — frontmatter or body
            # (see "body tags" above). `fm_tags`/`body_tags` say where, for the
            # writers that have to change it.
            "tags": list(dict.fromkeys(
                [norm_tag(t) for t in _tags(meta)] + body_tags(body))),
            "fm_tags": _tags(meta),
            "body_tags": body_tags(body),
            "status": status,
            "when": when.isoformat() if when else None,
            "written": written.isoformat() if written else None,
            "created": created.strftime("%Y-%m-%d %H:%M") if created else None,
            "age_days": (today - created.date()).days if created else None,
        })
    return items


# ── grouping ───────────────────────────────────────────────────────────────

AGENDA_KEYS = ("sooner", "later")


def in_pool(it) -> bool:
    """On the plate at all. A note carrying only `todo/sooner` counts — the
    child implies the parent, and requiring both would make a hand-typed
    `tags: [todo/sooner]` vanish from the one view that exists to hold it."""
    return any(t == POOL_TAG or t.startswith(POOL_TAG + "/")
               for t in (norm_tag(x) for x in it["tags"]))


def is_sooner(it) -> bool:
    return SOONER in (norm_tag(x) for x in it["tags"])


def _line_ages(today=None):
    """{0-based line index: days since that line of todo.md was written}, from
    `git blame` — todo.md carries no dates, and git already knows when each line
    arrived. A line not yet committed is 0 days old. Blame failing (no git, no
    file) means every age is 0, never a crash: age only sorts and nudges."""
    import subprocess
    today = today or date.today()
    try:
        out = subprocess.run(["git", "-C", str(TODO.parent), "blame", "--line-porcelain",
                              "--", TODO.name], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ages, n = {}, 0
    for ln in out.splitlines():
        if ln.startswith("author-time "):
            ages[n] = max(0, (today - date.fromtimestamp(int(ln.split()[1]))).days)
            n += 1
    return ages


def todo_items(today=None):
    """Open todo.md lines as agenda rows. todo.py owns the parsing; this only
    shapes rows into the keys every consumer already reads (nudge, Surface
    /review, kbview, Hermes): slug/title/age_days/tags. `slug` is the text
    slugified — `cl todo done <slug>` matches it word for word."""
    ages = _line_ages(today)
    rows = []
    for it in todo.open_items():
        slug = re.sub(r"[^a-z0-9]+", "-", it["text"].lower()).strip("-")[:80] or "todo"
        rows.append({"slug": slug, "title": it["text"], "text": it["text"],
                     "band": it["band"], "path": str(TODO),
                     "relpath": str(TODO.relative_to(KB)) if TODO.is_relative_to(KB) else str(TODO),
                     "tags": [], "age_days": ages.get(it["line"], 0)})
    return rows


def agenda_groups(items, today=None):
    """The two bands of todo.md (2026-10-05: the todo list is a file, not a tag).
    `items` is ignored and kept only for call compatibility."""
    groups = {k: [] for k in AGENDA_KEYS}
    for it in todo_items(today):
        groups["sooner" if it["band"] == "Sooner" else "later"].append(it)
    return groups


def _old_agenda_groups(items, today=None):
    """Split the pool into the two bands. Order is the order you read them in."""
    groups = {k: [] for k in AGENDA_KEYS}
    for it in items:
        if not in_pool(it):
            continue
        groups["sooner" if is_sooner(it) else "later"].append(it)
    # Oldest first in both bands: the thing that has been sitting longest is
    # the thing a glance should land on, and it is the only ordering the data
    # actually supports now that nothing carries a date.
    for k in AGENDA_KEYS:
        groups[k].sort(key=lambda i: -(i["age_days"] or 0))
    return groups


# ── writing ────────────────────────────────────────────────────────────────

def find(slug, items=None):
    """Exact stem, else unique substring. Ambiguity is an error, never a guess —
    the whole point of file-as-identity is that it can't pick the wrong note."""
    items = items if items is not None else load()
    exact = [i for i in items if i["slug"] == slug]
    if exact:
        return exact[0]
    part = [i for i in items if slug.lower() in i["slug"].lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise SystemExit(f"cl stream: no stream note matching '{slug}'")
    names = "\n  ".join(i["slug"] for i in part[:10])
    raise SystemExit(f"cl stream: '{slug}' matches {len(part)} notes:\n  {names}")


def apply_set(item, *, add_tags=None, rm_tags=None):
    """One targeted frontmatter edit. fm.set_fields rewrites only the keys named
    and leaves the body plus untouched keys byte-identical — which is why no
    surface here ever needs to re-emit a whole note.

    Removal exists because tagging is now how a note gets placed. While `--tag`
    was the only verb, every write was additive and a wrong tag was permanent
    short of opening the file — survivable while you were the only one tagging,
    not once anything proposes tags for you. The fix for a bad suggestion has
    to be another command."""
    path = Path(item["path"])
    updates = {}
    fm_tags = list(item.get("fm_tags", item["tags"]))
    have = {norm_tag(t) for t in item["tags"]}
    if add_tags:
        # Added tags go in frontmatter — unless the note already carries them
        # anywhere, body included.
        fm_tags = list(dict.fromkeys(fm_tags + [t for t in add_tags if norm_tag(t) not in have]))
    drop = {norm_tag(t) for t in rm_tags or []}
    if drop:
        fm_tags = [t for t in fm_tags if norm_tag(t) not in drop]
    if fm_tags != list(item.get("fm_tags", item["tags"])):
        updates["tags"] = fm_tags
    body_hit = drop & set(item.get("body_tags", []))
    if not updates and not body_hit:
        return {}
    if updates:
        fm.set_fields(path, updates)
    if body_hit:
        # Removing a tag that lives in the body means editing the body.
        _, body, _ = fm.split(path)
        _write_body(path, _edit_body_tags(body, lambda t: "" if t in drop else None))
        updates["body_untagged"] = sorted(body_hit)
    return updates


def retag_plan(items, old, new):
    """Every note that `retag old -> new` would touch, and what it becomes.

    A tag is a NAMESPACE, so renaming `blog` carries its children: `blog/kids`
    becomes `writing/blog/kids`, not an orphan under the old parent. Renaming
    only the exact tag is the failure this function exists to prevent — it
    leaves a namespace half-moved, which no view can show as one thing again.

    `new` empty means REMOVE the tag and its children. That is how a one-off
    coined tag gets taken back out of the vocabulary.

    Returns a list of {slug, path, before, after} for notes that actually
    change. A merge onto an existing tag is not special-cased: two tags
    becoming one is the same write as a rename, minus the surprise.
    """
    old = norm_tag(old)
    new = norm_tag(new) if new else ""
    if not old:
        raise ValueError("retag: no tag to rename")
    if old == new:
        return []
    plan = []
    for it in items:
        before = [norm_tag(t) for t in it["tags"]]
        after = []
        for t in before:
            if t == old or t.startswith(old + "/"):
                if not new:
                    continue                       # removal
                t = new + t[len(old):]
            if t not in after:                     # a merge can collide
                after.append(t)
        if after != before:
            plan.append({"slug": it["slug"], "path": it["path"],
                         "before": before, "after": after, "old": old, "new": new})
    return plan


def apply_retag(plan):
    """Write a plan out. Whole-list `tags:` writes, one note at a time, through
    fm.set_fields — so a note the plan did not name is not opened at all."""
    for row in plan:
        path = Path(row["path"])
        old, new = row.get("old"), row.get("new", "")
        meta = fm.read(path)
        fm_before = [norm_tag(t) for t in _tags(meta)]
        fm_after = []
        for t in fm_before:
            if old and (t == old or t.startswith(old + "/")):
                if not new:
                    continue
                t = new + t[len(old):]
            if t not in fm_after:
                fm_after.append(t)
        if fm_after != fm_before:
            fm.set_fields(path, {"tags": fm_after})
        if old:
            _, body, _ = fm.split(path)
            def swap(t):
                if t == old or t.startswith(old + "/"):
                    return (new + t[len(old):]) if new else ""
                return None
            nb = _edit_body_tags(body, swap)
            if nb != body:
                _write_body(path, nb)
    return len(plan)


# ── rendering ──────────────────────────────────────────────────────────────

_LABELS = {"sooner": "sooner", "later": "on the plate"}


def _color(on):
    if not on:
        return lambda s, _c: s
    codes = {"dim": "2", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}
    return lambda s, c: f"\033[{codes[c]}m{s}\033[0m"


def render_agenda(groups, today=None, color=False, show_pool=True):
    today = today or date.today()
    c = _color(color)
    out = [c(f"  AGENDA{' ' * 34}{today.strftime('%-d %b, %A')}", "bold"), ""]
    for key in AGENDA_KEYS:
        rows = groups[key]
        if not rows or (key == "later" and not show_pool):
            continue
        label = f"{_LABELS[key]} ({len(rows)})"
        out.append(c(f"  ── {label} " + "─" * max(0, 56 - len(label)), "dim"))
        for it in rows:
            age = f"{it['age_days']}d" if it["age_days"] is not None else ""
            mark = c("⚠", "red") if key == "sooner" else c("·", "dim")
            # The pool tags themselves are the band you are already reading —
            # printing them on every row says nothing and buries the tags that
            # do (`#home`, `#blog/x`).
            tags = " ".join("#" + t for t in it["tags"]
                            if norm_tag(t) not in (POOL_TAG, SOONER))
            line = f"  {mark} {age:<7} {it['title']}"
            if tags:
                pad = max(1, 66 - len(f"  · {age:<7} {it['title']}"))
                line += " " * pad + c(tags, "cyan")
            out.append(line)
        out.append("")
    if not any(groups[k] for k in groups):
        out.append(c("  todo.md is empty.", "dim"))
        out.append(c("  add one:  cl todo add [--sooner] TEXT", "dim"))
        out.append("")
    return "\n".join(out)


def render_list(rows, color=False, header=""):
    c = _color(color)
    out = ([c(f"  {header}", "bold"), ""] if header else [])
    for it in rows:
        tags = " ".join("#" + t for t in it["tags"])
        age = f"{it['age_days']}d" if it["age_days"] is not None else ""
        out.append(f"  {c(age.rjust(5), 'dim')}  {it['title'][:58]:<58} {c(tags, 'cyan')}")
    if not rows:
        out.append(c("  (nothing)", "dim"))
    return "\n".join(out) + "\n"


def render_chrono(rows, color=False, header=""):
    c = _color(color)
    out = ([c(f"  {header}", "bold"), ""] if header else [])
    for it in rows:
        tags = " ".join("#" + t for t in it["tags"])
        out.append(f"  {c((it['on'] or '?').rjust(10), 'dim')}  "
                   f"{it['title'][:54]:<54} {c(tags, 'cyan')}")
    if not rows:
        out.append(c("  (nothing)", "dim"))
    return "\n".join(out) + "\n"


# ── cli ────────────────────────────────────────────────────────────────────

def _emit(payload, args, text):
    if args.json:
        print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
    else:
        print(text)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cl stream",
        description="views over the capture stream. a view is a query, not a file.")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("agenda", help="todo items grouped by intent date")
    a.add_argument("--json", action="store_true")
    a.add_argument("--no-pool", action="store_true", help="hide the undated pool")

    i = sub.add_parser("inbox", help="untagged notes — nobody has decided yet")
    i.add_argument("--json", action="store_true")

    t = sub.add_parser("tag", help="every note carrying a tag (hierarchical)")
    t.add_argument("tag")
    t.add_argument("--json", action="store_true")

    l = sub.add_parser("ls", help="filter the whole stream")
    l.add_argument("--status", default=None)
    l.add_argument("--tag", default=None)
    l.add_argument("--stale", type=int, default=None, metavar="DAYS")
    l.add_argument("--json", action="store_true")

    ch = sub.add_parser("chrono",
                        help="notes in the order they were WRITTEN, oldest first")
    ch.add_argument("--tag", default=None)
    ch.add_argument("--json", action="store_true")

    v = sub.add_parser("tags", help="the tag vocabulary in use, most-used first")
    v.add_argument("--json", action="store_true")

    s = sub.add_parser("set", help="edit one note's pool membership / tags")
    s.add_argument("slug")
    s.add_argument("--todo", action="store_true", help="put it on the plate")
    s.add_argument("--sooner", action="store_true", help="on the plate, urgent")
    s.add_argument("--later", action="store_true",
                   help="on the plate, no longer urgent")
    s.add_argument("--done", action="store_true",
                   help="take it off the plate (the note stays)")
    s.add_argument("--tag", default=None, help="comma-separated tags to ADD")
    s.add_argument("--untag", default=None, help="comma-separated tags to REMOVE")
    s.add_argument("--new", action="store_true",
                   help="allow a tag the guard flagged as a near-duplicate")
    s.add_argument("--json", action="store_true")

    bt = sub.add_parser("bodytags",
                        help="hashtags in TEXT that already name a tag in use")
    bt.add_argument("text", nargs="?", default="",
                    help="the text; omit to read stdin")
    bt.add_argument("--json", action="store_true")

    rt = sub.add_parser("retag", help="rename or merge a tag across the whole kb")
    rt.add_argument("old")
    rt.add_argument("new", nargs="?", default="",
                    help="the new name; omit (or 'none') to REMOVE the tag")
    rt.add_argument("--apply", action="store_true",
                    help="actually write. without it this only shows the plan")
    rt.add_argument("--json", action="store_true")

    r = sub.add_parser("render", help="write the read-only agenda artifact to outbox/")
    r.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd, args.json, args.no_pool = "agenda", False, False

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    if args.cmd in ("tag", "tags", "set", "bodytags", "retag"):
        # Retired, not removed: exit 0 with an empty answer of the old shape.
        if getattr(args, "json", False):
            empty = {"tag": [], "tags": {"tags": []}, "bodytags": {"tags": []}}
            print(json.dumps(empty.get(args.cmd, {"ok": True, "retired": RETIRED})))
        else:
            print(RETIRED)
        return

    items = load()

    if args.cmd == "agenda":
        g = agenda_groups(items)
        _emit({"today": date.today().isoformat(),
               "groups": [{"key": k, "label": _LABELS[k], "items": g[k]}
                          for k in AGENDA_KEYS]},
              args, render_agenda(g, color=color, show_pool=not args.no_pool))

    elif args.cmd == "inbox":
        # The inbox is working/ (2026-10-05): every capture lands there, one file
        # each, until it is filed out. Newest first.
        rows = load(include_daily=True, dirs=(WORKING,))
        rows.sort(key=lambda i: i["created"] or "", reverse=True)
        _emit(rows, args, render_list(rows, color, f"WORKING ({len(rows)})"))

    elif args.cmd == "tag":
        q = args.tag.lstrip("#").rstrip("/")
        rows = [i for i in items
                if any(t == q or t.startswith(q + "/") for t in i["tags"])]
        rows.sort(key=lambda i: i["created"] or "", reverse=True)
        _emit(rows, args, render_list(rows, color, f"#{q} ({len(rows)})"))

    elif args.cmd == "chrono":
        # include_daily: a daily note is a journal entry, and a timeline of the
        # journal that skipped them would have holes in it.
        # writing/ working/ threads/ — not archive/, which is the past by intent.
        if args.tag:
            print(RETIRED, file=sys.stderr)
        rows = load(include_daily=True, dirs=(WRITING, WORKING, THREADS))
        # A note with no `written:` was written when it was captured, so the
        # typed journal and the transcribed notebooks share one timeline.
        for i in rows:
            i["on"] = i["written"] or (i["created"] or "")[:10] or None
        rows.sort(key=lambda i: (i["on"] or "", i["created"] or ""))
        head = "KB"
        _emit(rows, args, render_chrono(rows, color, f"{head} by date written ({len(rows)})"))

    elif args.cmd == "tags":
        # include_daily: `journal` is the most-used tag in the system and lives
        # only on notes every other view here filters out.
        vocab = vocabulary(load(include_daily=True))
        c = _color(color)
        lines = [c(f"VOCABULARY ({len(vocab)} tags)", "bold")]
        if not vocab:
            lines.append(c("  nothing tagged yet", "dim"))
        width = max((len(t) for t in vocab), default=0)
        for t, n in vocab.items():
            kids = [k for k in vocab if k.startswith(t + "/")]
            tail = c(f"  +{len(kids)} under it", "dim") if kids else ""
            lines.append(f"  {c(t.ljust(width), 'cyan')}  "
                         f"{c(str(n).rjust(3), 'dim')}{tail}")
        _emit({"tags": [{"tag": t, "count": n} for t, n in vocab.items()]},
              args, "\n".join(lines))

    elif args.cmd == "ls":
        rows = items
        if args.status is not None:
            rows = [i for i in rows if i["status"] == args.status]
        if args.tag:
            q = args.tag.lstrip("#")
            rows = [i for i in rows
                    if any(t == q or t.startswith(q + "/") for t in i["tags"])]
        if args.stale is not None:
            rows = [i for i in rows if (i["age_days"] or 0) >= args.stale]
        rows.sort(key=lambda i: i["created"] or "", reverse=True)
        _emit(rows, args, render_list(rows, color, f"STREAM ({len(rows)})"))

    elif args.cmd == "set":
        # include_daily: excluding daily notes is right for a VIEW — a daily
        # note is a writing surface, not an agenda item or something to route.
        # It is wrong for a WRITE. There is no reason a daily note cannot carry
        # tags (most of them do), and `find` searching only the view meant
        # `cl stream set 2026-07-17 --tag journal` answered "no stream note
        # matching", which is false: the note is right there.
        it = find(args.slug, load(include_daily=True))
        raw_add = [x for x in (args.tag or "").split(",") if x.strip()]
        rm = [norm_tag(x) for x in (args.untag or "").split(",") if x.strip()]

        # Pool membership is spelled in tags like everything else, but it does
        # NOT go through resolve_tags: the near-duplicate guard exists to stop
        # a vocabulary growing a second word for one idea, and `todo/sooner`
        # beside `todo` is exactly the shape it is built to flag. These two are
        # the vocabulary, not a proposal against it.
        pool_add, pool_rm = [], []
        if args.done:
            pool_rm += [POOL_TAG, SOONER]
        if args.todo:
            pool_add.append(POOL_TAG)
        if args.sooner:
            pool_add.append(SOONER)
            pool_rm.append(POOL_TAG)
        if args.later:
            pool_add.append(POOL_TAG)
            pool_rm.append(SOONER)

        add, notes, blocked = resolve_tags(
            raw_add, vocabulary(load(include_daily=True)), allow_new=args.new)
        if blocked:
            # Refuse the WHOLE command, not just the flagged tag. A partial
            # apply that quietly dropped the one tag you were unsure about
            # reads as success and leaves the note half-placed.
            payload = {"ok": False, "error": "near-duplicate tag(s)",
                       "blocked": blocked}
            if args.json:
                print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
            else:
                for b in blocked:
                    print(f"  '{b['input']}' looks like: "
                          f"{', '.join(b['candidates'])}")
                print("  nothing written. use one of those, or --new to "
                      "create it anyway.")
            raise SystemExit(2)

        # Removing a tag the note does not carry is a mistake worth naming —
        # silence here means a typo'd --untag reports success and changes
        # nothing, which is the failure mode this flag exists to end.
        missing = [t for t in rm if t not in [norm_tag(x) for x in it["tags"]]]

        # --done wins over --todo if both are passed: the removals are applied
        # after the additions in apply_set, so say so here rather than leave it
        # to that ordering.
        add = [t for t in list(add) + pool_add if t not in pool_rm]
        rm = list(dict.fromkeys(rm + pool_rm))
        got = apply_set(it, add_tags=add or None, rm_tags=rm or None)

        # Coming off the plate with nothing else on it makes the note UNTAGGED,
        # which is not merely unfiled: `cl inbox --prune-noise` feeds on
        # untagged notes, and a just-finished errand reads a lot like noise.
        # Since 2026-09-15 that takes a confident judgement against a rubric
        # that explicitly protects errands, and the trash is restorable by
        # name — so this is a "go back through triage", not a "it evaporated".
        if args.done and "tags" in got and not got["tags"]:
            notes = list(notes) + ["now untagged — it will show in triage; "
                                   "`--prune-noise` may bin it (recover with "
                                   "`cl triage restore`)"]
        changed = ", ".join(f"{k}: {v or '(cleared)'}" for k, v in got.items())
        text = [f"  {it['title']}", f"  → {changed or 'nothing to change'}"]
        text += [f"  · {n}" for n in notes]
        text += [f"  · not on this note: {t}" for t in missing]
        _emit({"ok": True, "slug": it["slug"], "path": it["path"],
               "applied": got, "notes": notes, "not_present": missing},
              args, "\n".join(text))

    elif args.cmd == "bodytags":
        # The one door (`kb-inbox`) calls this so that "is this a real tag"
        # keeps exactly one implementation. A shell script grepping the store
        # itself would be the fifth hand-rolled tag reader in the system.
        text = args.text if args.text else sys.stdin.read()
        found = body_tags(text, vocabulary(load(include_daily=True)))
        _emit({"tags": found}, args, "\n".join(found))

    elif args.cmd == "retag":
        # include_daily: a rename that skipped daily notes would leave `journal`
        # — the most-used tag in the store — behind on 17 files while claiming
        # to have renamed it everywhere.
        new = "" if args.new.strip().lower() in ("", "none", "-") else args.new
        try:
            plan = retag_plan(load(include_daily=True), args.old, new)
        except ValueError as e:
            raise SystemExit(f"cl stream: {e}")
        verb = "remove" if not new else f"-> #{norm_tag(new)}"
        c = _color(color)
        if not plan:
            _emit({"ok": True, "applied": False, "count": 0, "plan": []},
                  args, c(f"  #{norm_tag(args.old)}: no notes carry it — "
                          f"nothing to {'remove' if not new else 'rename'}", "dim"))
        elif not args.apply:
            # Dry run is the DEFAULT, not a flag. This is the one command here
            # that rewrites many notes at once, and the vocabulary is the thing
            # every view is built on — seeing the blast radius first is the
            # cheapest guard there is.
            lines = [c(f"  #{norm_tag(args.old)} {verb} — "
                       f"{len(plan)} note(s) would change", "bold"), ""]
            for row in plan:
                lines.append(f"  {row['slug'][:44]:<44} "
                             + c(" ".join("#" + t for t in row["after"]) or "(no tags)",
                                 "cyan"))
            lines += ["", c("  nothing written. add --apply to do it.", "dim")]
            _emit({"ok": True, "applied": False, "count": len(plan), "plan": plan},
                  args, "\n".join(lines))
        else:
            n = apply_retag(plan)
            _emit({"ok": True, "applied": True, "count": n, "plan": plan},
                  args, c(f"  #{norm_tag(args.old)} {verb} — {n} note(s) rewritten",
                          "bold"))

    elif args.cmd == "render":
        g = agenda_groups(items)
        OUTBOX.mkdir(parents=True, exist_ok=True)
        dest = OUTBOX / "agenda.md"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        head = ("---\n"
                f"created: {stamp}\n"
                "tags: [report]\n"
                "---\n\n"
                "# Agenda\n\n"
                f"*Derived view, regenerated {stamp}. Do not edit — "
                "edit the notes, or use the agenda view.*\n\n")
        body = []
        for key in AGENDA_KEYS:
            if not g[key]:
                continue
            body.append(f"## {_LABELS[key]}\n")
            for it in g[key]:
                tg = " ".join("#" + t for t in it["tags"]
                              if norm_tag(t) not in (POOL_TAG, SOONER))
                body.append(f"- {it['title']} {tg}".rstrip())
            body.append("")
        dest.write_text(head + "\n".join(body) + "\n")
        _emit({"wrote": str(dest), "items": sum(len(v) for v in g.values())},
              args, f"  wrote {dest}")


if __name__ == "__main__":
    main()
