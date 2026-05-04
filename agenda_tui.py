"""agenda_tui.py — `cl agenda` TUI.

Single-pane daily anchor:
  ┌─ AGENDA ──────────────────────────────────────────┐
  │ all-day:  Sydney here this week                  │
  │ ─────────────────────────────────────────────    │
  │ [x] 07:00–08:40  morning-practice  (spiritual…)  │
  │      09:00–10:00  Meeting w/ Gregory             │
  │ [ ] 10:10–11:40  writing-block     (writing…)    │
  │ ...                                               │
  └───────────────────────────────────────────────────┘
  3/5 done · 1 skipped · 1 pending     n/p day · t today · q quit

j/k navigate · space/enter/x done · ~ partial · s skip · c comment · m move
w cross-day · o open working · r refresh
"""

import asyncio
import os
import subprocess
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static

from agenda import (
    DAYS,
    annotate_with_status,
    append_log,
    block_from_title,
    fetch_day_events,
    log_path,
    read_log,
    remove_last_entry,
    status_on,
    update_last_note,
)
from week import (
    append_skip,
    delete_event_by_title,
    fetch_events,
    find_block,
    list_calendars,
    parse_duration_minutes,
)
from week_tui import (
    CALENDAR_COLORS,
    MiniConfirmScreen,
    SkipReasonScreen,
    color_for,
    fmt_min,
    minutes_between,
    add_minutes,
)


# ── Comment input modal ─────────────────────────────────────────────────────


class CommentScreen(ModalScreen):
    """Optional freeform note attached to the most recent log entry."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    CSS = """
    CommentScreen { align: center middle; }
    #comment-dialog {
        width: 64;
        height: auto;
        background: #161b22;
        border: round #e8a87c;
        padding: 1 2;
    }
    """

    def __init__(self, block_name: str, default: str = ""):
        super().__init__()
        self.block_name = block_name
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="comment-dialog"):
            yield Static(
                f"[bold]Note:[/bold] [yellow]{self.block_name}[/yellow]\n"
                f"[grey50]Enter to save, Esc cancel[/grey50]"
            )
            yield Input(value=self.default, placeholder="comment…", id="comment-input")

    def on_mount(self) -> None:
        field = self.query_one("#comment-input", Input)
        field.focus()
        field.cursor_position = len(self.default)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Time-nudge modal (same-day move) ────────────────────────────────────────


class TimeNudgeScreen(ModalScreen):
    """Bottom-anchored time nudge with live preview.

    j/k or ↑/↓ nudge by 5min — each nudge mutates the cache, refreshes the
    agenda pane behind, and updates the cursor to follow the moving event.
    Enter commits (cache stays as-is, gcal write happens in background).
    Esc reverts the cache to the original time.
    """

    BINDINGS = [
        Binding("enter", "commit", show=False),
        Binding("escape", "cancel", show=False),
        Binding("up",   "up", show=False),
        Binding("down", "down", show=False),
        Binding("k",    "up", show=False),
        Binding("j",    "down", show=False),
    ]

    CSS = """
    TimeNudgeScreen {
        align: center bottom;
        background: transparent;
    }
    #nudge-bar {
        width: auto;
        max-width: 92;
        height: 3;
        background: #1f1611;
        border: round #e8a87c;
        padding: 0 2;
        margin-bottom: 2;
        color: #e8a87c;
    }
    """

    def __init__(self, app: "AgendaApp", ev: dict, duration_min: int):
        super().__init__()
        self._target_app = app
        self.cal = ev["calendar"]
        self.event_title = ev["title"]
        self.date_obj = app.target_date
        self.original_start = ev["start"]
        self.original_end = ev["end"]
        self.time = ev["start"]
        self.duration_min = duration_min

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), id="nudge-bar")

    def _render_text(self) -> str:
        end = add_minutes(self.time, self.duration_min)
        delta_min = self._delta_minutes()
        if delta_min == 0:
            delta_str = ""
        else:
            sign = "+" if delta_min > 0 else "−"
            delta_str = f"  [grey50]({sign}{abs(delta_min)}m)[/grey50]"
        return (
            f"[bold yellow]moving:[/bold yellow] "
            f"[#e8ddd4]{self.event_title}[/]  →  "
            f"[reverse] {self.time}–{end} [/reverse]{delta_str}    "
            f"[grey50]↑↓ / k j  nudge ±5m  ·  Enter save  ·  Esc cancel[/grey50]"
        )

    def _delta_minutes(self) -> int:
        try:
            oh, om = map(int, self.original_start.split(":"))
            nh, nm = map(int, self.time.split(":"))
            return (nh * 60 + nm) - (oh * 60 + om)
        except (ValueError, AttributeError):
            return 0

    def _refresh_bar(self) -> None:
        self.query_one("#nudge-bar", Static).update(self._render_text())

    def _apply_to_cache(self, new_time: str) -> None:
        """Update the cached event's start/end and slide the cursor to follow it."""
        new_end = add_minutes(new_time, self.duration_min)
        self.time = new_time
        app = self._target_app
        app.cache_update_time(self.date_obj, self.event_title, self.cal,
                              new_time, new_end)
        # Update focus_index to track the moving event through the new sort order
        raw = app._events_cache.get(self.date_obj, []) or []
        timed = [r for r in raw if r["start"]]
        for i, r in enumerate(timed):
            if r["title"] == self.event_title and r["calendar"] == self.cal:
                try:
                    app.query_one(AgendaPane).focus_index = i
                except Exception:
                    pass
                break
        app._refresh_pane()

    def _nudge(self, delta: int) -> None:
        try:
            h, m = map(int, self.time.split(":"))
        except ValueError:
            return
        total = max(0, min(23 * 60 + 55, h * 60 + m + delta))
        new_time = f"{total // 60:02d}:{total % 60:02d}"
        if new_time == self.time:
            return
        self._apply_to_cache(new_time)
        self._refresh_bar()

    def action_up(self) -> None:
        self._nudge(5)

    def action_down(self) -> None:
        self._nudge(-5)

    def action_commit(self) -> None:
        # Cache already reflects the new time. Caller will fire the gcal write.
        if self.time == self.original_start:
            self.dismiss(None)
            return
        self.dismiss(self.time)

    def action_cancel(self) -> None:
        # Restore original times in the cache
        if self.time != self.original_start:
            app = self._target_app
            app.cache_update_time(self.date_obj, self.event_title, self.cal,
                                  self.original_start, self.original_end)
            app._refresh_pane()
        self.dismiss(None)


# ── Main pane ───────────────────────────────────────────────────────────────


class AgendaPane(Static):
    """Single-pane today view. j/k navigate; status keys + mutations."""

    can_focus = True
    BINDINGS = [
        Binding("j",     "cursor_down", "down", show=False),
        Binding("k",     "cursor_up",   "up",   show=False),
        Binding("down",  "cursor_down", "down", show=False),
        Binding("up",    "cursor_up",   "up",   show=False),
        Binding("g",     "top",         "top",  show=False),
        Binding("G",     "bottom",      "bot",  show=False),
        Binding("space", "cycle_status","cycle"),
        Binding("enter", "cycle_status","cycle", show=False),
        Binding("x",     "mark_done",   "done", show=False),
        Binding("~",     "mark_partial","partial"),
        Binding("s",     "mark_skip",   "skip"),
        Binding("n",     "comment",     "note"),
        Binding("c",     "comment",     "note", show=False),
        Binding("m",     "move",        "move"),
        Binding("w",     "cross_day",   "→ week"),
        Binding("o",     "open",        "open"),
    ]

    focus_index: reactive[int] = reactive(0)
    selectable_count: int = 0
    selectable_events: list = []
    focus_y_lines: list = []

    def render(self) -> str:
        rows = self.app.rows_for(self.app.target_date)
        if rows is None:
            return "[dim]loading…[/dim]"

        all_day = [r for r in rows if not r["start"]]
        timed = [r for r in rows if r["start"]]

        # Selectable = ALL timed events (cursor mental model stays simple).
        self.selectable_events = list(timed)
        self.selectable_count = len(self.selectable_events)
        if self.focus_index >= self.selectable_count:
            self.focus_index = max(0, self.selectable_count - 1)

        # "Now" line — only on today's view
        is_today = self.app.target_date == _date.today()
        now_str = datetime.now().strftime("%H:%M") if is_today else None
        now_marked = not is_today

        def now_line() -> str:
            return f"  [bold #b0c4d8]──── now {now_str} {'─' * 22}[/bold #b0c4d8]"

        lines = []
        focus_y_lines = []

        if all_day:
            for r in all_day:
                color = color_for(r["calendar"])
                lines.append(f"  [grey50]all-day[/grey50]  [{color}]{r['title']}[/]")
            lines.append("")
            lines.append("[grey42]" + "─" * 60 + "[/grey42]")
            lines.append("")

        prev_end = None
        sel_idx = 0
        for i, r in enumerate(timed):
            focused = self.has_focus and sel_idx == self.focus_index
            st_obj = r["status"] or {}
            st = st_obj.get("status")
            is_block = bool(r["meta"])

            # Insert now line before this event if now falls before/at it
            if not now_marked and r["start"] >= now_str:
                if i > 0:
                    lines.append("")
                lines.append(now_line())
                lines.append("")
                now_marked = True

            if is_block:
                marker = {
                    None:      "[ ]",
                    "done":    "[bold green]\\[x][/bold green]",
                    "partial": "[bold yellow]\\[~][/bold yellow]",
                    "skip":    "[bold #d4878a]\\[—][/bold #d4878a]",
                }.get(st, "[ ]")
            else:
                marker = "   "

            color = color_for(r["calendar"])
            time_col = f"{r['start']}–{r['end']}" if r["end"] else r["start"]

            # Gap annotation
            if prev_end and r["start"]:
                gap = minutes_between(prev_end, r["start"])
                if gap >= 30:
                    lines.append(f"     [grey50]↓ gap: {fmt_min(gap)}[/grey50]")

            title = r["title"]
            if st == "done":
                title_md = f"[strike grey50]{title}[/strike grey50]"
            elif st == "skip":
                title_md = f"[grey50 strike]{title}[/grey50 strike]"
            elif st == "partial":
                title_md = f"[strike grey50]{title}[/strike grey50] [dim](partial)[/dim]"
            elif is_block:
                title_md = f"[{color}]{title}[/]"
            else:
                title_md = f"[grey50]{title}[/grey50]"

            if focused:
                line = f"[bold yellow]▶[/bold yellow] {marker} [reverse]{time_col}   {title}[/reverse]"
            else:
                line = f"  {marker} [grey50]{time_col}[/grey50]   {title_md}"

            # Blank line between events (skip before the very first)
            if i > 0:
                lines.append("")

            focus_y_lines.append(len(lines))
            lines.append(line)

            note = st_obj.get("note") if isinstance(st_obj, dict) else ""
            if note:
                lines.append(f"             [dim italic]note: {note}[/dim italic]")

            if r["end"]:
                prev_end = r["end"]
            sel_idx += 1

        # Now line at the bottom if we've passed all events
        if not now_marked:
            if timed:
                lines.append("")
            lines.append(now_line())

        if not timed and not all_day:
            lines.append("[dim]nothing on the calendar[/dim]")

        self.focus_y_lines = focus_y_lines
        self.styles.height = max(1, len(lines))
        text = Text.from_markup("\n".join(lines), overflow="ellipsis")
        text.no_wrap = True
        return text

    # ── Navigation ──────────────────────────────────────────────────────

    def _scroll_to_focused(self) -> None:
        if not (0 <= self.focus_index < len(self.focus_y_lines)):
            return
        y = self.focus_y_lines[self.focus_index]
        parent = self.parent
        if parent is None or not hasattr(parent, "scroll_to"):
            return
        scroll_y = parent.scroll_y
        viewport_h = parent.size.height
        if y < scroll_y + 2:
            parent.scroll_to(y=max(0, y - 2), animate=False)
        elif y > scroll_y + viewport_h - 3:
            parent.scroll_to(y=y - viewport_h + 3, animate=False)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def action_cursor_down(self) -> None:
        if self.selectable_count and self.focus_index < self.selectable_count - 1:
            self.focus_index += 1
            self.call_after_refresh(self._scroll_to_focused)

    def action_cursor_up(self) -> None:
        if self.focus_index > 0:
            self.focus_index -= 1
            self.call_after_refresh(self._scroll_to_focused)

    def action_top(self) -> None:
        self.focus_index = 0
        self.call_after_refresh(self._scroll_to_focused)

    def action_bottom(self) -> None:
        if self.selectable_count:
            self.focus_index = self.selectable_count - 1
            self.call_after_refresh(self._scroll_to_focused)

    # ── Status mutations ────────────────────────────────────────────────

    def _focused(self):
        if not self.selectable_count:
            return None
        if self.focus_index >= self.selectable_count:
            return None
        return self.selectable_events[self.focus_index]

    def _toggle_status(self, new_status: str) -> None:
        """Apply or toggle-off a status on the focused block.

        - If current status == new_status: remove the entry (revert to pending).
        - Else: append a new entry. If a different status was already there,
          we leave the old entry in the log too (audit trail) — but the
          *current* state shown is the most recent.
          Wait — re-read spec: toggle-off-removes-log-entry only applies to
          UN-doing the SAME status. Switching done → partial just appends.
        """
        ev = self._focused()
        if not ev:
            return
        if not ev["meta"]:
            self.app.notify("status only applies to system blocks", severity="warning")
            return

        sys_slug = ev["system"]
        block_name = ev["meta"]["block"]
        date_str = self.app.target_date.strftime("%Y-%m-%d")
        current = (ev["status"] or {}).get("status") if ev["status"] else None

        if current == new_status:
            remove_last_entry(sys_slug, block_name, date_str)
        else:
            # Skip needs a reason — defer to skip flow
            if new_status == "skip":
                self.app.push_screen(
                    SkipReasonScreen(block_name, date_str),
                    lambda reason: self._do_skip(ev, reason) if reason is not None else None,
                )
                return
            append_log(sys_slug, block_name, new_status, date_str, "")

        # Logs changed — re-render picks up new status via rows_for() annotation
        self.app._refresh_pane()
        self.app._update_status_bar()

    def _do_skip(self, ev, reason: str) -> None:
        """Skip = log + optimistic-remove + background gcal delete."""
        sys_slug = ev["system"]
        block_name = ev["meta"]["block"]
        date_str = self.app.target_date.strftime("%Y-%m-%d")
        cal = ev["calendar"]
        title = ev["title"]
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Per-block streak log
        append_log(sys_slug, block_name, "skip", date_str, reason or "")
        # Cross-week skip log — what cl week reads to keep the bank coherent
        # (without this, a skipped block reappears as needing placement)
        append_skip(block_name, date_str, reason or "")
        # Optimistic: drop from cache for this date so the event disappears
        self.app.cache_remove(date_obj, title, cal)
        self.app._refresh_pane()
        self.app._update_status_bar()
        # Background gcal delete — only refetches on failure
        self.app.background_delete(cal, title, date_obj)

    _STATUS_CYCLE = [None, "done", "partial"]

    def action_cycle_status(self) -> None:
        """space/enter — cycle [ ] → [x] → [~] → [ ]. Skip is excluded (use s)."""
        ev = self._focused()
        if not ev or not ev["meta"]:
            if ev and not ev["meta"]:
                self.app.notify("status only applies to system blocks", severity="warning")
            return
        sys_slug = ev["system"]
        block_name = ev["meta"]["block"]
        date_str = self.app.target_date.strftime("%Y-%m-%d")
        current = (ev["status"] or {}).get("status") if ev["status"] else None
        if current == "skip":
            self.app.notify("skipped — event already removed", severity="warning")
            return
        idx = self._STATUS_CYCLE.index(current) if current in self._STATUS_CYCLE else 0
        next_status = self._STATUS_CYCLE[(idx + 1) % len(self._STATUS_CYCLE)]
        # Replace: drop the most recent entry on this date, append new (unless pending)
        remove_last_entry(sys_slug, block_name, date_str)
        if next_status is not None:
            append_log(sys_slug, block_name, next_status, date_str, "")
        self.app._refresh_pane()
        self.app._update_status_bar()

    def action_mark_done(self) -> None:
        self._toggle_status("done")

    def action_mark_partial(self) -> None:
        self._toggle_status("partial")

    def action_mark_skip(self) -> None:
        self._toggle_status("skip")

    # ── Comment / refresh / open ────────────────────────────────────────

    def action_comment(self) -> None:
        ev = self._focused()
        if not ev or not ev["meta"]:
            self.app.notify("comments only apply to system blocks", severity="warning")
            return
        sys_slug = ev["system"]
        block_name = ev["meta"]["block"]
        date_str = self.app.target_date.strftime("%Y-%m-%d")
        existing = (ev["status"] or {}).get("note", "") if ev["status"] else ""

        def _save(note):
            if note is None:
                return
            if ev["status"]:
                update_last_note(sys_slug, block_name, date_str, note)
            else:
                # No status entry yet — create a "done" entry with the note?
                # Cleaner: tell user to mark a status first.
                self.app.notify("mark a status first, then add the note", severity="warning")
                return
            self.app._refresh_pane()
            self.app._update_status_bar()

        self.app.push_screen(CommentScreen(block_name, default=existing), _save)

    def action_open(self) -> None:
        """Open the focused block's working list in $EDITOR.

        Falls back to system.md if working.md doesn't exist — some systems
        won't ever need a project list.
        """
        ev = self._focused()
        if not ev or not ev["meta"]:
            self.app.notify("open only applies to system blocks", severity="warning")
            return
        sys_dir = Path.home() / "kb" / "systems" / ev["system"]
        target = sys_dir / "working.md"
        if not target.exists():
            target = sys_dir / "system.md"
        if not target.exists():
            self.app.notify(f"no system file for {ev['system']}", severity="error")
            return
        editor = os.environ.get("EDITOR", "nvim")
        with self.app.suspend():
            subprocess.run([editor, str(target)])

    # ── Move flows ──────────────────────────────────────────────────────

    def action_move(self) -> None:
        try:
            self._action_move_impl()
        except Exception as exc:
            import traceback
            with open("/tmp/cl-agenda-move.log", "a") as f:
                f.write("=" * 60 + "\n")
                traceback.print_exc(file=f)
            self.app.notify(f"move error: {exc}", severity="error")

    def _action_move_impl(self) -> None:
        ev = self._focused()
        if not ev:
            return
        if not ev["start"] or not ev["meta"]:
            self.app.notify("move only applies to timed system blocks", severity="warning")
            return
        meta = ev["meta"]
        duration_min = parse_duration_minutes(meta.get("duration", "")) or 60
        # Snapshot identifiers for the background write — ev is mutated live as
        # the modal nudges, so we capture what we need now.
        cal = ev["calendar"]
        title = ev["title"]
        date_obj = self.app.target_date
        screen = TimeNudgeScreen(self.app, ev, duration_min)

        def _committed(new_time):
            try:
                if new_time is None:
                    return  # cancelled or no-op (cache already restored)
                # Cache already reflects new time — kick the gcal write.
                self.app.background_move(cal, title, date_obj, new_time, duration_min)
            except Exception as exc:
                import traceback
                with open("/tmp/cl-agenda-move.log", "a") as f:
                    f.write("=" * 60 + "\n")
                    traceback.print_exc(file=f)
                self.app.notify(f"move callback error: {exc}", severity="error")

        self.app.push_screen(screen, _committed)

    def action_cross_day(self) -> None:
        """Pop event from gcal, suspend agenda, launch cl week, return + refresh."""
        ev = self._focused()
        if not ev:
            return
        if not ev["meta"]:
            self.app.notify("cross-day move only applies to system blocks", severity="warning")
            return
        cal = ev["calendar"]
        title = ev["title"]
        date_obj = self.app.target_date

        # Confirm — this deletes from gcal and pops to cl week's bank
        msg = f"Pop [yellow]{title}[/yellow] to cl week to reschedule?"

        def _confirmed(yes):
            if not yes:
                return
            # Sync delete — we want it gone before cl week opens
            ok = delete_event_by_title(cal, title, date_obj)
            if not ok:
                self.app.notify(f"delete failed for {title}", severity="error")
                return
            # Optimistic: drop the event from the visible day's cache so the
            # deleted event doesn't reappear when we resume agenda.
            self.app.cache_remove(date_obj, title, cal)
            # Suspend agenda, launch cl week
            cl = os.path.join(os.path.dirname(__file__), "cl")
            with self.app.suspend():
                subprocess.run([cl, "week"])
            # cl week could have placed the block on any day. Strategy on return:
            #   - keep the visible day's cache (instant render, already reflects delete)
            #   - drop neighbor caches so they refetch when navigated to
            #   - silently reconcile the visible day in the background, in case
            #     cl week placed the block back on today
            target = self.app.target_date
            for d in list(self.app._events_cache.keys()):
                if d != target:
                    self.app._events_cache.pop(d, None)
            self.app.load_day(target, prefetch=True, force=True)
            for delta in (-1, 1):
                self.app.load_day(target + timedelta(days=delta), prefetch=True)
            self.app._refresh_pane()
            self.app._update_status_bar()

        self.app.push_screen(MiniConfirmScreen(msg), _confirmed)


# ── App ─────────────────────────────────────────────────────────────────────


class AgendaApp(App):
    """cl agenda — daily anchor."""

    CSS = """
    Screen { background: #0e1116; }
    #status-bar {
        height: 1;
        padding: 0 2;
        background: #0e1116;
        color: #768390;
    }
    #agenda-scroll {
        border: round #30363d;
        background: #161b22;
        padding: 1 2;
        scrollbar-size-vertical: 1;
    }
    #agenda-scroll:focus-within {
        border: round #e8a87c;
    }
    AgendaPane {
        height: auto;
        background: transparent;
    }
    Footer {
        background: #0e1116;
    }
    """

    BINDINGS = [
        Binding("l",      "next_day",   "→ day"),
        Binding("h",      "prev_day",   "← day"),
        Binding("t",      "today",      "today"),
        Binding("r",      "refresh",    "refresh"),
        Binding("q",      "quit",       "quit"),
        Binding("escape", "quit",       "quit", show=False),
    ]

    target_date: reactive[_date] = reactive(_date.today())
    status: reactive[str] = reactive("starting…")
    pending_writes: reactive[int] = reactive(0)

    def __init__(self, start_date: _date | None = None):
        super().__init__()
        # Stash for on_mount — assigning to self.target_date here would fire
        # the watcher before the event loop exists (load_events → create_task).
        self._pending_start_date = start_date
        # Date-keyed cache of raw gcal event lists (pre-annotation).
        # None = not yet loaded; [] = loaded, empty.
        self._events_cache: dict[_date, list | None] = {}
        self._mounted_once = False

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        with VerticalScroll(id="agenda-scroll", can_focus=False):
            yield AgendaPane(id="agenda")
        yield Footer()

    def on_mount(self) -> None:
        self.write_lock = asyncio.Lock()
        self._mounted_once = True
        if self._pending_start_date and self._pending_start_date != self.target_date:
            # Triggers watch_target_date which calls ensure_loaded
            self.target_date = self._pending_start_date
        else:
            self.title = self._title_for(self.target_date)
            self._update_status_bar()
            self.ensure_loaded(self.target_date)
        self.query_one(AgendaPane).focus()

    def _title_for(self, d: _date) -> str:
        return f"cl agenda — {DAYS[d.weekday()].title()} {d}"

    # ── Cache lookups ────────────────────────────────────────────────────

    def rows_for(self, d: _date):
        """Return annotated rows for date d, or None if not loaded yet.

        Annotation happens fresh on every read so log changes are picked up
        without re-fetching gcal.
        """
        raw = self._events_cache.get(d)
        if raw is None:
            return None
        return annotate_with_status(raw, d.strftime("%Y-%m-%d"))

    def cache_remove(self, d: _date, title: str, calendar: str) -> None:
        """Drop a single event from a cached day (optimistic skip/cross-day)."""
        raw = self._events_cache.get(d)
        if not raw:
            return
        date_str = d.strftime("%Y-%m-%d")
        self._events_cache[d] = [
            r for r in raw
            if not (r["title"] == title and r["calendar"] == calendar and r["date"] == date_str)
        ]

    def cache_update_time(self, d: _date, title: str, calendar: str,
                          new_start: str, new_end: str) -> None:
        """Edit an event's start/end in-place (optimistic same-day move)."""
        raw = self._events_cache.get(d)
        if not raw:
            return
        date_str = d.strftime("%Y-%m-%d")
        for r in raw:
            if r["title"] == title and r["calendar"] == calendar and r["date"] == date_str:
                r["start"] = new_start
                r["end"] = new_end
                break
        # Resort by start time
        raw.sort(key=lambda r: (r["start"] or "", r["title"]))

    def invalidate(self, d: _date) -> None:
        """Drop the cache for d so the next render triggers a fresh fetch."""
        self._events_cache.pop(d, None)

    # ── Loading ──────────────────────────────────────────────────────────

    @work(exclusive=False)
    async def load_day(self, d: _date, prefetch: bool = False,
                       force: bool = False) -> None:
        """Fetch one day from gcal into the cache.

        - No-op if d is already cached and force=False.
        - prefetch=True suppresses the spinner.
        - force=True always fetches, overwriting the cached entry on completion
          (used to silently reconcile after cl week may have changed state).
        """
        if not force and self._events_cache.get(d) is not None:
            return
        if not prefetch:
            self.status = "fetching gcal…"
            self._update_status_bar()
        try:
            events = await asyncio.to_thread(fetch_day_events, d)
            self._events_cache[d] = events
            if d == self.target_date:
                self._refresh_pane()
        except Exception as exc:
            if not prefetch:
                self.status = f"load error: {exc}"
                self._update_status_bar()
            return
        finally:
            if not prefetch and self.status == "fetching gcal…":
                self.status = ""
                self._update_status_bar()

    def ensure_loaded(self, d: _date) -> None:
        """Foreground-load if missing, then prefetch ±1."""
        if self._events_cache.get(d) is None:
            self.load_day(d)
        for delta in (-1, 1):
            neighbor = d + timedelta(days=delta)
            if self._events_cache.get(neighbor) is None:
                self.load_day(neighbor, prefetch=True)

    # ── Mutations ────────────────────────────────────────────────────────

    @work(exclusive=False)
    async def background_delete(self, cal, title, date_obj) -> None:
        """Delete from gcal. Trusts optimistic state on success; refetches only on failure."""
        self.pending_writes += 1
        try:
            async with self.write_lock:
                ok = await asyncio.to_thread(delete_event_by_title, cal, title, date_obj)
                if ok:
                    return
                self.notify(f"delete failed for {title}", severity="error")
                # Invalidate + reload that day
                self.invalidate(date_obj)
                self.load_day(date_obj)
        finally:
            self.pending_writes -= 1
            self._update_status_bar()

    @work(exclusive=False)
    async def background_move(self, cal, title, date_obj, new_time, duration_min) -> None:
        """Same-day move: delete old, add new with same title/calendar at new_time."""
        self.pending_writes += 1
        try:
            async with self.write_lock:
                ok_del = await asyncio.to_thread(delete_event_by_title, cal, title, date_obj)
                if not ok_del:
                    self.notify(f"move: delete failed for {title}", severity="error")
                    self.invalidate(date_obj)
                    self.load_day(date_obj)
                    return
                when = f"{date_obj.strftime('%Y-%m-%d')} {new_time}"
                cmd = [
                    "gcalcli", "add",
                    "--calendar", cal,
                    "--title", title,
                    "--when", when,
                    "--duration", str(duration_min),
                    "--noprompt",
                ]
                result = await asyncio.to_thread(
                    subprocess.run, cmd, capture_output=True, text=True
                )
                if result.returncode != 0:
                    self.notify(f"move: re-add failed: {result.stderr.strip()}", severity="error")
                    self.invalidate(date_obj)
                    self.load_day(date_obj)
        finally:
            self.pending_writes -= 1
            self._update_status_bar()

    def _refresh_pane(self) -> None:
        try:
            self.query_one(AgendaPane).refresh()
        except Exception:
            pass

    def _tally(self) -> str:
        rows = self.rows_for(self.target_date)
        if not rows:
            return ""
        done = partial = skipped = pending = 0
        for r in rows:
            if not r.get("meta") or not r.get("start"):
                continue
            st = (r["status"] or {}).get("status") if r["status"] else None
            if st == "done":      done += 1
            elif st == "partial": partial += 1
            elif st == "skip":    skipped += 1
            else:                 pending += 1
        total = done + partial + skipped + pending
        if not total:
            return ""
        return f"{done}/{total} done · {partial} partial · {skipped} skip · {pending} pending"

    def _update_status_bar(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
            parts = [f"  {self.title}"]
            tally = self._tally()
            if tally:
                parts.append(tally)
            if self.status:
                parts.append(f"[italic]{self.status}[/italic]")
            if self.pending_writes:
                parts.append(f"[yellow]⟳ {self.pending_writes} pending[/yellow]")
            bar.update("  ·  ".join(parts))
        except Exception:
            pass

    def watch_status(self, _o, _n) -> None:
        self._update_status_bar()

    def watch_pending_writes(self, _o, _n) -> None:
        self._update_status_bar()

    def watch_target_date(self, _old: _date, new: _date) -> None:
        # Guard: don't fire async work before the event loop is running
        if not getattr(self, "_mounted_once", False):
            return
        self.title = self._title_for(new)
        # Reset cursor when changing day
        try:
            self.query_one(AgendaPane).focus_index = 0
        except Exception:
            pass
        self._refresh_pane()
        self._update_status_bar()
        self.ensure_loaded(new)

    def action_next_day(self) -> None:
        self.target_date = self.target_date + timedelta(days=1)

    def action_prev_day(self) -> None:
        self.target_date = self.target_date - timedelta(days=1)

    def action_today(self) -> None:
        self.target_date = _date.today()

    def action_refresh(self) -> None:
        # Drop the visible day's cache + neighbors; reload visible day, prefetch others.
        for delta in (-1, 0, 1):
            self.invalidate(self.target_date + timedelta(days=delta))
        self._refresh_pane()
        self._update_status_bar()
        self.ensure_loaded(self.target_date)


def main() -> None:
    AgendaApp().run()


if __name__ == "__main__":
    main()
