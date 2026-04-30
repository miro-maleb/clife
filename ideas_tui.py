"""
ideas_tui.py — Textual TUI version of the ideas reviewer.

Parallel to ideas.py. Data helpers imported from ideas.py.
Run via: lo ideas --tui  (or automatically on Termux)
"""

import os
import subprocess
import sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
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
from ideas import (
    ideas_path, get_project_areas,
    get_frontmatter_field, get_title, get_body,
)


class IdeasApp(App):
    CSS = COMMON_CSS + """
    Screen {
        background: #1a1612;
        layers: base overlay;
    }
    #ideas-header {
        height: 3;
        background: #251f1b;
        border-bottom: tall #4a3f38;
        padding: 0 3;
        align: left middle;
    }
    #header-title {
        color: #c9a87c;
        text-style: bold;
        width: auto;
    }
    #header-filename {
        color: #e8ddd4;
        margin-left: 2;
        width: 1fr;
        text-style: italic;
    }
    #header-counter {
        color: #6b5c54;
        text-align: right;
        width: auto;
        background: #312a25;
        padding: 0 2;
    }
    #content-scroll {
        background: #251f1b;
        scrollbar-size-vertical: 0;
    }
    #content-label {
        padding: 1 2;
        color: #e8ddd4;
    }
    #key-bar {
        height: 5;
        background: #251f1b;
        border-top: solid #4a3f38;
        padding: 0 1;
    }
    .key-row {
        height: 3;
        align: left middle;
    }
    """

    BINDINGS = [
        Binding("k", "keep",    show=False),
        Binding("p", "promote", show=False),
        Binding("e", "edit",    show=False),
        Binding("d", "discard", show=False),
        Binding("q", "quit",    show=False),
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
        f = self.current_file
        title = get_title(f.read_text()) or f.stem
        self.query_one("#header-filename", Static).update(title)
        self.query_one("#header-counter", Static).update(self._counter_text())
        self.query_one("#content-label", Markdown).update(f.read_text().strip())

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
        title = get_title(f.read_text()) or f.stem
        with Horizontal(id="ideas-header"):
            yield Static("💡 IDEAS", id="header-title")
            yield Static(title, id="header-filename")
            yield Static(self._counter_text(), id="header-counter")
        with ScrollableContainer(id="content-scroll"):
            yield Markdown(f.read_text().strip(), id="content-label")
        with Vertical(id="key-bar"):
            with Horizontal(classes="key-row"):
                if TERMUX:
                    yield KeyButton("k", "💾",  "keep")
                    yield KeyButton("p", "⬆️",  "promote")
                    yield KeyButton("e", "✍️",  "edit",    color="muted")
                    yield KeyButton("d", "🗑",   "discard", color="destructive")
                    yield KeyButton("q", "←",   "quit",    color="muted")
                else:
                    yield KeyButton("k", "💾 keep",    "keep")
                    yield KeyButton("p", "⬆  promote", "promote")
                    yield KeyButton("e", "✍  edit",    "edit",    color="muted")
                    yield KeyButton("d", "🗑  discard", "discard", color="destructive")
                    yield KeyButton("q", "✖  quit",    "quit",    color="muted")

    def action_keep(self) -> None:
        self._advance()

    def action_edit(self) -> None:
        editor = os.environ.get("EDITOR", "nvim")
        with self.suspend():
            subprocess.run([editor, str(self.current_file)])
        self._refresh_view()

    def action_discard(self) -> None:
        def handle(confirmed: bool) -> None:
            if confirmed:
                self.current_file.unlink()
                self._advance()
        self.push_screen(ConfirmScreen("Discard this idea?"), handle)

    def action_promote(self) -> None:
        areas = get_project_areas()
        names = [a.name for a in areas]
        default_slug = ""

        def handle_name(slug: str | None) -> None:
            if slug:
                _promote_idea(self.current_file, slug.lower().replace(" ", "-"), areas[self._chosen_area_idx])
                self._advance()

        def handle_area(area_name: str | None) -> None:
            if area_name:
                self._chosen_area_idx = next(
                    i for i, a in enumerate(areas) if a.name == area_name
                )
                self.push_screen(
                    NameInputScreen("project slug", default=default_slug),
                    handle_name,
                )

        self.push_screen(ProjectPickerScreen(names), handle_area)

    def action_quit(self) -> None:
        self.exit()


def _promote_idea(idea_file: Path, slug: str, target_area: Path) -> None:
    content = idea_file.read_text()
    project_dir = target_area / slug
    project_dir.mkdir(exist_ok=True)
    created = get_frontmatter_field(content, "created") or datetime.now().strftime("%Y-%m-%d")
    tags = get_frontmatter_field(content, "tags") or "[]"
    body = get_body(content)
    title = slug.replace("-", " ").title()
    (project_dir / "project.md").write_text(f"""---
created: {created}
deadline:
status: on-hold
completed:
abandoned:
area: {target_area.name}
tags: {tags}
---

# {title}

{body}
""")
    idea_file.unlink()


if TERMUX:
    IdeasApp.CSS = apply_termux_css(IdeasApp.CSS)


def run():
    ideas = sorted(ideas_path.glob("*.md"))
    if not ideas:
        print("\n  no ideas\n")
        return
    IdeasApp(ideas).run()


def main():
    run()
    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
