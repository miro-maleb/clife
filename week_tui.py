"""week_tui.py — `cl week` TUI.

Two-pane Monday placement assistant:
  ┌─ BANK ─────────────┬─ THIS WEEK ──────────┐
  │ Daily              │ MON 5/4              │
  │  ☐ writing-block 0/5│  07:00 morning ...  │
  │  ...               │  ↓ gap: 1h20         │
  │                    │  10:10 writing ...   │
  │ Weekly             │ ...                  │
  │  ☐ budget 0/1      │ TUE 5/5              │
  └────────────────────┴──────────────────────┘

Tab cycles focus. j/k navigate. Enter (bank) opens schedule modal.
d/s/m (week pane) delete/skip/move. q or esc quits.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Static
from textual import work
from rich.text import Text

import re
from datetime import date as _date

from textual.containers import Vertical
from textual.widgets import Input

from week import (
    DAYS,
    delete_event_by_title,
    expected_count,
    fetch_events,
    find_block,
    list_calendars,
    load_blocks,
    parse_day,
    parse_duration_minutes,
    pick_title,
    place_event,
    skip_event,
    suggest_time,
    week_range,
    week_skip_counts,
    week_skip_dates,
)


# Calendars to skip in the right-pane display. The Sydney calendar tracks her
# travel/whereabouts (useful as background context but not events I act on).
# Edit this set if you want to silence other calendars from the bank view.
EXCLUDE_CALENDARS = {"Sydney"}

# Per-calendar event color (Rich color). Falls back to white for unknown cals.
CALENDAR_COLORS = {
    "Professional":              "#e67c73",  # flamingo (gcal) — work / output
    "Miro-Personal":             "#7fbf3f",  # green  — daily life
    "Spiritual Practice":        "#bf7fff",  # purple — practice
    "Retreats":                  "#ffaf5f",  # orange — retreats
    "Travel":                    "#5fdfdf",  # cyan   — travel
    "Holidays in United States": "#7f7f7f",  # gray   — context only
    "sydneyslavitt@gmail.com":   "#df87af",  # pink   — sydney's invites
}


def color_for(cal: str) -> str:
    return CALENDAR_COLORS.get(cal, "white")


def minutes_between(start, end):
    """Minutes between two HH:MM strings."""
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return (eh * 60 + em) - (sh * 60 + sm)


def fmt_min(mins):
    """Pretty: 90 → '1h30', 30 → '30m', 60 → '1h'."""
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}" if m else f"{h}h"


def add_minutes(time_str, mins):
    """'10:10' + 90 → '11:40'. Clamps to 23:59."""
    h, m = map(int, time_str.split(":"))
    total = min(23 * 60 + 59, h * 60 + m + mins)
    return f"{total // 60:02d}:{total % 60:02d}"


def next_available_day(meta, monday, events_for_cal):
    """First day this week where this block can be placed.

    Daily: first day in meta.days not yet placed for this block.
    Weekly: first day in meta.days (instance numbering handled by pick_title).
    """
    days = meta.get("days") or DAYS
    block_name = meta.get("block", "")
    cadence = meta.get("cadence", "")
    for offset in range(7):
        date = monday + timedelta(days=offset)
        weekday = DAYS[date.weekday()]
        if weekday not in days:
            continue
        if cadence == "daily":
            target = date.strftime("%Y-%m-%d")
            placed = any(t == block_name and d == target for d, _, _, t in events_for_cal)
            if placed:
                continue
        return date
    return monday  # fallback


class MiniConfirmScreen(ModalScreen):
    """A compact y/n prompt anchored to the bottom of the screen.

    Smaller and less jarring than a full-screen-ish dialog — the bank and
    week pane stay visible behind it.
    """

    BINDINGS = [
        Binding("y", "yes", show=False),
        Binding("n", "no", show=False),
        Binding("escape", "no", show=False),
        Binding("enter", "yes", show=False),
    ]

    CSS = """
    MiniConfirmScreen {
        align: center bottom;
        background: transparent;
    }
    #mini-confirm {
        width: auto;
        max-width: 80;
        height: 3;
        background: #1f1411;
        border: round #d4878a;
        padding: 0 2;
        margin-bottom: 2;
        color: #e8a87c;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(
            f"{self._message}  [bold #a8c5a0]y[/]es / [bold #d4878a]n[/]o",
            id="mini-confirm",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class SkipReasonScreen(ModalScreen):
    """Modal: optional reason for a skip. Empty input = skip with no reason."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    CSS = """
    SkipReasonScreen { align: center middle; }
    #skip-dialog {
        width: 64;
        height: auto;
        background: #161b22;
        border: round #e8a87c;
        padding: 1 2;
    }
    """

    def __init__(self, block_name: str, day_str: str):
        super().__init__()
        self.block_name = block_name
        self.day_str = day_str

    def compose(self) -> ComposeResult:
        with Vertical(id="skip-dialog"):
            yield Static(
                f"[bold]Skip:[/bold] [yellow]{self.block_name}[/yellow] on {self.day_str}\n"
                f"[grey50]Reason (optional) — Enter to confirm, Esc cancel[/grey50]"
            )
            yield Input(placeholder="reason…", id="skip-reason-input")

    def on_mount(self) -> None:
        self.query_one("#skip-reason-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ScheduleScreen(ModalScreen):
    """Modal: schedule a block. Up/down/jk nudge time ±5m. Left/right cycle day."""

    BINDINGS = [
        Binding("enter", "commit", "schedule"),
        Binding("escape", "cancel", "cancel"),
        Binding("tab", "next_field", "switch field"),
        Binding("left", "prev_day", "← day", show=False),
        Binding("right", "next_day", "day →", show=False),
        Binding("h", "prev_day", "← day", show=False),
        Binding("l", "next_day", "day →", show=False),
        Binding("up", "time_up", "+5m", show=False),
        Binding("down", "time_down", "-5m", show=False),
        Binding("k", "time_up", "+5m", show=False),
        Binding("j", "time_down", "-5m", show=False),
    ]

    CSS = """
    ScheduleScreen {
        align: center top;
        background: transparent;
    }
    #schedule-panel {
        width: auto;          /* size to content — no extra slack */
        height: auto;
        background: #161b22;
        border: round #e8a87c;
        padding: 0 1;
        margin-top: 0;
    }
    """

    def __init__(self, block_name, meta, default_day_str, default_time, offset_weeks, replace_old=None):
        super().__init__()
        self.block_name = block_name
        self.meta = meta
        self.day = default_day_str
        self.time = default_time
        self.offset_weeks = offset_weeks
        self.field = "time"
        self.last_msg = ""
        # replace_old: (calendar, title, date_obj) — if set, will delete that event after commit
        self.replace_old = replace_old
        # Live-preview state
        self._preview_pseudo = None        # the optimistic event we've added to events_by_cal
        self._reverted_old = None          # the event we've removed (for move) — to restore on cancel
        self._committed = False

    def compose(self) -> ComposeResult:
        yield Static(self._render_panel(), id="schedule-panel")

    def _render_panel(self) -> str:
        day_label = (
            f"[reverse]{self.day}[/reverse]" if self.field == "day" else self.day
        )
        time_label = (
            f"[reverse]{self.time}[/reverse]" if self.field == "time" else self.time
        )
        verb = "move" if self.replace_old else "place"
        msg = f"  [red]{self.last_msg}[/red]" if self.last_msg else ""
        # Single line — keeps the modal at 3 rows (1 content + 2 border)
        return (
            f"{verb} [yellow]{self.block_name}[/yellow]  "
            f"{day_label} [grey50]←→[/grey50]  "
            f"{time_label} [grey50]↑↓[/grey50]  "
            f"[grey50]⏎[/grey50] save  [grey50]esc[/grey50]{msg}"
        )

    def _refresh(self) -> None:
        self.query_one("#schedule-panel", Static).update(self._render_panel())

    def on_mount(self) -> None:
        # Apply live preview as soon as the modal opens (so the user sees the
        # event in its proposed position from the start)
        self._apply_preview()

    def _resolve(self):
        """Compute (cal, date, duration_min, title, err_msg). title is None on failure."""
        cal = self.meta.get("calendar")
        if not cal:
            return (None, None, None, None, f"{self.block_name} has no calendar")
        duration_min = parse_duration_minutes(self.meta.get("duration", ""))
        if not duration_min:
            return (cal, None, None, None, "no parseable duration")
        monday, _ = week_range(offset_weeks=self.offset_weeks)
        date = parse_day(self.day, monday)
        if not date:
            return (cal, None, duration_min, None, f"bad day: {self.day}")
        days = self.meta.get("days") or DAYS
        weekday = DAYS[date.weekday()]
        if weekday not in days:
            return (cal, date, duration_min, None,
                    f"{self.block_name} doesn't run on {weekday}")
        # Filter out the old event when computing pick_title (for move)
        existing = list(self.app.events_by_cal.get(cal, []))
        if self._reverted_old:
            pass  # already removed from state
        elif self.replace_old:
            old_cal, old_title, old_date_obj = self.replace_old
            if old_cal == cal:
                old_date_str = old_date_obj.strftime("%Y-%m-%d")
                existing = [e for e in existing if not (e[0] == old_date_str and e[3] == old_title)]
        title = pick_title(self.meta, self.block_name, date, existing)
        if not title:
            cadence = self.meta.get("cadence", "")
            if cadence == "daily":
                err = f"already scheduled on {weekday}"
            elif cadence == "weekly":
                instances = int(self.meta.get("instances", 1) or 1)
                err = "already scheduled this week" if instances == 1 else f"all {instances} slots taken this week"
            else:
                err = "slot taken"
            return (cal, date, duration_min, None, err)
        return (cal, date, duration_min, title, "")

    def _apply_preview(self) -> None:
        """Mutate events_by_cal to reflect the current proposed day/time."""
        cal, date, duration_min, title, err = self._resolve()
        if not title:
            self.last_msg = err or "slot not available"
            self._refresh()
            return
        # First time only: remove the OLD event for moves
        if self.replace_old and self._reverted_old is None:
            old_cal, old_title, old_date_obj = self.replace_old
            old_date_str = old_date_obj.strftime("%Y-%m-%d")
            self.app.events_by_cal[old_cal] = [
                e for e in self.app.events_by_cal.get(old_cal, [])
                if not (e[0] == old_date_str and e[3] == old_title)
            ]
            self._reverted_old = (old_cal, old_date_str, old_title)
        date_str = date.strftime("%Y-%m-%d")
        end_str = add_minutes(self.time, duration_min)
        pseudo = (date_str, self.time, end_str, title)
        self.app.events_by_cal.setdefault(cal, []).append(pseudo)
        self._preview_pseudo = (cal, pseudo)
        # Mark this event so the WeekPane can highlight it as the in-progress
        # preview (distinguishable from regular events)
        self.app.preview_marker = (cal, date_str, title)
        self.last_msg = ""
        self._refresh()
        self.app._refresh_panes()
        self._scroll_weekpane_to_preview()

    def _scroll_weekpane_to_preview(self) -> None:
        if not self._preview_pseudo:
            return
        _cal, pseudo = self._preview_pseudo
        date_str, _start, _end, title = pseudo
        try:
            wp = self.app.query_one(WeekPane)
        except Exception:
            return
        # Force a synchronous render so focus_y_lines / selectable_events
        # reflect the just-applied preview.
        wp.render()
        for i, ev in enumerate(wp.selectable_events):
            if ev[0] == date_str and ev[3] == title:
                if i < len(wp.focus_y_lines):
                    y = wp.focus_y_lines[i]
                    parent = wp.parent
                    if parent is None or not hasattr(parent, "scroll_to"):
                        return
                    viewport_h = parent.size.height
                    max_scroll = parent.max_scroll_y
                    scroll_y = parent.scroll_y
                    # Modal occupies top 3 lines (1 content + 2 border)
                    modal_buffer = 3
                    # Already comfortably visible? Don't move.
                    if scroll_y + modal_buffer <= y <= scroll_y + viewport_h - 2:
                        return
                    # Otherwise scroll just enough to put preview below modal
                    target = max(0, min(max_scroll, y - modal_buffer))
                    parent.scroll_to(y=target, animate=False)
                return

    def _revert_preview(self) -> None:
        """Undo the optimistic mutations (used on cancel or before re-applying with new values)."""
        if self._preview_pseudo:
            cal, pseudo = self._preview_pseudo
            self.app.events_by_cal[cal] = [
                e for e in self.app.events_by_cal.get(cal, [])
                if e is not pseudo  # identity match to avoid removing unrelated dupes
            ]
            self._preview_pseudo = None
        self.app.preview_marker = None

    def _restore_old(self) -> None:
        """For move: re-add the original event we removed on apply."""
        if self._reverted_old and self.replace_old:
            old_cal, old_title, old_date_obj = self.replace_old
            # Reconstruct from the original gcal data — best we have is title + date.
            # The actual start/end times aren't preserved here; refetch will reconcile.
            # For visual continuity, push a placeholder back; refetch will replace.
            # To keep it simple, just trigger a refetch on the old cal.
            self._reverted_old = None
            self.app.refetch_one(old_cal)

    def action_next_field(self) -> None:
        self.field = "time" if self.field == "day" else "day"
        self._refresh()

    def action_prev_day(self) -> None:
        idx = DAYS.index(self.day) if self.day in DAYS else 0
        if idx == 0:
            return  # already on monday — don't wrap to sunday
        self.day = DAYS[idx - 1]
        self._revert_preview()
        self._apply_preview()

    def action_next_day(self) -> None:
        idx = DAYS.index(self.day) if self.day in DAYS else 0
        if idx >= 6:
            return  # already on sunday — don't wrap to monday
        self.day = DAYS[idx + 1]
        self._revert_preview()
        self._apply_preview()

    def _nudge(self, delta: int) -> None:
        try:
            h, m = map(int, self.time.split(":"))
        except ValueError:
            return
        total = max(0, min(23 * 60 + 55, h * 60 + m + delta))
        self.time = f"{total // 60:02d}:{total % 60:02d}"
        self._revert_preview()
        self._apply_preview()

    def action_time_up(self) -> None:
        self._nudge(5)

    def action_time_down(self) -> None:
        self._nudge(-5)

    def action_commit(self) -> None:
        """Confirm the live preview + schedule the gcal write in the background.

        Preview was already applied on mount/adjust, so the user has been
        seeing the "facade" the whole time. Commit just dismisses + persists.
        """
        if not self._preview_pseudo:
            # No valid preview applied (e.g. slot taken). Try again, may show error.
            self._apply_preview()
            if not self._preview_pseudo:
                return
        self._committed = True
        cal, pseudo = self._preview_pseudo
        date_str, start, end, title = pseudo
        when = f"{date_str} {start}"
        # Clear the preview marker — event is now confirmed, render normally
        self.app.preview_marker = None
        self.app._refresh_panes()
        # Schedule the gcal write — preview stays in place
        self.app.background_place(
            self.block_name, self.day, self.time,
            replace_old=self.replace_old,
        )
        self.dismiss({"ok": True, "title": title, "calendar": cal, "when": when})

    def action_cancel(self) -> None:
        # Revert the live preview before dismissing
        self._revert_preview()
        self._restore_old()
        self.app.preview_marker = None
        self.app._refresh_panes()
        self.dismiss(None)


class BankPane(Static):
    """Left pane — placement bank, daily + weekly sections."""

    can_focus = True
    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("enter", "schedule", "schedule"),
        Binding("s", "skip", "skip"),
    ]

    focus_index: reactive[int] = reactive(0)
    selectable_blocks: list = []
    focus_y_lines: list = []

    def render(self) -> str:
        active = getattr(self.app, "active", None)
        events_by_cal = getattr(self.app, "events_by_cal", None)
        if not active or events_by_cal is None:
            return "[dim]loading…[/dim]"

        # In-flight schedule modal preview shouldn't count toward "placed" —
        # the bank should only mark off on commit, not on modal open.
        preview_marker = getattr(self.app, "preview_marker", None)

        skips = week_skip_counts(self.app.monday, self.app.sunday)

        # Compute (found, expected) per block
        rows = []
        for sys_slug, meta, _ in active:
            block_name = meta.get("block", "?")
            cal = meta.get("calendar", "")
            try:
                instances = int(meta.get("instances", 1) or 1)
            except (ValueError, TypeError):
                instances = 1
            expected_raw = expected_count(meta)
            if expected_raw == 0:
                continue
            expected = max(0, expected_raw - skips.get(block_name, 0))
            events = events_by_cal.get(cal, [])
            if preview_marker is not None:
                pm_cal, pm_date, pm_title = preview_marker
                if pm_cal == cal:
                    events = [e for e in events if not (e[0] == pm_date and e[3] == pm_title)]
            if instances <= 1:
                found = sum(1 for _, _, _, t in events if t == block_name)
            else:
                found = sum(
                    1 for i in range(1, instances + 1)
                    if any(t == f"{block_name} #{i}" for _, _, _, t in events)
                )
            rows.append((sys_slug, meta, found, expected))

        def daily_key(r):
            # Sort by default_start so they appear in chronological order
            # (mornings first). Blocks with no default_start sort to the end.
            ds = (r[1].get("default_start") or "").strip()
            return (ds or "99:99", r[1].get("block", ""))

        daily = sorted(
            [r for r in rows if r[1].get("cadence") == "daily"],
            key=daily_key,
        )
        weekly = sorted(
            [r for r in rows if r[1].get("cadence") == "weekly"],
            key=lambda r: r[1].get("block", ""),
        )

        self.selectable_blocks = [r for r in (daily + weekly) if r[2] < r[3]]

        if self.focus_index >= len(self.selectable_blocks):
            self.focus_index = max(0, len(self.selectable_blocks) - 1)

        lines = []
        sel_idx = 0
        focus_y_lines = []
        for label, items in [("Daily", daily), ("Weekly", weekly)]:
            if not items:
                continue
            lines.append(f"[bold cyan]{label}[/bold cyan]")
            for sys_slug, meta, found, expected in items:
                block_name = meta.get("block", "?")
                placed = found >= expected

                if placed:
                    lines.append(
                        f"  [green]☑[/green] [strike grey50]{block_name:22s} {found}/{expected}[/strike grey50]"
                    )
                else:
                    focused = self.has_focus and sel_idx == self.focus_index
                    focus_y_lines.append(len(lines))
                    if focused:
                        lines.append(
                            f"[bold yellow]▶[/bold yellow] ☐ [reverse]{block_name:22s} {found}/{expected}[/reverse]"
                        )
                    else:
                        lines.append(
                            f"  ☐ {block_name:22s} [grey50]{found}/{expected}[/grey50]"
                        )
                    sel_idx += 1
            lines.append("")

        if not self.selectable_blocks:
            lines.append("[dim]everything placed for the week ✓[/dim]")

        self.focus_y_lines = focus_y_lines
        self.styles.height = max(1, len(lines))
        text = Text.from_markup("\n".join(lines), overflow="ellipsis")
        text.no_wrap = True
        return text

    def _scroll_to_focused(self) -> None:
        if not (0 <= self.focus_index < len(self.focus_y_lines)):
            return
        y = self.focus_y_lines[self.focus_index]
        parent = self.parent
        if parent is not None and hasattr(parent, "scroll_to"):
            parent.scroll_to(y=max(0, y - 2), animate=False)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def action_cursor_down(self) -> None:
        if self.selectable_blocks and self.focus_index < len(self.selectable_blocks) - 1:
            self.focus_index += 1
            self.call_after_refresh(self._scroll_to_focused)

    def action_cursor_up(self) -> None:
        if self.focus_index > 0:
            self.focus_index -= 1
            self.call_after_refresh(self._scroll_to_focused)

    def action_schedule(self) -> None:
        if not self.selectable_blocks:
            return
        if self.focus_index >= len(self.selectable_blocks):
            return
        sys_slug, meta, found, expected = self.selectable_blocks[self.focus_index]
        block_name = meta.get("block", "?")
        cal = meta.get("calendar", "")
        events_for_cal = self.app.events_by_cal.get(cal, [])
        default_date = next_available_day(meta, self.app.monday, events_for_cal)
        default_day = DAYS[default_date.weekday()]
        default_time = suggest_time(meta, default_date, events_for_cal)
        screen = ScheduleScreen(
            block_name, meta, default_day, default_time, self.app.offset_weeks
        )
        self.app.push_screen(screen, self.app.on_schedule_dismissed)

    def action_skip(self) -> None:
        if not self.selectable_blocks:
            return
        if self.focus_index >= len(self.selectable_blocks):
            return
        sys_slug, meta, found, expected = self.selectable_blocks[self.focus_index]
        block_name = meta.get("block", "?")
        cadence = meta.get("cadence", "")
        cal = meta.get("calendar", "")
        monday = self.app.monday
        sunday = self.app.sunday

        if cadence == "daily":
            events_for_cal = self.app.events_by_cal.get(cal, [])
            skipped_dates = week_skip_dates(block_name, monday, sunday)
            days = meta.get("days") or DAYS
            target_date = None
            for offset in range(7):
                date = monday + timedelta(days=offset)
                weekday = DAYS[date.weekday()]
                if weekday not in days:
                    continue
                target_str = date.strftime("%Y-%m-%d")
                already_placed = any(
                    t == block_name and d == target_str
                    for d, _, _, t in events_for_cal
                )
                if already_placed or date in skipped_dates:
                    continue
                target_date = date
                break
            if not target_date:
                self.app.notify(
                    f"no remaining days to skip for {block_name}",
                    severity="warning",
                )
                return
            day_label = DAYS[target_date.weekday()]
            date_str = target_date.strftime("%Y-%m-%d")
            msg = f"Skip [yellow]{block_name}[/yellow] for [bold]{day_label}[/bold] ({date_str})?"
            self.app.push_screen(
                MiniConfirmScreen(msg),
                lambda yes: self._do_bank_skip(block_name, date_str) if yes else None,
            )
            return

        # Weekly (instances=1 or multi): one slot per `s`, dated to monday
        date_str = monday.strftime("%Y-%m-%d")
        msg = f"Skip [yellow]{block_name}[/yellow] for the week?"
        self.app.push_screen(
            MiniConfirmScreen(msg),
            lambda yes: self._do_bank_skip(block_name, date_str) if yes else None,
        )

    def _do_bank_skip(self, block_name, day_str) -> None:
        result = skip_event(
            block_name, day_str, "", offset_weeks=self.app.offset_weeks
        )
        if result["ok"]:
            self.app._refresh_panes()
        else:
            self.app.notify(result["msg"], severity="error")


class WeekPane(Static):
    """Right pane — this week's events grouped by day with gap annotations."""

    can_focus = True
    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("d", "delete", "delete"),
        Binding("s", "skip", "skip"),
        Binding("m", "move", "move"),
    ]

    focus_index: reactive[int] = reactive(0)
    selectable_count: int = 0
    focus_y_lines: list = []  # line index for each selectable event (for auto-scroll)

    def render(self) -> str:
        events_by_cal = getattr(self.app, "events_by_cal", None)
        monday = getattr(self.app, "monday", None)
        if events_by_cal is None or monday is None:
            return "[dim]loading…[/dim]"

        # Merge all events across calendars, sort by date then start time
        all_events = []
        for cal, events in events_by_cal.items():
            for d, s, e, t in events:
                all_events.append((d, s, e, t, cal))
        all_events.sort(key=lambda x: (x[0], x[1] or "00:00"))

        # Index events by date for per-day rendering
        events_by_date = {}
        for ev in all_events:
            events_by_date.setdefault(ev[0], []).append(ev)

        # Selectable events = all real events (in display order)
        self.selectable_events = list(all_events)
        self.selectable_count = len(self.selectable_events)
        if self.focus_index >= self.selectable_count:
            self.focus_index = max(0, self.selectable_count - 1)

        lines = []
        sel_idx = 0
        focus_y_lines = []
        preview_marker = getattr(self.app, "preview_marker", None)
        for offset in range(7):
            date = monday + timedelta(days=offset)
            d_str = date.strftime("%Y-%m-%d")
            weekday = DAYS[date.weekday()].upper()
            day_label = f"[bold cyan]{weekday} {date.month}/{date.day}[/bold cyan]"

            day_events = events_by_date.get(d_str, [])
            if not day_events:
                lines.append(f"{day_label}  [dim]·[/dim]")
                continue

            for i, (d, s, e, t, cal) in enumerate(day_events):
                focused = self.has_focus and sel_idx == self.focus_index
                is_preview = preview_marker is not None and preview_marker == (cal, d, t)
                color = color_for(cal)
                if is_preview:
                    marker = "[bold #ffd75f]★[/bold #ffd75f]"
                elif focused:
                    marker = "[bold yellow]▶[/bold yellow]"
                else:
                    marker = " "
                # First event of the day shows the day label in the prefix;
                # subsequent events on the same day get a blank prefix of equal width
                prefix = day_label if i == 0 else "       "  # ≈ "MON 5/4" width
                if not s:
                    body = f"[dim]all-day[/dim]   [{color}]{t}[/]"
                else:
                    dur = fmt_min(minutes_between(s, e)) if e else "?"
                    if is_preview:
                        body = f"[bold #ffd75f underline on #3a2f1a]{s} {t} ({dur})[/]"
                    elif focused:
                        body = f"[{color} reverse]{s} {t} ({dur})[/]"
                    else:
                        body = f"[grey50]{s}[/grey50] [{color}]{t}[/] [grey50]({dur})[/grey50]"
                focus_y_lines.append(len(lines))
                lines.append(f"{prefix} {marker} {body}")
                sel_idx += 1

        self.focus_y_lines = focus_y_lines
        # Trailing blank lines so the last events on Sunday can be scrolled
        # into view above the viewport's bottom edge / status bar / footer.
        lines.extend([""] * 10)
        self.styles.height = max(1, len(lines))
        # no_wrap so each logical line stays on one visual line — otherwise
        # narrow-width wrap inflates real height and clips later days
        text = Text.from_markup("\n".join(lines), overflow="ellipsis")
        text.no_wrap = True
        return text

    def _scroll_to_focused(self) -> None:
        if not (0 <= self.focus_index < len(self.focus_y_lines)):
            return
        y = self.focus_y_lines[self.focus_index]
        parent = self.parent
        if parent is None or not hasattr(parent, "scroll_to"):
            return
        viewport_h = parent.size.height
        max_scroll = parent.max_scroll_y
        scroll_y = parent.scroll_y
        # At the last event: jump all the way to max so the bottom is fully revealed
        if self.focus_index == self.selectable_count - 1:
            parent.scroll_to(y=max_scroll, animate=False)
            return
        # Otherwise: only scroll if the focus is outside the viewport.
        # Keeps the view stable instead of jumping on every keypress.
        top_margin, bot_margin = 1, 2
        if y < scroll_y + top_margin:
            parent.scroll_to(y=max(0, y - top_margin), animate=False)
        elif y > scroll_y + viewport_h - bot_margin:
            parent.scroll_to(
                y=max(0, min(max_scroll, y - viewport_h + bot_margin + 1)),
                animate=False,
            )

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

    def _focused_event(self):
        if not self.selectable_count:
            return None
        if self.focus_index >= self.selectable_count:
            return None
        return self.selectable_events[self.focus_index]

    def _block_from_title(self, title):
        """Return (sys_slug, meta) if title matches a known block, else (None, None)."""
        base = re.sub(r" #\d+$", "", title)
        return find_block(base)

    def action_delete(self) -> None:
        ev = self._focused_event()
        if not ev:
            return
        d, s, e, t, cal = ev
        msg = f"Delete [yellow]{t}[/yellow] on {d}?"
        self.app.push_screen(
            MiniConfirmScreen(msg),
            lambda yes: self._do_delete(ev) if yes else None,
        )

    def _do_delete(self, ev) -> None:
        d, s, e, t, cal = ev
        date_obj = datetime.strptime(d, "%Y-%m-%d").date()
        # Optimistic: remove from local state immediately
        self.app.events_by_cal[cal] = [
            x for x in self.app.events_by_cal.get(cal, [])
            if not (x[0] == d and x[3] == t)
        ]
        self.app._refresh_panes()
        # Background gcal delete + reconcile (only notifies on failure)
        self.app.background_delete(cal, t, date_obj)

    def action_skip(self) -> None:
        ev = self._focused_event()
        if not ev:
            return
        d, s, e, t, cal = ev
        sys_slug, meta = self._block_from_title(t)
        if not meta:
            self.app.notify("skip only applies to system block events", severity="warning")
            return
        block_name = meta.get("block")
        self.app.push_screen(
            SkipReasonScreen(block_name, d),
            lambda reason: self._do_skip(block_name, d, reason) if reason is not None else None,
        )

    def _do_skip(self, block_name, day_str, reason) -> None:
        result = skip_event(block_name, day_str, reason or "", offset_weeks=self.app.offset_weeks)
        if result["ok"]:
            sys_slug, meta = find_block(block_name)
            cal = meta.get("calendar") if meta else ""
            self.app.refetch_one(cal)
        else:
            self.app.notify(result["msg"], severity="error")

    def action_move(self) -> None:
        ev = self._focused_event()
        if not ev:
            return
        d, s, e, t, cal = ev
        if not s:
            self.app.notify("can't move all-day events from here", severity="warning")
            return
        sys_slug, meta = self._block_from_title(t)
        if not meta:
            self.app.notify("move only applies to system block events", severity="warning")
            return
        block_name = meta.get("block")
        date_obj = datetime.strptime(d, "%Y-%m-%d").date()
        weekday = DAYS[date_obj.weekday()]
        screen = ScheduleScreen(
            block_name, meta, weekday, s, self.app.offset_weeks,
            replace_old=(cal, t, date_obj),
        )
        self.app.push_screen(screen, self._after_move)

    def _after_move(self, result) -> None:
        # Optimistic update already happened in ScheduleScreen.action_commit;
        # background_place is doing the gcal delete-old + add-new + reconcile.
        if result and result.get("ok"):
            self.app._refresh_panes()


class WeekApp(App):
    """cl week — Monday placement assistant."""

    CSS = """
    Screen {
        background: #0e1116;
    }
    #status-bar {
        height: 1;
        padding: 0 2;
        background: #0e1116;
        color: #768390;
    }
    #bank-scroll, #week-scroll {
        border: round #30363d;
        background: #161b22;
        padding: 0;                    /* no chrome eating viewport rows */
        scrollbar-size-vertical: 1;    /* thin bar so overflow is visible */
        scrollbar-background: #161b22;
        scrollbar-color: #30363d;
    }
    #bank-scroll {
        width: 38;
    }
    #bank-scroll:focus-within, #week-scroll:focus-within {
        border: round #e8a87c;
    }
    BankPane, WeekPane {
        height: auto;
        background: transparent;
        padding: 1 2;                  /* padding lives on the inner pane */
    }
    Footer {
        background: #0e1116;
    }
    """

    BINDINGS = [
        Binding("tab", "focus_next", "next pane"),
        Binding("shift+tab", "focus_previous", "prev pane"),
        Binding("ctrl+w", "bank_weekly", "bank: weekly", show=False),
        Binding("ctrl+d", "bank_daily", "bank: daily", show=False),
        Binding("n", "next_week", "→ week"),
        Binding("p", "prev_week", "← week"),
        Binding("r", "refresh", "refresh"),
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
        Binding("?", "help", "help", show=False),
    ]

    offset_weeks: reactive[int] = reactive(0)
    status: reactive[str] = reactive("starting…")
    pending_writes: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        with Horizontal():
            # can_focus=False so Tab lands on the inner pane (not the scroll wrapper)
            with VerticalScroll(id="bank-scroll", can_focus=False):
                yield BankPane(id="bank")
            with VerticalScroll(id="week-scroll", can_focus=False):
                yield WeekPane(id="week")
        yield Footer()

    def on_mount(self) -> None:
        # Serialize gcalcli writes — concurrent invocations were the source of
        # the "rapid-fire commits sometimes don't write" bug. Lock ensures one
        # gcalcli operation runs at a time while UI stays optimistic.
        self.write_lock = asyncio.Lock()
        self._setup_state()
        bank = self.query_one(BankPane)
        bank.focus()
        self._update_status_bar()
        self.load_events()

    def _setup_state(self) -> None:
        """Sync setup — no network. Clears events_by_cal so the panes show 'loading…'."""
        self.blocks = load_blocks()
        self.active = [(s, m, st) for s, m, st in self.blocks if st == "active"]
        monday, sunday = week_range(offset_weeks=self.offset_weeks)
        self.monday = monday
        self.sunday = sunday
        self.events_by_cal = {}
        if not hasattr(self, "all_calendars"):
            self.all_calendars = []
        self.title = f"cl week — {monday} → {sunday}"

    @work(exclusive=True)
    async def load_events(self) -> None:
        """Async loader: each gcalcli call runs in a thread via asyncio.to_thread.

        UI thread stays responsive between fetches; events_by_cal fills in
        progressively and panes refresh after each calendar.
        """
        try:
            if not self.all_calendars:
                self.status = "discovering calendars…"
                cals = await asyncio.to_thread(list_calendars)
                self.all_calendars = [c for c in cals if c not in EXCLUDE_CALENDARS]
            for cal in self.all_calendars:
                self.status = f"fetching {cal}…"
                events = await asyncio.to_thread(fetch_events, cal, self.monday, self.sunday)
                self.events_by_cal[cal] = events
                self._refresh_panes()
            self.status = ""
        except Exception as exc:
            self.status = f"load error: {exc}"

    @work(exclusive=False)
    async def refetch_one(self, cal: str) -> None:
        """Single-calendar refetch in the background after a mutation."""
        if not cal:
            return
        self.status = f"refetching {cal}…"
        try:
            self.events_by_cal[cal] = await asyncio.to_thread(
                fetch_events, cal, self.monday, self.sunday
            )
        finally:
            self.status = ""
        self._refresh_panes()

    @work(exclusive=False)
    async def background_delete(self, cal, title, date_obj) -> None:
        """Delete an event from gcal. Serialized via write_lock; refetch only on failure."""
        self.pending_writes += 1
        try:
            async with self.write_lock:
                ok = await asyncio.to_thread(delete_event_by_title, cal, title, date_obj)
                if not ok:
                    self.notify(f"delete failed for {title}", severity="error")
                    self.events_by_cal[cal] = await asyncio.to_thread(
                        fetch_events, cal, self.monday, self.sunday
                    )
                    self._refresh_panes()
        finally:
            self.pending_writes -= 1

    @work(exclusive=False)
    async def background_place(self, block_name, day_arg, time_arg, replace_old=None) -> None:
        """Persist a placement (and optionally delete an old event for move) to gcal.

        Serialized via write_lock — only one gcalcli call at a time. Pairs with
        the optimistic UI in ScheduleScreen so the user can rapid-fire commits
        without waiting on each gcalcli round-trip.
        On success: trust the write, keep optimistic state.
        On failure: refetch the affected calendar(s) to revert.
        """
        self.pending_writes += 1
        try:
            async with self.write_lock:
                if replace_old:
                    old_cal, old_title, old_date_obj = replace_old
                    await asyncio.to_thread(delete_event_by_title, old_cal, old_title, old_date_obj)
                result = await asyncio.to_thread(
                    place_event, block_name, day_arg, time_arg, offset_weeks=self.offset_weeks
                )
                if result["ok"]:
                    return  # trust the write — keep optimistic state
                self.notify(f"write failed: {result['msg']}", severity="error")
                sys_slug, meta = find_block(block_name)
                cal = meta.get("calendar") if meta else None
                if cal:
                    self.events_by_cal[cal] = await asyncio.to_thread(
                        fetch_events, cal, self.monday, self.sunday
                    )
                    if replace_old and replace_old[0] != cal:
                        self.events_by_cal[replace_old[0]] = await asyncio.to_thread(
                            fetch_events, replace_old[0], self.monday, self.sunday
                        )
                    self._refresh_panes()
        finally:
            self.pending_writes -= 1

    def _set_status(self, text: str) -> None:
        self.status = text

    def _refresh_panes(self) -> None:
        try:
            self.query_one(BankPane).refresh()
            self.query_one(WeekPane).refresh()
        except Exception:
            pass

    def _update_status_bar(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
            base = self.title
            parts = [f"  {base}"]
            if self.status:
                parts.append(f"[italic]{self.status}[/italic]")
            if self.pending_writes:
                parts.append(f"[yellow]⟳ {self.pending_writes} pending[/yellow]")
            bar.update("  ·  ".join(parts))
        except Exception:
            pass

    def watch_status(self, _old: str, _new: str) -> None:
        self._update_status_bar()

    def watch_pending_writes(self, _old: int, _new: int) -> None:
        self._update_status_bar()

    def watch_offset_weeks(self, _old: int, _new: int) -> None:
        if hasattr(self, "blocks"):
            self._setup_state()
            self._refresh_panes()
            self._update_status_bar()
            self.load_events()

    def action_next_week(self) -> None:
        self.offset_weeks = self.offset_weeks + 1

    def action_prev_week(self) -> None:
        # Clamp at 0 — no scheduling in the past
        if self.offset_weeks > 0:
            self.offset_weeks = self.offset_weeks - 1

    def action_refresh(self) -> None:
        self._setup_state()
        self._refresh_panes()
        self._update_status_bar()
        self.load_events()

    def on_schedule_dismissed(self, result) -> None:
        # Optimistic update already happened in ScheduleScreen.action_commit;
        # background_place is doing the gcalcli write + reconcile. Just refresh panes.
        if result and result.get("ok"):
            self._refresh_panes()


def main() -> None:
    WeekApp().run()


if __name__ == "__main__":
    main()
