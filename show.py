"""
show.py — `cl show <path>` — overview for an area / project / sub-project.

Resolves <path> to one of:
  - area.md          → projects under this area
  - project.md       → sub-projects + open tasks (sample) + recent notes
  - sub-project.md   → the Goal / Tasks / Open Questions sections

Path can be a directory (we look inside for area.md / project.md / sub-project.md)
or any of those three files directly.

Primary caller: the dashboard's left pane (`cl tree --pane`) dispatches
`cl show <path>` into the center pane when the user hits Enter on a node.
Designed to render into a ~half-width pane, single screen if possible.
"""

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule

sys.path.insert(0, os.path.dirname(__file__))

import projects as proj
from tui_common import ACCENT, ACCENT_DIM, BODY, BORDER, MUTED

console = Console()

from paths import KB
PROJECTS = KB / "projects"

OVERVIEW_FILES = ("area.md", "project.md", "sub-project.md")
TASK_SAMPLE_LIMIT = 12
RECENT_NOTES_LIMIT = 6


# ── path resolution ───────────────────────────────────────────────────────────

def resolve(arg: str) -> Path | None:
    """Resolve <arg> to one of the OVERVIEW_FILES, or None."""
    p = Path(arg).expanduser().resolve()
    if p.is_file() and p.name in OVERVIEW_FILES:
        return p
    if p.is_dir():
        for name in OVERVIEW_FILES:
            cand = p / name
            if cand.is_file():
                return cand
    return None


# ── shared bits ───────────────────────────────────────────────────────────────

def _title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# ") and not s.startswith("##"):
            return s[2:].strip()
    return fallback


def _meta_line(content: str, fields: list[str]) -> str:
    parts = []
    for f in fields:
        v = proj.get_field(content, f)
        if v and v not in ("~", "null", "[]"):
            parts.append(f"[{MUTED}]{f}:[/{MUTED}] [{BODY}]{escape(v)}[/{BODY}]")
    return "  ·  ".join(parts)


def _status_chip(status: str) -> str:
    color = proj.status_color(status)
    return f"[{color}]{status}[/{color}]"


def _header(name: str, status: str, meta: str, summary: str) -> Panel:
    lines = [f"[bold {ACCENT}]{escape(name)}[/bold {ACCENT}]  {_status_chip(status)}"]
    if meta:
        lines.append(meta)
    if summary:
        lines.append(f"\n[{BODY}]{escape(summary)}[/{BODY}]")
    return Panel("\n".join(lines), border_style=BORDER, padding=(1, 2))


def _section_rule(label: str, count: int | None = None) -> Rule:
    text = f"[bold {ACCENT_DIM}]{label}[/bold {ACCENT_DIM}]"
    if count is not None:
        text += f"  [{MUTED}]({count})[/{MUTED}]"
    return Rule(text, align="left", style=BORDER)


# ── content extraction ───────────────────────────────────────────────────────

def _open_tasks(root: Path, limit: int) -> tuple[list[tuple[Path, str]], int]:
    """Walk markdown files under root, collect open task lines.

    Returns (sample, total). Sample is up to `limit` (path, line) pairs.
    """
    sample: list[tuple[Path, str]] = []
    total = 0
    for md in sorted(root.rglob("*.md")):
        try:
            text = md.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("- [ ]"):
                total += 1
                if len(sample) < limit:
                    sample.append((md, s[5:].strip()))
    return sample, total


def _recent_notes(project_dir: Path, limit: int) -> list[tuple[Path, float]]:
    """Recently-modified markdown files under <project>/notes/, newest first."""
    notes_dir = project_dir / "notes"
    if not notes_dir.is_dir():
        return []
    items = []
    for md in notes_dir.rglob("*.md"):
        items.append((md, md.stat().st_mtime))
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]


def _list_sections(content: str) -> list[str]:
    """Return all `## heading` titles in the file, in order."""
    out: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("## ") and not s.startswith("###"):
            out.append(s[3:].strip())
    return out


def _section_body(content: str, heading: str) -> str:
    """Return the body of a `## heading` section, stripped, until the next `##`."""
    lines = content.splitlines()
    target = f"## {heading}".lower()
    out: list[str] = []
    in_section = False
    for line in lines:
        s = line.strip()
        if s.lower() == target:
            in_section = True
            continue
        if in_section:
            if s.startswith("## "):
                break
            out.append(line)
    return "\n".join(out).strip()


# ── renderers ─────────────────────────────────────────────────────────────────

def render_area(area_md: Path) -> None:
    area_dir = area_md.parent
    content = area_md.read_text()
    name = _title(content, area_dir.name)
    status = proj.get_status(content)
    meta = _meta_line(content, ["created", "tags"])
    summary = _first_paragraph(content)

    console.print()
    console.print(_header(name, status, meta, summary))
    console.print()

    project_mds = sorted(area_dir.rglob("project.md"))
    console.print(_section_rule("Projects", len(project_mds)))
    if not project_mds:
        console.print(f"  [{MUTED}](none)[/{MUTED}]")
    for pmd in project_mds:
        try:
            pcontent = pmd.read_text()
        except OSError:
            continue
        pstatus = proj.get_status(pcontent)
        pcolor = proj.status_color(pstatus)
        rel = pmd.parent.relative_to(area_dir)
        tasks = proj.open_task_count(pmd)
        line = (
            f"  [{BODY}]{escape(str(rel))}[/{BODY}]  "
            f"[{pcolor}]{pstatus}[/{pcolor}]"
        )
        if tasks:
            line += f"  [{MUTED}]{tasks} open[/{MUTED}]"
        console.print(line)
    console.print()


def render_project(project_md: Path) -> None:
    project_dir = project_md.parent
    content = project_md.read_text()
    name = _title(content, project_dir.name)
    status = proj.get_status(content)
    meta = _meta_line(content, ["area", "deadline", "completed", "last_reviewed"])
    goal = proj.get_goal(content) or _first_paragraph(content)

    console.print()
    console.print(_header(name, status, meta, goal))
    console.print()

    sub_dirs = []
    for child in sorted(project_dir.iterdir()):
        sp = child / "sub-project.md"
        if child.is_dir() and sp.exists():
            sub_dirs.append((child, sp))

    console.print(_section_rule("Sub-projects", len(sub_dirs)))
    if not sub_dirs:
        console.print(f"  [{MUTED}](none)[/{MUTED}]")
    for sub_dir, sp in sub_dirs:
        try:
            sp_content = sp.read_text()
        except OSError:
            continue
        sp_status = proj.get_status(sp_content)
        sp_color = proj.status_color(sp_status)
        sp_tasks = proj.open_task_count(sp)
        line = (
            f"  [{BODY}]{escape(sub_dir.name)}[/{BODY}]  "
            f"[{sp_color}]{sp_status}[/{sp_color}]"
        )
        if sp_tasks:
            line += f"  [{MUTED}]{sp_tasks} open[/{MUTED}]"
        console.print(line)
    console.print()

    sample, total = _open_tasks(project_dir, TASK_SAMPLE_LIMIT)
    console.print(_section_rule("Open tasks", total))
    if not sample:
        console.print(f"  [{MUTED}](none)[/{MUTED}]")
    for md, task in sample:
        rel = md.relative_to(project_dir)
        console.print(
            f"  [{MUTED}]·[/{MUTED}] [{BODY}]{escape(task)}[/{BODY}]  "
            f"[{MUTED}]{escape(str(rel))}[/{MUTED}]"
        )
    if total > len(sample):
        console.print(f"  [{MUTED}]… {total - len(sample)} more[/{MUTED}]")
    console.print()

    notes = _recent_notes(project_dir, RECENT_NOTES_LIMIT)
    if notes:
        console.print(_section_rule("Recent notes", len(notes)))
        for md, _ in notes:
            console.print(
                f"  [{BODY}]{escape(md.stem)}[/{BODY}]  "
                f"[{MUTED}]notes/{escape(md.name)}[/{MUTED}]"
            )
        console.print()


def render_sub_project(sp_md: Path) -> None:
    sub_dir = sp_md.parent
    content = sp_md.read_text()
    name = _title(content, sub_dir.name)
    status = proj.get_status(content)
    meta = _meta_line(content, ["depends_on", "started", "deadline", "last_reviewed"])
    goal = proj.get_goal(content) or _first_paragraph(content)

    console.print()
    console.print(_header(name, status, meta, goal))
    console.print()

    tasks_body = _section_body(content, "Tasks")
    if tasks_body:
        open_count = tasks_body.count("- [ ]")
        console.print(_section_rule("Tasks", open_count))
        for line in tasks_body.splitlines():
            console.print(f"  {escape(line)}" if line.strip() else "")
        console.print()

    questions = _section_body(content, "Open questions") or _section_body(content, "Open")
    if questions:
        console.print(_section_rule("Open questions"))
        for line in questions.splitlines():
            console.print(f"  {escape(line)}" if line.strip() else "")
        console.print()

    sections = _list_sections(content)
    skip = {"goal", "tasks", "open questions", "open"}
    other = [s for s in sections if s.lower() not in skip]
    if other:
        console.print(_section_rule("Other sections"))
        for s in other:
            console.print(f"  [{MUTED}]·[/{MUTED}] [{BODY}]{escape(s)}[/{BODY}]")
        console.print()


def _first_paragraph(content: str) -> str:
    """First non-frontmatter, non-heading paragraph (one line)."""
    past_frontmatter = False
    dash_count = 0
    buf = []
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if not past_frontmatter:
            continue
        s = line.strip()
        if not s or s.startswith("#"):
            if buf:
                break
            continue
        buf.append(s)
    return " ".join(buf)


# ── entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="cl show")
    parser.add_argument("path", help="area, project, or sub-project (dir or .md file)")
    args = parser.parse_args()

    resolved = resolve(args.path)
    if not resolved:
        print(f"  not an area/project/sub-project: {args.path}", file=sys.stderr)
        sys.exit(1)

    console.clear()
    if resolved.name == "area.md":
        render_area(resolved)
    elif resolved.name == "project.md":
        render_project(resolved)
    elif resolved.name == "sub-project.md":
        render_sub_project(resolved)


if __name__ == "__main__":
    main()
