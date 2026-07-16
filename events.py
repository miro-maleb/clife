"""events.py — `cl events` — Google Calendar event CRUD by id.

`cl week` and `cl pool` place events through gcalcli. gcalcli can't *edit by id*
(its `edit` is a search + interactive prompt), so this command talks to the
Calendar API directly via `gcal_api` — reusing gcalcli's existing OAuth token.
No new auth, no Google Cloud setup.

This is what lets Surface's month grid write back: `cl agenda --month` already
emits an `id` + `calendar` per event, which round-trips straight into here.

  cl events show ID [--calendar NAME] [--json]
  cl events new  --title T --calendar NAME --start WHEN [--duration 30m]
                 [--all-day] [--end DATE] [--json]
  cl events set  ID [--calendar NAME] [--title T] [--start WHEN]
                 [--duration 30m] [--all-day] [--json]
  cl events rm   ID [--calendar NAME] [--force] [--json]

--start WHEN takes three forms:
  YYYY-MM-DD HH:MM   timed event at that instant
  HH:MM              (set only) keep the date, change the time
  YYYY-MM-DD         all-day on new; on set, move the day and keep the time

No `mon|tue|…` weekday forms, deliberately — unlike `cl week`, ids are absolute
and cross-month, so a bare weekday is ambiguous outside a week view.

--calendar on `set` MOVES the event. The gcal event id survives a move (verified),
so Surface's grid ids stay valid.

Recurring events: v1 edits a single INSTANCE — which is what clicking one chip in
a month grid means. Series-wide edits need a this/following/all prompt and are
deliberately out of scope; `show` reports `recurring: true` so a UI can label it.
"""

import argparse
import json
import re
import sys
from datetime import date as _date, datetime, timedelta

from rich.console import Console

import gcal_api
import week
from gcal_api import GcalError
from paths import DEFAULT_CALENDAR

console = Console()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]([01]?\d|2[0-3]:[0-5]\d|\d{1,2}:[0-5]\d)$")


# ── --start parsing ──────────────────────────────────────────────────────────

def parse_when(s):
    """'YYYY-MM-DD HH:MM' | 'HH:MM' | 'YYYY-MM-DD' → (date|None, time|None).

    Exactly one of the two may be None; both None is never returned (we raise).
    """
    s = (s or "").strip()
    m = DATETIME_RE.match(s)
    if m:
        return m.group(1), _pad(m.group(2))
    if DATE_RE.match(s):
        return s, None
    if TIME_RE.match(s):
        return None, _pad(s)
    raise GcalError("bad-start",
                    f"--start '{s}' unparseable — use 'YYYY-MM-DD HH:MM', 'HH:MM', or 'YYYY-MM-DD'")


def _pad(t):
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def _duration_min(s):
    n = week.parse_duration_minutes(s)
    if n is None:
        raise GcalError("bad-duration", f"--duration '{s}' unparseable (use 30m / 90m / 2h)")
    return n


def _timed(date_s, time_s, tz):
    """Build a start/end pair of API dicts for a timed event. `date: None`
    explicitly clears the sibling key — a patch will NOT drop it on its own."""
    return {"dateTime": f"{date_s}T{time_s}:00", "timeZone": tz, "date": None}


def _allday(date_s):
    return {"date": date_s, "dateTime": None, "timeZone": None}


def _plus(date_s, time_s, minutes):
    dt = datetime.fromisoformat(f"{date_s}T{time_s}:00") + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_show(args):
    name, _cal_id, res = gcal_api.find_event(args.id, args.calendar)
    ev = gcal_api.event_dict(res, name)
    if args.json:
        print(json.dumps({"ok": True, "event": ev}))
        return
    console.print(f"[bold]{ev['title'] or '(untitled)'}[/bold]  ({ev['calendar']})")
    when = ev["start_date"] if ev["all_day"] else f"{ev['start_date']} {ev['start_time']}"
    if ev["all_day"] and ev["end_date"] != ev["start_date"]:
        when += f" → {ev['end_date']}"
    elif not ev["all_day"] and ev["end_time"]:
        when += f" → {ev['end_time']}"
    console.print(f"  {'when':10} {when}{'  (all-day)' if ev['all_day'] else ''}")
    console.print(f"  {'id':10} {ev['id']}")
    console.print(f"  {'status':10} {ev['status']}")
    if ev["recurring"]:
        console.print("  [yellow]↻[/yellow] one instance of a repeating event — "
                      "edits apply to this occurrence only")


def cmd_new(args):
    cal = args.calendar or DEFAULT_CALENDAR
    cal_id, tz = gcal_api.resolve_calendar(cal, writable=True)
    d, t = parse_when(args.start)
    if not d:
        raise GcalError("bad-start", "--start on `new` needs a date (YYYY-MM-DD [HH:MM])")

    body = {"summary": args.title, "reminders": {"useDefault": True}}
    if args.all_day or not t:
        end_incl = args.end or d
        if not DATE_RE.match(end_incl):
            raise GcalError("bad-end", f"--end '{end_incl}' must be YYYY-MM-DD")
        if end_incl < d:
            raise GcalError("bad-end", f"--end {end_incl} is before --start {d}")
        # --end is inclusive for the user; gcal wants it exclusive.
        excl = (_date.fromisoformat(end_incl) + timedelta(days=1)).isoformat()
        body["start"], body["end"] = {"date": d}, {"date": excl}
    else:
        mins = _duration_min(args.duration) if args.duration else 30
        ed, et = _plus(d, t, mins)
        body["start"] = {"dateTime": f"{d}T{t}:00", "timeZone": tz}
        body["end"] = {"dateTime": f"{ed}T{et}:00", "timeZone": tz}

    res = gcal_api._wrap(lambda: gcal_api.service().events().insert(
        calendarId=cal_id, body=body, sendUpdates="none").execute())
    ev = gcal_api.event_dict(res, cal)
    _ok({"created": ev["id"], "event": ev},
        f"created {ev['title']} → {cal} ({ev['start_date']}"
        f"{'' if ev['all_day'] else ' ' + (ev['start_time'] or '')})", args.json)


def cmd_set(args):
    name, cal_id, res = gcal_api.find_event(args.id, args.calendar)
    cur = gcal_api.event_dict(res, name)
    moved = False

    # 1. Move first (if asked), then patch fields on the destination.
    if args.to_calendar and args.to_calendar != name:
        if cur["recurring"]:
            raise GcalError("recurring-move",
                            "can't move a single instance of a repeating event to another "
                            "calendar — gcal forbids it; move the whole series in gcal instead")
        dest_id, _tz = gcal_api.resolve_calendar(args.to_calendar, writable=True)
        gcal_api.resolve_calendar(name, writable=True)   # source must be writable too
        res = gcal_api._wrap(lambda: gcal_api.service().events().move(
            calendarId=cal_id, eventId=args.id, destination=dest_id,
            sendUpdates="none").execute())
        name, cal_id, moved = args.to_calendar, dest_id, True
    else:
        gcal_api.resolve_calendar(name, writable=True)

    _cid, tz = gcal_api.resolve_calendar(name)
    patch = {}
    if args.title is not None:
        patch["summary"] = args.title

    # 2. Resolve the new when against the event's CURRENT value.
    d, t = (None, None)
    if args.start:
        d, t = parse_when(args.start)
    want_allday = args.all_day or (cur["all_day"] and not t and not args.duration)
    new_date = d or cur["start_date"]
    new_time = t or cur["start_time"]

    if args.start or args.duration or args.all_day:
        if want_allday:
            end_incl = args.end or (new_date if d or args.all_day else cur["end_date"])
            if not DATE_RE.match(end_incl or ""):
                raise GcalError("bad-end", f"--end '{end_incl}' must be YYYY-MM-DD")
            if end_incl < new_date:
                raise GcalError("bad-end", f"--end {end_incl} is before start {new_date}")
            excl = (_date.fromisoformat(end_incl) + timedelta(days=1)).isoformat()
            patch["start"], patch["end"] = _allday(new_date), _allday(excl)
        else:
            if not new_time:
                raise GcalError("bad-start",
                                "this is an all-day event — pass --start with a time "
                                "(HH:MM or 'YYYY-MM-DD HH:MM') to make it timed")
            mins = _duration_min(args.duration) if args.duration else _cur_minutes(cur)
            ed, et = _plus(new_date, new_time, mins)
            patch["start"] = _timed(new_date, new_time, tz)
            patch["end"] = _timed(ed, et, tz)

    if not patch and not moved:
        _fail("nothing to change (pass --title, --start, --duration, --all-day, or --calendar)",
              args.json)
        return

    if patch:
        res = gcal_api._wrap(lambda: gcal_api.service().events().patch(
            calendarId=cal_id, eventId=args.id, body=patch, sendUpdates="none").execute())
    ev = gcal_api.event_dict(res, name)
    note = f" (moved → {name})" if moved else ""
    _ok({"updated": ev["id"], "moved": moved, "event": ev},
        f"updated {ev['title']}{note}", args.json)


def _cur_minutes(cur):
    """The event's current length, so `set --start` retimes without resizing."""
    if cur["all_day"] or not cur["start_time"] or not cur["end_time"]:
        return 30
    s = datetime.fromisoformat(f"{cur['start_date']}T{cur['start_time']}")
    e = datetime.fromisoformat(f"{cur['end_date']}T{cur['end_time']}")
    return max(int((e - s).total_seconds() // 60), 1) if e > s else 30


def cmd_rm(args):
    name, cal_id, res = gcal_api.find_event(args.id, args.calendar)
    gcal_api.resolve_calendar(name, writable=True)
    ev = gcal_api.event_dict(res, name)

    # Confirm unless --force; --json is the Surface path and never prompts.
    if not args.force and not args.json:
        when = ev["start_date"] if ev["all_day"] else f"{ev['start_date']} {ev['start_time']}"
        console.print(f"[yellow]delete[/yellow] '{ev['title']}' — {when} ({name}) ?")
        if ev["recurring"]:
            console.print("  [yellow]↻[/yellow] this cancels only this occurrence")
        if input("  type 'yes' to confirm: ").strip().lower() != "yes":
            console.print("  aborted.")
            return
    gcal_api._wrap(lambda: gcal_api.service().events().delete(
        calendarId=cal_id, eventId=args.id, sendUpdates="none").execute())
    _ok({"deleted": ev["id"], "title": ev["title"], "calendar": name},
        f"deleted {ev['title']} ({name})", args.json)


# ── output helpers ───────────────────────────────────────────────────────────

def _ok(payload, msg, as_json):
    if as_json:
        print(json.dumps({"ok": True, **payload}))
    else:
        console.print(f"[green]✓[/green] {msg}")


def _fail(msg, as_json, code="error"):
    if as_json:
        print(json.dumps({"ok": False, "error": msg, "code": code}))
    else:
        console.print(f"[red]✗[/red] {msg}")
    # exit 0 under --json so Surface's _cl_json parses the envelope instead of
    # choking on a non-zero rc.
    sys.exit(0 if as_json else 1)


# ── entry ────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="cl events",
                                description="Google Calendar event CRUD by id")
    sub = p.add_subparsers(dest="cmd")

    shp = sub.add_parser("show")
    shp.add_argument("id")
    shp.add_argument("--calendar", help="the event's calendar (skips the scan — always pass it)")
    shp.add_argument("--json", action="store_true")

    np = sub.add_parser("new")
    np.add_argument("--title", required=True)
    np.add_argument("--calendar", help=f"target calendar (default {DEFAULT_CALENDAR})")
    np.add_argument("--start", required=True, help="'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'")
    g = np.add_mutually_exclusive_group()
    g.add_argument("--duration", help="30m | 90m | 2h (default 30m)")
    g.add_argument("--all-day", action="store_true")
    np.add_argument("--end", help="all-day last day, INCLUSIVE (YYYY-MM-DD)")
    np.add_argument("--json", action="store_true")

    stp = sub.add_parser("set")
    stp.add_argument("id")
    stp.add_argument("--calendar", help="the event's CURRENT calendar (skips the scan)")
    stp.add_argument("--to-calendar", help="move the event to this calendar (id is preserved)")
    stp.add_argument("--title")
    stp.add_argument("--start", help="'YYYY-MM-DD HH:MM' | 'HH:MM' | 'YYYY-MM-DD'")
    g2 = stp.add_mutually_exclusive_group()
    g2.add_argument("--duration", help="30m | 90m | 2h")
    g2.add_argument("--all-day", action="store_true")
    stp.add_argument("--end", help="all-day last day, INCLUSIVE (YYYY-MM-DD)")
    stp.add_argument("--json", action="store_true")

    rp = sub.add_parser("rm")
    rp.add_argument("id")
    rp.add_argument("--calendar", help="the event's calendar (skips the scan)")
    rp.add_argument("--force", action="store_true")
    rp.add_argument("--json", action="store_true")
    return p


def main():
    p = build_parser()
    args = p.parse_args(sys.argv[1:])
    if not args.cmd:
        p.print_help()
        return
    dispatch = {"show": cmd_show, "new": cmd_new, "set": cmd_set, "rm": cmd_rm}
    try:
        dispatch[args.cmd](args)
    except GcalError as e:
        _fail(e.msg, args.json, e.code)


if __name__ == "__main__":
    main()
