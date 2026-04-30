import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

project_path = Path.home() / "kb" / "projects"
plans_dir = Path.home() / "kb" / "journal" / "plans"
config_dir = str(Path.home() / ".config" / "gcalcli")

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
EXCLUDED_PROJECTS = {"life-os", "personal-life", "daily-tasks"}

def get_editor():
    return "nano" if os.path.isdir(TERMUX_BIN) else "nvim"

def week_monday():
    today = datetime.now()
    days_since_monday = today.weekday()
    monday = today - timedelta(days=days_since_monday)
    if today.weekday() >= 4:
        monday += timedelta(weeks=1)
    return monday

def get_calendar_week(monday):
    sunday = monday + timedelta(days=6)
    result = subprocess.run(
        ["gcalcli", "--config-folder", config_dir, "agenda", "--nocolor",
         monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")],
        capture_output=True, text=True
    )
    return re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)

def get_active_projects():
    projects = []
    seen_labels = set()
    for project_file in sorted(project_path.rglob("project.md")):
        parts = project_file.parts
        top_level = parts[parts.index("projects") + 1]
        if top_level in EXCLUDED_PROJECTS:
            continue
        content = project_file.read_text()
        if "status: active" in content and "status: on-hold" not in content:
            label = project_file.parent.name
            if label not in seen_labels:
                seen_labels.add(label)
                projects.append({"label": label, "folder": project_file.parent})
    return sorted(projects, key=lambda p: p["label"])


def fzf_select_multi(items, prompt="Select: "):
    result = subprocess.run(
        ["fzf", f"--prompt={prompt}", "--height=15", "--border", "--multi", "--bind=j:down,k:up"],
        input="\n".join(items), text=True, capture_output=True
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.strip().splitlines() if line]

def prompt_projects(active_projects):
    labels = [p["label"] for p in active_projects]
    selected_labels = fzf_select_multi(labels, prompt="Projects in focus this week (Tab to select, Enter to confirm): ")
    return [p for p in active_projects if p["label"] in selected_labels]

def get_project_tasks(project_folder):
    tasks = []
    for file in Path(project_folder).rglob("*.md"):
        for line in file.read_text().splitlines():
            if "- [ ]" in line:
                tasks.append(line.strip())
    return tasks

def prompt_intentions(selected_projects):
    intentions = {}
    print("\nOne-line intention for each project (what does progress look like this week?):")
    for p in selected_projects:
        tasks = get_project_tasks(p["folder"])
        if tasks:
            print(f"\n  {p['label']} — open tasks:")
            for t in tasks:
                print(f"    {t}")
        intention = input(f"  {p['label']}: ").strip()
        intentions[p["label"]] = intention
    return intentions

def prompt_priorities():
    print("\nTop 3 priorities this week:")
    priorities = []
    for i in range(1, 4):
        p = input(f"  {i}. ").strip()
        priorities.append(p)
    return priorities

def prompt_gratitudes():
    print("\n3 gratitudes:")
    gratitudes = []
    for i in range(1, 4):
        g = input(f"  {i}. ").strip()
        gratitudes.append(g)
    return gratitudes

def build_content(monday, calendar, selected_projects, intentions, priorities, gratitudes):
    priority_lines = "\n".join(f"- {p}" for p in priorities)
    gratitude_lines = "\n".join(f"- {g}" for g in gratitudes)

    project_sections = []
    for p in selected_projects:
        name = p["label"]
        project_file = p["folder"] / "project.md"
        link = f"[[projects/{project_file.relative_to(Path.home() / 'kb')}]]" if project_file.exists() else name
        section = f"### {name}\n_{intentions[name]}_\n\n{link}"
        project_sections.append(section)

    projects_content = "\n\n".join(project_sections)

    return f"""---
date: {monday.strftime('%Y-%m-%d')}
week: {monday.strftime('%Y-W%W')}
---

# Week of {monday.strftime('%B %d, %Y')}

## Gratitudes

{gratitude_lines}

## Priorities

{priority_lines}

## Projects in Focus

{projects_content}

## Calendar

{calendar}
## Notes

"""

def main():
    editor = get_editor()
    regenerate = "--regenerate" in sys.argv or "--overwrite" in sys.argv
    monday = week_monday()
    plan_path = plans_dir / f"{monday.strftime('%Y-%m-%d')}-week.md"

    if plan_path.exists() and not regenerate:
        os.execlp(editor, editor, str(plan_path))
        return

    plans_dir.mkdir(parents=True, exist_ok=True)

    active_projects = get_active_projects()
    selected = prompt_projects(active_projects)
    intentions = prompt_intentions(selected)
    priorities = prompt_priorities()
    gratitudes = prompt_gratitudes()
    calendar = get_calendar_week(monday)

    content = build_content(monday, calendar, selected, intentions, priorities, gratitudes)
    plan_path.write_text(content)
    os.execlp(editor, editor, str(plan_path))

if __name__ == "__main__":
    main()
