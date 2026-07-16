#!/usr/bin/env python3
"""kb-lint — deterministic health sweep over ~/kb. Report-only; never edits a note.

Layer 1 (this file): no LLM, so nothing here can hallucinate. It reports only what is
mechanically true — a link points at nothing, a deadline has passed. That is the whole
design: the checks that can't be wrong ship first, and a contradiction/stale-claim pass
(Layer 2, LLM) can layer on later without ever touching these. See
~/kb/ideas/overnight-local-ai-jobs.md.

Checks:
  broken-link       a [[wikilink]] resolving to no file/folder in the vault
    · unresolved-concept   a broken target referenced >=2x — probably a note worth
                           creating, not a typo (Karpathy's "system emits its own todo")
  overdue-deadline  an active/on-hold project.md whose `deadline:` is in the past

Deliberately NOT here, and why:
  orphans      — this kb isn't a zettelkasten; ~85% of files (recipes, logs, weeks,
                 inbox, agent configs) aren't meant to be linked, so orphan reports
                 would be noise. Would need tight scoping to be useful.
  stale review — Surface's Projects tab already flags projects unreviewed in 14d;
                 no point duplicating the nag here.

Resolution note: with no folder-note plugin, Obsidian technically sees [[hearth]] (a
folder, no hearth.md) as unresolved — but it's intentional. So a directory-name match
counts as resolved; otherwise 24 intentional [[hearth]] links would drown the one real
signal ([[calm-interface]], referenced but never written).

    kb_lint.py                 # scan, write ~/kb/_lint/latest.md + dated, print summary
    kb_lint.py --stdout        # print the report, don't write it
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
import yaml

KB = Path(os.path.expanduser("~/kb"))
LINT_DIR = KB / "_lint"

# ── Layer 2 (LLM) config ──────────────────────────────────────────────────────
# The deep pass runs on the tower's local qwen (sovereign, free), overnight, so it
# isn't tuned for speed. think:false — thinking was measured not worth its cost on the
# ai-rss pipeline; revisit only if an eval separates them here.
OLLAMA_HOST = os.environ.get("KB_LINT_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("KB_LINT_MODEL", "qwen3.6:27b")
NUM_CTX = 16384

# Only these top-level dirs hold standing CLAIMS that can go stale or contradict.
# Logs are timestamped records (not claims), recipes/shopping/weeks/inbox are not
# assertions, archive is frozen, agent configs aren't prose. Feeding them the LLM
# would be cost with no signal — the same "scope tightly or drown in noise" lesson.
CLAIM_DIRS = {"projects", "ideas", "notes", "systems", "orientations", "goals"}
MIN_CLAIM_CHARS = 200          # a stub too short to hold a stale claim
DOC_TRUNC = 8000               # per-doc text fed to the stale pass
CLUSTER_DOC_TRUNC = 3500       # per-doc excerpt in a contradiction cluster
CLUSTER_MAX_DOCS = 6           # biggest folder we'll feed at once

# Dirs whose contents we neither scan nor count as link targets. `_lint` is our own
# output (don't lint the linter); `oil:` is an oil.nvim accident (untracked cruft);
# templates carry intentional placeholder links; archive is frozen history.
SKIP_DIRS = {".git", ".obsidian", "_lint", "oil:", "templates", "archive"}

WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
# Non-note link targets that legitimately exist as files in the vault.
KNOWN_EXTS = (".md", ".canvas", ".base", ".excalidraw", ".png", ".jpg", ".pdf")

STALE_PROJECT_STATUSES = {"active", "on-hold"}


def _skip(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


# ── build the resolution index ────────────────────────────────────────────────
def build_index() -> tuple[set[str], set[str]]:
    """Return (resolvable_names, rel_paths). A wikilink resolves if its target hits
    either: a name (file basename in several ext-stripped forms, or a directory name),
    or a vault-relative path (for path-style links like [[drafts/march-2026]])."""
    names: set[str] = set()
    rel_paths: set[str] = set()
    for root, dirs, files in os.walk(KB):
        rootp = Path(root)
        rel_root = rootp.relative_to(KB)
        # prune skipped dirs in-place so os.walk doesn't descend
        dirs[:] = [d for d in dirs if not _skip(rel_root / d)]
        if _skip(rel_root) and rel_root != Path("."):
            continue
        for d in dirs:
            names.add(d)                                   # folder-note targets
        for f in files:
            names.add(f)                                   # exact filename incl ext
            stem_md = f[:-3] if f.endswith(".md") else f   # drop trailing .md
            names.add(stem_md)
            names.add(f.split(".")[0])                     # pure stem (hearth-map.excalidraw.md -> hearth-map)
            rel = (rel_root / f) if rel_root != Path(".") else Path(f)
            rel_paths.add(str(rel))
            if f.endswith(".md"):
                rel_paths.add(str(rel)[:-3])               # path without .md
    return names, rel_paths


def resolves(target: str, names: set[str], rel_paths: set[str]) -> bool:
    t = target.split("|")[0]                # drop display alias
    t = t.split("#")[0].strip()             # drop heading/block anchor
    t = t.lstrip("./").strip()
    if not t:                               # pure same-file #anchor
        return True
    last = t.split("/")[-1]
    cands = {t, last, last[:-3] if last.endswith(".md") else last, last.split(".")[0]}
    if cands & names:
        return True
    # path-style: literal relative path, with or without .md, and adding a default ext
    if t in rel_paths or f"{t}.md" in rel_paths:
        return True
    if last.endswith(KNOWN_EXTS) and (t in rel_paths):
        return True
    return False


# ── checks ────────────────────────────────────────────────────────────────────
def scan_links(names: set[str], rel_paths: set[str]) -> dict[str, list[tuple]]:
    """Return {broken_target: [(file, line, raw), ...]} across the vault."""
    broken: dict[str, list[tuple]] = {}
    for md in KB.rglob("*.md"):
        rel = md.relative_to(KB)
        if _skip(rel):
            continue
        try:
            lines = md.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for m in WIKILINK.finditer(line):
                target = m.group(1)
                if not resolves(target, names, rel_paths):
                    key = target.split("|")[0].split("#")[0].strip().lstrip("./")
                    broken.setdefault(key, []).append((str(rel), i, m.group(0)))
    return broken


def _frontmatter(md: Path) -> dict | None:
    try:
        text = md.read_text(errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def scan_deadlines(today: dt.date) -> list[tuple]:
    """Active/on-hold project.md files with a deadline in the past."""
    overdue = []
    for md in KB.rglob("project.md"):
        rel = md.relative_to(KB)
        if _skip(rel):
            continue
        fm = _frontmatter(md)
        if not fm or fm.get("status") not in STALE_PROJECT_STATUSES:
            continue
        dl = fm.get("deadline")
        if isinstance(dl, dt.date) and dl < today:
            overdue.append((str(rel), dl, fm.get("status"), (today - dl).days))
        elif isinstance(dl, str) and dl.strip():
            try:
                d = dt.date.fromisoformat(dl.strip())
                if d < today:
                    overdue.append((str(rel), d, fm.get("status"), (today - d).days))
            except ValueError:
                pass
    overdue.sort(key=lambda x: -x[3])
    return overdue


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
        "options": {"temperature": temperature, "num_ctx": NUM_CTX},
    }
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    c = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", c, flags=re.DOTALL).strip()


def _claim_files() -> list[Path]:
    """Substantive, claim-bearing notes only — the LLM's scope."""
    out = []
    for md in KB.rglob("*.md"):
        rel = md.relative_to(KB)
        if _skip(rel) or rel.parts[0] not in CLAIM_DIRS:
            continue
        if md.name.endswith(".excalidraw.md"):
            continue
        try:
            if len(md.read_text(errors="replace")) >= MIN_CLAIM_CHARS:
                out.append(md)
        except OSError:
            pass
    return out


def _body(md: Path, limit: int) -> str:
    """Note text minus its frontmatter, truncated."""
    t = md.read_text(errors="replace")
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end >= 0:
            t = t[end + 4:]
    return t.strip()[:limit]


def stale_pass(files: list[Path], today: dt.date, log=lambda s: None) -> list[dict]:
    """Per-doc: flag time-bound claims that today has probably overtaken. O(n), no
    pairing. Conservative by construction — most notes have zero."""
    system = (
        "You audit ONE personal note for STALE claims: statements true when written but "
        f"probably false or outdated as of {today.isoformat()}. Stale means time has "
        "overtaken it — 'currently doing X', 'X is being set up', 'next week', a plan whose "
        "date has passed, 'the new Y' now old. Do NOT flag: timeless notes, opinions, "
        "aspirations, or ANYTHING you are unsure about. Most notes have ZERO stale claims — "
        "returning an empty list is the common, correct answer. Never invent. Reply ONLY as JSON."
    )
    out = []
    for md in files:
        rel = str(md.relative_to(KB))
        fm = _frontmatter(md) or {}
        date = fm.get("created") or fm.get("updated") or "unknown"
        user = (f"Note: {rel}\nWritten: {date}\nToday: {today.isoformat()}\n\n"
                f"{_body(md, DOC_TRUNC)}\n\n"
                'Return JSON: {"stale": [{"claim": "<short quote>", "why": "<one line>"}]}')
        try:
            items = [i for i in _json_list(_llm(system, user), "stale")
                     if isinstance(i, dict) and i.get("claim")]
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            log(f"  stale skip {rel}: {e}")
            continue
        if items:
            log(f"  {rel}: {len(items)} stale")
            out.append({"file": rel, "items": items})
    return out


def _clusters(files: list[Path]) -> list[tuple[str, list[Path]]]:
    """Group claim-bearing files by their immediate parent dir — a project + its
    sub-projects/notes is the tightest topical unit and the likeliest place for an
    internal contradiction. Only folders with 2+ files, capped in size."""
    by_dir: dict[str, list[Path]] = {}
    for md in files:
        by_dir.setdefault(str(md.parent.relative_to(KB)), []).append(md)
    return [(d, fs[:CLUSTER_MAX_DOCS]) for d, fs in sorted(by_dir.items())
            if len(fs) >= 2]


def contradiction_pass(files: list[Path], log=lambda s: None) -> list[dict]:
    """Within each project-folder cluster, ask for factual contradictions, then
    adversarially VERIFY each before reporting — the ai-rss fact-check lesson: a fresh
    skeptic told to default to 'not a contradiction' kills the plausible-but-wrong ones."""
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
    out = []
    for d, group in _clusters(files):
        blob = "\n\n".join(f"=== {md.name} ===\n{_body(md, CLUSTER_DOC_TRUNC)}"
                           for md in group)
        user = (f"Folder: {d}\nNotes:\n\n{blob}\n\n"
                'Return JSON: {"conflicts": [{"a": "<quote from one note>", '
                '"b": "<quote from another>", "issue": "<one line>"}]}')
        try:
            found = _json_list(_llm(find_sys, user), "conflicts")
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            log(f"  contra skip {d}: {e}")
            continue
        for c in found:
            if not (isinstance(c, dict) and c.get("a") and c.get("b")):
                continue
            vuser = (f"Note A says: {c['a']}\nNote B says: {c['b']}\n"
                     f"Claimed issue: {c.get('issue','')}\n\n"
                     'Return JSON: {"real": <true|false>, "why": "<one line>"}')
            try:
                v = json.loads(_llm(verify_sys, vuser, temperature=0.1))
            except (requests.RequestException, json.JSONDecodeError, ValueError):
                continue
            if isinstance(v, dict) and v.get("real"):
                log(f"  {d}: contradiction confirmed")
                out.append({"folder": d, **c, "why": v.get("why", "")})
    return out


# ── report ────────────────────────────────────────────────────────────────────
def render(broken: dict, overdue: list, today: dt.date,
           stale: list | None = None, contradictions: list | None = None) -> str:
    concepts = {k: v for k, v in broken.items() if len(v) >= 2}
    typos = {k: v for k, v in broken.items() if len(v) == 1}
    total_broken = sum(len(v) for v in broken.values())
    deep = stale is not None or contradictions is not None
    stale = stale or []
    contradictions = contradictions or []
    stale_n = sum(len(s["items"]) for s in stale)

    p = [f"# kb-lint — {today.isoformat()}\n",
         "*Report only — nothing was changed."
         + (" Deep pass (local qwen) ran.*\n" if deep else " Deterministic, no AI.*\n"),
         "## Summary\n",
         f"- **{total_broken}** broken wikilink(s) across **{len(broken)}** distinct target(s)",
         f"- **{len(concepts)}** unresolved concept(s) (referenced 2+ times — worth a note?)",
         f"- **{len(overdue)}** overdue project deadline(s)"]
    if deep:
        p.append(f"- **{stale_n}** possible stale claim(s) · "
                 f"**{len(contradictions)}** verified contradiction(s) *(AI, review each)*")
    p.append("")

    if concepts:
        p.append("## Unresolved concepts — referenced but never written")
        p.append("*A link you lean on with no home. Create the note, or fix the name.*\n")
        for tgt, refs in sorted(concepts.items(), key=lambda x: -len(x[1])):
            p.append(f"### `[[{tgt}]]` — {len(refs)} references")
            for f, ln, _ in refs:
                p.append(f"- `{f}:{ln}`")
            p.append("")

    if overdue:
        p.append("## Overdue project deadlines\n")
        for f, d, status, days in overdue:
            p.append(f"- **{d.isoformat()}** ({days}d ago, `{status}`) — `{f}`")
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
            p.append(f"- **`{c['folder']}`** — {c.get('issue','')}")
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

    if not (concepts or overdue or typos or stale or contradictions):
        p.append("Nothing flagged. The kb is clean. ✓")
    return "\n".join(p) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="deterministic kb health lint")
    ap.add_argument("--stdout", action="store_true", help="print, don't write a file")
    ap.add_argument("--deep", action="store_true",
                    help="add the LLM pass (stale claims + verified contradictions)")
    ap.add_argument("--limit", type=int, help="cap claim-files in --deep (for testing)")
    args = ap.parse_args()

    today = dt.date.today()
    names, rel_paths = build_index()
    broken = scan_links(names, rel_paths)
    overdue = scan_deadlines(today)

    stale = contradictions = None
    if args.deep:
        log = lambda s: print(s, file=sys.stderr, flush=True)
        files = _claim_files()
        if args.limit:
            files = files[:args.limit]
        log(f"deep pass: {len(files)} claim-bearing notes via {MODEL}")
        stale = stale_pass(files, today, log)
        contradictions = contradiction_pass(files, log)
    report = render(broken, overdue, today, stale, contradictions)

    if args.stdout:
        print(report)
        return
    LINT_DIR.mkdir(exist_ok=True)
    (LINT_DIR / f"lint-{today.isoformat()}.md").write_text(report)
    (LINT_DIR / "latest.md").write_text(report)
    total = sum(len(v) for v in broken.values())
    extra = ""
    if args.deep:
        extra = (f", {sum(len(s['items']) for s in (stale or []))} stale, "
                 f"{len(contradictions or [])} contradiction(s)")
    print(f"kb-lint: {total} broken link(s), {len(overdue)} overdue deadline(s){extra} "
          f"-> {LINT_DIR}/latest.md")


if __name__ == "__main__":
    main()
