"""
week.py — `cl week` — Monday plan.

Output: ~/kb/weeks/YYYY-Www.md  (ISO week)
Streamlined. Shows last week's plan as context, prompts for top 3 + intentions.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

console = Console()

KB = Path.home() / "kb"
PROJECTS = KB / "projects"
WEEKS_DIR = KB / "weeks"
GCALCLI_CONFIG = str(Path.home() / ".config" / "gcalcli")

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"

# Match projects.py exclusions for consistency
EXCLUDED_TOP = {"infrastructure", "personal-life"}


def get_editor():
    return "nano" if os.path.isdir(TERMUX_BIN) else "nvim"


def week_monday():
    """Monday of the planning week. If today is Fri/Sat/Sun, plan next week."""
    today = datetime.now()
    days_since_monday = today.weekday()
    monday = today - timedelta(days=days_since_monday)
    if today.weekday() >= 4:
        monday += timedelta(weeks=1)
    return monday


def iso_week_path(monday):
    return WEEKS_DIR / f"{monday.strftime('%G-W%V')}.md"


def get_calendar_week(monday):
    sunday = monday + timedelta(days=6)
    if not Path(GCALCLI_CONFIG).exists():
        return ""
    result = subprocess.run(
        ["gcalcli", "--config-folder", GCALCLI_CONFIG, "agenda", "--nocolor",
         monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")],
        capture_output=True, text=True,
    )
    return re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)


def get_active_projects():
    out = []
    seen = set()
    for project_md in sorted(PROJECTS.rglob("project.md")):
        parts = project_md.parts
        top = parts[parts.index("projects") + 1]
        if top in EXCLUDED_TOP:
            continue
        content = project_md.read_text()
        if "status: active" not in content:
            continue
        label = project_md.parent.name
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "folder": project_md.parent, "area": top})
    return sorted(out, key=lambda p: p["label"])


def fzf_select_multi(items, prompt):
    result = subprocess.run(
        ["fzf", f"--prompt={prompt}", "--height=15", "--border", "--multi",
         "--bind=j:down,k:up", "--reverse", "--no-info"],
        input="\n".join(items), text=True, capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.strip().splitlines() if line]


def get_project_open_tasks(project_folder):
    tasks = []
    for f in Path(project_folder).rglob("*.md"):
        for line in f.read_text().splitlines():
            if "- [ ]" in line:
                tasks.append(line.strip())
    return tasks


def show_last_week():
    """If last week's plan exists, display it as context."""
    last_monday = week_monday() - timedelta(weeks=1)
    last_path = iso_week_path(last_monday)
    if not last_path.exists():
        return
    console.print()
    console.print(Rule(
        f"[grey50]  last week:[/grey50] [tan]{last_path.name}[/tan]",
        style="grey23",
    ))
    console.print(Panel(
        last_path.read_text(),
        border_style="grey30",
        padding=(0, 2),
    ))


def prompt_intentions(selected):
    intentions = {}
    console.print(
        "\n  [grey70]one-line intention per project — what does progress look like this week?[/grey70]"
    )
    for p in selected:
        tasks = get_project_open_tasks(p["folder"])
        if tasks:
            console.print(f"\n  [tan]{p['label']}[/tan]  [grey50](open tasks:)[/grey50]")
            for t in tasks[:5]:
                console.print(f"    [grey70]{t}[/grey70]")
            if len(tasks) > 5:
                console.print(f"    [grey50]... and {len(tasks) - 5} more[/grey50]")
        console.print(f"\n  [grey50]→[/grey50] ", end="")
        try:
            intention = input().strip()
        except EOFError:
            intention = ""
        intentions[p["label"]] = intention
    return intentions


def prompt_top3():
    console.print("\n  [grey70]top 3 priorities this week:[/grey70]")
    out = []
    for i in range(1, 4):
        console.print(f"  [grey50]{i}.[/grey50] ", end="")
        try:
            out.append(input().strip())
        except EOFError:
            out.append("")
    return out


def prompt_gratitudes():
    console.print("\n  [grey70]3 gratitudes:[/grey70]")
    out = []
    for i in range(1, 4):
        console.print(f"  [grey50]{i}.[/grey50] ", end="")
        try:
            out.append(input().strip())
        except EOFError:
            out.append("")
    return out


def build_content(monday, calendar, selected, intentions, priorities, gratitudes):
    priority_lines = "\n".join(f"- {p}" for p in priorities if p)
    gratitude_lines = "\n".join(f"- {g}" for g in gratitudes if g)

    project_sections = []
    for p in selected:
        intention = intentions.get(p["label"], "").strip()
        link = f"[[projects/{p['area']}/{p['label']}/project.md]]"
        if intention:
            project_sections.append(f"### {p['label']}\n_{intention}_\n\n{link}")
        else:
            project_sections.append(f"### {p['label']}\n\n{link}")
    projects_block = "\n\n".join(project_sections) or "_(none selected)_"

    cal_block = calendar.strip() if calendar.strip() else "_(no calendar configured)_"

    return f"""---
date: {monday.strftime('%Y-%m-%d')}
week: {monday.strftime('%G-W%V')}
---

# Week of {monday.strftime('%B %d, %Y')}

## Top 3

{priority_lines or "-"}

## Gratitudes

{gratitude_lines or "-"}

## Projects in Focus

{projects_block}

## Calendar

{cal_block}

## Notes / Retro

"""


def main():
    regenerate = "--regenerate" in sys.argv or "--overwrite" in sys.argv
    monday = week_monday()
    plan_path = iso_week_path(monday)

    if plan_path.exists() and not regenerate:
        os.execlp(get_editor(), get_editor(), str(plan_path))
        return

    WEEKS_DIR.mkdir(parents=True, exist_ok=True)

    show_last_week()

    console.print()
    console.print(Rule(
        f"[bold steel_blue1]  Week of {monday.strftime('%B %d, %Y')}[/bold steel_blue1]  "
        f"[grey50]{monday.strftime('%G-W%V')}[/grey50]",
        style="steel_blue1 dim",
    ))

    active = get_active_projects()
    if not active:
        console.print("\n  [grey50]no active projects[/grey50]\n")
        selected = []
    else:
        labels = [p["label"] for p in active]
        console.print()
        chosen_labels = fzf_select_multi(
            labels, prompt="  projects in focus (Tab to select, Enter to confirm): ",
        )
        selected = [p for p in active if p["label"] in chosen_labels]

    intentions = prompt_intentions(selected)
    priorities = prompt_top3()
    gratitudes = prompt_gratitudes()
    calendar = get_calendar_week(monday)

    content = build_content(monday, calendar, selected, intentions, priorities, gratitudes)
    plan_path.write_text(content)
    console.print(f"\n  [dark_sea_green4]→ wrote {plan_path.relative_to(KB)}[/dark_sea_green4]\n")
    os.execlp(get_editor(), get_editor(), str(plan_path))


if __name__ == "__main__":
    main()
