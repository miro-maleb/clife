"""agenda.py — `cl agenda` — daily anchor surface.

Today's gcal events + system-block status. Mark blocks done / partial / missed;
verdicts land in the pool DB (review_mark) — the same store the daily review
(`cl checkin`) and habit dashboard (`cl habits`) read.

  cl agenda                       launch TUI
  cl agenda --dump                print today's items + status, headless
  cl agenda --date YYYY-MM-DD     operate on a specific date (combine with --dump)

Title contract (shared with cl week): gcal event title == block name (with
`#N` suffix when a block has `instances > 1`). Block names are globally unique
across systems, so block_name → (system, meta) is unambiguous.
"""

import argparse
import re
import subprocess
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

from rich.console import Console

import pool
from tui_common import ACCENT, BODY, MUTED, EMPTY
from week import (
    DAYS,
    delete_event_by_title,
    fetch_events,
    find_block,
    list_calendars,
    load_blocks,
    parse_duration_minutes,
)

console = Console()

from paths import KB, gcalcli
STATUSES = ("done", "partial", "missed")


# ── Per-block status (pool DB, review_mark) ─────────────────────────────────
# Keyed by block name — the same store the daily review + habit dashboard read.
# These keep the old function names/signatures so agenda_tui stays untouched; the
# retired `_logs/*.yaml` artifact is gone. `system_slug` is accepted but ignored
# (block names are globally unique, which is the DB key).


def status_on(system_slug: str, block_name: str, date_str: str) -> dict | None:
    """This block's verdict on date_str as {status, note}, or None."""
    with pool.connect() as conn:
        row = pool.get_review_mark(conn, date_str, block_name)
    if not row:
        return None
    return {"status": row["status"], "note": row["note"] or ""}


def append_log(system_slug: str, block_name: str, status: str,
               date_str: str, note: str = "") -> None:
    """Record a verdict (done | partial | missed). 'skip' is a scheduling action
    — the event gets deleted, so it's not a habit verdict and isn't stored."""
    if status == "skip":
        return
    if status not in STATUSES:
        raise ValueError(f"bad status: {status}")
    pool.upsert_review_mark(date_str, block_name, status, kind="block",
                            note=note or None)


def remove_last_entry(system_slug: str, block_name: str, date_str: str) -> bool:
    """Clear this block's verdict on date_str (toggle-off / replace)."""
    pool.delete_review_mark(date_str, block_name)
    return True


def update_last_note(system_slug: str, block_name: str, date_str: str,
                     note: str) -> bool:
    """Edit the comment on an existing verdict (agenda's `c` key)."""
    return pool.set_review_note(date_str, block_name, note)


# ── Title → block resolution ────────────────────────────────────────────────


def block_from_title(title: str):
    """If title matches a known block (with optional ` #N` suffix), return
    (system_slug, meta, instance_or_none). Else (None, None, None).
    """
    m = re.match(r"^(.*?)(?:\s*#(\d+))?$", title.strip())
    if not m:
        return None, None, None
    base = m.group(1).strip()
    instance = int(m.group(2)) if m.group(2) else None
    sys_slug, meta = find_block(base)
    if not meta:
        return None, None, None
    return sys_slug, meta, instance


# ── Today's events (across all calendars — no exclusions) ───────────────────


def active_calendars() -> list[str]:
    """Your OWN calendars only (owner/writer access). Excludes calendars shared
    *from* other people (reader) — e.g. sydneyslavitt@gmail.com, Holidays — which
    you don't want on your surface. Matches the "My calendars" set in gcal, and
    keeps your own "Sydney" calendar (owner) visible."""
    return list_calendars(access=("owner", "writer"))


def fetch_day_events(target_date: _date) -> list[dict]:
    """Fetch all events on target_date from every accessible calendar.

    Returns rows ordered by (start_time, title). All-day events sort first
    (empty start string).
    """
    cals = active_calendars()
    target = target_date.strftime("%Y-%m-%d")
    out = []
    for cal in cals:
        for d, s, e, t in fetch_events(cal, target_date, target_date):
            if d != target:
                continue
            out.append({
                "date": d,
                "start": s,
                "end": e,
                "title": t,
                "calendar": cal,
            })
    out.sort(key=lambda r: (r["start"] or "", r["title"]))
    return out


def annotate_with_status(events: list[dict], date_str: str) -> list[dict]:
    """Attach system_slug / meta / instance / status to each event row.

    For non-block events, system_slug is None and status is None.
    """
    annotated = []
    for ev in events:
        sys_slug, meta, inst = block_from_title(ev["title"])
        st = status_on(sys_slug, meta["block"], date_str) if meta else None
        annotated.append({
            **ev,
            "system": sys_slug,
            "meta": meta,
            "instance": inst,
            "status": st,
        })
    return annotated


# ── Headless dump ────────────────────────────────────────────────────────────


_MARKERS = {
    None:      "[ ]",
    "done":    "[x]",
    "partial": "[~]",
    "missed":  "[/]",
}


def _now_line(now_str: str) -> str:
    return f"  [bold {ACCENT}]──── now {now_str} {'─' * 22}[/bold {ACCENT}]"


def dump(target_date: _date) -> None:
    weekday = DAYS[target_date.weekday()]
    console.print(
        f"\n  [bold {ACCENT}]agenda[/bold {ACCENT}]  "
        f"[{MUTED}]{weekday.title()} {target_date}[/{MUTED}]\n"
    )

    events = fetch_day_events(target_date)
    rows = annotate_with_status(events, target_date.strftime("%Y-%m-%d"))

    if not rows:
        console.print(f"  [{EMPTY}]nothing on the calendar today[/{EMPTY}]\n")
        return

    is_today = target_date == _date.today()
    now_str = datetime.now().strftime("%H:%M") if is_today else None
    now_marked = not is_today

    all_day = [r for r in rows if not r["start"]]
    timed = [r for r in rows if r["start"]]

    if all_day:
        for r in all_day:
            console.print(
                f"  [{MUTED}]all-day[/{MUTED}]    [{BODY}]{r['title']}[/{BODY}]  "
                f"[{EMPTY}]({r['calendar']})[/{EMPTY}]"
            )
        console.print()

    done = partial = missed = pending = 0
    for r in timed:
        if not now_marked and r["start"] >= now_str:
            console.print(_now_line(now_str))
            now_marked = True
        st = (r["status"] or {}).get("status") if r["status"] else None
        marker = _MARKERS.get(st, _MARKERS[None])
        if r["meta"]:
            tag = f"[{MUTED}]({r['system']})[/{MUTED}]"
            color = BODY
        else:
            marker = "   "
            tag = f"[{EMPTY}](gcal — {r['calendar']})[/{EMPTY}]"
            color = MUTED
        title = r["title"]
        if st == "done":
            title_md = f"[strike {EMPTY}]{title}[/strike {EMPTY}]"
        elif st == "missed":
            title_md = f"[{EMPTY}]{title}[/{EMPTY}] [{EMPTY}](missed)[/{EMPTY}]"
        elif st == "partial":
            title_md = f"[strike {MUTED}]{title}[/strike {MUTED}] [{EMPTY}](partial)[/{EMPTY}]"
        else:
            title_md = f"[{color}]{title}[/{color}]"

        time_col = f"{r['start']}–{r['end']}" if r["end"] else r["start"]
        console.print(f"  {marker} [{MUTED}]{time_col:13s}[/{MUTED}] {title_md:40s} {tag}")

        note = (r["status"] or {}).get("note") if r["status"] else ""
        if note:
            console.print(f"          [{MUTED}]note: {note}[/{MUTED}]")

        if r["meta"]:
            if st == "done":
                done += 1
            elif st == "partial":
                partial += 1
            elif st == "missed":
                missed += 1
            else:
                pending += 1

    if not now_marked:
        console.print(_now_line(now_str))

    total_blocks = done + partial + missed + pending
    console.print()
    console.print(
        f"  [bold {ACCENT}]{done}/{total_blocks}[/bold {ACCENT}] [{BODY}]done[/{BODY}]"
        f"  [{MUTED}]·  {partial} partial  ·  {missed} missed  ·  {pending} pending[/{MUTED}]\n"
    )


# ── Clean HTML fragment (web / SilverBullet widget surface) ─────────────────


_HTML_ICON = {
    "done":    ("✓", "#3ba55d"),
    "partial": ("◐", "#d8a13a"),
    "missed":  ("✗", "#d9503c"),
    None:      ("○", "#6f7682"),
}


def render_html(target_date: _date) -> str:
    date_str = target_date.strftime("%Y-%m-%d")
    weekday = DAYS[target_date.weekday()].title()
    rows = annotate_with_status(fetch_day_events(target_date), date_str)

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    css = (
        "font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:640px;"
    )
    out = [f'<div style="{css}">']
    out.append(
        f'<div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#8a8f98;margin:0 0 10px">{weekday} · {date_str}</div>'
    )

    if not rows:
        out.append('<div style="color:#8a8f98;padding:8px 0">nothing on the calendar today</div></div>')
        return "".join(out)

    all_day = [r for r in rows if not r["start"]]
    timed = [r for r in rows if r["start"]]

    for r in all_day:
        out.append(
            f'<div style="display:flex;gap:10px;align-items:center;padding:5px 0;color:#6f7682">'
            f'<span style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
            f'min-width:58px">all-day</span>'
            f'<span style="color:#cfd3da">{esc(r["title"])}</span>'
            f'<span style="font-size:12px;color:#6f7682">({esc(r["calendar"])})</span></div>'
        )

    done = partial = missed = pending = 0
    for r in timed:
        st = (r["status"] or {}).get("status") if r["status"] else None
        is_block = r["is_block"]
        time_col = f'{r["start"]}–{r["end"]}' if r["end"] else r["start"]

        if is_block:
            icon, color = _HTML_ICON.get(st, _HTML_ICON[None])
            if st == "done":   done += 1
            elif st == "partial": partial += 1
            elif st == "missed": missed += 1
            else: pending += 1
            title_style = "color:#e6e8eb"
            if st == "done":
                title_style = "color:#6f7682;text-decoration:line-through"
            elif st == "missed":
                title_style = "color:#8a8f98"
            elif st == "partial":
                title_style = "color:#b9bdc6;text-decoration:line-through"
            tag = f'<span style="font-size:12px;color:#6f7682">· {esc(r["system"])}</span>'
        else:
            icon, color = "", ""
            title_style = "color:#8a8f98"
            tag = f'<span style="font-size:12px;color:#5b616b">· {esc(r["calendar"])}</span>'

        bullet = (f'<span style="color:{color};min-width:16px;text-align:center">{icon}</span>'
                  if is_block else '<span style="min-width:16px"></span>')
        out.append(
            f'<div style="display:flex;gap:10px;align-items:baseline;padding:6px 0;'
            f'border-bottom:1px solid #23262c">'
            f'{bullet}'
            f'<span style="min-width:96px;color:#7d828c;font-variant-numeric:tabular-nums;'
            f'font-size:13px">{time_col}</span>'
            f'<span style="flex:1;{title_style}">{esc(r["title"])}</span>'
            f'{tag}</div>'
        )
        note = (r["status"] or {}).get("note") if r["status"] else None
        if note:
            out.append(f'<div style="margin:0 0 4px 122px;color:#6f7682;font-size:12px">{esc(note)}</div>')

    total = done + partial + missed + pending
    out.append(
        f'<div style="margin-top:12px;font-size:13px;color:#8a8f98">'
        f'<b style="color:#e6e8eb">{done}/{total}</b> done · {partial} partial · '
        f'{missed} missed · {pending} pending</div>'
    )
    out.append("</div>")
    return "".join(out)


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_date_arg(s: str) -> _date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cl agenda")
    parser.add_argument("--dump", action="store_true",
                        help="print today's items + status, headless")
    parser.add_argument("--json", action="store_true",
                        help="emit today's items + status as JSON (for web/widget surfaces)")
    parser.add_argument("--html", action="store_true",
                        help="emit today as a clean styled HTML fragment (for web/widget surfaces)")
    parser.add_argument("--month", default=None, metavar="YYYY-MM",
                        help="emit a month's events grouped by day as JSON (one range fetch)")
    parser.add_argument("--mark", nargs=2, metavar=("BLOCK", "STATUS"),
                        help="toggle a block's status (done|partial|missed) for the date; "
                             "emits JSON result (for web/widget surfaces)")
    parser.add_argument("--watch", action="store_true",
                        help="loop --dump, refresh every 60s (legacy non-interactive pane)")
    parser.add_argument("--pane", action="store_true",
                        help="interactive TUI for the dashboard right pane; "
                             "`open` dispatches into the center pane")
    parser.add_argument("--date", type=parse_date_arg, default=None,
                        help="operate on this date (YYYY-MM-DD); default today")
    args = parser.parse_args()

    if args.watch:
        # gcal fetch is slow (~15s). Render to a buffer first, then clear+print
        # atomically so the old content stays visible during the fetch.
        import time
        while True:
            with console.capture() as cap:
                dump(args.date or datetime.now().date())
            console.clear()
            sys.stdout.write(cap.get())
            sys.stdout.flush()
            time.sleep(300)

    target = args.date or datetime.now().date()
    date_str = target.strftime("%Y-%m-%d")

    if args.mark:
        import json as _json
        block_title, status = args.mark
        if status not in STATUSES:
            print(_json.dumps({"ok": False, "error": f"bad status: {status}"}))
            sys.exit(1)
        sys_slug, meta, _inst = block_from_title(block_title)
        if not meta:
            print(_json.dumps({"ok": False, "error": f"unknown block: {block_title}"}))
            sys.exit(1)
        cur = status_on(sys_slug, meta["block"], date_str)
        if cur and cur.get("status") == status:
            remove_last_entry(sys_slug, meta["block"], date_str)  # toggle off
            new = None
        else:
            if cur:
                remove_last_entry(sys_slug, meta["block"], date_str)  # replace
            append_log(sys_slug, meta["block"], status, date_str)
            new = status
        print(_json.dumps({"ok": True, "block": meta["block"], "status": new}))
        return

    if args.json:
        import json as _json
        date_str = target.strftime("%Y-%m-%d")
        rows = annotate_with_status(fetch_day_events(target), date_str)
        items = []
        for r in rows:
            st = (r["status"] or {}).get("status") if r["status"] else None
            items.append({
                "title": r["title"],
                "start": r["start"] or None,
                "end": r["end"] or None,
                "calendar": r["calendar"],
                "all_day": not r["start"],
                "is_block": bool(r["meta"]),
                "system": r["system"],
                "status": st,
                "note": (r["status"] or {}).get("note") if r["status"] else None,
            })
        print(_json.dumps({
            "date": date_str,
            "weekday": DAYS[target.weekday()],
            "items": items,
        }))
        return

    if args.html:
        print(render_html(target))
        return

    if args.month:
        import json as _json
        import subprocess as _sp
        from calendar import monthrange
        y, m = (int(x) for x in args.month.split("-"))
        first = _date(y, m, 1)
        last = _date(y, m, monthrange(y, m)[1])
        # extend to the full Sunday–Saturday grid so spillover days show events too
        grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
        grid_end = last + timedelta(days=(5 - last.weekday() + 7) % 7)
        # Emit raw event *spans* (start_date..end_date inclusive), not per-day rows,
        # so the surface can render multi-day events as continuous bars with lanes.
        events: list[dict] = []
        for cal in active_calendars():
            cmd = gcalcli("agenda", "--calendar", cal,
                   grid_start.strftime("%Y-%m-%d"),
                   (grid_end + timedelta(days=1)).strftime("%Y-%m-%d"), "--tsv")
            try:
                r = _sp.run(cmd, capture_output=True, text=True, check=False, timeout=60)
            except (FileNotFoundError, _sp.TimeoutExpired):
                continue
            if r.returncode != 0:
                continue
            for line in r.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 5 or parts[0] == "start_date":
                    continue
                sd, st, ed, _et, title = parts[0], parts[1], parts[2], parts[3], parts[4]
                try:
                    sdate = _date.fromisoformat(sd)
                except ValueError:
                    continue
                all_day = not st
                if all_day:
                    # gcal all-day end_date is exclusive → subtract a day for inclusive
                    try:
                        edate = _date.fromisoformat(ed) - timedelta(days=1)
                    except ValueError:
                        edate = sdate
                    if edate < sdate:
                        edate = sdate
                else:
                    edate = sdate  # timed events render as single-day chips
                if edate < grid_start or sdate > grid_end:
                    continue
                events.append({
                    "calendar": cal, "title": title,
                    "start_date": max(sdate, grid_start).isoformat(),
                    "end_date": min(edate, grid_end).isoformat(),
                    "start_time": st or None,
                    "all_day": all_day,
                })
        print(_json.dumps({
            "month": args.month,
            "grid_start": grid_start.isoformat(),
            "grid_end": grid_end.isoformat(),
            "events": events,
        }))
        return

    if args.dump:
        dump(target)
        return

    from agenda_tui import AgendaApp
    app = AgendaApp(start_date=target, pane_mode=args.pane)
    app.run()


if __name__ == "__main__":
    main()
