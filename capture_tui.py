"""
capture_tui.py — Textual TUI for capture.

Split-screen launcher → voice recording loop or text input loop.
Voice: New Round button stops/transcribes/restarts. Done stops/transcribes/exits.
Text: Input widget, Enter submits each item, Done/Esc exits.

Run via: lo capture  (automatically on Termux when no mode flags given)
"""

import os
import re
import subprocess
import sys
import threading
import time
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.screen import Screen
from textual.widgets import Input, Static

from tui_common import TERMUX, git_push_kb, exit_to_launcher
from capture import groq_transcribe, write_inbox, MIC_RECORD, flush_pending, PENDING_DIR


TMPWAV = str(Path.home() / "capture.wav")


# ── Big tap tile ───────────────────────────────────────────────────────────────

class BigTile(Static, can_focus=False):
    """Full-area clickable tile — fires an action on the current screen."""

    def __init__(self, content: str, action: str, widget_id: str) -> None:
        self._action_name = action
        super().__init__(content, id=widget_id)

    def on_click(self) -> None:
        for target in (self.screen, self.app):
            fn = getattr(target, f"action_{self._action_name}", None)
            if fn:
                fn()
                return


# ── Launcher screen ────────────────────────────────────────────────────────────

class LauncherScreen(Screen):
    BINDINGS = [
        Binding("v", "voice", show=False),
        Binding("t", "text",  show=False),
        Binding("q", "quit",  show=False),
    ]

    def compose(self) -> ComposeResult:
        if TERMUX:
            yield BigTile("🎤\nVoice",  "voice", "voice-tile")
            yield BigTile("⌨️\nText",   "text",  "text-tile")
            yield BigTile("←\nBack",    "quit",  "back-tile")
        else:
            yield BigTile(
                "v  🎤  Voice\n[#8a7a70]say 'break' to split · New Round to continue[/#8a7a70]",
                "voice", "voice-tile",
            )
            yield BigTile(
                "t  ⌨️  Text\n[#8a7a70]one line per item · Esc when finished[/#8a7a70]",
                "text", "text-tile",
            )

    def action_voice(self) -> None:
        if TERMUX:
            self.app.push_screen(VoiceScreen())
        else:
            # Desktop: exit TUI and hand off to terminal voice mode
            self.app._desktop_voice = True
            self.app.exit()

    def action_text(self) -> None:
        self.app.push_screen(TextScreen())

    def action_quit(self) -> None:
        self.app.exit()


# ── Voice screen (Termux) ──────────────────────────────────────────────────────

class VoiceScreen(Screen):
    BINDINGS = [
        Binding("n", "new_round",  show=False),
        Binding("d", "done_voice", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: list[str] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static("🔴  Recording…", id="voice-status")
        with ScrollableContainer(id="items-scroll"):
            yield Static("", id="items-log")
        if TERMUX:
            yield BigTile("🔄\nNew Round", "new_round",  "new-round-tile")
            yield BigTile("✅\nDone",      "done_voice", "done-tile")
        else:
            yield BigTile("n  🔄  New Round", "new_round",  "new-round-tile")
            yield BigTile("d  ✅  Done",       "done_voice", "done-tile")

    def on_mount(self) -> None:
        self._start_recording()

    def _start_recording(self) -> None:
        subprocess.run([MIC_RECORD, "-q"], capture_output=True)
        Path(TMPWAV).unlink(missing_ok=True)
        subprocess.run([MIC_RECORD, "-f", TMPWAV, "-l", "0"], capture_output=True)
        self.query_one("#voice-status", Static).update("🔴  Recording…")
        self._busy = False

    def _update_log(self) -> None:
        text = "\n".join(f"· {item}" for item in self._items)
        self.query_one("#items-log", Static).update(text)
        self.query_one("#items-scroll").scroll_end(animate=False)

    def _process_text(self, text: str) -> None:
        """Write chunks to inbox and append to items list. No UI calls — caller handles those."""
        chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
        chunks = [c.strip() for c in chunks if re.search(r'\w', c)]
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        use_index = len(chunks) > 1
        for i, chunk in enumerate(chunks, 1):
            write_inbox(chunk, stamp, i if use_index else None)
            self._items.append(chunk)

    def _stop_transcribe_continue(self, then_exit: bool) -> None:
        subprocess.run([MIC_RECORD, "-q"], capture_output=True)
        time.sleep(0.5)
        wav = Path(TMPWAV)
        if wav.exists() and wav.stat().st_size > 0:
            self.app.call_from_thread(
                lambda: self.query_one("#voice-status", Static).update("⏳  Transcribing…")
            )
            text, _ = groq_transcribe(TMPWAV)
            wav.unlink(missing_ok=True)
            if text:
                self._process_text(text)
        else:
            wav.unlink(missing_ok=True)

        # All UI updates scheduled together in explicit order
        self.app.call_from_thread(self._update_log)
        if then_exit:
            self.app.call_from_thread(self.app.exit)
        else:
            self.app.call_from_thread(self._start_recording)

    def action_new_round(self) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(
            target=self._stop_transcribe_continue, args=(False,), daemon=True
        ).start()

    def action_done_voice(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.query_one("#voice-status", Static).update("⏳  Finishing…")
        threading.Thread(
            target=self._stop_transcribe_continue, args=(True,), daemon=True
        ).start()


# ── Text screen ────────────────────────────────────────────────────────────────

class TextScreen(Screen):
    BINDINGS = [
        Binding("escape", "done_text", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("✍  CAPTURE — TEXT  ·  Esc when done", id="text-header")
        with ScrollableContainer(id="text-items-scroll"):
            yield Static("", id="text-items-log")
        yield Input(placeholder="type and press Enter…", id="text-input")
        if TERMUX:
            yield BigTile("✅\nDone", "done_text", "text-done-tile")
        else:
            yield BigTile("Esc  ✅  Done", "done_text", "text-done-tile")

    def on_mount(self) -> None:
        self.query_one("#text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            self.action_done_text()
            return
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        write_inbox(text, stamp)
        self._items.append(text)
        self.query_one("#text-items-log", Static).update(
            "\n".join(f"· {item}" for item in self._items)
        )
        self.query_one("#text-items-scroll").scroll_end(animate=False)
        event.input.clear()

    def action_done_text(self) -> None:
        self.app.exit()


# ── App ────────────────────────────────────────────────────────────────────────

class CaptureApp(App):
    CSS = """
    Screen {
        background: #0e1116;
    }

    /* ── Launcher ── */
    #voice-tile {
        height: 1fr;
        content-align: center middle;
        background: #161b22;
        color: #e8a87c;
        text-style: bold;
        border-bottom: heavy #30363d;
    }
    #voice-tile:hover { background: #1c2128; }
    #text-tile {
        height: 1fr;
        content-align: center middle;
        background: #161b22;
        color: #a8c5a0;
        text-style: bold;
    }
    #text-tile:hover { background: #1c2128; }
    #back-tile {
        height: 4;
        content-align: center middle;
        background: #0e1116;
        color: #768390;
        text-style: bold;
        border-top: heavy #30363d;
    }
    #back-tile:hover { background: #1c2128; color: #cdd9e5; }

    /* ── Voice screen ── */
    #voice-status {
        height: 2;
        background: #0e1116;
        border-bottom: heavy #30363d;
        content-align: center middle;
        color: #d4878a;
        text-style: bold;
    }
    #items-scroll {
        height: 1fr;
        background: #161b22;
        border: round #30363d;
        margin: 0 1;
        padding: 1 2;
        color: #cdd9e5;
        scrollbar-size-vertical: 0;
    }
    #new-round-tile {
        height: 5;
        content-align: center middle;
        background: #161b22;
        color: #e8a87c;
        text-style: bold;
        border-top: heavy #30363d;
        border-bottom: heavy #30363d;
    }
    #new-round-tile:hover { background: #1c2128; }
    #done-tile {
        height: 5;
        content-align: center middle;
        background: #161b22;
        color: #a8c5a0;
        text-style: bold;
    }
    #done-tile:hover { background: #1c2128; }

    /* ── Text screen ── */
    #text-header {
        height: 2;
        background: #0e1116;
        border-bottom: heavy #30363d;
        content-align: center middle;
        color: #cdd9e5;
    }
    #text-items-scroll {
        height: 1fr;
        background: #161b22;
        border: round #30363d;
        margin: 0 1;
        padding: 1 2;
        color: #cdd9e5;
        scrollbar-size-vertical: 0;
    }
    #text-input {
        height: 3;
        background: #161b22;
        color: #cdd9e5;
        border: solid #30363d;
        padding: 0 2;
    }
    #text-input:focus {
        border: solid #e8a87c;
    }
    #text-done-tile {
        height: 4;
        content-align: center middle;
        background: #161b22;
        color: #a8c5a0;
        text-style: bold;
    }
    #text-done-tile:hover { background: #1c2128; }
    """

    def on_mount(self) -> None:
        self._desktop_voice = False
        self.push_screen(LauncherScreen())


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    pending = sorted(PENDING_DIR.glob("*.wav"))
    if pending:
        print(f"\n  Processing {len(pending)} pending recording(s)…\n")
        flush_pending()

    app = CaptureApp()
    app.run()

    if getattr(app, "_desktop_voice", False):
        from capture import voice_mode
        voice_mode()
        return

    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
