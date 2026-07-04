"""trips.py — `cl trips` — travel-aware scheduling.

While you're traveling, the daily routine mostly doesn't apply, so the default
is: **schedule nothing daily on a Travel day.** Each trip then carries an
*allowlist* — the specific daily blocks you actually want laid down for that
trip — set once and saved for the whole trip. `week.fill_day` consults this, so
"Schedule defaults" on a travel day only places what you've allowed.

A **trip** is a contiguous run of `Travel`-calendar dates sharing one event
title (the trip's name). Its allowlist lives in `~/kb/_state/travel-plans.json`
keyed by `START:name`, so it's git-synced + portable — the same trip resolves
the same on every device.

This is the *scheduling* side of travel. The per-block `travel: pause` flag is a
separate, *tracking* concern (don't count a habit as missed while away).

  cl trips show DAY [--json]        the trip covering DAY + its allowlist
  cl trips set  DAY --blocks a,b    replace the trip's allowlist (empty = none)
  cl trips set  DAY --all           allow every daily block
  cl trips set  DAY --none          pause everything (clear the allowlist)
"""

import argparse
import json
from datetime import date as _date, timedelta

import week

STATE = week.KB / "_state" / "travel-plans.json"
_SCAN = 60   # days to look on each side of a date when tracing a trip's extent


# ── trip detection ───────────────────────────────────────────────────────────

def trips_in(start, end):
    """Group Travel-calendar dates in [start, end] into trips. Each trip:
    {key, name, start, end, dates:set}. Contiguous same-title dates merge; a gap
    or a title change starts a new trip."""
    if isinstance(start, str):
        start = _date.fromisoformat(start)
    if isinstance(end, str):
        end = _date.fromisoformat(end)
    by_date = {}
    for d, _s, _e, t in week.fetch_events(week.TRAVEL_CALENDAR, start, end):
        by_date.setdefault(d, week.strip_display_suffix(t))
    trips, cur = [], None
    for d in sorted(by_date):
        dd = _date.fromisoformat(d)
        if cur and dd == _date.fromisoformat(cur["end"]) + timedelta(days=1) \
                and by_date[d] == cur["name"]:
            cur["end"] = d
            cur["dates"].add(d)
        else:
            if cur:
                trips.append(cur)
            cur = {"name": by_date[d], "start": d, "end": d, "dates": {d}}
    if cur:
        trips.append(cur)
    for t in trips:
        t["key"] = f"{t['start']}:{t['name']}"
    return trips


def trip_for(day):
    """The trip covering `day` (iso str), traced to its full extent, or None."""
    d = _date.fromisoformat(day)
    for t in trips_in(d - timedelta(days=_SCAN), d + timedelta(days=_SCAN)):
        if day in t["dates"]:
            return t
    return None


# ── allowlist store ──────────────────────────────────────────────────────────

def _load():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def allowlist(trip_key):
    """The set of daily block names allowed to schedule on this trip. A trip with
    no saved entry allows nothing (pause-all is the default)."""
    return set(_load().get(trip_key, []))


def set_allowlist(trip_key, names):
    data = _load()
    names = sorted(set(names))
    if names:
        data[trip_key] = names
    else:
        data.pop(trip_key, None)   # empty = pause everything → drop the key
    _save(data)
    return names


def is_allowed(day, block_name):
    """Should `block_name` schedule on `day`? Always yes when not traveling; on a
    trip, only if it's in that trip's allowlist."""
    t = trip_for(day)
    if not t:
        return True
    return block_name in allowlist(t["key"])


# ── the daily universe (what the modal offers) ───────────────────────────────

def daily_blocks():
    """Every active daily block name — the things a trip can allow."""
    return sorted(meta.get("block", "?")
                  for _slug, meta, st in week.load_blocks()
                  if st == "active" and meta.get("cadence") == "daily")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _resolve_day(day_arg):
    if day_arg in ("today", ""):
        return _date.today().isoformat()
    if day_arg == "tomorrow":
        return (_date.today() + timedelta(days=1)).isoformat()
    return day_arg   # assume YYYY-MM-DD


def _show(day, as_json):
    t = trip_for(day)
    allow = sorted(allowlist(t["key"])) if t else []
    out = {"day": day, "traveling": bool(t),
           "trip": ({"key": t["key"], "name": t["name"],
                     "start": t["start"], "end": t["end"]} if t else None),
           "allow": allow, "daily": daily_blocks()}
    if as_json:
        print(json.dumps(out))
        return
    if not t:
        print(f"{day}: not traveling")
        return
    print(f"{day}: {t['name']}  ({t['start']} → {t['end']})")
    print(f"  allowed: {', '.join(allow) or '(none — everything paused)'}")


def _set(day, args):
    t = trip_for(day)
    if not t:
        print(f"{day}: not a travel day — nothing to set")
        return
    if args.all:
        names = daily_blocks()
    elif args.none:
        names = []
    else:
        names = [b.strip() for b in (args.blocks or "").split(",") if b.strip()]
    saved = set_allowlist(t["key"], names)
    if args.json:
        print(json.dumps({"trip": t["key"], "allow": saved}))
    else:
        print(f"{t['name']}: allowing {', '.join(saved) or '(none)'}")


def main():
    p = argparse.ArgumentParser(prog="cl trips")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("show", help="the trip covering DAY + its allowlist")
    ps.add_argument("day", nargs="?", default="today")
    ps.add_argument("--json", action="store_true")

    pt = sub.add_parser("set", help="replace a trip's schedule allowlist")
    pt.add_argument("day", nargs="?", default="today")
    pt.add_argument("--blocks", help="comma-separated daily block names to allow")
    pt.add_argument("--all", action="store_true", help="allow every daily block")
    pt.add_argument("--none", action="store_true", help="pause everything")
    pt.add_argument("--json", action="store_true")

    args = p.parse_args()
    if args.cmd == "set":
        _set(_resolve_day(args.day), args)
    else:   # default + 'show'
        day = _resolve_day(getattr(args, "day", "today"))
        _show(day, getattr(args, "json", False))


if __name__ == "__main__":
    main()
