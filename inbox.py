import os
import re
import subprocess
import sys
import tty
import termios
from datetime import datetime
from pathlib import Path

from kb_utils import insert_journal_bullet, today_journal

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

console = Console()

notes_path = Path.home() / "kb" / "notes"
project_path = Path.home() / "kb" / "projects"
inbox_path = Path.home() / "kb" / "inbox"
shopping_path = Path.home() / "kb" / "shopping"

HOTKEYS = (
    "[grey50][[/grey50][steel_blue1]j[/steel_blue1][grey50]][/grey50] journal  "
    "[grey50][[/grey50][steel_blue1]n[/steel_blue1][grey50]][/grey50] note  "
    "[grey50][[/grey50][steel_blue1]t[/steel_blue1][grey50]][/grey50] task  "
    "[grey50][[/grey50][steel_blue1]c[/steel_blue1][grey50]][/grey50] calendar  "
    "[grey50][[/grey50][steel_blue1]p[/steel_blue1][grey50]][/grey50] new project  "
    "[grey50][[/grey50][steel_blue1]v[/steel_blue1][grey50]][/grey50] → project  "
    "[grey50][[/grey50][steel_blue1]g[/steel_blue1][grey50]][/grey50] grocery  "
    "[grey50][[/grey50][steel_blue1]h[/steel_blue1][grey50]][/grey50] household  "
    "[grey50][[/grey50][grey70]s[/grey70][grey50]][/grey50] skip  "
    "[grey50][[/grey50][grey70]d[/grey70][grey50]][/grey50] delete  "
    "[grey50][[/grey50][grey70]q[/grey70][grey50]][/grey50] quit"
)


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_project_names():
    names = []
    for f in project_path.rglob("project.md"):
        names.append(f.parent.name)
    return sorted(set(names))


def get_project_areas():
    """Return top-level project folders that are areas (no project.md directly inside)."""
    areas = []
    for item in sorted(project_path.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            if not (item / "project.md").exists():
                areas.append(item)
    return areas


def select_project_fzf(project_names):
    result = subprocess.run(
        ["fzf", "--prompt=  project: ", "--height=40%", "--reverse", "--no-info"],
        input="\n".join(project_names),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


CALENDARS = ["Miro-Personal", "Professional", "Spiritual Practice", "Sydney", "Travel", "Retreats"]


def convert_military_time(text):
    """Convert 4-digit military time to 12h format, rounding to nearest 5 min for Google Quick Add."""
    def replace(m):
        h, mins = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mins <= 59):
            return m.group(0)
        mins = round(mins / 5) * 5
        if mins == 60:
            mins, h = 0, h + 1
        suffix = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{h12}:{mins:02d}{suffix}" if mins else f"{h12}{suffix}"
    return re.sub(r'\b([01]\d|2[0-3])([0-5]\d)\b', replace, text)


def normalize_duration(text):
    """Normalize duration shorthands and default to 1 hour if none present."""
    def expand(m):
        val, unit = m.group(1), m.group(2).lower()
        if unit == "h":
            n = float(val)
            return f"for {int(n * 60)} minutes" if n % 1 else f"for {int(n)} hour{'s' if n != 1 else ''}"
        return f"for {val} minutes"
    # normalize "for 2h" / "for 30m" first, then bare "2h" / "30m"
    text = re.sub(r'\bfor\s+(\d+(?:\.\d+)?)\s*(h|m)\b', expand, text, flags=re.IGNORECASE)
    text, n = re.subn(r'\b(\d+(?:\.\d+)?)\s*(h|m)\b', expand, text, flags=re.IGNORECASE)
    if n == 0 and not re.search(r'\bfor\s+\d', text, re.IGNORECASE):
        text += " for 1 hour"
    return text


def select_calendar_fzf():
    result = subprocess.run(
        ["fzf", "--prompt=  calendar: ", "--height=40%", "--reverse", "--no-info",
         "--header=enter to accept  esc to cancel"],
        input="\n".join(CALENDARS),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def select_area_fzf(areas):
    result = subprocess.run(
        ["fzf", "--prompt=  area: ", "--height=40%", "--reverse", "--no-info"],
        input="\n".join(a.name for a in areas),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def slug_from_name(name):
    return name.stem.lower().replace(" ", "-")


def restore_terminal():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    termios.tcsetattr(fd, termios.TCSADRAIN, old)


def route_journal(file):
    content = file.read_text().strip()
    insert_journal_bullet(content)
    file.unlink()
    console.print("[dark_sea_green4]  → journal[/dark_sea_green4]")


def route_calendar(file):
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else content
    console.print(f"\n  [grey50]adding to calendar:[/grey50] [tan]{first_line}[/tan]")
    console.print("  [grey50]confirm?[/grey50] [grey50][[/grey50][steel_blue1]y[/steel_blue1][grey50]/[/grey50][grey70]e[/grey70][grey50]dit/[/grey50][grey70]n[/grey70][grey50]][/grey50] ", end="")
    key = getch()
    console.print()
    if key == "n":
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    if key == "e":
        restore_terminal()
        console.print("  [grey50]event text:[/grey50] ", end="")
        try:
            first_line = input().strip() or first_line
        except EOFError:
            pass
    print()
    calendar = select_calendar_fzf()
    if not calendar:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    event_text = normalize_duration(convert_military_time(first_line))
    result = subprocess.run(["gcalcli", "quick", "--calendar", calendar, event_text])
    if result.returncode == 0:
        file.unlink()
        console.print(f"[dark_sea_green4]  → {calendar}[/dark_sea_green4]")
    else:
        console.print("[indian_red]  gcalcli failed — file kept in inbox[/indian_red]")


def route_note(file):
    content = file.read_text().strip()
    slug = slug_from_name(file)
    notes_path.mkdir(parents=True, exist_ok=True)
    dest = notes_path / f"{slug}.md"
    dest.write_text(f"""---
created: {datetime.now().strftime('%Y-%m-%d')}
tags: []
status: seed
---

{content}
""")
    file.unlink()
    console.print(f"[dark_sea_green4]  → saved to notes/{slug}.md[/dark_sea_green4]")


def route_task(file):
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else file.stem
    project_names = get_project_names()
    print()
    project = select_project_fzf(project_names)
    if not project:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    project_file = next((f for f in project_path.rglob("project.md") if f.parent.name == project), None)
    if not project_file:
        console.print(f"[indian_red]  project '{project}' not found — skipping[/indian_red]")
        return
    with project_file.open("a") as f:
        f.write(f"\n- [ ] {first_line}")
    file.unlink()
    console.print(f"[dark_sea_green4]  → task added to {project}[/dark_sea_green4]")


def route_new_project(file):
    """Create a new on-hold project from this inbox item."""
    content = file.read_text().strip()

    # Get project name
    restore_terminal()
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

    # Choose area — default to ideas/
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

    (project_dir / "project.md").write_text(f"""---
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

## Idea

{content}
""")
    file.unlink()
    console.print(f"[dark_sea_green4]  → new project: {area_name}/{slug}[/dark_sea_green4]")


def route_paste_project(file):
    """Attach this inbox file to an existing project folder."""
    project_names = get_project_names()
    print()
    project = select_project_fzf(project_names)
    if not project:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    project_file = next((f for f in project_path.rglob("project.md") if f.parent.name == project), None)
    if not project_file:
        console.print(f"[indian_red]  project '{project}' not found — skipping[/indian_red]")
        return
    dest = project_file.parent / file.name
    file.rename(dest)
    console.print(f"[dark_sea_green4]  → moved to {project}/{file.name}[/dark_sea_green4]")


def route_shopping(file, list_name):
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else content
    shopping_path.mkdir(parents=True, exist_ok=True)
    list_file = shopping_path / f"{list_name}.md"
    if not list_file.exists():
        list_file.write_text(f"# {list_name.capitalize()}\n\n")
    with list_file.open("a") as f:
        f.write(f"- [ ] {first_line}\n")
    file.unlink()
    console.print(f"[dark_sea_green4]  → added to {list_name}[/dark_sea_green4]")


def route_delete(file):
    console.print("  delete? [grey50][[/grey50][steel_blue1]y[/steel_blue1][grey50]/[/grey50][grey70]n[/grey70][grey50]][/grey50] ", end="")
    key = getch()
    print()
    if key == "y":
        file.unlink()
        console.print("[indian_red]  → deleted[/indian_red]")
    else:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")


def process_file(file, index, total):
    console.print()
    console.print(Rule(f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{file.name}[/tan]", style="grey23"))
    content = file.read_text().strip()
    console.print(Panel(content, border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {HOTKEYS}\n")

    while True:
        key = getch()
        if key == "j":
            route_journal(file)
            return True
        elif key == "c":
            route_calendar(file)
            return True
        elif key == "n":
            route_note(file)
            return True
        elif key == "t":
            route_task(file)
            return True
        elif key == "p":
            route_new_project(file)
            return True
        elif key == "v":
            route_paste_project(file)
            return True
        elif key == "g":
            route_shopping(file, "grocery")
            return True
        elif key == "h":
            route_shopping(file, "household")
            return True
        elif key == "s":
            console.print("[rosy_brown]  → skipped[/rosy_brown]")
            return True
        elif key == "d":
            route_delete(file)
            return True
        elif key == "q":
            return False


def main():
    files = sorted([
        f for f in inbox_path.iterdir()
        if f.is_file() and f.name != ".gitkeep"
    ])

    if not files:
        console.print()
        console.print("[grey50]  inbox is empty[/grey50]")
        console.print()
        return

    total = len(files)
    console.print()
    console.print(Rule(f"[bold steel_blue1]  Inbox[/bold steel_blue1]  [grey50]{total} files[/grey50]", style="steel_blue1 dim"))
    console.print()

    for i, file in enumerate(files, 1):
        keep_going = process_file(file, i, total)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
