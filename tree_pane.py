"""
tree_pane.py — Textual TUI for the dashboard's project-tree pane.

Top-of-pane tabs switch between views of kb/:
  active   active projects only (default)
  all      every project regardless of status
  goals    kb/goals, organised by year
  orient   kb/orientations, flat list
  systems  kb/systems → blocks

Live-refreshes every 60s. Designed for the left pane of `cl dashboard`.

Enter or click on a node dispatches into the dashboard's center pane
(tmux `dashboard:main.2`): area / project / sub-project → `cl show <path>`,
everything else → `nvim <path>`. No-op when not running inside the dashboard.
"""

import os
import shlex
import sys
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Tab, Tabs, Tree

sys.path.insert(0, os.path.dirname(__file__))

import projects as proj
from tui_common import (
    ACCENT, ACCENT_DIM, BG, BODY, BORDER, MUTED,
    dispatch_subprocess,
)

CL = str(Path(__file__).parent / "cl")
OVERVIEW_TYPES = {"area", "project", "sub-project"}
FILE_TYPES = {"goal", "orientation", "system", "block"}

KB           = Path.home() / "kb"
PROJECTS     = KB / "projects"
SYSTEMS      = KB / "systems"
GOALS        = KB / "goals"
ORIENTATIONS = KB / "orientations"


CSS = f"""
Screen {{
    background: {BG};
    color: {BODY};
}}

Tabs {{
    background: {BG};
    height: 2;
}}
Tab {{
    color: {MUTED};
    padding: 0 1;
}}
Tab.-active {{
    color: {ACCENT};
    text-style: bold;
}}
Underline > .underline--bar {{
    color: {ACCENT};
    background: {BORDER};
}}
Tabs:focus .underline--bar {{
    background: {BORDER};
}}
Tabs:focus .-active {{
    color: {ACCENT};
    background: {BG};
    text-style: bold;
}}

#tree {{
    background: {BG};
    color: {BODY};
    height: 1fr;
    padding: 0 1;
    scrollbar-size-vertical: 0;
}}

Tree > .tree--cursor {{
    background: {BORDER};
    color: {ACCENT};
    text-style: bold;
}}
Tree:focus > .tree--cursor {{
    background: {BORDER};
    color: {ACCENT};
    text-style: bold;
}}
Tree > .tree--guides {{
    color: {BORDER};
}}
Tree > .tree--guides-selected {{
    color: {ACCENT};
}}
"""


VIEWS = [
    ("active",  "active"),
    ("all",     "all"),
    ("goals",   "goals"),
    ("orient",  "orient"),
    ("systems", "systems"),
]


def _label(markup: str) -> Text:
    return Text.from_markup(markup)


class TreePaneApp(App):
    CSS = CSS
    BINDINGS = [
        Binding("r", "refresh",   "refresh"),
        Binding("o", "open_in_nvim", "edit"),
        Binding("q", "quit",      "quit"),
        Binding("1", "show('active')",  show=False),
        Binding("2", "show('all')",     show=False),
        Binding("3", "show('goals')",   show=False),
        Binding("4", "show('orient')",  show=False),
        Binding("5", "show('systems')", show=False),
    ]

    def __init__(self, active_only: bool = True) -> None:
        super().__init__()
        # Initial view: --active starts on "active", otherwise "all".
        self._current_view = "active" if active_only else "all"

    def compose(self) -> ComposeResult:
        yield Tabs(*(Tab(label, id=tab_id) for tab_id, label in VIEWS))
        yield Tree("·", id="tree")

    def on_mount(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        tree.guide_depth = 2

        tabs = self.query_one(Tabs)
        tabs.active = self._current_view

        self._refresh()
        self.set_interval(60, self._refresh)

    # ── tab + key handlers ────────────────────────────────────────────────────

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab is None:
            return
        self._current_view = event.tab.id
        self._refresh()

    def action_show(self, view_id: str) -> None:
        tabs = self.query_one(Tabs)
        tabs.active = view_id

    def action_refresh(self) -> None:
        self._refresh()

    def action_open_in_nvim(self) -> None:
        """`o` — open the cursor node's file in nvim in the center pane,
        regardless of node_type. Enter still gives you the quick `cl show`
        view; this is for when you want to actually edit."""
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if not node or not node.data:
            return
        path = node.data.get("path")
        if not path:
            return
        self._dispatch_to_center(f"nvim {shlex.quote(str(path))}")

    # ── click / Enter → dispatch into the dashboard center pane ──────────────

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        path = data.get("path")
        node_type = data.get("type")
        if not path:
            return

        if node_type in OVERVIEW_TYPES:
            cmd = f"{shlex.quote(CL)} show {shlex.quote(str(path))}"
        elif node_type in FILE_TYPES:
            cmd = f"nvim {shlex.quote(str(path))}"
        else:
            return  # grouping nodes (year, etc.) — no action

        self._dispatch_to_center(cmd)

    @work(exclusive=False)
    async def _dispatch_to_center(self, cmd: str) -> None:
        """Send a command into the dashboard center pane off the UI thread.
        Doing this synchronously would freeze the whole left pane if tmux
        send-keys stalls — see tui_common.dispatch_subprocess for the why."""
        rc = await dispatch_subprocess(cmd)
        if rc is None:
            self.notify("dashboard dispatch timed out", severity="warning", timeout=2)
        elif rc != 0:
            self.notify("dashboard center pane unavailable", severity="warning", timeout=2)

    # ── render dispatch ───────────────────────────────────────────────────────

    def _refresh(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.clear()

        v = self._current_view
        if v == "active":
            self._load_projects(tree, active_only=True)
        elif v == "all":
            self._load_projects(tree, active_only=False)
        elif v == "goals":
            self._load_goals(tree)
        elif v == "orient":
            self._load_orientations(tree)
        elif v == "systems":
            self._load_systems(tree)

    # ── per-view loaders ──────────────────────────────────────────────────────

    def _load_projects(self, tree: Tree, active_only: bool) -> None:
        if not PROJECTS.exists():
            return
        for area_dir in sorted(PROJECTS.iterdir()):
            if not area_dir.is_dir() or not (area_dir / "area.md").exists():
                continue

            area_projects = []
            for project_md in sorted(area_dir.rglob("project.md")):
                try:
                    content = project_md.read_text()
                except OSError:
                    continue
                status = proj.get_status(content)
                if active_only and status != "active":
                    continue
                tasks = proj.open_task_count(project_md)
                display_path = project_md.parent.relative_to(area_dir)
                area_projects.append((project_md, display_path, status, tasks))

            if active_only and not area_projects:
                continue

            area_node = tree.root.add(
                _label(f"[{ACCENT_DIM}]{area_dir.name}/[/{ACCENT_DIM}]"),
                expand=True,
                data={"path": area_dir, "type": "area"},
            )
            for project_md, display_path, status, tasks in area_projects:
                color = proj.status_color(status)
                label = (
                    f"[bold {BODY}]{display_path}[/bold {BODY}]  "
                    f"[{color}]{status}[/{color}]"
                )
                if tasks > 0:
                    label += f"  [{MUTED}]{tasks} open[/{MUTED}]"

                sub_mds = []
                for child in sorted(project_md.parent.iterdir()):
                    sp = child / "sub-project.md"
                    if child.is_dir() and sp.exists():
                        try:
                            sp_status = proj.get_status(sp.read_text())
                        except OSError:
                            continue
                        sub_mds.append((child, sp, sp_status))

                if sub_mds:
                    project_node = area_node.add(
                        _label(label),
                        expand=False,
                        data={"path": project_md, "type": "project"},
                    )
                    for sub_dir, sp, sp_status in sub_mds:
                        sp_color = proj.status_color(sp_status)
                        sp_label = (
                            f"[{BODY}]{sub_dir.name}[/{BODY}]  "
                            f"[{sp_color}]{sp_status}[/{sp_color}]"
                        )
                        project_node.add_leaf(
                            _label(sp_label),
                            data={"path": sp, "type": "sub-project"},
                        )
                else:
                    area_node.add_leaf(
                        _label(label),
                        data={"path": project_md, "type": "project"},
                    )

    def _load_goals(self, tree: Tree) -> None:
        if not GOALS.exists():
            return
        for year_dir in sorted(GOALS.iterdir()):
            if not year_dir.is_dir():
                continue
            year_goals = list(sorted(year_dir.glob("*.md")))
            if not year_goals:
                continue
            year_node = tree.root.add(
                _label(f"[{ACCENT_DIM}]{year_dir.name}/[/{ACCENT_DIM}]"),
                expand=True,
                data={"path": year_dir, "type": "year"},
            )
            for gf in year_goals:
                try:
                    status = proj.get_status(gf.read_text())
                except OSError:
                    continue
                color = proj.status_color(status)
                year_node.add_leaf(
                    _label(
                        f"[bold {BODY}]{gf.stem}[/bold {BODY}]  "
                        f"[{color}]{status}[/{color}]"
                    ),
                    data={"path": gf, "type": "goal"},
                )

    def _load_orientations(self, tree: Tree) -> None:
        if not ORIENTATIONS.exists():
            return
        for of in sorted(ORIENTATIONS.glob("*.md")):
            try:
                status = proj.get_status(of.read_text())
            except OSError:
                continue
            color = proj.status_color(status)
            tree.root.add_leaf(
                _label(
                    f"[bold {BODY}]{of.stem}[/bold {BODY}]  "
                    f"[{color}]{status}[/{color}]"
                ),
                data={"path": of, "type": "orientation"},
            )

    def _load_systems(self, tree: Tree) -> None:
        if not SYSTEMS.exists():
            return
        for sys_dir in sorted(SYSTEMS.iterdir()):
            if not sys_dir.is_dir():
                continue
            sf = sys_dir / "system.md"
            if not sf.exists():
                continue
            try:
                status = proj.get_status(sf.read_text())
            except OSError:
                continue
            color = proj.status_color(status)
            sys_node = tree.root.add(
                _label(
                    f"[bold {BODY}]{sys_dir.name}[/bold {BODY}]  "
                    f"[{color}]{status}[/{color}]"
                ),
                expand=True,
                data={"path": sf, "type": "system"},
            )
            bd = sys_dir / "blocks"
            if not bd.exists():
                continue
            for bf in sorted(bd.iterdir()):
                if bf.suffix != ".md":
                    continue
                sys_node.add_leaf(
                    _label(f"[{MUTED}]{bf.stem}[/{MUTED}]"),
                    data={"path": bf, "type": "block"},
                )


def run(active_only: bool = True) -> None:
    TreePaneApp(active_only=active_only).run()


if __name__ == "__main__":
    run()
