import argparse
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

project_path = Path.home() / "kb" / "projects"
EXCLUDED_TOP = {"life-os", "personal-life"}
STATUS_ORDER = {"active": 0, "on-hold": 1, "sleeping": 2, "complete": 3, "abandoned": 4}

# Shown by default in lo projects
DEFAULT_STATUSES = {"active", "on-hold"}
# Shown in lo review (includes sleeping)
REVIEW_STATUSES = {"active", "on-hold", "sleeping"}


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_top_folder(md_file):
    parts = md_file.parts
    idx = parts.index("projects")
    return parts[idx + 1]


def is_structural_area(area_file):
    """True for container folder markers — area.md files with no created: field."""
    return "created:" not in area_file.read_text()


def get_status(content):
    for line in content.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def get_goal(content):
    in_goal = False
    for line in content.splitlines():
        if line.strip() == "## Goal":
            in_goal = True
            continue
        if in_goal:
            if line.startswith("##"):
                break
            if line.strip():
                return line.strip()

    past_frontmatter = False
    dash_count = 0
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if not past_frontmatter:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped

    return ""


def open_task_count(md_file):
    count = 0
    for f in md_file.parent.rglob("*.md"):
        count += f.read_text().count("- [ ]")
    return count


def set_status(md_file, new_status):
    content = md_file.read_text()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    for line in content.splitlines():
        if line.startswith("status:"):
            lines.append(f"status: {new_status}")
        else:
            lines.append(line)
    content = "\n".join(lines)
    if new_status == "complete":
        content = content.replace("completed:\n", f"completed: {today}\n")
    elif new_status == "abandoned":
        content = content.replace("abandoned:\n", f"abandoned: {today}\n")
    elif new_status == "sleeping":
        content = content.replace("sleeping:\n", f"sleeping: {today}\n")
    md_file.write_text(content)


def reclassify(md_file):
    """Rename project.md ↔ area.md. Returns the new type label."""
    if md_file.name == "project.md":
        md_file.rename(md_file.parent / "area.md")
        return "area"
    else:
        md_file.rename(md_file.parent / "project.md")
        return "project"


def open_in_nvim(file):
    os.spawnlp(os.P_WAIT, "nvim", "nvim", str(file))


def status_color(status):
    return {
        "active":   "steel_blue1",
        "on-hold":  "rosy_brown",
        "sleeping": "grey50",
        "complete": "dark_sea_green4",
        "abandoned": "indian_red",
    }.get(status, "grey70")


def build_hotkeys(is_project, status):
    def key(k, label, color="steel_blue1"):
        return f"[grey50][[/grey50][{color}]{k}[/{color}][grey50]][/grey50] {label}"

    parts = [key("k", "keep")]

    if status == "sleeping":
        parts.append(key("w", "wake", "rosy_brown"))
    else:
        if status != "active":
            parts.append(key("a", "activate", "dark_sea_green4"))
        if status == "active":
            parts.append(key("h", "hold", "rosy_brown"))
        if status in ("active", "on-hold"):
            parts.append(key("z", "sleep", "grey50"))
        if is_project and status in ("active", "on-hold"):
            parts.append(key("c", "complete", "dark_sea_green4"))

    parts.append(key("x", "abandon", "indian_red"))

    reclassify_label = "→ Area" if is_project else "→ Project"
    parts.append(key("r", reclassify_label, "gold3"))
    parts.append(key("o", "open"))
    parts.append(key("q", "quit", "grey70"))

    return "  ".join(parts)


def get_all_reviewable(statuses=None):
    if statuses is None:
        statuses = DEFAULT_STATUSES
    items = []

    for pattern in ("project.md", "area.md"):
        for md_file in sorted(project_path.rglob(pattern)):
            if get_top_folder(md_file) in EXCLUDED_TOP:
                continue
            if pattern == "area.md" and is_structural_area(md_file):
                continue
            content = md_file.read_text()
            status = get_status(content)
            if status not in statuses:
                continue
            items.append(md_file)

    items.sort(key=lambda f: (STATUS_ORDER.get(get_status(f.read_text()), 99), str(f)))
    return items


def review_item(md_file, index, total):
    name = md_file.parent.name
    content = md_file.read_text()
    is_project = md_file.name == "project.md"
    status = get_status(content)
    goal = get_goal(content)
    task_count = open_task_count(md_file)

    type_label = "[grey50]project[/grey50]" if is_project else "[gold3]area[/gold3]"
    status_str = f"[{status_color(status)}]{status}[/{status_color(status)}]"

    lines = [f"{type_label}  {status_str}"]
    if goal:
        lines.append(f"\n[grey50]goal[/grey50]  [grey80]{goal}[/grey80]")
    lines.append(f"[grey50]open[/grey50]  [steel_blue1]{task_count}[/steel_blue1] [grey50]tasks[/grey50]")

    console.print()
    console.print(Rule(
        f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{name}[/tan]",
        style="grey23"
    ))
    console.print(Panel("\n".join(lines), border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {build_hotkeys(is_project, status)}\n")

    while True:
        key = getch()
        if key == "k":
            console.print("[steel_blue1]  → keeping[/steel_blue1]")
            return True
        elif key == "a" and status != "active":
            set_status(md_file, "active")
            console.print("[dark_sea_green4]  → activated[/dark_sea_green4]")
            return True
        elif key == "h" and status == "active":
            set_status(md_file, "on-hold")
            console.print("[rosy_brown]  → put on hold[/rosy_brown]")
            return True
        elif key == "z" and status in ("active", "on-hold"):
            set_status(md_file, "sleeping")
            console.print("[grey50]  → sleeping[/grey50]")
            return True
        elif key == "w" and status == "sleeping":
            set_status(md_file, "on-hold")
            console.print("[rosy_brown]  → woken to on-hold[/rosy_brown]")
            return True
        elif key == "c" and is_project and status in ("active", "on-hold"):
            set_status(md_file, "complete")
            console.print("[dark_sea_green4]  → marked complete[/dark_sea_green4]")
            return True
        elif key == "x":
            set_status(md_file, "abandoned")
            console.print("[indian_red]  → abandoned[/indian_red]")
            return True
        elif key == "r":
            new_type = reclassify(md_file)
            console.print(f"[gold3]  → reclassified as {new_type}[/gold3]")
            return True
        elif key == "o":
            open_in_nvim(md_file)
            return True
        elif key == "q":
            return False


def main(statuses=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--active",    action="store_true")
    parser.add_argument("--on-hold",   action="store_true", dest="on_hold")
    parser.add_argument("--sleeping",  action="store_true")
    parser.add_argument("--complete",  action="store_true")
    parser.add_argument("--abandoned", action="store_true")
    parser.add_argument("--all",       action="store_true")
    args, _ = parser.parse_known_args()

    if statuses is not None:
        # Called programmatically (e.g. from review.py)
        pass
    elif args.all:
        statuses = {"active", "on-hold", "sleeping", "complete", "abandoned"}
    elif any([args.active, args.on_hold, args.sleeping, args.complete, args.abandoned]):
        statuses = set()
        if args.active:    statuses.add("active")
        if args.on_hold:   statuses.add("on-hold")
        if args.sleeping:  statuses.add("sleeping")
        if args.complete:  statuses.add("complete")
        if args.abandoned: statuses.add("abandoned")
    else:
        statuses = DEFAULT_STATUSES

    items = get_all_reviewable(statuses)

    if not items:
        console.print()
        console.print("[grey50]  nothing to review[/grey50]")
        console.print()
        return

    counts = {}
    for f in items:
        s = get_status(f.read_text())
        counts[s] = counts.get(s, 0) + 1

    status_summary = "  ".join(
        f"[{status_color(s)}]{counts[s]} {s}[/{status_color(s)}]"
        for s in ("active", "on-hold", "sleeping", "complete", "abandoned")
        if counts.get(s, 0) > 0
    )

    console.print()
    console.print(Rule(
        f"[bold steel_blue1]  Project Review[/bold steel_blue1]  [grey50]—[/grey50]  {status_summary}",
        style="steel_blue1 dim"
    ))
    console.print()

    total = len(items)
    for i, md_file in enumerate(items, 1):
        keep_going = review_item(md_file, i, total)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
