"""
review.py — `cl review` — comprehensive periodic review.

Walks:
  1. Stats overview
  2. Areas — gentle prompt per area
  3. Projects (incl. sleeping) — calls projects.main with REVIEW_STATUSES
  4. Open questions — surfaces `## Open questions` sections across kb
  5. Inbox — calls inbox.main if non-empty
  6. Stale — files unmodified for > 90 days (excluding terminal-state)

Cadence: every 2 weeks or monthly. ~30 min.
"""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

sys.path.insert(0, os.path.dirname(__file__))

import projects
import inbox

console = Console()

KB = Path.home() / "kb"
PROJECTS_DIR = KB / "projects"
INBOX_DIR = KB / "inbox"
STALE_DAYS = 90


def getch():
    import tty, termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pause(label="press any key to continue, q to quit"):
    console.print(f"  [grey35]— {label} —[/grey35]  ", end="")
    key = getch()
    console.print()
    return key != "q"


def stats():
    counts = {"active": 0, "on-hold": 0, "sleeping": 0, "complete": 0,
              "abandoned": 0, "archived": 0}
    for md in PROJECTS_DIR.rglob("project.md"):
        if projects.get_top_folder(md) in projects.EXCLUDED_TOP:
            continue
        s = projects.get_status(md.read_text())
        counts[s] = counts.get(s, 0) + 1

    inbox_count = 0
    if INBOX_DIR.exists():
        inbox_count = sum(
            1 for f in INBOX_DIR.iterdir()
            if f.is_file() and f.name != ".gitkeep"
        )

    n_areas = sum(
        1 for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and (d / "area.md").exists()
    )

    t = Table.grid(padding=(0, 2))
    t.add_column(style="grey50")
    t.add_column()
    t.add_row("areas", f"[steel_blue1]{n_areas}[/steel_blue1]")
    t.add_row("active projects", f"[steel_blue1]{counts.get('active', 0)}[/steel_blue1]")
    t.add_row("on-hold", f"[rosy_brown]{counts.get('on-hold', 0)}[/rosy_brown]")
    t.add_row("sleeping", f"[grey50]{counts.get('sleeping', 0)}[/grey50]")
    t.add_row("complete (un-archived)", f"[dark_sea_green4]{counts.get('complete', 0)}[/dark_sea_green4]")
    t.add_row("inbox unprocessed", f"[gold3]{inbox_count}[/gold3]" if inbox_count else "[grey50]0[/grey50]")
    console.print(Panel(t, title="[grey70]state[/grey70]", border_style="grey30", padding=(1, 2)))
    console.print()


def section_areas():
    console.print(Rule("[bold steel_blue1]  Areas[/bold steel_blue1]", style="grey23"))
    console.print()
    areas = sorted([
        d for d in PROJECTS_DIR.iterdir()
        if d.is_dir() and (d / "area.md").exists()
    ])
    for area in areas:
        active_count = 0
        for proj_md in area.rglob("project.md"):
            if projects.get_status(proj_md.read_text()) == "active":
                active_count += 1
        console.print(
            f"  [tan]{area.name}[/tan]  [grey50]·[/grey50]  "
            f"[steel_blue1]{active_count}[/steel_blue1] [grey50]active[/grey50]"
        )
    console.print()
    console.print(
        "  [grey50]any area need attention you've been avoiding?[/grey50]\n"
        "  [grey50]anything that's grown into a project? promote with `cl new --project`[/grey50]"
    )
    console.print()


def section_projects():
    console.print(Rule("[bold steel_blue1]  Projects[/bold steel_blue1]", style="grey23"))
    projects.main(statuses=projects.REVIEW_STATUSES)


def section_open_questions():
    console.print(Rule("[bold steel_blue1]  Open questions[/bold steel_blue1]", style="grey23"))
    console.print()
    found = 0
    for marker in ("project.md", "sub-project.md"):
        for md in sorted(PROJECTS_DIR.rglob(marker)):
            if projects.get_top_folder(md) in projects.EXCLUDED_TOP:
                continue
            content = md.read_text()
            if projects.get_status(content) in ("complete", "abandoned", "archived", "superseded"):
                continue
            m = re.search(
                r"^## Open questions\s*\n(.+?)(?=^## |\Z)",
                content,
                flags=re.MULTILINE | re.DOTALL,
            )
            if not m:
                continue
            block = m.group(1).strip()
            if not block:
                continue
            rel = md.parent.relative_to(PROJECTS_DIR)
            console.print(f"  [tan]{rel}[/tan]")
            for line in block.splitlines():
                if line.strip():
                    console.print(f"    [grey70]{line}[/grey70]")
            console.print()
            found += 1
    if not found:
        console.print("  [grey50]no open questions surfaced.[/grey50]\n")


def section_inbox():
    console.print(Rule("[bold steel_blue1]  Inbox[/bold steel_blue1]", style="grey23"))
    if not INBOX_DIR.exists():
        console.print("\n  [grey50]inbox dir doesn't exist[/grey50]\n")
        return
    files = [f for f in INBOX_DIR.iterdir() if f.is_file() and f.name != ".gitkeep"]
    if not files:
        console.print("\n  [grey50]inbox empty[/grey50]\n")
        return
    console.print(f"\n  [gold3]{len(files)}[/gold3] [grey50]unprocessed items — running cl inbox[/grey50]\n")
    inbox.main()


def section_stale():
    console.print(Rule(
        f"[bold steel_blue1]  Stale[/bold steel_blue1]  [grey50](>{STALE_DAYS}d unmodified)[/grey50]",
        style="grey23",
    ))
    console.print()
    cutoff = time.time() - STALE_DAYS * 86400
    stale = []
    for marker in ("project.md", "sub-project.md"):
        for md in PROJECTS_DIR.rglob(marker):
            if projects.get_top_folder(md) in projects.EXCLUDED_TOP:
                continue
            content = md.read_text()
            status = projects.get_status(content)
            if status in ("complete", "abandoned", "archived", "superseded"):
                continue
            if md.stat().st_mtime >= cutoff:
                continue
            stale.append((md, status))
    if not stale:
        console.print("  [grey50]nothing stale.[/grey50]\n")
        return
    for md, status in sorted(stale, key=lambda t: t[0].stat().st_mtime):
        rel = md.parent.relative_to(PROJECTS_DIR)
        days = int((time.time() - md.stat().st_mtime) // 86400)
        console.print(
            f"  [tan]{rel}[/tan]  "
            f"[{projects.status_color(status)}]{status}[/{projects.status_color(status)}]  "
            f"[grey50]{days}d ago[/grey50]"
        )
    console.print(
        "\n  [grey50]consider: sleep, abandon, or just touch with a fresh task[/grey50]\n"
    )


SECTIONS = [
    ("areas",          section_areas),
    ("projects",       section_projects),
    ("open questions", section_open_questions),
    ("inbox",          section_inbox),
    ("stale",          section_stale),
]


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    console.print()
    console.print(Rule(
        f"[bold steel_blue1]  Full Review[/bold steel_blue1]  [grey50]{today}[/grey50]",
        style="steel_blue1 dim",
    ))
    console.print()
    stats()

    for name, fn in SECTIONS:
        fn()
        if not pause(f"{name} done. press any key, q to quit"):
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
