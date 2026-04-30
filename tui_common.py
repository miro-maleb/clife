"""
tui_common.py — Shared Textual UI components for Life OS TUI scripts.
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

TERMUX = "com.termux" in os.environ.get("PREFIX", "")
GIT = "/data/data/com.termux/files/usr/bin/git" if TERMUX else "git"

# ── Warm pastel palette ────────────────────────────────────────────────────────
# Desktop uses hex; Termux uses named colors (older Rich compat)
_COLORS = {
    "action":      "#e8a87c",   # warm amber — primary interactive
    "destructive": "#d4878a",   # muted rose — danger
    "muted":       "#6b5c54",   # warm dark gray — secondary
    "label":       "#8a7a70",   # warm mid-gray — key hints
}

# Named-color fallbacks for Termux (older Rich builds)
_COLORS_NAMED = {
    "action":      "dark_orange",
    "destructive": "light_coral",
    "muted":       "grey35",
    "label":       "grey46",
}


class KeyButton(Static, can_focus=False):
    """A styled label that fires an app action on click or tap."""

    def __init__(self, key: str, label: str, action: str, color: str = "action") -> None:
        self._action_name = action
        if TERMUX:
            c = _COLORS_NAMED.get(color, color)
            lc = _COLORS_NAMED["label"]
            markup = f"[{lc}]{label}[/{lc}]"
        else:
            c = _COLORS.get(color, color)
            lc = _COLORS["label"]
            markup = f"[bold {c}]{key}[/bold {c}] [{lc}]{label}[/{lc}]"
        super().__init__(markup, classes="key-btn")

    def on_click(self) -> None:
        # Check the current screen first (for screen-level actions), then the app.
        name = f"action_{self._action_name}"
        fn = getattr(self.screen, name, None) or getattr(self.app, name, None)
        if fn:
            fn()


class ConfirmButton(Static, can_focus=False):
    """Clickable yes/no button inside ConfirmScreen."""

    def __init__(self, label: str, action: str) -> None:
        self._action_name = action
        super().__init__(label, classes="confirm-btn")

    def on_click(self) -> None:
        fn = getattr(self.screen, f"action_{self._action_name}", None)
        if fn:
            fn()


class ConfirmScreen(ModalScreen):
    BINDINGS = [
        Binding("y", "confirm", show=False),
        Binding("n", "cancel",  show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, message: str = "Delete this file?") -> None:
        super().__init__()
        self._message = message

    def compose(self):
        with Vertical(id="confirm-dialog"):
            yield Static(self._message, id="confirm-question")
            with Horizontal(id="confirm-buttons"):
                yield ConfirmButton(
                    "[bold #a8c5a0]y[/bold #a8c5a0]  yes", "confirm"
                )
                yield ConfirmButton(
                    "[bold #6b5c54]n[/bold #6b5c54]  no", "cancel"
                )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProjectPickerScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, project_names: list, prompt: str = "pick a project") -> None:
        super().__init__()
        self._names = project_names
        self._prompt = prompt

    def compose(self):
        title = "↑↓ select  ·  Esc cancel" if TERMUX else f"{self._prompt}  ·  ↑↓ navigate  ·  Enter select  ·  Esc cancel"
        with Vertical(id="picker-dialog"):
            yield Static(title, id="picker-title")
            yield ListView(
                *[ListItem(Static(n), name=n) for n in self._names],
                id="picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NameInputScreen(ModalScreen):
    """Single-line text input modal. Dismisses with the entered string, or None on Escape."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, prompt: str, default: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._default = default

    def compose(self):
        hint = "Enter to confirm  ·  Esc cancel" if TERMUX else "Enter to confirm  ·  Esc cancel"
        with Vertical(id="name-input-dialog"):
            yield Static(self._prompt, id="name-input-prompt")
            yield Static(hint, id="name-input-hint")
            yield Input(value=self._default, id="name-input-field")

    def on_mount(self) -> None:
        field = self.query_one("#name-input-field", Input)
        field.focus()
        field.cursor_position = len(self._default)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Shared CSS (modals + key buttons) ─────────────────────────────────────────

COMMON_CSS = """
.key-btn {
    width: auto;
    height: 3;
    padding: 0 2;
    content-align: center middle;
    background: #251f1b;
}
.key-btn:hover {
    background: #3a302a;
}

ConfirmScreen {
    align: center middle;
    background: #1a1612 95%;
}
#confirm-dialog {
    width: 44;
    height: 9;
    border: double #d4878a;
    background: #251f1b;
    padding: 1 3;
    align: center middle;
}
#confirm-question {
    text-align: center;
    color: #d4878a;
    text-style: bold;
    margin-bottom: 1;
}
#confirm-buttons {
    height: 3;
}
.confirm-btn {
    width: 1fr;
    height: 3;
    content-align: center middle;
    background: #312a25;
    border: tall #4a3f38;
}
.confirm-btn:hover {
    background: #3a302a;
}

NameInputScreen {
    align: center middle;
    background: #1a1612 95%;
}
#name-input-dialog {
    width: 60;
    height: 11;
    border: double #a8c5a0;
    background: #251f1b;
    padding: 1 2;
}
#name-input-prompt {
    text-align: center;
    color: #e8ddd4;
    text-style: bold;
    margin-bottom: 1;
}
#name-input-hint {
    text-align: center;
    color: #6b5c54;
    margin-bottom: 1;
}
#name-input-field {
    height: 3;
    background: #312a25;
    color: #e8ddd4;
    border: solid #4a3f38;
    padding: 0 2;
}
#name-input-field:focus {
    border: solid #a8c5a0;
}

ProjectPickerScreen {
    align: center middle;
    background: #1a1612 95%;
}
#picker-dialog {
    width: 60;
    height: 26;
    border: double #e8a87c;
    background: #251f1b;
    padding: 1 2;
}
#picker-title {
    text-align: center;
    color: #8a7a70;
    margin-bottom: 1;
    border-bottom: solid #4a3f38;
    padding-bottom: 1;
}
#picker-list {
    height: 1fr;
    background: #251f1b;
    border: none;
    scrollbar-size-vertical: 0;
}
#picker-list > ListItem {
    padding: 0 2;
    background: #251f1b;
    color: #e8ddd4;
}
#picker-list > ListItem:hover {
    background: #312a25;
    color: #e8a87c;
}
#picker-list > ListItem.--highlight {
    background: #312a25;
    color: #e8a87c;
    text-style: bold;
}
#picker-list:focus > ListItem.--highlight {
    background: #3a302a;
    color: #c9a87c;
    text-style: bold;
}
"""


def apply_termux_css(css: str) -> str:
    """Apply Termux-specific overrides: solid borders, tighter margins, full-width buttons."""
    result = (
        css
        .replace("border: double", "border: solid")
        .replace("margin: 1 3 0 3", "margin: 1 1 0 1")
        .replace("margin: 1 3", "margin: 1 1")
        .replace("padding: 1 3", "padding: 1 1")
        .replace("padding: 0 3", "padding: 0 1")
    )
    result += """
.key-btn {
    width: 1fr;
    height: 3;
    padding: 0 0;
}
#key-bar { border: none; margin: 1 0 0 0; }
/* Modals: shrink to fit narrow phone screens */
#confirm-dialog    { width: 90%; }
#picker-dialog     { width: 90%; height: 20; }
#name-input-dialog { width: 90%; }
"""
    return result


def exit_to_launcher() -> None:
    """On Termux, replace the current process with lo launch. No-op on desktop."""
    if not TERMUX:
        return
    lo = os.path.join(os.path.dirname(__file__), "lo")
    os.execv(sys.executable, [sys.executable, lo, "launch"])


def git_push_kb() -> None:
    """Stage, commit, and push kb/ after a TUI session."""
    kb = Path.home() / "kb"
    subprocess.run([GIT, "-C", str(kb), "add", "-A"], check=False)
    result = subprocess.run(
        [GIT, "-C", str(kb), "commit", "-m", f"auto sync {datetime.date.today()}"],
        capture_output=True, check=False,
    )
    if result.returncode == 0:
        # Pull remote changes first (rebase) so the push is always fast-forward
        subprocess.run(
            [GIT, "-C", str(kb), "pull", "--rebase", "origin", "main"],
            check=False,
        )
        subprocess.run([GIT, "-C", str(kb), "push", "origin", "main"], check=False)
