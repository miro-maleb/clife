"""
notes_tui.py — Textual TUI version of the notes reviewer.

Parallel to notes.py. Scanning logic imported from notes.py.
Run via: lo notes --tui  (or automatically on Termux)
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
from notes import (
    kb_path, ideas_path,
    build_link_index, get_candidate_notes,
    get_frontmatter_field, get_title, get_snippet, get_reasons,
    GRADUATED_STATUSES,
)


class NotesApp(App):
    CSS = COMMON_CSS + """
    Screen {
        background: #0e1116;
        layers: base overlay;
    }
    #notes-header {
        height: 2;
        background: #0e1116;
        border-bottom: heavy #30363d;
        padding: 0 2;
        align: left middle;
    }
    #header-title {
        color: #c4b4d4;
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
        height: 5;
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
        Binding("k", "keep",     show=False),
        Binding("i", "to_ideas", show=False),
        Binding("e", "edit",     show=False),
        Binding("d", "discard",  show=False),
        Binding("q", "quit",     show=False),
    ]

    def __init__(self, files: list, link_index: set) -> None:
        super().__init__()
        self._files = list(files)
        self._total = len(files)
        self._index = 0
        self._link_index = link_index

    @property
    def current_file(self) -> Path:
        return self._files[self._index]

    def _counter_text(self) -> str:
        return f"{self._index + 1} / {self._total}"

    def _summary(self, f: Path) -> str:
        content = f.read_text(errors="ignore")
        rel = str(f.relative_to(kb_path))
        status = get_frontmatter_field(content, "status")
        tags = get_frontmatter_field(content, "tags")
        created = get_frontmatter_field(content, "created")
        reasons = get_reasons(f, content, self._link_index)
        snippet = get_snippet(content)

        lines = []
        meta = []
        if status:
            sc = {"seed": "grey70", "growing": "steel_blue1"}.get(status, "grey70")
            meta.append(f"[{sc}]{status}[/{sc}]")
        if created:
            meta.append(f"[grey50]{created}[/grey50]")
        meta.append(f"[grey35]{rel}[/grey35]")
        lines.append("  ".join(meta))

        if reasons:
            lines.append("  ".join(f"[indian_red]{r}[/indian_red]" for r in reasons))
        if tags and tags not in ("[]", ""):
            lines.append(f"[grey50]tags[/grey50]  [gold3]{tags}[/gold3]")
        if snippet:
            lines.append(f"\n[grey80]{snippet}[/grey80]")

        return "\n".join(lines)

    def _refresh_view(self) -> None:
        f = self.current_file
        title = get_title(f.read_text(errors="ignore"), f.stem)
        self.query_one("#header-filename", Static).update(title)
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
        title = get_title(f.read_text(errors="ignore"), f.stem)
        with Horizontal(id="notes-header"):
            yield Static("📝 NOTES", id="header-title")
            yield Static(title, id="header-filename")
            yield Static(self._counter_text(), id="header-counter")
        with ScrollableContainer(id="content-scroll"):
            yield Static(self._summary(f), id="content-label")
        with Vertical(id="key-bar"):
            with Horizontal(classes="key-row"):
                if TERMUX:
                    yield KeyButton("k", "💾",  "keep")
                    yield KeyButton("i", "💡",  "to_ideas")
                    yield KeyButton("e", "✍️",  "edit",    color="muted")
                    yield KeyButton("d", "🗑",   "discard", color="destructive")
                    yield KeyButton("q", "←",   "quit",    color="muted")
                else:
                    yield KeyButton("k", "💾 keep",      "keep")
                    yield KeyButton("i", "💡 → ideas",   "to_ideas")
                    yield KeyButton("e", "✍  edit",      "edit",    color="muted")
                    yield KeyButton("d", "🗑  discard",   "discard", color="destructive")
                    yield KeyButton("q", "✖  quit",      "quit",    color="muted")

    def action_keep(self) -> None:
        self._advance()

    def action_to_ideas(self) -> None:
        f = self.current_file
        ideas_path.mkdir(parents=True, exist_ok=True)
        dest = ideas_path / f.name
        if dest.exists():
            dest = ideas_path / f"{f.stem}-note{f.suffix}"
        f.rename(dest)
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
        self.push_screen(ConfirmScreen("Discard this note?"), handle)

    def action_quit(self) -> None:
        self.exit()


if TERMUX:
    NotesApp.CSS = apply_termux_css(NotesApp.CSS)


def run():
    print("\n  scanning notes…\n")
    link_index = build_link_index()
    candidates = get_candidate_notes()

    flagged = []
    for md_file in candidates:
        content = md_file.read_text(errors="ignore")
        status = get_frontmatter_field(content, "status")
        if status in GRADUATED_STATUSES:
            continue
        if get_reasons(md_file, content, link_index):
            flagged.append(md_file)

    flagged.sort(key=lambda f: (
        -len(get_reasons(f, f.read_text(errors="ignore"), link_index)),
        f.name
    ))

    if not flagged:
        print("  all notes look good\n")
        return

    NotesApp(flagged, link_index).run()


def main():
    run()
    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
