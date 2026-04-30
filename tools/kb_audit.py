"""
kb_audit.py — read-only audit of ~/kb/projects/ structure.

Classifies every directory and flags inconsistencies against the CLIfe schema:
  area.md         → AREA          (forever, ongoing domain)
  project.md      → PROJECT       (discrete, completable)
  sub-project.md  → SUB-PROJECT   (track within a project)
"""

import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

KB = Path.home() / "kb"
PROJECTS = KB / "projects"

AREA_FIELDS = {"created", "status", "tags"}
PROJECT_FIELDS = {"created", "deadline", "status", "completed", "abandoned",
                  "sleeping", "last_reviewed", "area", "tags"}
SUBPROJECT_FIELDS = {"created", "status", "depends_on"}

VALID_STATUSES = {"active", "on-hold", "sleeping", "complete", "abandoned",
                  "archived", "superseded", "dormant", "pending"}


def parse_frontmatter(path):
    """Return dict of frontmatter fields, or empty dict."""
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}
    fm = {}
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def classify(d):
    """Return list of marker types found directly in directory d."""
    found = []
    for marker in ("area.md", "project.md", "sub-project.md"):
        if (d / marker).exists():
            found.append(marker[:-3])
    return found


SKIP_NAMES = {"__pycache__", "node_modules", "venv", ".venv", ".git"}


def is_skipped(d):
    """True if d is inside a venv, cache, or other ignored tree."""
    if any(part in SKIP_NAMES for part in d.parts):
        return True
    if (d / "pyvenv.cfg").exists():
        return True
    return False


def child_dirs(d):
    return [
        x for x in d.iterdir()
        if x.is_dir() and not x.name.startswith(".") and x.name not in SKIP_NAMES
    ]


def main():
    if not PROJECTS.exists():
        console.print(f"[red]No {PROJECTS} directory.[/red]")
        sys.exit(1)

    classifications = []
    for d in sorted(PROJECTS.rglob("*")):
        if not d.is_dir():
            continue
        if any(part.startswith(".") for part in d.parts):
            continue
        if is_skipped(d):
            continue
        classifications.append((d, classify(d)))

    top_levels = sorted(child_dirs(PROJECTS))

    console.print()
    console.print(Panel.fit(
        "[bold]CLIfe — kb structure audit[/bold]  read-only · 02 schema & migration",
        style="steel_blue1",
    ))
    console.print()

    # Top-level overview
    t = Table(title="Top-level dirs under projects/", show_lines=False)
    t.add_column("dir", style="bold")
    t.add_column("type")
    t.add_column("children", justify="right")
    t.add_column("status")

    for d in top_levels:
        types = classify(d)
        n_children = len(child_dirs(d))
        type_str = "+".join(types) if types else "[yellow]—[/yellow]"
        status = ""
        if "project" in types:
            status = parse_frontmatter(d / "project.md").get("status", "?")
        elif "area" in types:
            status = parse_frontmatter(d / "area.md").get("status", "?")
        t.add_row(d.name, type_str, str(n_children), status)

    console.print(t)
    console.print()

    # Inconsistency checks
    issues = defaultdict(list)

    for d, types in classifications:
        rel = d.relative_to(PROJECTS)
        depth = len(rel.parts)
        kids = child_dirs(d)

        # Only flag potential missing-area-marker for top-level / shallow dirs.
        # Subfolders inside projects (drafts/, notes/, etc.) are fine without markers.
        if not types and kids and depth == 1:
            issues["container-without-area-marker"].append(str(rel))

        if len(types) > 1:
            issues["multiple-markers"].append(f"{rel} → {types}")

        if depth == 1 and "project" in types:
            issues["top-level-project (no area parent)"].append(str(rel))

        if depth == 1 and not types and not kids:
            issues["stray-empty-top-level"].append(str(rel))

        if depth >= 2 and "project" in types:
            parent_types = classify(d.parent)
            if "project" in parent_types or "sub-project" in parent_types:
                issues["should-be-sub-project (uses project.md inside a project)"].append(str(rel))

        if "sub-project" in types:
            parent_types = classify(d.parent)
            if "project" not in parent_types and "sub-project" not in parent_types:
                issues["sub-project-without-project-parent"].append(str(rel))

        for t_type in types:
            marker_file = d / f"{t_type}.md"
            fm = parse_frontmatter(marker_file)
            expected = {
                "area": AREA_FIELDS,
                "project": PROJECT_FIELDS,
                "sub-project": SUBPROJECT_FIELDS,
            }[t_type]
            missing = expected - fm.keys()
            if missing:
                issues[f"{t_type}.md missing frontmatter fields"].append(
                    f"{rel} → missing: {', '.join(sorted(missing))}"
                )

            status = fm.get("status", "")
            if status and status not in VALID_STATUSES:
                issues["invalid status value"].append(f"{rel} ({t_type}) → status: {status}")

    if not issues:
        console.print("[green]No inconsistencies found.[/green]")
    else:
        for category, items in issues.items():
            console.print(f"[bold yellow]⚠ {category}[/bold yellow]  [grey50]({len(items)})[/grey50]")
            for item in items[:25]:
                console.print(f"   [grey70]{item}[/grey70]")
            if len(items) > 25:
                console.print(f"   [grey50]... and {len(items) - 25} more[/grey50]")
            console.print()

    n_areas = sum(1 for _, t in classifications if "area" in t)
    n_projects = sum(1 for _, t in classifications if "project" in t)
    n_sub = sum(1 for _, t in classifications if "sub-project" in t)
    console.print(
        f"[grey50]Totals:[/grey50]  {n_areas} areas · {n_projects} projects · {n_sub} sub-projects · "
        f"{len(classifications)} total dirs"
    )
    console.print()


if __name__ == "__main__":
    main()
