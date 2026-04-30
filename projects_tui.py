"""
projects_tui.py — Textual TUI version of the projects reviewer.

Parallel to projects.py. Logic imported from projects.py.
Run via: lo projects --tui  (or automatically on Termux)
"""

import os
import subprocess
import sys
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Static

from tui_common import (
    TERMUX, COMMON_CSS,
    KeyButton, ConfirmScreen,
    apply_termux_css, git_push_kb, exit_to_launcher,
)
from projects import (
    get_all_reviewable,
    get_status, get_goal, open_task_count, status_color,
    set_status, reclassify,
)


class ProjectsApp(App):
    CSS = COMMON_CSS + """
    Screen {
        background: #0e1116;
        layers: base overlay;
    }
    #projects-header {
        height: 2;
        background: #0e1116;
        border-bottom: heavy #30363d;
        padding: 0 2;
        align: left middle;
    }
    #header-title {
        color: #a8c5a0;
        text-style: bold;
        width: auto;
    }
    #header-filename {
        color: #cdd9e5;
        margin-left: 2;
        width: 1fr;
        text-style: italic;
    }
    #header-counter {
        color: #768390;
        text-align: right;
        width: auto;
    }
    #content-scroll {
        background: #0e1116;
        border: round #30363d;
        margin: 0 1;
        scrollbar-size-vertical: 0;
    }
    #content-label {
        padding: 1 2;
        color: #cdd9e5;
    }
    #key-bar {
        height: 7;
        background: #0e1116;
        border-top: heavy #30363d;
        padding: 0 1;
    }
    .key-row {
        height: 3;
        align: left middle;
    }
    """

    BINDINGS = [
        Binding("k", "keep",        show=False),
        Binding("a", "activate",    show=False),
        Binding("h", "hold",        show=False),
        Binding("c", "complete",    show=False),
        Binding("x", "abandon",     show=False),
        Binding("r", "reclassify",  show=False),
        Binding("e", "edit",        show=False),
        Binding("q", "quit",        show=False),
    ]

    def __init__(self, files: list) -> None:
        super().__init__()
        self._files = list(files)
        self._total = len(files)
        self._index = 0

    @property
    def current_file(self) -> Path:
        return self._files[self._index]

    def _counter_text(self) -> str:
        return f"{self._index + 1} / {self._total}"

    def _summary(self, f: Path) -> str:
        content = f.read_text()
        is_project = f.name == "project.md"
        status = get_status(content)
        goal = get_goal(content)
        tasks = open_task_count(f)
        sc = status_color(status)

        type_label = "[grey50]project[/grey50]" if is_project else "[gold3]area[/gold3]"
        lines = [
            f"{type_label}  [{sc}]{status}[/{sc}]",
        ]
        if goal:
            lines.append(f"\n[grey50]goal[/grey50]  [grey80]{goal}[/grey80]")
        lines.append(f"[grey50]open tasks[/grey50]  [steel_blue1]{tasks}[/steel_blue1]")
        return "\n".join(lines)

    def _refresh_view(self) -> None:
        f = self.current_file
        self.query_one("#header-filename", Static).update(f.parent.name)
        self.query_one("#header-counter", Static).update(self._counter_text())
        self.query_one("#content-label", Static).update(self._summary(f))

    def _advance(self) -> None:
        self._index += 1
        if self._index >= self._total:
            self.exit()
            return
        scroll = self.query_one("#content-scroll")
        scroll.styles.background = "#2a3a2a"
        self.set_timer(0.15, lambda: setattr(scroll.styles, "background", "#251f1b"))
        self._refresh_view()
        scroll.scroll_home(animate=False)

    def compose(self) -> ComposeResult:
        f = self.current_file
        with Horizontal(id="projects-header"):
            yield Static("📁 PROJECTS", id="header-title")
            yield Static(f.parent.name, id="header-filename")
            yield Static(self._counter_text(), id="header-counter")
        with ScrollableContainer(id="content-scroll"):
            yield Static(self._summary(f), id="content-label")
        with Vertical(id="key-bar"):
            if TERMUX:
                with Horizontal(classes="key-row"):
                    yield KeyButton("k", "💾",  "keep")
                    yield KeyButton("a", "🟢",  "activate")
                    yield KeyButton("h", "🟡",  "hold")
                    yield KeyButton("c", "✅",  "complete")
                with Horizontal(classes="key-row"):
                    yield KeyButton("x", "🔴",  "abandon",    color="destructive")
                    yield KeyButton("r", "🔄",  "reclassify")
                    yield KeyButton("e", "✍️",  "edit",       color="muted")
                    yield KeyButton("q", "←",   "quit",       color="muted")
            else:
                with Horizontal(classes="key-row"):
                    yield KeyButton("k", "💾 keep",         "keep")
                    yield KeyButton("a", "🟢 activate",     "activate")
                    yield KeyButton("h", "⏸  hold",         "hold")
                    yield KeyButton("c", "✅ complete",      "complete")
                with Horizontal(classes="key-row"):
                    yield KeyButton("x", "🗑  abandon",      "abandon",    color="destructive")
                    yield KeyButton("r", "🔄 reclassify",   "reclassify")
                    yield KeyButton("e", "✍  edit",         "edit",       color="muted")
                    yield KeyButton("q", "✖  quit",         "quit",       color="muted")

    def action_keep(self) -> None:
        self._advance()

    def action_activate(self) -> None:
        f = self.current_file
        if get_status(f.read_text()) != "active":
            set_status(f, "active")
        self._advance()

    def action_hold(self) -> None:
        f = self.current_file
        if get_status(f.read_text()) == "active":
            set_status(f, "on-hold")
        self._advance()

    def action_complete(self) -> None:
        f = self.current_file
        content = f.read_text()
        if f.name == "project.md" and get_status(content) in ("active", "on-hold"):
            set_status(f, "complete")
        self._advance()

    def action_abandon(self) -> None:
        def handle(confirmed: bool) -> None:
            if confirmed:
                set_status(self.current_file, "abandoned")
            self._advance()
        self.push_screen(ConfirmScreen("Abandon this project?"), handle)

    def action_reclassify(self) -> None:
        reclassify(self.current_file)
        self._advance()

    def action_edit(self) -> None:
        editor = os.environ.get("EDITOR", "nvim")
        with self.suspend():
            subprocess.run([editor, str(self.current_file)])
        self._refresh_view()

    def action_quit(self) -> None:
        self.exit()


if TERMUX:
    ProjectsApp.CSS = apply_termux_css(ProjectsApp.CSS)


def run():
    items = get_all_reviewable()
    if not items:
        print("\n  nothing to review\n")
        return
    ProjectsApp(items).run()


def main():
    run()
    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
