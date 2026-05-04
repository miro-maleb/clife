"""
tree.py — `cl tree` — bird's-eye view of areas → projects → sub-projects.

Default: areas + projects with status color and open task count.
--full   adds sub-projects under each project.
--active only shows active projects (and their parent areas).
"""

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.tree import Tree

import projects as proj

console = Console()

KB = Path.home() / "kb"
PROJECTS = KB / "projects"
SYSTEMS = KB / "systems"
GOALS = KB / "goals"
ORIENTATIONS = KB / "orientations"


def fmt_project(proj_dir, status, open_tasks):
    color = proj.status_color(status)
    label = f"[bold]{proj_dir.name}[/bold]  [{color}]{status}[/{color}]"
    if open_tasks > 0:
        label += f"  [grey50]{open_tasks} open[/grey50]"
    return label


def fmt_subproject(sub_dir, status):
    color = proj.status_color(status)
    return f"{sub_dir.name}  [{color}]{status}[/{color}]"


def render_systems_tree(active_only):
    """Build a Tree of systems → blocks. Returns (tree, n_systems, n_blocks)."""
    tree = Tree("[bold steel_blue1]kb/systems[/bold steel_blue1]")
    n_systems = 0
    n_blocks = 0
    if not SYSTEMS.exists():
        return tree, n_systems, n_blocks
    for sys_dir in sorted(SYSTEMS.iterdir()):
        if not sys_dir.is_dir():
            continue
        sf = sys_dir / "system.md"
        if not sf.exists():
            continue
        status = proj.get_status(sf.read_text())
        if active_only and status != "active":
            continue
        n_systems += 1
        color = proj.status_color(status)
        label = f"[bold]{sys_dir.name}[/bold]  [{color}]{status}[/{color}]"
        sys_branch = tree.add(label)
        bd = sys_dir / "blocks"
        if not bd.exists():
            continue
        for bf in sorted(bd.iterdir()):
            if bf.suffix != ".md":
                continue
            n_blocks += 1
            sys_branch.add(f"[grey70]{bf.stem}[/grey70]")
    return tree, n_systems, n_blocks


def render_orientations_tree(active_only):
    """Build a flat Tree of orientations. Returns (tree, n_orientations)."""
    tree = Tree("[bold steel_blue1]kb/orientations[/bold steel_blue1]")
    n = 0
    if not ORIENTATIONS.exists():
        return tree, n
    for of in sorted(ORIENTATIONS.glob("*.md")):
        status = proj.get_status(of.read_text())
        if active_only and status != "active":
            continue
        n += 1
        color = proj.status_color(status)
        tree.add(f"[bold]{of.stem}[/bold]  [{color}]{status}[/{color}]")
    return tree, n


def render_goals_tree(active_only):
    """Build a Tree of year → goals. Returns (tree, n_goals)."""
    tree = Tree("[bold steel_blue1]kb/goals[/bold steel_blue1]")
    n_goals = 0
    if not GOALS.exists():
        return tree, n_goals
    for year_dir in sorted(GOALS.iterdir()):
        if not year_dir.is_dir():
            continue
        year_goals = []
        for gf in sorted(year_dir.glob("*.md")):
            status = proj.get_status(gf.read_text())
            if active_only and status != "active":
                continue
            year_goals.append((gf, status))
        if active_only and not year_goals:
            continue
        year_branch = tree.add(f"[tan]{year_dir.name}/[/tan]")
        for gf, status in year_goals:
            n_goals += 1
            color = proj.status_color(status)
            year_branch.add(f"[bold]{gf.stem}[/bold]  [{color}]{status}[/{color}]")
    return tree, n_goals


def main():
    parser = argparse.ArgumentParser(prog="cl tree")
    parser.add_argument("--full", action="store_true",
                        help="include sub-projects under each project")
    parser.add_argument("--active", action="store_true",
                        help="only active projects (and their parent areas)")
    args = parser.parse_args()

    tree = Tree("[bold steel_blue1]kb/projects[/bold steel_blue1]")

    n_areas = 0
    n_projects = 0
    n_subprojects = 0

    for area_dir in sorted(PROJECTS.iterdir()):
        if not area_dir.is_dir() or not (area_dir / "area.md").exists():
            continue

        # Collect every project.md under this area (recursive — picks up
        # projects nested in sub-areas like retreats/cooking/<event>/).
        area_projects = []
        for project_md in sorted(area_dir.rglob("project.md")):
            project_dir = project_md.parent
            content = project_md.read_text()
            status = proj.get_status(content)
            if args.active and status != "active":
                continue
            tasks = proj.open_task_count(project_md)
            # Display path relative to the area (so nested projects show their
            # sub-area context, e.g. "cooking/pbc-jun-2026").
            display_path = project_dir.relative_to(area_dir)
            area_projects.append((project_dir, display_path, status, tasks))

        if args.active and not area_projects:
            continue

        n_areas += 1
        area_branch = tree.add(f"[tan]{area_dir.name}/[/tan]")

        for project_dir, display_path, status, tasks in area_projects:
            n_projects += 1
            color = proj.status_color(status)
            label = f"[bold]{display_path}[/bold]  [{color}]{status}[/{color}]"
            if tasks > 0:
                label += f"  [grey50]{tasks} open[/grey50]"
            project_branch = area_branch.add(label)

            if args.full:
                for sub_dir in sorted(project_dir.iterdir()):
                    sp_file = sub_dir / "sub-project.md"
                    if not sub_dir.is_dir() or not sp_file.exists():
                        continue
                    sp_status = proj.get_status(sp_file.read_text())
                    n_subprojects += 1
                    project_branch.add(fmt_subproject(sub_dir, sp_status))

    console.print()
    console.print(tree)

    n_systems = n_blocks = n_goals = n_orientations = 0
    if args.full:
        sys_tree, n_systems, n_blocks = render_systems_tree(args.active)
        goals_tree, n_goals = render_goals_tree(args.active)
        orient_tree, n_orientations = render_orientations_tree(args.active)
        console.print()
        console.print(sys_tree)
        console.print()
        console.print(goals_tree)
        console.print()
        console.print(orient_tree)

    summary = f"  [grey50]{n_areas} areas · {n_projects} projects"
    if args.full:
        summary += (
            f" · {n_subprojects} sub-projects"
            f" · {n_systems} systems · {n_blocks} blocks"
            f" · {n_goals} goals · {n_orientations} orientations"
        )
    if args.active:
        summary += " · active only"
    summary += "[/grey50]"
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
