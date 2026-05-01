"""
notes.py — `cl notes` — flat fzf browser over every note in the kb.

A note is any markdown file that is either:
  1. has `type: note` in frontmatter, OR
  2. lives in a `notes/` subfolder anywhere under `projects/`, OR
  3. lives in the top-level `kb/notes/` (legacy)

Each row shows: location · title · tags · flags (orphan/stale) · age.
fzf does the searching — type any visible string to filter inline.

Filter flags:
  cl notes --orphans      only orphan notes (no project home, no inbound link)
  cl notes --stale        only notes unmodified > 90 days
  cl notes --area NAME    only notes under projects/NAME/
  cl notes --project NAME only notes under any project named NAME
  cl notes --tag NAME     only notes with NAME in their tags

Selecting a row opens it in $EDITOR.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

console = Console()

KB = Path.home() / "kb"
PROJECTS = KB / "projects"
LEGACY_NOTES = KB / "notes"

EXCLUDE_PARTS = {
    ".git", "venv", ".venv", "__pycache__", "node_modules",
    "archive", "inbox", "weeks", "journal", "templates",
    ".obsidian", ".trash",
}

STALE_DAYS = 90


def parse_frontmatter(content):
    if not content.startswith("---"):
        return {}
    lines = content.splitlines()
    fm = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def excluded(path):
    return any(p in EXCLUDE_PARTS for p in path.parts)


def find_notes():
    """Return list of Path objects for every note in the kb."""
    notes = set()

    # 1. Files inside any */notes/ subdir under projects/
    for notes_dir in PROJECTS.rglob("notes"):
        if not notes_dir.is_dir() or excluded(notes_dir):
            continue
        for f in notes_dir.glob("*.md"):
            notes.add(f)

    # 2. Top-level legacy kb/notes/
    if LEGACY_NOTES.is_dir():
        for f in LEGACY_NOTES.glob("*.md"):
            notes.add(f)

    # 3. Anywhere with type: note frontmatter
    for f in KB.rglob("*.md"):
        if excluded(f) or f in notes:
            continue
        try:
            content = f.read_text(errors="ignore")
        except OSError:
            continue
        fm = parse_frontmatter(content)
        if fm.get("type", "").lower() == "note":
            notes.add(f)

    return sorted(notes, key=lambda p: p.stat().st_mtime, reverse=True)


def build_link_index():
    """Set of [[wiki-link]] targets referenced anywhere in the kb."""
    pattern = re.compile(r"\[\[([^\]|#]+)")
    refs = set()
    for f in KB.rglob("*.md"):
        if excluded(f):
            continue
        try:
            content = f.read_text(errors="ignore")
        except OSError:
            continue
        for m in pattern.finditer(content):
            slug = m.group(1).strip().lower()
            refs.add(slug)
            refs.add(Path(slug).stem)
    return refs


def categorize(path):
    """Return (area, project) from path, either may be ''."""
    try:
        rel = path.relative_to(KB)
    except ValueError:
        return ("", "")
    parts = rel.parts
    if not parts or parts[0] != "projects" or len(parts) < 3:
        return ("", "")
    area = parts[1]
    project = parts[2] if len(parts) > 2 else ""
    if project.endswith(".md"):
        project = ""
    return (area, project)


def is_orphan(path, link_index):
    """Orphan = no project home AND no inbound [[wiki-link]]."""
    _, project = categorize(path)
    if project:
        return False
    stem = path.stem.lower()
    rel = str(path.relative_to(KB)).lower().replace(".md", "")
    return stem not in link_index and rel not in link_index


def get_title(path):
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem


def get_tags(fm):
    raw = fm.get("tags", "").strip()
    if not raw or raw == "[]":
        return []
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
    return [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]


# ANSI color codes (fzf renders these with --ansi)
GREY  = "\033[90m"
TAN   = "\033[38;5;180m"
GOLD  = "\033[33m"
RED   = "\033[31m"
BLUE  = "\033[38;5;111m"
RESET = "\033[0m"


def format_row(path, fm, link_index, now):
    area, project = categorize(path)
    title = get_title(path)
    tags = get_tags(fm)
    mtime = path.stat().st_mtime
    days = int((now - mtime) // 86400)

    if project:
        location = f"{GREY}{area}/{project}{RESET}"
    elif area:
        location = f"{GREY}{area}{RESET}"
    else:
        location = f"{GREY}—{RESET}"

    flags = []
    if is_orphan(path, link_index):
        flags.append(f"{RED}orphan{RESET}")
    if days > STALE_DAYS:
        flags.append(f"{GOLD}stale{RESET}")

    tag_str = " ".join(f"#{t}" for t in tags)

    visible = f"{location}  {BLUE}{title}{RESET}"
    if tag_str:
        visible += f"  {GOLD}{tag_str}{RESET}"
    if flags:
        visible += "  " + " ".join(flags)
    visible += f"  {GREY}{days}d{RESET}"

    # Tab-separated: visible part | path (hidden via fzf --with-nth=1)
    return f"{visible}\t{path}"


def filter_notes(notes_with_fm, args, link_index):
    out = []
    for path, fm in notes_with_fm:
        area, project = categorize(path)
        days = int((time.time() - path.stat().st_mtime) // 86400)

        if args.orphans and not is_orphan(path, link_index):
            continue
        if args.stale and days <= STALE_DAYS:
            continue
        if args.area and area != args.area:
            continue
        if args.project and project != args.project:
            continue
        if args.tag:
            tags = [t.lower() for t in get_tags(fm)]
            if args.tag.lower() not in tags:
                continue
        out.append((path, fm))
    return out


def fzf_pick(rows, header):
    """Run fzf over preformatted rows, return selected path or None."""
    if not rows:
        return None
    result = subprocess.run(
        ["fzf",
         "--ansi",
         "--delimiter=\t",
         "--with-nth=1",
         "--reverse",
         "--height=80%",
         "--no-info",
         "--header", header,
         "--prompt=  notes: "],
        input="\n".join(rows), text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    parts = line.split("\t", 1)
    if len(parts) < 2:
        return None
    return Path(parts[1])


def open_in_editor(path):
    editor = os.environ.get("EDITOR", "nvim")
    subprocess.run([editor, str(path)])


def main():
    parser = argparse.ArgumentParser(prog="cl notes", add_help=True)
    parser.add_argument("--orphans", action="store_true",
                        help="only notes with no project home and no inbound link")
    parser.add_argument("--stale", action="store_true",
                        help=f"only notes unmodified > {STALE_DAYS} days")
    parser.add_argument("--area", metavar="NAME", help="filter to one area")
    parser.add_argument("--project", metavar="NAME", help="filter to one project")
    parser.add_argument("--tag", metavar="NAME", help="filter to one tag")
    args = parser.parse_args()

    # Discover notes + their frontmatter
    paths = find_notes()
    notes_with_fm = []
    for p in paths:
        try:
            fm = parse_frontmatter(p.read_text(errors="ignore"))
        except OSError:
            fm = {}
        notes_with_fm.append((p, fm))

    link_index = build_link_index()

    filtered = filter_notes(notes_with_fm, args, link_index)

    if not filtered:
        if any([args.orphans, args.stale, args.area, args.project, args.tag]):
            console.print("\n  [grey50]no notes match those filters[/grey50]\n")
        else:
            console.print("\n  [grey50]no notes yet[/grey50]\n")
        return

    now = time.time()
    rows = [format_row(p, fm, link_index, now) for p, fm in filtered]

    # Build header (filter context + count)
    filter_bits = []
    if args.orphans: filter_bits.append("orphans")
    if args.stale:   filter_bits.append("stale")
    if args.area:    filter_bits.append(f"area={args.area}")
    if args.project: filter_bits.append(f"project={args.project}")
    if args.tag:     filter_bits.append(f"tag={args.tag}")
    label = " · ".join(filter_bits) if filter_bits else "all"
    header = f"{len(filtered)} notes · {label} · enter to open · esc to quit"

    selected = fzf_pick(rows, header)
    if selected:
        open_in_editor(selected)


if __name__ == "__main__":
    main()
