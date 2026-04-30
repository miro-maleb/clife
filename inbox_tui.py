"""
inbox_tui.py — Textual TUI version of the inbox processor.

Parallel to inbox.py (the original terminal version). Route logic imported
from inbox.py so it stays in one place.

Run via: lo inbox --tui
"""

import os
import subprocess
import sys
sys.path.insert(0, os.path.dirname(__file__))

from inbox import (
    inbox_path,
    project_path,
    get_project_names,
    get_project_areas,
    route_journal,
    route_note,
    route_shopping,
)

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Markdown, Static

from tui_common import (
    TERMUX, COMMON_CSS,
    KeyButton, ConfirmScreen, ProjectPickerScreen, NameInputScreen,
    apply_termux_css, git_push_kb, exit_to_launcher,
)

# ── Main app ──────────────────────────────────────────────────────────────────

class InboxApp(App):
    CSS = COMMON_CSS + """
    Screen {
        background: #0e1116;
        layers: base overlay;
    }

    /* ── Header ── */
    #inbox-header {
        height: 2;
        background: #0e1116;
        border-bottom: heavy #30363d;
        padding: 0 2;
        align: left middle;
    }
    #header-title {
        color: #e8a87c;
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

    /* ── Content well ── */
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

    /* ── Key bar ── */
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
    .key-sep {
        width: 1;
        height: 3;
        background: #1c2128;
        color: #30363d;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("j", "route_journal", show=False),
        Binding("n", "route_note", show=False),
        Binding("t", "route_task", show=False),
        Binding("p", "new_project", show=False),
        Binding("v", "paste_project", show=False),
        Binding("g", "route_grocery", show=False),
        Binding("h", "route_household", show=False),
        Binding("e", "edit", show=False),
        Binding("s", "skip", show=False),
        Binding("d", "delete", show=False),
        Binding("q", "quit", show=False),
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

    def _refresh_view(self) -> None:
        self.query_one("#header-filename", Static).update(self.current_file.name)
        self.query_one("#header-counter", Static).update(self._counter_text())
        self.query_one("#content-label", Markdown).update(
            self.current_file.read_text().strip()
        )

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

    def _do_route(self, fn, *args) -> None:
        fn(self.current_file, *args)
        self._advance()

    def compose(self) -> ComposeResult:
        with Horizontal(id="inbox-header"):
            yield Static("✉ INBOX", id="header-title")
            yield Static(self.current_file.name, id="header-filename")
            yield Static(self._counter_text(), id="header-counter")
        with ScrollableContainer(id="content-scroll"):
            yield Markdown(self.current_file.read_text().strip(), id="content-label")
        with Vertical(id="key-bar"):
            if TERMUX:
                # 2 rows of 6 icons for mobile touch targets
                with Horizontal(classes="key-row"):
                    yield KeyButton("j", "📖",  "route_journal")
                    yield KeyButton("n", "📝",  "route_note")
                    yield KeyButton("t", "✅",  "route_task")
                    yield KeyButton("p", "🌱",  "new_project")
                    yield KeyButton("v", "📁",  "paste_project")
                with Horizontal(classes="key-row"):
                    yield KeyButton("g", "🛒",  "route_grocery")
                    yield KeyButton("h", "🏠",  "route_household")
                    yield KeyButton("e", "✍️",  "edit",   color="muted")
                    yield KeyButton("s", "⏩",   "skip",   color="muted")
                    yield KeyButton("d", "🗑",   "delete", color="destructive")
                    yield KeyButton("q", "←",   "quit",   color="muted")
            else:
                # Desktop: key letter + icon + label
                with Horizontal(classes="key-row"):
                    yield KeyButton("j", "📖 journal",      "route_journal")
                    yield KeyButton("n", "📝 note",         "route_note")
                    yield KeyButton("t", "✅ task",         "route_task")
                    yield KeyButton("p", "🌱 new project",  "new_project")
                    yield KeyButton("v", "📁 → project",    "paste_project")
                with Horizontal(classes="key-row"):
                    yield KeyButton("g", "🛒 grocery",   "route_grocery")
                    yield KeyButton("h", "🏠 household", "route_household")
                    yield KeyButton("e", "✍  edit",      "edit",   color="muted")
                    yield KeyButton("s", "⏭  skip",      "skip",   color="muted")
                    yield KeyButton("d", "🗑  delete",    "delete", color="destructive")
                    yield KeyButton("q", "✖  quit",      "quit",   color="muted")

    def action_route_journal(self) -> None:
        self._do_route(route_journal)

    def action_route_note(self) -> None:
        self._do_route(route_note)

    def action_route_grocery(self) -> None:
        self._do_route(route_shopping, "grocery")

    def action_route_household(self) -> None:
        self._do_route(route_shopping, "household")

    def action_edit(self) -> None:
        editor = os.environ.get("EDITOR", "nvim")
        with self.suspend():
            subprocess.run([editor, str(self.current_file)])
        self._refresh_view()

    def action_skip(self) -> None:
        self._advance()

    def action_route_task(self) -> None:
        names = get_project_names()
        def handle(project: str | None) -> None:
            if project:
                self._do_route(_route_task, project)
        self.push_screen(ProjectPickerScreen(names), handle)

    def action_new_project(self) -> None:
        areas = get_project_areas()
        area_names = ["ideas"] + [a.name for a in areas if a.name != "ideas"]

        def handle_name(slug: str | None) -> None:
            if not slug:
                return
            slug = slug.strip().lower().replace(" ", "-")
            self._do_route(_route_new_project, slug, self._chosen_area)

        def handle_area(area_name: str | None) -> None:
            self._chosen_area = area_name or "ideas"
            self.push_screen(NameInputScreen("project name"), handle_name)

        self.push_screen(ProjectPickerScreen(area_names), handle_area)

    def action_paste_project(self) -> None:
        names = get_project_names()
        def handle(project: str | None) -> None:
            if project:
                self._do_route(_route_paste_project, project)
        self.push_screen(ProjectPickerScreen(names), handle)

    def action_delete(self) -> None:
        def handle(confirmed: bool) -> None:
            if confirmed:
                self.current_file.unlink()
                self._advance()
        self.push_screen(ConfirmScreen(), handle)

    def action_quit(self) -> None:
        self.exit()


if TERMUX:
    InboxApp.CSS = apply_termux_css(InboxApp.CSS)


# ── TUI-specific route variants (no fzf, no getch) ───────────────────────────

def _route_task(file: Path, project: str) -> None:
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else file.stem
    project_file = next(
        (f for f in project_path.rglob("project.md") if f.parent.name == project), None
    )
    if project_file:
        with project_file.open("a") as f:
            f.write(f"\n- [ ] {first_line}")
    file.unlink()


def _route_paste_project(file: Path, project: str) -> None:
    project_file = next(
        (f for f in project_path.rglob("project.md") if f.parent.name == project), None
    )
    if project_file:
        file.rename(project_file.parent / file.name)
    else:
        file.unlink()


def _route_new_project(file: Path, slug: str, area_name: str) -> None:
    from datetime import datetime
    content = file.read_text().strip()
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    files = sorted([
        f for f in inbox_path.iterdir()
        if f.is_file() and f.name != ".gitkeep"
    ])
    if not files:
        print("\n  inbox is empty\n")
        return
    InboxApp(files).run()
    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
