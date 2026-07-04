"""checkin.py — `cl checkin` — the daily review (evening counterpart to agenda).

Morning `cl agenda` sets the anchor; evening `cl checkin` closes the loop. It
pulls what gcal SAYS was scheduled today (the plan of record) and lets you verify
what actually got done vs missed. Verdicts persist in the pool DB — NOT the
`_logs` YAML — because tracking is the DB's job (pool.review_mark).

Each scheduled event is classified:

  - pool   — matches a `placement` row for today (a `cl pool schedule` one-off).
             done → complete_placement; missed → return_placement (back to pool).
  - block  — title resolves to a routine-block definition. The verdict is the raw
             material for habit streaks (pool.review_streak).
  - event  — a plain gcal event. Just gets a verdict.

  cl checkin                       launch (TODO tui) — for now prints the dump
  cl checkin --dump                print today's plan + verdicts, headless
  cl checkin --json                emit today's items + verdicts as JSON (Surface)
  cl checkin --date YYYY-MM-DD     operate on a specific date
  cl checkin --mark TITLE STATUS   record a verdict (done|missed); toggles off if
                                   re-applied; emits JSON result
"""

import argparse
import json
import sys
from datetime import date as _date, datetime

from rich.console import Console

import pool
from agenda import active_calendars, block_from_title, fetch_day_events
from week import DAYS, is_habit, load_blocks

console = Console()


# ── classify + reconcile ─────────────────────────────────────────────────────

def _classify(conn, date_str, ev):
    """Attach kind / system / placement / verdict to one gcal event row."""
    title = ev["title"]
    pl = pool.placement_for_title(conn, date_str, title)
    sys_slug, meta, inst = block_from_title(title)
    if pl:
        kind = "pool"
    elif meta and is_habit(meta):
        kind = "block"
    else:
        # plain gcal event OR a non-habit anchor (lunch/dinner) — neither reviewed
        kind = "event"
    mark = pool.get_review_mark(conn, date_str, title)
    return {
        **ev,
        "all_day": not ev["start"],
        "kind": kind,
        "markable": kind in ("pool", "block"),   # habit block or pool one-off
        "is_block": kind == "block",              # compat with the agenda template
        "system": sys_slug,
        "placement_id": pl["id"] if pl else None,
        "status": mark["status"] if mark else None,
    }


def _habit_blocks_for(date_str):
    """Today's applicable DAILY habit blocks, straight from the definitions — so
    the review is a real daily checklist, not just whatever landed on the calendar.
    (Weekly blocks stay placement-driven + live in the weekly review.)"""
    weekday = DAYS[_date.fromisoformat(date_str).weekday()]
    out = []
    for sys_slug, meta, sys_status in load_blocks():
        if sys_status != "active" or meta.get("cadence") != "daily" or not is_habit(meta):
            continue
        days = meta.get("days")
        if days and weekday not in days:          # block only runs on certain weekdays
            continue
        out.append((sys_slug, meta))
    return out


def rows_for(date_str, full=False):
    """The day's scheduled things, classified + overlaid with any recorded verdict.

    full=False (the review): the day's markable commitments — pool one-offs
    (→ done | back-to-pool) and habit blocks (→ streaks) — PLUS today's applicable
    daily habit blocks that weren't placed on the calendar, so the review is a true
    daily habit checklist (tick a habit → logs the streak, no scheduling needed).
    full=True (the agenda): every gcal row for schedule context, unchanged."""
    events = fetch_day_events(_date.fromisoformat(date_str))
    with pool.connect() as conn:
        rows = [_classify(conn, date_str, ev) for ev in events]
        if full:
            return rows
        rows = [r for r in rows if r["markable"]]
        placed = {r["title"] for r in rows if r["kind"] == "block"}
        for sys_slug, meta in _habit_blocks_for(date_str):
            name = meta["block"]
            if name in placed:                    # already on the calendar today
                continue
            mark = pool.get_review_mark(conn, date_str, name)
            start = meta.get("default_start") or ""
            rows.append({
                "title": name, "start": start, "end": "", "calendar": meta.get("calendar", ""),
                "all_day": not start, "kind": "block", "markable": True, "is_block": True,
                "system": sys_slug, "placement_id": None,
                "status": mark["status"] if mark else None,
            })
    rows.sort(key=lambda r: (bool(r["all_day"]), r["start"] or ""))
    return rows


def apply_mark(date_str, title, status):
    """Record / toggle a verdict, driving the pool lifecycle for pool placements.

    Returns the new status (or None if toggled off). Raises ValueError on a bad
    status.
    """
    if status not in pool.REVIEW_STATUSES:
        raise ValueError(f"bad status: {status} (use done|missed)")

    with pool.connect() as conn:
        pl = pool.placement_for_title(conn, date_str, title)
        existing = pool.get_review_mark(conn, date_str, title)
        sys_slug, meta, _inst = block_from_title(title)
    kind = "pool" if pl else ("block" if meta and is_habit(meta) else "event")
    pid = pl["id"] if pl else None

    # re-applying the same verdict clears it
    if existing and existing["status"] == status:
        pool.delete_review_mark(date_str, title)
        if pid:
            pool.reset_placement(pid)
        return None

    pool.upsert_review_mark(date_str, title, status, calendar=None, kind=kind,
                            placement_id=pid)
    if pid:
        if status in ("done", "partial"):
            pool.complete_placement(pid)   # you did it (or some of it) — resolved
        elif status == "missed":
            pool.return_placement(pid, reason="daily review")  # back to pool to reschedule
    return status


# ── surfaces ─────────────────────────────────────────────────────────────────

_MARK = {"done": "[green][x][/green]", "partial": "[yellow][~][/yellow]",
         "missed": "[red][/][/red]", None: "[ ]"}


def dump(target):
    date_str = target.strftime("%Y-%m-%d")
    weekday = DAYS[target.weekday()].title()
    console.print(f"\n  [bold]checkin[/bold]  [grey50]{weekday} {date_str}[/grey50]\n")
    rows = rows_for(date_str)
    if not rows:
        console.print("  [grey50]nothing was scheduled[/grey50]\n")
        return
    done = partial = missed = pending = 0
    for r in rows:
        m = _MARK.get(r["status"], _MARK[None])
        when = "all-day" if r["all_day"] else (r["start"] or "")
        tag = {"pool": "pool", "block": r["system"], "event": r["calendar"]}.get(r["kind"], "")
        title = r["title"]
        if r["status"] == "done":
            title = f"[strike grey50]{title}[/strike grey50]"
        elif r["status"] == "partial":
            title = f"[yellow]{title}[/yellow]"
        elif r["status"] == "missed":
            title = f"[red]{title}[/red]"
        console.print(f"  {m} [grey50]{when:9s}[/grey50] {title:38s} [grey42]{tag}[/grey42]")
        if r["status"] == "done":
            done += 1
        elif r["status"] == "partial":
            partial += 1
        elif r["status"] == "missed":
            missed += 1
        else:
            pending += 1
    console.print(
        f"\n  [bold]{done}[/bold] done  ·  [yellow]{partial}[/yellow] partial  ·  "
        f"[red]{missed}[/red] missed  ·  [grey50]{pending} pending[/grey50]\n"
    )


def emit_json(target, full=False):
    date_str = target.strftime("%Y-%m-%d")
    rows = rows_for(date_str, full=full)
    items = []
    with pool.connect() as conn:
        for r in rows:
            item = {
                "title": r["title"],
                "start": r["start"] or None,
                "end": r["end"] or None,
                "calendar": r["calendar"],
                "all_day": r["all_day"],
                "kind": r["kind"],
                "markable": r["markable"],
                "is_block": r["is_block"],
                "system": r["system"],
                "placement_id": r["placement_id"],
                "status": r["status"],
            }
            if r["kind"] == "block":
                item["streak"] = pool.review_streak(r["title"], conn=conn)
            items.append(item)
    # summary counts the commitments (markable rows) only — events don't count
    marks = [i for i in items if i["markable"]]
    done = sum(1 for i in marks if i["status"] == "done")
    partial = sum(1 for i in marks if i["status"] == "partial")
    missed = sum(1 for i in marks if i["status"] == "missed")
    print(json.dumps({
        "date": date_str,
        "weekday": DAYS[target.weekday()],
        "items": items,
        "summary": {"done": done, "partial": partial, "missed": missed,
                    "pending": len(marks) - done - partial - missed, "total": len(marks)},
    }))


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    parser = argparse.ArgumentParser(prog="cl checkin")
    parser.add_argument("--dump", action="store_true", help="print today's plan + verdicts")
    parser.add_argument("--json", action="store_true", help="emit JSON (for Surface)")
    parser.add_argument("--full", action="store_true",
                        help="with --json: include the whole day (all-day + gcal events), "
                             "not just markable commitments — for the agenda surface")
    parser.add_argument("--mark", nargs=2, metavar=("TITLE", "STATUS"),
                        help="record a verdict: done|partial|missed (toggles off if repeated)")
    parser.add_argument("--date", type=_parse_date, default=None,
                        help="operate on this date (YYYY-MM-DD); default today")
    args = parser.parse_args()

    target = args.date or _date.today()

    if args.mark:
        title, status = args.mark
        try:
            new = apply_mark(target.strftime("%Y-%m-%d"), title, status)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)
        print(json.dumps({"ok": True, "title": title, "status": new}))
        return

    if args.json:
        emit_json(target, full=args.full)
        return

    # no TUI yet — the dump is the headless surface; Surface's /review is the UI
    dump(target)


if __name__ == "__main__":
    main()
