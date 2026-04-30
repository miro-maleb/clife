import os
import sys
import tty
import termios
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

console = Console()

ideas_path = Path.home() / "kb" / "ideas"
project_path = Path.home() / "kb" / "projects"
EXCLUDED_TOP = {"life-os", "personal-life"}


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_frontmatter_field(content, field):
    for line in content.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def get_title(content):
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def get_snippet(content):
    past_frontmatter = False
    dash_count = 0
    lines = []
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if not past_frontmatter:
            continue
        if line.startswith("#"):
            continue
        if line.strip():
            lines.append(line.strip())
        if len(lines) >= 2:
            break
    return " ".join(lines)


def get_body(content):
    """Return everything after frontmatter."""
    lines = []
    past_frontmatter = False
    dash_count = 0
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if past_frontmatter:
            lines.append(line)
    return "\n".join(lines).strip()


def get_project_areas():
    areas = []
    for folder in sorted(project_path.iterdir()):
        if not folder.is_dir():
            continue
        if folder.name in EXCLUDED_TOP:
            continue
        if folder.name.startswith((".", "_")):
            continue
        areas.append(folder)
    return areas


def promote_idea(idea_file, content):
    areas = get_project_areas()

    console.print()
    console.print("  [grey50]promote to — pick an area:[/grey50]")
    console.print()
    for i, area in enumerate(areas, 1):
        console.print(f"  [steel_blue1]{i}[/steel_blue1]  [grey80]{area.name}[/grey80]")
    console.print(f"\n  [grey50]q[/grey50] cancel\n")

    while True:
        key = getch()
        if key == "q":
            console.print("[grey50]  → cancelled[/grey50]")
            return True  # stay in review, don't quit
        try:
            idx = int(key) - 1
            if 0 <= idx < len(areas):
                target_area = areas[idx]
                break
        except ValueError:
            pass

    # Restore terminal for line input
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    slug = ""
    while not slug:
        console.print(f"\n  [grey50]project name (slug):[/grey50] ", end="")
        try:
            raw = input().strip()
        except EOFError:
            raw = ""
        slug = raw.lower().replace(" ", "-")
        if not slug:
            console.print("[red]  name required[/red]")
    project_dir = target_area / slug
    project_dir.mkdir(exist_ok=True)

    created = get_frontmatter_field(content, "created") or datetime.now().strftime("%Y-%m-%d")
    tags = get_frontmatter_field(content, "tags") or "[]"
    body = get_body(content)
    title = slug.replace("-", " ").title()

    project_content = f"""---
created: {created}
deadline:
status: on-hold
completed:
abandoned:
area: {target_area.name}
tags: {tags}
---

# {title}

{body}
"""
    (project_dir / "project.md").write_text(project_content)
    idea_file.unlink()

    console.print(f"[dark_sea_green4]  → promoted to {target_area.name}/{slug}[/dark_sea_green4]")
    return True


def review_idea(idea_file, index, total):
    content = idea_file.read_text()
    title = get_title(content) or idea_file.stem
    status = get_frontmatter_field(content, "status") or "seed"
    created = get_frontmatter_field(content, "created")
    snippet = get_snippet(content)

    status_colors = {"seed": "grey70", "growing": "steel_blue1", "mature": "gold3"}
    sc = status_colors.get(status, "grey70")

    meta = f"[{sc}]{status}[/{sc}]"
    if created:
        meta += f"  [grey50]{created}[/grey50]"

    summary = f"{meta}\n\n[grey80]{snippet}[/grey80]" if snippet else meta

    hotkeys = (
        "[grey50][[/grey50][steel_blue1]k[/steel_blue1][grey50]][/grey50] keep  "
        "[grey50][[/grey50][dark_sea_green4]p[/dark_sea_green4][grey50]][/grey50] promote  "
        "[grey50][[/grey50][indian_red]d[/indian_red][grey50]][/grey50] discard  "
        "[grey50][[/grey50][steel_blue1]o[/steel_blue1][grey50]][/grey50] open  "
        "[grey50][[/grey50][grey70]q[/grey70][grey50]][/grey50] quit"
    )

    console.print()
    console.print(Rule(
        f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{title}[/tan]",
        style="grey23"
    ))
    console.print(Panel(summary, border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {hotkeys}\n")

    while True:
        key = getch()
        if key == "k":
            console.print("[steel_blue1]  → keeping[/steel_blue1]")
            return True
        elif key == "p":
            return promote_idea(idea_file, content)
        elif key == "d":
            idea_file.unlink()
            console.print("[indian_red]  → discarded[/indian_red]")
            return True
        elif key == "o":
            os.spawnlp(os.P_WAIT, "nvim", "nvim", str(idea_file))
            return True
        elif key == "q":
            return False


def main():
    ideas = sorted(ideas_path.glob("*.md"))

    if not ideas:
        console.print()
        console.print("[grey50]  no ideas[/grey50]")
        console.print()
        return

    total = len(ideas)
    console.print()
    console.print(Rule(
        f"[bold gold3]  Ideas Review[/bold gold3]  [grey50]{total} ideas[/grey50]",
        style="gold3 dim"
    ))
    console.print()

    for i, idea_file in enumerate(ideas, 1):
        keep_going = review_idea(idea_file, i, total)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="gold3 dim"))
    console.print()


if __name__ == "__main__":
    main()
