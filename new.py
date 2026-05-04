"""
new.py — `cl new` — scaffold a new area / project / sub-project / goal / system.

  cl new                                       interactive
  cl new --area NAME
  cl new --project NAME [--in AREA]
  cl new --sub-project NAME [--in PROJECT]
  cl new --goal NAME [--year YYYY]             default year: current
  cl new --system NAME

When --in is omitted, fzf prompts among existing parents.
Sub-project folders auto-numbered (NN-slug, matching the parent's series).
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

KB = Path.home() / "kb"
PROJECTS = KB / "projects"
GOALS = KB / "goals"
SYSTEMS = KB / "systems"


def slugify(name):
    s = name.lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def list_areas():
    return sorted(
        d.name for d in PROJECTS.iterdir()
        if d.is_dir() and (d / "area.md").exists()
    )


def list_projects():
    """Return [(area_slug, project_slug)] for every project.md."""
    out = []
    for area_dir in sorted(PROJECTS.iterdir()):
        if not area_dir.is_dir() or not (area_dir / "area.md").exists():
            continue
        for project_dir in sorted(area_dir.iterdir()):
            if project_dir.is_dir() and (project_dir / "project.md").exists():
                out.append((area_dir.name, project_dir.name))
    return out


def fzf_pick(items, prompt):
    if not items:
        return None
    result = subprocess.run(
        ["fzf", f"--prompt={prompt}", "--height=20", "--reverse", "--no-info"],
        input="\n".join(items),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def prompt_name(label):
    console.print(f"\n  [grey50]{label}:[/grey50] ", end="")
    try:
        return input().strip()
    except EOFError:
        return ""


def open_in_editor(path):
    editor = os.environ.get("EDITOR", "nvim")
    subprocess.run([editor, str(path)])


def next_subproject_number(project_dir):
    max_n = 0
    for d in project_dir.iterdir():
        if d.is_dir():
            m = re.match(r"^(\d+)-", d.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def title_of(name):
    """Title-case a name for the H1 header. Preserve exact input if user typed mixed-case."""
    if name == name.lower():
        return " ".join(w.capitalize() for w in name.split())
    return name


def create_area(name):
    slug = slugify(name)
    if not slug:
        console.print("[red]  name required[/red]")
        sys.exit(1)

    area_dir = PROJECTS / slug
    if area_dir.exists():
        console.print(f"[red]  already exists: {area_dir.relative_to(KB)}[/red]")
        sys.exit(1)

    area_dir.mkdir(parents=True)
    today = datetime.now().strftime("%Y-%m-%d")
    area_file = area_dir / "area.md"
    area_file.write_text(
        f"---\n"
        f"created: {today}\n"
        f"status: active\n"
        f"tags: []\n"
        f"---\n\n"
        f"# {title_of(name)}\n\n"
    )
    console.print(f"[dark_sea_green4]  → created {slug}/area.md[/dark_sea_green4]")
    open_in_editor(area_file)


def create_project(name, parent_area=None):
    slug = slugify(name)
    if not slug:
        console.print("[red]  name required[/red]")
        sys.exit(1)

    if parent_area is None:
        areas = list_areas()
        if not areas:
            console.print("[red]  no areas exist — create one first with `cl new --area NAME`[/red]")
            sys.exit(1)
        parent_area = fzf_pick(areas, prompt="  area: ")
        if not parent_area:
            console.print("[rosy_brown]  → cancelled[/rosy_brown]")
            return

    area_dir = PROJECTS / parent_area
    if not area_dir.is_dir() or not (area_dir / "area.md").exists():
        console.print(f"[red]  area not found: {parent_area}[/red]")
        sys.exit(1)

    project_dir = area_dir / slug
    if project_dir.exists():
        console.print(f"[red]  already exists: {project_dir.relative_to(KB)}[/red]")
        sys.exit(1)

    project_dir.mkdir()
    today = datetime.now().strftime("%Y-%m-%d")
    project_file = project_dir / "project.md"
    project_file.write_text(
        f"---\n"
        f"created: {today}\n"
        f"deadline: \n"
        f"status: active\n"
        f"completed: \n"
        f"abandoned: \n"
        f"sleeping: \n"
        f"last_reviewed: \n"
        f"area: {parent_area}\n"
        f"tags: []\n"
        f"---\n\n"
        f"# {title_of(name)}\n\n"
        f"## Goal\n\n"
        f"## Tasks\n\n"
        f"## Notes\n"
    )
    console.print(
        f"[dark_sea_green4]  → created {parent_area}/{slug}/project.md[/dark_sea_green4]"
    )
    open_in_editor(project_file)


def create_subproject(name, parent_project=None):
    slug = slugify(name)
    if not slug:
        console.print("[red]  name required[/red]")
        sys.exit(1)

    projects = list_projects()
    if not projects:
        console.print("[red]  no projects exist — create one first[/red]")
        sys.exit(1)

    if parent_project is None:
        items = [f"{a}/{p}" for a, p in projects]
        choice = fzf_pick(items, prompt="  project: ")
        if not choice:
            console.print("[rosy_brown]  → cancelled[/rosy_brown]")
            return
        area_name, project_name = choice.split("/", 1)
    else:
        candidates = [(a, p) for a, p in projects if p == parent_project]
        if not candidates:
            console.print(f"[red]  project not found: {parent_project}[/red]")
            sys.exit(1)
        if len(candidates) == 1:
            area_name, project_name = candidates[0]
        else:
            choice = fzf_pick(
                [f"{a}/{p}" for a, p in candidates],
                prompt=f"  multiple {parent_project} — pick: ",
            )
            if not choice:
                console.print("[rosy_brown]  → cancelled[/rosy_brown]")
                return
            area_name, project_name = choice.split("/", 1)

    project_dir = PROJECTS / area_name / project_name
    n = next_subproject_number(project_dir)
    subdir = project_dir / f"{n:02d}-{slug}"
    if subdir.exists():
        console.print(f"[red]  already exists: {subdir.relative_to(KB)}[/red]")
        sys.exit(1)

    subdir.mkdir()
    today = datetime.now().strftime("%Y-%m-%d")
    sp_file = subdir / "sub-project.md"
    sp_file.write_text(
        f"---\n"
        f"created: {today}\n"
        f"status: pending\n"
        f"depends_on: []\n"
        f"---\n\n"
        f"# {n:02d} — {title_of(name)}\n\n"
        f"## Goal\n\n"
        f"## Tasks\n"
    )
    console.print(
        f"[dark_sea_green4]  → created {area_name}/{project_name}/{n:02d}-{slug}/sub-project.md[/dark_sea_green4]"
    )
    open_in_editor(sp_file)


def create_goal(name, year=None):
    slug = slugify(name)
    if not slug:
        console.print("[red]  name required[/red]")
        sys.exit(1)

    if year is None:
        year = datetime.now().year
    year_dir = GOALS / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)

    goal_file = year_dir / f"{slug}.md"
    if goal_file.exists():
        console.print(f"[red]  already exists: {goal_file.relative_to(KB)}[/red]")
        sys.exit(1)

    goal_file.write_text(
        f"---\n"
        f"goal: {slug}\n"
        f"year: {year}\n"
        f"status: active\n"
        f'marker: ""\n'
        f"systems: []\n"
        f"projects: []\n"
        f"---\n\n"
        f"# {title_of(name)}\n\n"
        f"## Why now\n\n"
        f"## End-of-year marker\n\n"
        f"*Concrete numbers — the more specific, the harder to lie to yourself about.*\n\n"
        f"## Quarterly checkpoints\n\n"
        f"- **Q2 (by {year}-06-30):**\n"
        f"- **Q3 (by {year}-09-30):**\n"
        f"- **Q4 (by {year}-12-31):**\n\n"
        f"## Notes\n"
    )
    console.print(
        f"[dark_sea_green4]  → created goals/{year}/{slug}.md[/dark_sea_green4]"
    )
    open_in_editor(goal_file)


def create_system(name):
    """Scaffold a system as a folder: <slug>/system.md + <slug>/blocks/<block>.md.

    The uniform folder structure supports both single-block and multi-block systems
    — start with one block; add more later by creating additional files in blocks/.

    The first block is named by stripping any leading cadence prefix from the system
    slug (`daily-writing-block` → `writing-block`, `weekly-meal-prep` → `meal-prep`).
    Block names must be globally unique across all systems — they double as gcal
    event titles.
    """
    slug = slugify(name)
    if not slug:
        console.print("[red]  name required[/red]")
        sys.exit(1)

    system_dir = SYSTEMS / slug
    if system_dir.exists():
        console.print(f"[red]  already exists: {system_dir.relative_to(KB)}[/red]")
        sys.exit(1)

    block_name = re.sub(r"^(daily|weekly|monthly)-", "", slug)

    blocks_dir = system_dir / "blocks"
    blocks_dir.mkdir(parents=True)

    system_file = system_dir / "system.md"
    system_file.write_text(
        f"---\n"
        f"system: {slug}\n"
        f"status: active\n"
        f"goals: []\n"
        f"orientations: []\n"
        f"---\n\n"
        f"# {title_of(name)}\n\n"
        f"## Why it exists\n\n"
        f"## Blocks\n\n"
        f"- [{block_name}](blocks/{block_name}.md)\n\n"
        f"## Notes\n"
    )

    main_block = blocks_dir / f"{block_name}.md"
    main_block.write_text(
        f"---\n"
        f"block: {block_name}\n"
        f"parent: {slug}\n"
        f"calendar: \n"
        f"cadence: \n"
        f"days: []\n"
        f"duration: \n"
        f"instances: 1\n"
        f'default_start: ""\n'
        f"preferred_when: \n"
        f"---\n\n"
        f"# {title_of(name)} — {block_name}\n\n"
        f"## What this block is\n\n"
        f'## "Done" looks like\n\n'
        f"## Notes\n"
    )

    console.print(
        f"[dark_sea_green4]  → created systems/{slug}/system.md + blocks/{block_name}.md[/dark_sea_green4]"
    )
    open_in_editor(system_file)


def interactive():
    """Bare `cl new` — pick type, then name, then parent."""
    types = ["area", "project", "sub-project", "goal", "system"]
    choice = fzf_pick(types, prompt="  what to create: ")
    if not choice:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    name = prompt_name(f"{choice} name")
    if not name:
        console.print("[red]  name required[/red]")
        return
    if choice == "area":
        create_area(name)
    elif choice == "project":
        create_project(name)
    elif choice == "sub-project":
        create_subproject(name)
    elif choice == "goal":
        create_goal(name)
    else:
        create_system(name)


def main():
    parser = argparse.ArgumentParser(prog="cl new", add_help=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--area", metavar="NAME", help="create a new area")
    group.add_argument("--project", metavar="NAME", help="create a new project")
    group.add_argument("--sub-project", dest="subproject", metavar="NAME",
                       help="create a new sub-project")
    group.add_argument("--goal", metavar="NAME", help="create a new goal")
    group.add_argument("--system", metavar="NAME", help="create a new system")
    parser.add_argument("--in", dest="parent", metavar="PARENT",
                        help="parent area (for --project) or project (for --sub-project)")
    parser.add_argument("--year", type=int, metavar="YYYY",
                        help="year for --goal (default: current year)")
    args = parser.parse_args()

    if args.area:
        if args.parent:
            console.print("[red]  --in is not used with --area[/red]")
            sys.exit(1)
        create_area(args.area)
    elif args.project:
        create_project(args.project, parent_area=args.parent)
    elif args.subproject:
        create_subproject(args.subproject, parent_project=args.parent)
    elif args.goal:
        if args.parent:
            console.print("[red]  --in is not used with --goal[/red]")
            sys.exit(1)
        create_goal(args.goal, year=args.year)
    elif args.system:
        if args.parent or args.year:
            console.print("[red]  --in / --year are not used with --system[/red]")
            sys.exit(1)
        create_system(args.system)
    else:
        interactive()


if __name__ == "__main__":
    main()
