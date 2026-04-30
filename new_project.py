import sys
import tty
import termios
from datetime import datetime
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent))
from inbox import get_project_areas, select_area_fzf

console = Console()

project_path = Path.home() / "kb" / "projects"


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    # Project name
    slug = ""
    while not slug:
        console.print("\n  [grey50]project name:[/grey50] ", end="")
        try:
            raw = input().strip()
        except EOFError:
            raw = ""
        slug = raw.lower().replace(" ", "-")
        if not slug:
            console.print("[red]  name required[/red]")

    # Area — default ideas/
    console.print(f"\n  [grey50]pick area?[/grey50] [grey70](default: ideas/)[/grey70] [grey50][[/grey50][steel_blue1]y[/steel_blue1][grey50]/[/grey50][grey70]N[/grey70][grey50]][/grey50] ", end="")
    key = getch()
    console.print()

    if key in ("y", "Y"):
        areas = get_project_areas()
        area_name = select_area_fzf(areas)
        if not area_name:
            console.print("[rosy_brown]  → cancelled[/rosy_brown]")
            return
    else:
        area_name = "ideas"

    area_dir = project_path / area_name
    area_dir.mkdir(parents=True, exist_ok=True)
    project_dir = area_dir / slug
    project_dir.mkdir(exist_ok=True)

    title = slug.replace("-", " ").title()
    today = datetime.now().strftime("%Y-%m-%d")

    project_file = project_dir / "project.md"
    project_file.write_text(f"""---
created: {today}
deadline:
status: on-hold
completed:
abandoned:
sleeping:
area: {area_name}
tags: []
---

# {title}

## Goal

## Tasks

## Notes
""")

    console.print(f"[dark_sea_green4]  → created {area_name}/{slug}/project.md[/dark_sea_green4]\n")

    import os
    editor = os.environ.get("EDITOR", "nvim")
    os.spawnlp(os.P_WAIT, editor, editor, str(project_file))


if __name__ == "__main__":
    main()
