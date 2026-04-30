"""
dashboard_tui.py — Persistent lo dashboard.

Four panels: journal (top-left), projects (bottom-left),
             calendar (top-right), capture (bottom-right).

Capture panel modes:
  quick  — text input → inbox  (default, always ready)
  voice  — arecord + Groq transcription → inbox
  new    — create project / note / task directly via modals

1-4 to jump panels · tab to cycle · q to quit
"""

import os
import re
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.widgets import Input, ListItem, ListView, Static

from capture import write_inbox, groq_transcribe
from kb_utils import get_journal_path
from tui_common import COMMON_CSS, KeyButton, NameInputScreen, ProjectPickerScreen

KB = Path.home() / "kb"
VENV = Path(__file__).parent / "venv" / "bin"
GCALCLI = str(VENV / "gcalcli")
DASH_TMPWAV = "/tmp/lo-dashboard-capture.wav"

# ── CSS ────────────────────────────────────────────────────────────────────────

DASHBOARD_CSS = COMMON_CSS + """
Screen { background: #0e1116; }

#dash-header {
    height: 2;
    background: #0e1116;
    border-bottom: heavy #30363d;
    padding: 0 2;
    align: left middle;
}
#dash-title  { color: #e8a87c; text-style: bold; width: auto; }
#dash-date   { color: #768390; margin-left: 2; width: 1fr; }
#dash-clock  { color: #4a3f38; text-align: right; width: auto; }

#main-grid { height: 1fr; }
#col-left  { width: 3fr; border-right: heavy #30363d; }
#col-right { width: 2fr; }

/* Panel base */
.panel {
    border: round #30363d;
    border-title-align: left;
    background: #0e1116;
    margin: 0;
    padding: 0 1;
    scrollbar-size-vertical: 0;
}
.panel:focus        { border: round #e8a87c; }
.panel:focus-within { border: round #e8a87c; }

#panel-journal  { height: 3fr; }
#panel-projects { height: 2fr; }
#panel-calendar { height: 3fr; }
#panel-capture  { height: 2fr; }

/* Journal / Calendar text */
.panel-body     { color: #cdd9e5; padding: 0; }
.panel-heading  { color: #e8a87c; text-style: bold; }
.panel-muted    { color: #768390; }
.panel-empty    { color: #4a3f38; }

/* Projects list */
#proj-list {
    background: #0e1116;
    border: none;
    height: 1fr;
    padding: 0;
    scrollbar-size-vertical: 0;
}
#proj-list > ListItem {
    padding: 0 1;
    background: #0e1116;
    color: #cdd9e5;
}
#proj-list > ListItem.--highlight {
    background: #1c2128;
    color: #e8a87c;
    text-style: bold;
}
#proj-list:focus > ListItem.--highlight {
    background: #212830;
    color: #e8a87c;
}

/* Capture */
#capture-scroll {
    height: 1fr;
    background: #0e1116;
    padding: 0;
    scrollbar-size-vertical: 0;
}
#capture-log  { color: #cdd9e5; padding: 0 1; }
#capture-rule { color: #30363d; height: 1; padding: 0 1; }

/* Capture bottom bars — one visible at a time */
#capture-quick-bar, #capture-voice-bar, #capture-new-bar {
    height: 3;
    background: #0e1116;
}
#capture-voice-bar { display: none; }
#capture-new-bar   { display: none; }

#capture-input {
    height: 3;
    background: #161b22;
    color: #cdd9e5;
    border: solid #30363d;
    padding: 0 2;
    width: 1fr;
    margin: 0;
}
#capture-input:focus { border: solid #e8a87c; }

/* Small inline mode buttons */
.cap-btn {
    width: auto;
    height: 3;
    padding: 0 2;
    content-align: center middle;
    background: #161b22;
    border: solid #30363d;
    color: #768390;
    margin: 0 0 0 1;
}
.cap-btn:hover { background: #1c2128; color: #cdd9e5; }

/* Voice status */
#capture-voice-status {
    width: 1fr;
    height: 3;
    content-align: left middle;
    padding: 0 2;
    color: #cdd9e5;
}

/* New-type buttons */
.cap-new-btn {
    width: 1fr;
    height: 3;
    content-align: center middle;
    background: #161b22;
    border: solid #30363d;
    color: #768390;
    margin: 0 1 0 0;
}
.cap-new-btn:hover { background: #1c2128; color: #e8a87c; }

/* Footer */
#dash-footer {
    height: 2;
    background: #0e1116;
    border-top: heavy #30363d;
    padding: 0 1;
    align: left middle;
}
"""


# ── Journal panel ──────────────────────────────────────────────────────────────

class JournalPanel(ScrollableContainer):
    can_focus = True
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up",   show=False),
        Binding("o", "open_nvim",   show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._journal_path: Path = get_journal_path(datetime.now())

    def compose(self) -> ComposeResult:
        yield Static("", id="journal-body", classes="panel-body")

    def on_mount(self) -> None:
        self.border_title = "journal"
        self._load()
        self.set_interval(60, self._load)

    def _load(self) -> None:
        path = self._journal_path
        if not path.exists():
            self.query_one("#journal-body", Static).update(
                "[#4a3f38]no journal today — run lo daily[/#4a3f38]"
            )
            return

        content = path.read_text()
        lines: list[str] = []
        in_target = False

        for line in content.splitlines():
            if re.match(r"^## (Schedule|Journal)", line):
                in_target = True
                lines.append(f"[#e8a87c]{line}[/#e8a87c]")
            elif re.match(r"^## ", line):
                if re.match(r"^## (Notes|Today)", line):
                    in_target = False
                else:
                    in_target = True
                    lines.append(f"[#e8a87c]{line}[/#e8a87c]")
            elif in_target:
                if re.match(r"^- ", line):
                    lines.append(f"[#768390]·[/#768390]  {line[2:]}")
                elif line.startswith("#"):
                    lines.append(f"[#a8c5a0]{line}[/#a8c5a0]")
                else:
                    lines.append(line if line else " ")

        self.query_one("#journal-body", Static).update("\n".join(lines) or "[#4a3f38]empty[/#4a3f38]")

    def action_open_nvim(self) -> None:
        self.app._open_file = self._journal_path  # type: ignore[attr-defined]
        self.app.exit()


# ── Calendar panel ─────────────────────────────────────────────────────────────

class CalendarPanel(ScrollableContainer):
    can_focus = True
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up",   show=False),
        Binding("r", "refresh",     show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("[#4a3f38]loading…[/#4a3f38]", id="cal-body", classes="panel-body")

    def on_mount(self) -> None:
        self.border_title = "calendar"
        self._refresh()
        self.set_interval(270, self._refresh)

    def _refresh(self) -> None:
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            result = subprocess.run(
                [GCALCLI, "agenda", "--nocolor", "--details", "end"],
                capture_output=True, text=True, timeout=15,
            )
            text = result.stdout.strip() or "(no upcoming events)"
        except FileNotFoundError:
            text = "(gcalcli not installed)"
        except subprocess.TimeoutExpired:
            text = "(calendar timeout)"

        now = datetime.now().strftime("%H:%M")
        self.app.call_from_thread(self._update, text, now)

    def _update(self, text: str, timestamp: str) -> None:
        self.border_subtitle = f"↺ {timestamp}"
        lines: list[str] = []
        for line in text.splitlines():
            if re.match(r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", line):
                lines.append(f"[#e8a87c]{line}[/#e8a87c]")
            elif re.match(r"^\s+\d{1,2}:\d{2}", line):
                lines.append(f"[#cdd9e5]{line}[/#cdd9e5]")
            else:
                lines.append(f"[#768390]{line}[/#768390]" if line.strip() else " ")
        self.query_one("#cal-body", Static).update("\n".join(lines))
        self.scroll_home(animate=False)

    def action_refresh(self) -> None:
        self._refresh()


# ── Projects panel ─────────────────────────────────────────────────────────────

class ProjectsPanel(Vertical):
    can_focus = False

    def compose(self) -> ComposeResult:
        yield ListView(id="proj-list")

    def on_mount(self) -> None:
        self.border_title = "projects"
        self._load()

    def _load(self) -> None:
        lv = self.query_one("#proj-list", ListView)
        lv.clear()
        projects = self._scan_active()
        for p in projects:
            label = f"[#a8c5a0]●[/#a8c5a0]  {p['name']}"
            lv.append(ListItem(Static(label), name=str(p["path"])))
        if not projects:
            lv.append(ListItem(Static("[#4a3f38]no active projects[/#4a3f38]"), name=""))

    def _scan_active(self) -> list[dict]:
        projects: list[dict] = []
        projects_dir = KB / "projects"
        for proj_file in sorted(projects_dir.rglob("project.md")):
            folder = proj_file.parent
            if not proj_file.exists():
                continue
            content = proj_file.read_text()
            if re.search(r"^status:\s*active", content, re.MULTILINE):
                title_m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
                name = title_m.group(1).strip() if title_m else folder.name
                projects.append({"name": name, "path": proj_file})
        return projects

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        path_str = event.item.name
        if path_str:
            self.app._open_file = Path(path_str)  # type: ignore[attr-defined]
            self.app.exit()


# ── Capture panel ──────────────────────────────────────────────────────────────

class CapBtn(Static, can_focus=False):
    """Clickable label button for the capture panel bottom bars."""

    class Pressed(Message):
        def __init__(self, btn_id: str) -> None:
            super().__init__()
            self.btn_id = btn_id

    def on_click(self) -> None:
        self.post_message(CapBtn.Pressed(self.id or ""))


class CapturePanel(Vertical):
    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._items: list[str] = []
        self._mode: str = "quick"
        self._recording_proc: subprocess.Popen | None = None

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="capture-scroll"):
            yield Static("", id="capture-log")
        yield Static("─" * 60, id="capture-rule")

        # quick mode: input + voice button + new button
        with Horizontal(id="capture-quick-bar"):
            yield Input(placeholder="capture…", id="capture-input")
            yield CapBtn(
                "[bold #e8a87c]mic[/bold #e8a87c] [#6b5c54]voice[/#6b5c54]",
                id="btn-voice", classes="cap-btn",
            )
            yield CapBtn(
                "[bold #e8a87c]+[/bold #e8a87c] [#6b5c54]new[/#6b5c54]",
                id="btn-new", classes="cap-btn",
            )

        # voice mode: status + stop button
        with Horizontal(id="capture-voice-bar"):
            yield Static("", id="capture-voice-status")
            yield CapBtn(
                "[bold #d4878a]■[/bold #d4878a] [#6b5c54]stop[/#6b5c54]",
                id="btn-voice-stop", classes="cap-btn",
            )

        # new mode: three creation buttons + back
        with Horizontal(id="capture-new-bar"):
            yield CapBtn("[bold #e8a87c]P[/bold #e8a87c]  project", id="btn-np", classes="cap-new-btn")
            yield CapBtn("[bold #e8a87c]N[/bold #e8a87c]  note",    id="btn-nn", classes="cap-new-btn")
            yield CapBtn("[bold #e8a87c]T[/bold #e8a87c]  task",    id="btn-nt", classes="cap-new-btn")
            yield CapBtn("[#6b5c54]← back[/#6b5c54]", id="btn-new-back", classes="cap-btn")

    def on_mount(self) -> None:
        self.border_title = "capture"
        self._apply_mode()

    # ── mode management ────────────────────────────────────────────────────────

    def _apply_mode(self) -> None:
        self.query_one("#capture-quick-bar").display = (self._mode == "quick")
        self.query_one("#capture-voice-bar").display = (self._mode == "voice")
        self.query_one("#capture-new-bar").display   = (self._mode == "new")
        self.border_subtitle = {"voice": "voice", "new": "new"}.get(self._mode, "")
        if self._mode == "quick":
            self.query_one("#capture-input", Input).focus()

    def _set_mode(self, mode: str) -> None:
        if self._mode == "voice" and mode != "voice":
            self._cleanup_recording()
        self._mode = mode
        self._apply_mode()
        if mode == "voice":
            self._start_recording()

    # ── voice recording ────────────────────────────────────────────────────────

    def _start_recording(self) -> None:
        Path(DASH_TMPWAV).unlink(missing_ok=True)
        self.query_one("#capture-voice-status", Static).update("[#4a3f38]starting…[/#4a3f38]")
        try:
            self._recording_proc = subprocess.Popen(
                ["arecord", "-r", "16000", "-f", "S16_LE", "-c", "1", DASH_TMPWAV],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            self.query_one("#capture-voice-status", Static).update(
                "[#d4878a]● recording…[/#d4878a]  [#6b5c54]press stop when done[/#6b5c54]"
            )
        except FileNotFoundError:
            self.query_one("#capture-voice-status", Static).update(
                "[#d4878a]arecord not found — install alsa-utils[/#d4878a]"
            )

    def _cleanup_recording(self) -> None:
        if self._recording_proc:
            self._recording_proc.terminate()
            try:
                self._recording_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._recording_proc.kill()
            self._recording_proc = None

    def _stop_and_transcribe(self) -> None:
        self._cleanup_recording()
        wav = Path(DASH_TMPWAV)
        if not wav.exists() or wav.stat().st_size < 500:
            self._add_log("[#768390](no audio captured)[/#768390]")
            self._set_mode("quick")
            return
        self.query_one("#capture-voice-status", Static).update("[#768390]◌ transcribing…[/#768390]")
        threading.Thread(target=self._run_transcription, daemon=True).start()

    def _run_transcription(self) -> None:
        text, offline = groq_transcribe(DASH_TMPWAV)
        try:
            Path(DASH_TMPWAV).unlink(missing_ok=True)
        except Exception:
            pass
        self.app.call_from_thread(self._on_transcription_done, text, offline)

    def _on_transcription_done(self, text: str, offline: bool) -> None:
        if offline:
            self._add_log("[#768390](offline — not saved)[/#768390]")
        elif not text:
            self._add_log("[#768390](no speech detected)[/#768390]")
        else:
            chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
            chunks = [c.strip() for c in chunks if re.search(r'\w', c)]
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            for chunk in chunks:
                write_inbox(chunk, stamp)
                self._add_log(chunk)
        self._set_mode("quick")

    # ── new item creation ──────────────────────────────────────────────────────

    def _start_new_note(self) -> None:
        self.app.push_screen(NameInputScreen("note content"), self._create_note)

    def _create_note(self, content: str | None) -> None:
        if not content:
            self._set_mode("quick")
            return
        slug = re.sub(r'[^\w\s-]', '', content.lower())
        slug = re.sub(r'\s+', '-', slug.strip())[:40] or datetime.now().strftime("%Y%m%d%H%M%S")
        notes_dir = KB / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        dest = notes_dir / f"{slug}.md"
        n = 1
        while dest.exists():
            dest = notes_dir / f"{slug}-{n}.md"
            n += 1
        dest.write_text(
            f"---\ncreated: {datetime.now().strftime('%Y-%m-%d')}\ntags: []\nstatus: seed\n---\n\n{content}\n"
        )
        self._add_log(f"[#a8c5a0]note/{dest.name}[/#a8c5a0]")
        self._set_mode("quick")

    def _start_new_task(self) -> None:
        self.app.push_screen(NameInputScreen("task description"), self._on_task_text)

    def _on_task_text(self, text: str | None) -> None:
        if not text:
            self._set_mode("quick")
            return
        project_names = sorted(set(
            f.parent.name for f in (KB / "projects").rglob("project.md")
        ))
        self.app.push_screen(
            ProjectPickerScreen(project_names, prompt="pick a project for this task"),
            lambda p: self._on_task_project(text, p),
        )

    def _on_task_project(self, task: str, project: str | None) -> None:
        if not project:
            self._set_mode("quick")
            return
        project_file = next(
            (f for f in (KB / "projects").rglob("project.md") if f.parent.name == project),
            None,
        )
        if project_file:
            with project_file.open("a") as f:
                f.write(f"\n- [ ] {task}")
            self._add_log(f"[#a8c5a0]task → {project}[/#a8c5a0]")
        else:
            self._add_log(f"[#d4878a]project not found[/#d4878a]")
        self._set_mode("quick")

    def _start_new_project(self) -> None:
        self.app.push_screen(NameInputScreen("project name"), self._on_project_name)

    def _on_project_name(self, name: str | None) -> None:
        if not name:
            self._set_mode("quick")
            return
        slug = re.sub(r'[^\w-]', '', name.lower().replace(" ", "-"))
        projects_dir = KB / "projects"
        areas = sorted([
            item.name for item in projects_dir.iterdir()
            if item.is_dir() and not item.name.startswith(".")
            and not (item / "project.md").exists()
        ])
        options = ["ideas"] + [a for a in areas if a != "ideas"]
        self.app.push_screen(
            ProjectPickerScreen(options, prompt="pick an area"),
            lambda a: self._on_project_area(slug, name, a),
        )

    def _on_project_area(self, slug: str, name: str, area: str | None) -> None:
        if not area:
            self._set_mode("quick")
            return
        project_dir = KB / "projects" / area / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (project_dir / "project.md").write_text(
            f"---\ncreated: {today}\ndeadline:\nstatus: on-hold\ncompleted:\n"
            f"abandoned:\nsleeping:\narea: {area}\ntags: []\n---\n\n"
            f"# {name.title()}\n\n## Goal\n\n## Tasks\n\n## Notes\n"
        )
        self._add_log(f"[#a8c5a0]project → {area}/{slug}[/#a8c5a0]")
        self._set_mode("quick")

    # ── shared helpers ─────────────────────────────────────────────────────────

    def _add_log(self, text: str) -> None:
        self._items.append(text)
        log = "\n".join(f"[#768390]·[/#768390]  {item}" for item in self._items)
        self.query_one("#capture-log", Static).update(log)
        self.query_one("#capture-scroll").scroll_end(animate=False)

    # ── event handlers ─────────────────────────────────────────────────────────

    def on_cap_btn_pressed(self, event: CapBtn.Pressed) -> None:
        bid = event.btn_id
        if bid == "btn-voice":
            self._set_mode("voice" if self._mode != "voice" else "quick")
        elif bid == "btn-new":
            self._set_mode("new" if self._mode != "new" else "quick")
        elif bid == "btn-voice-stop":
            self._stop_and_transcribe()
        elif bid == "btn-new-back":
            self._set_mode("quick")
        elif bid == "btn-np":
            self._start_new_project()
        elif bid == "btn-nn":
            self._start_new_note()
        elif bid == "btn-nt":
            self._start_new_task()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        write_inbox(text, stamp)
        self._add_log(text)
        event.input.clear()


# ── App ────────────────────────────────────────────────────────────────────────

class DashboardApp(App):
    CSS = DASHBOARD_CSS
    BINDINGS = [
        Binding("1", "focus_journal",  show=False),
        Binding("2", "focus_projects", show=False),
        Binding("3", "focus_calendar", show=False),
        Binding("4", "focus_capture",  show=False),
        Binding("q", "quit_app",       show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._open_file: Path | None = None

    def compose(self) -> ComposeResult:
        now = datetime.now()
        with Horizontal(id="dash-header"):
            yield Static("lo", id="dash-title")
            yield Static(f"·  {now.strftime('%A, %B %-d')}", id="dash-date")
            yield Static(now.strftime("%H:%M"), id="dash-clock")
        with Horizontal(id="main-grid"):
            with Vertical(id="col-left"):
                yield JournalPanel(id="panel-journal", classes="panel")
                yield ProjectsPanel(id="panel-projects", classes="panel")
            with Vertical(id="col-right"):
                yield CalendarPanel(id="panel-calendar", classes="panel")
                yield CapturePanel(id="panel-capture", classes="panel")
        with Horizontal(id="dash-footer"):
            yield KeyButton("tab",  "cycle",   "noop")
            yield KeyButton("1-4",  "jump",    "noop")
            yield KeyButton("j/k",  "scroll",  "noop")
            yield KeyButton("o",    "open",    "noop")
            yield KeyButton("r",    "refresh", "noop")
            yield KeyButton("q",    "quit",    "quit_app")

    def on_mount(self) -> None:
        self.set_interval(60, self._tick_clock)
        self.query_one("#capture-input", Input).focus()

    def _tick_clock(self) -> None:
        self.query_one("#dash-clock", Static).update(datetime.now().strftime("%H:%M"))

    def action_focus_journal(self) -> None:
        self.query_one("#panel-journal", JournalPanel).focus()

    def action_focus_projects(self) -> None:
        self.query_one("#proj-list", ListView).focus()

    def action_focus_calendar(self) -> None:
        self.query_one("#panel-calendar", CalendarPanel).focus()

    def action_focus_capture(self) -> None:
        self.query_one("#capture-input", Input).focus()

    def action_noop(self) -> None:
        pass

    def action_quit_app(self) -> None:
        self.exit()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = DashboardApp()
    app.run()
    if app._open_file and app._open_file.exists():
        subprocess.run(["nvim", str(app._open_file)])


if __name__ == "__main__":
    main()
