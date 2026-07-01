"""agenda.py — `cl agenda` — daily anchor surface.

Today's gcal events + system-block status. Mark blocks done / partial / skipped;
log entries land in ~/kb/systems/_logs/<system>/<block>.yaml (per project schema).

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

KB = Path.home() / "kb"
LOGS = KB / "systems" / "_logs"
STATUSES = ("done", "partial", "skip")


# ── Per-block log file ──────────────────────────────────────────────────────


def log_path(system_slug: str, block_name: str) -> Path:
    return LOGS / system_slug / f"{block_name}.yaml"


def read_log(system_slug: str, block_name: str) -> list[dict]:
    """Parse the per-block log YAML. Same shape as cl week's load_skips."""
    path = log_path(system_slug, block_name)
    if not path.exists():
        return []
    entries = []
    current = None
    for line in path.read_text().splitlines():
        if line.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            kv = line[2:]
            if ":" in kv:
                k, _, v = kv.partition(":")
                current[k.strip()] = v.strip().strip('"')
        elif line.startswith("  ") and ":" in line and current is not None:
            k, _, v = line.strip().partition(":")
            current[k.strip()] = v.strip().strip('"')
    if current is not None:
        entries.append(current)
    return entries


def _serialize(entries: list[dict]) -> str:
    out = []
    for e in entries:
        out.append(f"- date: {e['date']}")
        out.append(f"  status: {e['status']}")
        note = e.get("note", "")
        safe = note.replace('"', "'")
        out.append(f'  note: "{safe}"')
    return "\n".join(out) + "\n" if out else ""


def append_log(system_slug: str, block_name: str, status: str,
               date_str: str, note: str = "") -> None:
    """Append one entry. Status must be one of done | partial | skip."""
    if status not in STATUSES:
        raise ValueError(f"bad status: {status}")
    path = log_path(system_slug, block_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = note.replace('"', "'")
    entry = (
        f"- date: {date_str}\n"
        f"  status: {status}\n"
        f'  note: "{safe}"\n'
    )
    with path.open("a") as f:
        f.write(entry)


def remove_last_entry(system_slug: str, block_name: str, date_str: str) -> bool:
    """Remove the most recent log entry on date_str. Returns True if removed.

    Used when toggling a status off (e.g. user marked done by mistake).
    """
    path = log_path(system_slug, block_name)
    if not path.exists():
        return False
    entries = read_log(system_slug, block_name)
    target_idx = None
    for i, e in enumerate(entries):
        if e.get("date") == date_str:
            target_idx = i  # keep last match
    if target_idx is None:
        return False
    entries.pop(target_idx)
    if entries:
        path.write_text(_serialize(entries))
    else:
        path.unlink()
    return True


def update_last_note(system_slug: str, block_name: str, date_str: str,
                     note: str) -> bool:
    """Edit the note on the most recent entry for this block on date_str.

    Returns True if updated. Used by the comment key (`c`) — only meaningful
    when there's already a status entry on that date.
    """
    path = log_path(system_slug, block_name)
    if not path.exists():
        return False
    entries = read_log(system_slug, block_name)
    target_idx = None
    for i, e in enumerate(entries):
        if e.get("date") == date_str:
            target_idx = i
    if target_idx is None:
        return False
    entries[target_idx]["note"] = note
    path.write_text(_serialize(entries))
    return True


def status_on(system_slug: str, block_name: str, date_str: str) -> dict | None:
    """Most recent log entry on date_str for this block, or None."""
    entries = read_log(system_slug, block_name)
    matches = [e for e in entries if e.get("date") == date_str]
    return matches[-1] if matches else None


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
    "skip":    "[—]",
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

    done = partial = skipped = pending = 0
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
        elif st == "skip":
            title_md = f"[{EMPTY}]{title}[/{EMPTY}]"
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
            elif st == "skip":
                skipped += 1
            else:
                pending += 1

    if not now_marked:
        console.print(_now_line(now_str))

    total_blocks = done + partial + skipped + pending
    console.print()
    console.print(
        f"  [bold {ACCENT}]{done}/{total_blocks}[/bold {ACCENT}] [{BODY}]done[/{BODY}]"
        f"  [{MUTED}]·  {partial} partial  ·  {skipped} skipped  ·  {pending} pending[/{MUTED}]\n"
    )


# ── Clean HTML fragment (web / SilverBullet widget surface) ─────────────────


_HTML_ICON = {
    "done":    ("✓", "#3ba55d"),
    "partial": ("◐", "#d8a13a"),
    "skip":    ("–", "#8a8f98"),
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

    done = partial = skipped = pending = 0
    for r in timed:
        st = (r["status"] or {}).get("status") if r["status"] else None
        is_block = r["is_block"]
        time_col = f'{r["start"]}–{r["end"]}' if r["end"] else r["start"]

        if is_block:
            icon, color = _HTML_ICON.get(st, _HTML_ICON[None])
            if st == "done":   done += 1
            elif st == "partial": partial += 1
            elif st == "skip": skipped += 1
            else: pending += 1
            title_style = "color:#e6e8eb"
            if st in ("done", "skip"):
                title_style = "color:#6f7682;text-decoration:line-through"
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

    total = done + partial + skipped + pending
    out.append(
        f'<div style="margin-top:12px;font-size:13px;color:#8a8f98">'
        f'<b style="color:#e6e8eb">{done}/{total}</b> done · {partial} partial · '
        f'{skipped} skipped · {pending} pending</div>'
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
                        help="toggle a block's status (done|partial|skip) for the date; "
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
            cmd = ["gcalcli", "agenda", "--calendar", cal,
                   grid_start.strftime("%Y-%m-%d"),
                   (grid_end + timedelta(days=1)).strftime("%Y-%m-%d"), "--tsv"]
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
