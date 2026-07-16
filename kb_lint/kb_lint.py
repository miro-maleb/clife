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
import os
import re
import sys
from pathlib import Path

import yaml

KB = Path(os.path.expanduser("~/kb"))
LINT_DIR = KB / "_lint"

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


# ── report ────────────────────────────────────────────────────────────────────
def render(broken: dict, overdue: list, today: dt.date) -> str:
    concepts = {k: v for k, v in broken.items() if len(v) >= 2}
    typos = {k: v for k, v in broken.items() if len(v) == 1}
    total_broken = sum(len(v) for v in broken.values())

    p = [f"# kb-lint — {today.isoformat()}\n",
         "*Deterministic sweep (no AI). Report only — nothing was changed.*\n",
         "## Summary\n",
         f"- **{total_broken}** broken wikilink(s) across **{len(broken)}** distinct target(s)",
         f"- **{len(concepts)}** unresolved concept(s) (referenced 2+ times — worth a note?)",
         f"- **{len(overdue)}** overdue project deadline(s)\n"]

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

    if not (concepts or overdue or typos):
        p.append("Nothing broken. The kb is clean. ✓")
    return "\n".join(p) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="deterministic kb health lint")
    ap.add_argument("--stdout", action="store_true", help="print, don't write a file")
    args = ap.parse_args()

    today = dt.date.today()
    names, rel_paths = build_index()
    broken = scan_links(names, rel_paths)
    overdue = scan_deadlines(today)
    report = render(broken, overdue, today)

    if args.stdout:
        print(report)
        return
    LINT_DIR.mkdir(exist_ok=True)
    (LINT_DIR / f"lint-{today.isoformat()}.md").write_text(report)
    (LINT_DIR / "latest.md").write_text(report)
    total = sum(len(v) for v in broken.values())
    print(f"kb-lint: {total} broken link(s), {len(overdue)} overdue deadline(s) "
          f"-> {LINT_DIR}/latest.md")


if __name__ == "__main__":
    main()
