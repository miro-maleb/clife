#!/usr/bin/env python3
"""kb-lint — health sweep over ~/kb. Report-only; never edits a note.

2026-10-05: rescoped to the folder layout (writing/ working/ threads/ archive/,
paths.NOTE_DIRS). Tags are retired, so records and clusters are told apart by
FOLDER again; subdirectories under writing/ are legitimate, so the old "shard"
check is gone and a duplicate filename across folders is flagged instead (a
[[link]] to it resolves by folder rank — links.DIR_RANK — which may not be the
one you meant). The history below is the flat-store version.

Written against the FLAT kb: one store, ~/kb/notes, placed by frontmatter tags
(flattened 2026-09-07). The version before this scoped everything by directory,
and after the flatten that failed quietly on every check at once — the deadline
check looked for `project.md` files that no longer exist and reported 0 forever,
the contradiction pass saw a single "folder" holding every note and read its
first six alphabetically, and the stale pass read journals and letters as status
reports because the paths that used to exclude them were gone. So scoping here is
by TAG, and tag questions are asked of `stream`, never of hand-walked frontmatter.

Layer 1 — deterministic, no LLM, so nothing in it can hallucinate:
  duplicate-name     one filename in two folders — links resolve to only one
  broken-link        a [[wikilink]] naming no file in the kb, split by what to DO:
    · dissolved-dir       a path-style link whose note still exists under a flat
                          name — the flatten moved the note, the link kept the path
    · memory-slug         names one of Claude's memory files: a Claude-written note
                          linking its own memory. Nothing to create.
    · unresolved-concept  referenced >=2x — probably a note worth writing
                          (Karpathy's "system emits its own todo")
  overdue-deadline   any note whose `deadline:` has passed and `status:` isn't closed
  transcription      a page "originally written by hand" with no `written:` date

Layer 2 — `--deep`, the local model; every item is for review, not trust:
  stale claims       per note, claim-bearing notes only (records excluded by tag)
  contradictions     within a tag's notes, each adversarially verified

Deliberately NOT here, and why:
  orphans   — most of the store (journal, recipes, captures) is not meant to be
              linked; an orphan report would be noise.
  untagged  — that is the inbox VIEW (`cl stream inbox`, triage), not a defect.
  frontmatter schema — `cl lint` owns it.
  Wikilinks inside `code` or fenced blocks are text, not links, and are skipped.

    kb_lint.py                 # scan, write ~/kb/_lint/latest.md + dated, print summary
    kb_lint.py --stdout        # print the report, don't write it
    kb_lint.py --deep          # add Layer 2 (the weekly timer runs this)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the clife root
import fm        # noqa: E402
import stream    # noqa: E402
from paths import KB, NOTE_DIRS, WRITING, ARCHIVE   # noqa: E402

# Surface's /reports serves this directory by name — don't move it.
LINT_DIR = KB / "_lint"

# ── Layer 2 (LLM) config ──────────────────────────────────────────────────────
# The deep pass runs on the tower's local qwen (sovereign, free), overnight, so it
# isn't tuned for speed. think:false — BORROWED, not measured here. The evidence is
# ai-rss's, re-run 2026-08-25: think on vs off scored 18/18 = 18/18 across six frozen
# cases, including its hardest judgment call, at ~5x the runtime. Two of those stages
# (verify, recommend) are shaped like this one — hold a claim against a source and say
# whether it stands — which is why the borrow is reasonable.
#
# Reasonable is not measured. kb-lint has no harness, so nothing here has ever been
# scored either way, and overnight runtime means thinking would cost this job close to
# nothing. That asymmetry is the argument FOR trying it; the standard this project
# holds — measured, not argued — is the reason it stays off until frozen cases exist.
# Build those (a note pair that genuinely contradicts, one that only looks like it
# does) before touching this line.
OLLAMA_HOST = os.environ.get("KB_LINT_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("KB_LINT_MODEL", "resident")
# No NUM_CTX. This job ran 17GB of its own beside the pinned resident because
# ollama keys a loaded model by (name, context) — asking for a different one
# forces a full unload/reload. The tag carries 112k; reuse what is already up.

# A note carrying any of these is a RECORD or a piece of writing, not a standing
# claim: a journal entry, a poem, a letter, a recipe, a blog post, a book's notes.
# These used to be excluded by path (log/, drafts/, recipes/). With one directory the
# path says nothing, and the first flat run flagged a personal letter as
# stale status. Matched hierarchically: `writing` covers `writing/letter`. Daily
# notes and transcriptions (`written:`) are records by construction and excluded too.
RECORD_TAGS = {"journal", "writing", "poem", "recipe", "archive", "blog", "book",
               "shopping", "teaching-story"}
MIN_CLAIM_CHARS = 200          # a stub too short to hold a stale claim
DOC_TRUNC = 8000               # per-doc text fed to the stale pass
CLUSTER_DOC_TRUNC = 3500       # per-doc excerpt in a contradiction cluster
CLUSTER_MAX_DOCS = 6           # biggest tag group fed at once

# A deadline on a note in one of these states is history, not a debt.
CLOSED_STATUSES = {"done", "dropped", "cut", "superseded", "archived", "abandoned",
                   "parked"}

# The wikilink layer lives in links.py — the regex, the flat-store name rule, the
# code masking and the broken/dissolved logic all used to be defined right here.
# They moved out when nvim-write and Surface needed to follow a link too: three
# copies of "what does [[x]] point at" is exactly the shape of the four disagreeing
# tag readers this kb already paid for. kb_lint is now one caller among several.
from links import (SKIP_DIRS, MEMORY_DIR, WIKILINK, build_index,  # noqa: E402,F401
                   scan_links, dissolved, resolves, target as _target,
                   memory_stems as _memory_slugs, md_files as _md_files,
                   skip as _skip)


def _body(md, limit: int) -> str:
    """Note text minus its frontmatter, truncated. Layer 2 feeds the model this."""
    _, body, _ = fm.split(md)
    return body.strip()[:limit]


# ── store checks ──────────────────────────────────────────────────────────────
def scan_dupes() -> list[tuple[str, list[str]]]:
    """One filename in two places under NOTE_DIRS. A [[link]] resolves by name, so
    only one of them is reachable (links.DIR_RANK picks which) — the other is
    either a stale copy or a note that needs a distinct name."""
    seen: dict[str, list[str]] = {}
    for d in NOTE_DIRS:
        if d.is_dir():
            for md in sorted(d.rglob("*.md")):
                seen.setdefault(md.name.lower(), []).append(str(md.relative_to(KB)))
    return sorted((n, ps) for n, ps in seen.items() if len(ps) > 1)


def scan_deadlines(items: list[dict], today: dt.date) -> list[tuple]:
    """Any note with a past `deadline:` whose `status:` is not closed. A project is a
    note tagged projects/<name> now, so there is no project.md to look for."""
    overdue = []
    for it in items:
        if Path(it["path"]).is_relative_to(ARCHIVE):
            continue
        meta = fm.read(Path(it["path"]))
        raw = str(meta.get("deadline") or "").split("#")[0].strip()
        if not raw:
            continue
        status = str(meta.get("status") or "").split("#")[0].strip().lower()
        if status in CLOSED_STATUSES:
            continue
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if d < today:
            overdue.append((it["relpath"], d, status or "no status", (today - d).days))
    return sorted(overdue, key=lambda x: -x[3])


def scan_tag_drift(vocab: dict) -> list[tuple]:
    """Pairs of tags in use that stream's tag guard would refuse to let coexist —
    the same check a capture door runs before minting a tag, run over the
    vocabulary as it stands. A pair here got past the guard by another door: a
    hand edit, a typo in frontmatter, a note older than the guard.

    Returns (smaller, n, larger, n); on a tie the nested spelling is the larger,
    since a hierarchy is what the vocabulary is converging on."""
    pairs: dict[frozenset, tuple] = {}
    for t in vocab:
        others = {k: n for k, n in vocab.items() if k != t}
        c = stream.classify_tag(t, others)
        if c["verdict"] not in ("variant", "near"):
            continue
        for k in c["candidates"]:
            if k.startswith(t + "/") or t.startswith(k + "/"):
                continue                    # parent and child: the hierarchy working
            if k == "projects/" + t or t == "projects/" + k:
                continue                    # a project's tag beside its topic, by design
            pairs.setdefault(frozenset((t, k)), (t, k))
    out = []
    for a, b in pairs.values():
        big, small = sorted((a, b), key=lambda x: (vocab[x], x.count("/"), x),
                            reverse=True)
        out.append((small, vocab[small], big, vocab[big]))
    return sorted(out)


def scan_transcriptions(items: list[dict]) -> list[str]:
    """Pages marked as transcribed that carry no parseable `written:` date. Until
    they do, `cl stream chrono` files them under the day they were typed."""
    out = []
    for it in items:
        if it["written"]:
            continue
        try:
            head = _body(Path(it["path"]), 400).splitlines()[:5]
        except OSError:
            continue
        if any(stream._PROVENANCE.match(line.strip()) for line in head):
            out.append(it["relpath"])
    return sorted(out)


# ── Layer 2: LLM passes (the --deep sweep) ────────────────────────────────────
def _json_list(raw: str, key: str) -> list:
    """Coerce a model JSON reply to a list. It may return {"key": [...]} or a bare
    [...] — accept both; anything else is empty. Raises nothing the caller must catch
    beyond json.JSONDecodeError."""
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        v = data.get(key)
        return v if isinstance(v, list) else []
    return []


def _llm(system: str, user: str, temperature: float = 0.2) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "think": False, "format": "json",
        "options": {"temperature": temperature},
    }
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    c = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", c, flags=re.DOTALL).strip()


def _is_record(it: dict) -> bool:
    """By FOLDER since 2026-10-05: everything under writing/ (journal, poems,
    letters, blog, stories, dreams/ideas) and archive/ is a record, not a claim."""
    p = Path(it["path"])
    return (bool(stream._DAILY.match(it["slug"])) or bool(it["written"])
            or p.is_relative_to(WRITING) or p.is_relative_to(ARCHIVE))


def _claim_notes(items: list[dict]) -> list[dict]:
    """Substantive, claim-bearing notes only — the LLM's scope."""
    out = []
    for it in items:
        if _is_record(it) or it["path"].endswith(".excalidraw.md"):
            continue
        try:
            if len(_body(Path(it["path"]), MIN_CLAIM_CHARS)) >= MIN_CLAIM_CHARS:
                out.append(it)
        except OSError:
            pass
    return out


def stale_pass(notes: list[dict], today: dt.date, log=lambda s: None) -> list[dict]:
    """Per-doc: flag time-bound claims that today has probably overtaken. O(n), no
    pairing. Conservative by construction — most notes have zero."""
    system = (
        "You audit ONE personal note for STALE STATUS: a claim about the person's CURRENT "
        "situation or PLANS that time has made wrong and that they would want to UPDATE. "
        f"Today is {today.isoformat()}. Worth flagging: 'currently doing X', 'waiting on Y', "
        "'in progress', 'next: Z', a plan or deadline implying work that is now overdue.\n"
        "Do NOT flag — RETURN EMPTY for these, they are records, not stale status:\n"
        "- inherently dated documents: forecasts, astrology/horoscope readings, predictions, "
        "meeting minutes, historical logs. Every line is time-bound BY DESIGN — that is not "
        "staleness. If the whole note is a record of a past period or event, return nothing.\n"
        "- narrative or creative writing (blog/journal prose) in present tense about a past moment.\n"
        "- anything where flagging would just restate 'this described a past period'.\n"
        "Only flag a claim if UPDATING it would genuinely help the person. Most notes have ZERO; "
        "an empty list is the common, correct answer. Never invent. Reply ONLY as JSON."
    )
    out = []
    for it in notes:
        rel = it["relpath"]
        date = it["written"] or it["created"] or "unknown"
        user = (f"Note: {rel}\nWritten: {date}\nToday: {today.isoformat()}\n\n"
                f"{_body(Path(it['path']), DOC_TRUNC)}\n\n"
                'Return JSON: {"stale": [{"claim": "<short quote>", "why": "<one line>"}]}')
        try:
            found = [i for i in _json_list(_llm(system, user), "stale")
                     if isinstance(i, dict) and i.get("claim")]
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            log(f"  stale skip {rel}: {e}")
            continue
        if found:
            log(f"  {rel}: {len(found)} stale")
            out.append({"file": rel, "items": found})
    return out


def _clusters(notes: list[dict]) -> list[tuple[str, list[dict]]]:
    """Claim notes grouped by TAG, the flat store's topical unit. Smallest groups
    first: a specific tag is the likeliest place for two notes to state one fact two
    ways. A big tag is capped to its newest notes, since a contradiction is usually a
    recent note overtaking an older one."""
    by_tag: dict[str, list[dict]] = {}
    for it in notes:
        # Tags are retired (2026-10-05): the folder is the cluster now.
        by_tag.setdefault(str(Path(it["relpath"]).parent), []).append(it)
    groups = []
    for t, its in sorted(by_tag.items(), key=lambda kv: (len(kv[1]), kv[0])):
        if len(its) >= 2:
            newest = sorted(its, key=lambda i: i["created"] or "", reverse=True)
            groups.append((t, newest[:CLUSTER_MAX_DOCS]))
    return groups


def contradiction_pass(notes: list[dict], log=lambda s: None) -> list[dict]:
    """Within each tag cluster, ask for factual contradictions, then adversarially
    VERIFY each before reporting — the ai-rss fact-check lesson: a fresh skeptic
    told to default to 'not a contradiction' kills the plausible-but-wrong ones."""
    find_sys = (
        "You find CONTRADICTIONS across a person's related notes: two places asserting "
        "incompatible FACTS about the same thing (status, decision, number, name, date). "
        "Quote both sides. Do NOT flag: different topics, complementary detail, or a clearly "
        "dated decision that was later revised (that is history, not conflict). Most groups "
        "have NONE — an empty list is the common, correct answer. Reply ONLY as JSON."
    )
    verify_sys = (
        "You are a skeptic checking a claimed contradiction between two notes. Default to "
        "NOT a contradiction. It is real ONLY if both statements are about the same thing and "
        "cannot both be true now. Superseded-over-time, different scope, or vagueness = not a "
        "contradiction. Reply ONLY as JSON."
    )
    out, seen = [], set()
    for tag, group in _clusters(notes):
        blob = "\n\n".join(f"=== {it['slug']} ===\n{_body(Path(it['path']), CLUSTER_DOC_TRUNC)}"
                           for it in group)
        user = (f"Notes sharing the tag #{tag}:\n\n{blob}\n\n"
                'Return JSON: {"conflicts": [{"a": "<quote from one note>", '
                '"b": "<quote from another>", "issue": "<one line>"}]}')
        try:
            found = _json_list(_llm(find_sys, user), "conflicts")
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            log(f"  contra skip #{tag}: {e}")
            continue
        for c in found:
            if not (isinstance(c, dict) and c.get("a") and c.get("b")):
                continue
            key = frozenset((c["a"], c["b"]))      # one note pair can share several tags
            if key in seen:
                continue
            seen.add(key)
            vuser = (f"Note A says: {c['a']}\nNote B says: {c['b']}\n"
                     f"Claimed issue: {c.get('issue','')}\n\n"
                     'Return JSON: {"real": <true|false>, "why": "<one line>"}')
            try:
                v = json.loads(_llm(verify_sys, vuser, temperature=0.1))
            except (requests.RequestException, json.JSONDecodeError, ValueError):
                continue
            if isinstance(v, dict) and v.get("real"):
                log(f"  #{tag}: contradiction confirmed")
                out.append({"tag": tag, **c, "why": v.get("why", "")})
    return out


# ── report ────────────────────────────────────────────────────────────────────
def _refs(refs: list[tuple], cap: int = 6) -> str:
    s = ", ".join(f"`{f}:{ln}`" for f, ln, _ in refs[:cap])
    return s + (f" +{len(refs) - cap} more" if len(refs) > cap else "")


def render(r: dict, today: dt.date,
           stale: list | None = None, contradictions: list | None = None) -> str:
    broken = r["broken"]
    memory, moved, concepts, typos = {}, {}, {}, {}
    for t, refs in broken.items():
        gone, hint = dissolved(t, r["stems"])
        if t.split("/")[-1].lower() in r["memory"]:
            memory[t] = refs
        elif gone:
            moved[t] = (refs, hint)
        elif len(refs) >= 2:
            concepts[t] = refs
        else:
            typos[t] = refs
    total_broken = sum(len(v) for v in broken.values())
    deep = stale is not None or contradictions is not None
    stale = stale or []
    contradictions = contradictions or []
    stale_n = sum(len(s["items"]) for s in stale)

    p = [f"# kb-lint — {today.isoformat()}\n",
         "*Report only — nothing was changed."
         + (" Deep pass (local qwen) ran.*\n" if deep else " Deterministic, no AI.*\n"),
         "## Summary\n",
         f"- **{len(r['dupes'])}** filename(s) present in more than one folder",
         f"- **{total_broken}** broken wikilink(s) across **{len(broken)}** target(s): "
         f"**{len(concepts)}** unresolved concept(s), **{len(moved)}** into dissolved "
         f"directories, **{len(memory)}** to Claude's memory, **{len(typos)}** one-off(s)",
         f"- **{len(r['overdue'])}** overdue deadline(s)",
         f"- **{len(r['transcriptions'])}** transcription(s) without `written:`"]
    if deep:
        p.append(f"- **{stale_n}** possible stale claim(s) in {r['claim_n']} claim-bearing "
                 f"notes · **{len(contradictions)}** verified contradiction(s) "
                 "*(AI, review each)*")
    p.append("")

    if r["dupes"]:
        p.append("## One name, several folders — fix first\n")
        p.append("*A [[link]] resolves by name to only one of these "
                 "(writing > threads > working > archive). Rename or remove the other.*\n")
        for n, ps in r["dupes"]:
            p.append(f"- `{n}` — " + ", ".join(f"`{x}`" for x in ps))
        p.append("")

    if r["overdue"]:
        p.append("## Overdue deadlines\n")
        for f, d, status, days in r["overdue"]:
            p.append(f"- **{d.isoformat()}** ({days}d ago, `{status}`) — `{f}`")
        p.append("")

    if r["transcriptions"]:
        p.append("## Transcriptions without `written:`\n")
        p.append("*They sort under the day they were typed until they carry the date "
                 "they were written.*\n")
        for f in r["transcriptions"]:
            p.append(f"- `{f}`")
        p.append("")

    if concepts:
        p.append("## Unresolved concepts — referenced but never written")
        p.append("*A link you lean on with no home. Create the note, or fix the name.*\n")
        for tgt, refs in sorted(concepts.items(), key=lambda x: (-len(x[1]), x[0])):
            p.append(f"- `[[{tgt}]]` — {len(refs)}×: {_refs(refs)}")
        p.append("")

    if moved:
        p.append("## Links into dissolved directories\n")
        p.append("*The link kept a path the flatten removed. Where a segment still "
                 "names a note, that note is the suggestion; otherwise nothing by that "
                 "name survived.*\n")
        for tgt, (refs, hint) in sorted(moved.items()):
            to = f"→ `[[{hint}]]`" if hint else "→ *no note by that name*"
            p.append(f"- `[[{tgt}]]` {to} — {_refs(refs)}")
        p.append("")

    if memory:
        p.append("## Links to Claude's memory, not the kb\n")
        p.append("*A Claude-written note linking one of its own memory files. Nothing "
                 "to create — unlink it, or put the fact in the note.*\n")
        for tgt, refs in sorted(memory.items(), key=lambda x: (-len(x[1]), x[0])):
            p.append(f"- `[[{tgt}]]` — {_refs(refs)}")
        p.append("")

    if typos:
        p.append("## One-off broken links — likely typos or renames\n")
        for tgt, refs in sorted(typos.items()):
            f, ln, raw = refs[0]
            p.append(f"- `{raw}` — `{f}:{ln}`")
        p.append("")

    if contradictions:
        p.append("## Possible contradictions *(AI-flagged, verified — still check each)*\n")
        for c in contradictions:
            p.append(f"- **`#{c['tag']}`** — {c.get('issue','')}")
            p.append(f"    - A: *{c['a']}*")
            p.append(f"    - B: *{c['b']}*")
        p.append("")

    if stale:
        p.append("## Possible stale claims *(AI-flagged — review, don't auto-trust)*\n")
        for s in stale:
            p.append(f"### `{s['file']}`")
            for it in s["items"]:
                p.append(f"- *{it['claim']}* — {it.get('why','')}")
            p.append("")

    if not (r["dupes"] or broken or r["overdue"] or r["transcriptions"]
            or stale or contradictions):
        p.append("Nothing flagged. The kb is clean. ✓")
    return "\n".join(p) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="kb health lint (report only)")
    ap.add_argument("--stdout", action="store_true", help="print, don't write a file")
    ap.add_argument("--deep", action="store_true",
                    help="add the LLM pass (stale claims + verified contradictions)")
    ap.add_argument("--limit", type=int, help="cap claim notes in --deep (for testing)")
    args = ap.parse_args()

    today = dt.date.today()
    # include_daily: for a sweep, a daily note is a note like any other — the
    # record-vs-claim distinction is made per check, not by leaving files unread.
    items = stream.load(include_daily=True, dirs=NOTE_DIRS)
    names = build_index()
    r = {
        "broken": scan_links(names),
        "stems": {p.stem.lower() for d in NOTE_DIRS if d.is_dir() for p in d.rglob("*.md")},
        "memory": _memory_slugs(),
        "dupes": scan_dupes(),
        "overdue": scan_deadlines(items, today),
        "transcriptions": scan_transcriptions(items),
        "claim_n": 0,
    }

    stale = contradictions = None
    if args.deep:
        log = lambda s: print(s, file=sys.stderr, flush=True)
        notes = _claim_notes(items)
        if args.limit:
            notes = notes[:args.limit]
        r["claim_n"] = len(notes)
        log(f"deep pass: {len(notes)} claim-bearing notes via {MODEL}")
        stale = stale_pass(notes, today, log)
        contradictions = contradiction_pass(notes, log)
    report = render(r, today, stale, contradictions)

    if args.stdout:
        print(report)
        return
    LINT_DIR.mkdir(exist_ok=True)
    (LINT_DIR / f"lint-{today.isoformat()}.md").write_text(report)
    (LINT_DIR / "latest.md").write_text(report)
    total = sum(len(v) for v in r["broken"].values())
    extra = ""
    if args.deep:
        extra = (f", {sum(len(s['items']) for s in (stale or []))} stale, "
                 f"{len(contradictions or [])} contradiction(s)")
    print(f"kb-lint: {len(r['dupes'])} duplicate name(s), {total} broken link(s), "
          f"{len(r['overdue'])} overdue, "
          f"{len(r['transcriptions'])} undated transcription(s){extra} "
          f"-> {LINT_DIR}/latest.md")


if __name__ == "__main__":
    main()
