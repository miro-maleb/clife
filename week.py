"""week.py — `cl week` — Monday placement assistant.

Reads system blocks for the current week, fetches this week's gcal events from
each relevant calendar, matches by title equality, and reports what's already
scheduled vs. what still needs to be placed.

  cl week                              launch TUI (not yet built)
  cl week --dump                       print this week's bank, headless
  cl week --place BLOCK DAY TIME       schedule a block onto gcal
                                       DAY is mon|tue|…|sun or YYYY-MM-DD
                                       TIME is HH:MM (24h)
  cl week --skip BLOCK DAY [REASON…]   log a skip (and delete matching gcal
                                       event if any). Reason is free text.
  cl week --next                       operate on next week instead of current
                                       (combine with --dump / --place / --skip)

Title contract: gcal event title == block name (with `#N` suffix for blocks
where `instances > 1`). All block names are globally unique across systems.
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

console = Console()

from paths import KB, gcalcli   # tenant-aware kb root + gcalcli argv builder
SYSTEMS = KB / "systems"        # legacy nested layout (pre-flatten tenants)
HABITS = KB / "habits"          # flat layout: one block file, self-contained
STATE = KB / "_state"
SKIPS_FILE = STATE / "skips.yaml"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Calendars never shown in cl week (Miro's planner): Sydney's own calendars are
# noise for his planning. NB the two distinct names — "Sydney" (shared) AND
# "sydneyslavitt@gmail.com" (her personal). Missing the second is the bug that
# leaked her invites into the week view. Canonical here; imported by every surface.
EXCLUDE_CALENDARS = {"Sydney", "sydneyslavitt@gmail.com"}


def parse_frontmatter(path):
    """Minimal YAML-ish parser: scalars and `[a, b, c]` lists. No nesting."""
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    fm = {}
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"')
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            fm[k] = [x.strip().strip('"') for x in inner.split(",") if x.strip()] if inner else []
        else:
            fm[k] = v
    return fm


def week_range(today=None, offset_weeks=0):
    today = today or datetime.now().date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _aslist(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def load_blocks():
    """Return [(group, block_meta, status)] for every block.

    `block_meta` is enriched with `status`, `goals`, `orientations` so every
    consumer reads the feeding chain uniformly, whatever the on-disk layout:

    - Flat (new):   KB/habits/<block>.md carries status/goals/orientations itself.
    - Nested (old): KB/systems/<sys>/blocks/<block>.md — those fields live on the
      parent system.md, and are copied onto the block here.

    Both are read (flat wins on a name clash) so the systems→habits migration can
    land per-tenant without a flag-day: an unmigrated tenant keeps its nested
    tree working untouched. `group` is the old system slug for nested blocks, ""
    for flat ones.
    """
    out, seen = [], set()

    if HABITS.exists():
        for bf in sorted(HABITS.glob("*.md")):
            meta = parse_frontmatter(bf)
            name = meta.get("block")
            if not name or name in seen:       # notes files have no block: key
                continue
            seen.add(name)
            status = meta.get("status", "active")
            meta["status"] = status
            meta["goals"] = _aslist(meta.get("goals"))
            meta["orientations"] = _aslist(meta.get("orientations"))
            out.append(("", meta, status))

    if SYSTEMS.exists():
        for system_dir in sorted(SYSTEMS.iterdir()):
            if not system_dir.is_dir():
                continue
            sf = system_dir / "system.md"
            if not sf.exists():
                continue
            sys_meta = parse_frontmatter(sf)
            sys_status = sys_meta.get("status", "active")
            bd = system_dir / "blocks"
            if not bd.exists():
                continue
            for bf in sorted(bd.iterdir()):
                if bf.suffix != ".md":
                    continue
                meta = parse_frontmatter(bf)
                name = meta.get("block")
                if not name or name in seen:
                    continue
                seen.add(name)
                meta["status"] = sys_status
                meta["goals"] = _aslist(sys_meta.get("goals"))
                meta["orientations"] = _aslist(sys_meta.get("orientations"))
                out.append((system_dir.name, meta, sys_status))

    return out


def is_habit(meta):
    """Is this block a tracked habit, or just a calendar anchor?

    Blocks are habits by default; set `habit: false` in a block's frontmatter to
    keep it on the calendar (still placed by `cl week`) but out of the daily
    review and the habit dashboard — for anchors like lunch/dinner that you don't
    build a streak on. Frontmatter scalars are strings, so accept string falsies.
    """
    v = meta.get("habit", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "off")
    return bool(v)


def expected_count(meta):
    """How many gcal slots this block should occupy this week."""
    cadence = meta.get("cadence", "")
    try:
        instances = int(meta.get("instances", 1) or 1)
    except (ValueError, TypeError):
        instances = 1
    if cadence == "daily":
        days = meta.get("days") or DAYS
        return len([d for d in days if d in DAYS]) * instances
    if cadence == "weekly":
        return instances
    return 0


def list_calendars(access=("owner", "writer", "reader")):
    """List gcal calendars accessible to user, filtered by access level (owner/writer/reader)."""
    cmd = gcalcli("--nocolor", "list")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    cals = []
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith(("Access", "------")):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        acc, title = parts
        if acc.lower() in access:
            cals.append(title)
    return cals


def strip_display_suffix(title):
    """Remove '(day N/M)' suffix added by multi-day event expansion.

    Used wherever a title needs to round-trip back to gcal (delete, search).
    System-block titles never carry this suffix.
    """
    return re.sub(r" \(day \d+/\d+\)$", "", title)


def fetch_events(calendar, start, end):
    """Run gcalcli agenda for one calendar; return list of (date, start_time, end_time, title).

    start_time and end_time are HH:MM strings, or "" for all-day events.
    Header row is filtered out.

    Multi-day all-day events are expanded into one row per covered date,
    with a "(day N/M)" suffix appended to the title for display. The suffix
    is stripped before any title round-trips back to gcal — see
    strip_display_suffix(). Parens not brackets because Rich would parse
    `[day 1/3]` as a style tag and crash.
    """
    cmd = gcalcli(
        "agenda",
        "--calendar", calendar,
        start.strftime("%Y-%m-%d"),
        (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        "--tsv",
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError:
        console.print("[red]gcalcli not installed[/red]")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]warning: gcalcli timed out for {calendar} (skipping)[/yellow]")
        return []
    if result.returncode != 0:
        console.print(f"[yellow]warning: gcalcli failed for {calendar}: {result.stderr.strip()}[/yellow]")
        return []
    events = []

    def in_window(date_str):
        """gcalcli returns events overlapping [start, end+1); clip to [start, end].

        Without this, multi-day events that began before `start` (e.g. a retreat
        starting last week) bleed into selectable_events as phantom rows that
        aren't rendered — focus_index lands on them and breaks move/skip.
        """
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return True
        return start <= d <= end

    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        if parts[0] == "start_date":  # header row
            continue
        start_date, start_time, end_date, end_time, title = parts[0], parts[1], parts[2], parts[3], parts[4]
        # All-day multi-day expansion: gcal end_date is exclusive, so an
        # event May 19 → May 26 covers May 19..25 (7 days). Single-day all-day
        # events (May 31 → Jun 1) have total=1 — no suffix, no expansion.
        total = 0
        if not start_time and start_date != end_date:
            try:
                sd = datetime.strptime(start_date, "%Y-%m-%d").date()
                ed = datetime.strptime(end_date, "%Y-%m-%d").date()
                total = (ed - sd).days
            except ValueError:
                total = 0
        if total > 1:
            d = sd
            n = 1
            while d < ed:
                date_str = d.strftime("%Y-%m-%d")
                if in_window(date_str):
                    events.append((date_str, "", "", f"{title} (day {n}/{total})"))
                d += timedelta(days=1)
                n += 1
        else:
            if in_window(start_date):
                events.append((start_date, start_time, end_time, title))
    return events


def fetch_titles(calendar, start, end):
    """Convenience: just titles."""
    return [t for _, _, _, t in fetch_events(calendar, start, end)]


TRAVEL_CALENDAR = "Travel"


def travel_days(start, end):
    """Set of dates in [start, end] the user is traveling — any day covered by a
    `Travel` calendar event. Multi-day trips are already expanded to one row per
    day by fetch_events, so a whole trip's dates land in the set."""
    return {d for d, _s, _e, _t in fetch_events(TRAVEL_CALENDAR, start, end)}


def suggest_time(meta, target_date, day_events):
    """Suggested HH:MM start time for placing this block on target_date.

    Priority:
      1. meta['default_start'] if set and well-formed
      2. last timed-event end + 10min on target_date
      3. "09:00" fallback
    """
    default = (meta.get("default_start") or "").strip()
    if default and re.match(r"^\d{1,2}:\d{2}$", default):
        return default

    target_str = target_date.strftime("%Y-%m-%d")
    end_times = [end for d, _, end, _ in day_events if d == target_str and end]
    if end_times:
        latest = max(end_times)
        try:
            h, m = map(int, latest.split(":"))
        except ValueError:
            return "09:00"
        m += 10
        if m >= 60:
            h += 1
            m -= 60
        if h >= 24:
            h, m = 23, 59
        return f"{h:02d}:{m:02d}"

    return "09:00"


def dump(offset_weeks=0):
    monday, sunday = week_range(offset_weeks=offset_weeks)
    console.print(f"\n[bold]Week of {monday} → {sunday}[/bold]\n")

    blocks = load_blocks()
    active = [(s, m, st) for s, m, st in blocks if st == "active"]
    skips = week_skip_counts(monday, sunday)

    cals = sorted({m.get("calendar", "") for _, m, _ in active if m.get("calendar")})
    if not cals:
        console.print("[red]no blocks have a calendar set[/red]")
        return

    cal_titles = {}
    for c in cals:
        console.print(f"  [grey50]fetching {c}…[/grey50]")
        cal_titles[c] = fetch_titles(c, monday, sunday)

    console.print()

    placed_total = 0
    unplaced_total = 0
    by_cal = {}
    for sys_slug, meta, _ in active:
        block_name = meta.get("block", "?")
        cal = meta.get("calendar", "") or "(no calendar)"
        try:
            instances = int(meta.get("instances", 1) or 1)
        except (ValueError, TypeError):
            instances = 1
        expected_raw = expected_count(meta)
        if expected_raw == 0:
            continue
        expected = max(0, expected_raw - skips.get(block_name, 0))
        events = cal_titles.get(cal, [])
        if instances <= 1:
            found = sum(1 for t in events if t == block_name)
        else:
            found = sum(1 for i in range(1, instances + 1) if f"{block_name} #{i}" in events)
        capped = min(found, expected)
        unplaced = max(0, expected - found)
        placed_total += capped
        unplaced_total += unplaced
        by_cal.setdefault(cal, []).append((block_name, found, expected, sys_slug))

    for cal in sorted(by_cal):
        console.print(f"[bold cyan]{cal}[/bold cyan]")
        for block_name, found, expected, sys_slug in sorted(by_cal[cal]):
            if found >= expected:
                mark = "[green]✓[/green]"
            elif found > 0:
                mark = "[yellow]·[/yellow]"
            else:
                mark = "[red]·[/red]"
            console.print(
                f"  {mark} {block_name:30s} [grey50]{found}/{expected}[/grey50]  [grey42]{sys_slug}[/grey42]"
            )
        console.print()

    console.print(f"[bold]Total:[/bold] {placed_total} scheduled · {unplaced_total} to place\n")


def found_in_titles(entry, titles):
    """How many of this block's instances are already on the calendar, given the
    multiset of titles from its OWN calendar for the window.

    The `instances` contract in one place: 1 instance matches the bare block name
    (counted, so a stray duplicate shows up); N instances match "name #1".."name #N"
    (each counted once — that's what caps a weekly block at N). Surfaces that build
    the bank from their own event payload (Surface's month grid) call THIS, so the
    count can't drift from what `cl week` reports.
    """
    name = entry["block"]
    n = entry.get("instances", 1) or 1
    if n <= 1:
        return sum(1 for t in titles if t == name)
    uniq = set(titles)
    return sum(1 for i in range(1, n + 1) if f"{name} #{i}" in uniq)


def build_bank(offset_weeks=0):
    """The week's routine bank + pool, WITHOUT touching gcal.

    Every input here is local — block defs (fs), skip counts (kb yaml), the pool
    (sqlite) — so this is the ~200ms part of build_view a surface can have
    without paying the ~5s calendar fetch. `found` is the one bank field that
    genuinely needs the calendar; it comes back 0 and the caller fills it in via
    found_in_titles().

    build_view() calls this. Do NOT copy this loop into a caller: the bank shape
    is read by `cl week`, the week planner, the day planner and the day modal,
    and four hand-synced copies is exactly the drift this repo keeps warning about.
    """
    import pool

    monday, sunday = week_range(offset_weeks=offset_weeks)
    skips = week_skip_counts(monday, sunday)

    daily, weekly = [], []
    for sys_slug, meta, st in load_blocks():
        if st != "active":
            continue
        block_name = meta.get("block", "?")
        expected_raw = expected_count(meta)
        if expected_raw == 0:
            continue
        try:
            instances = int(meta.get("instances", 1) or 1)
        except (ValueError, TypeError):
            instances = 1
        entry = {
            "block": block_name, "system": sys_slug,
            "found": 0, "expected": max(0, expected_raw - skips.get(block_name, 0)),
            "instances": instances,
            "calendar": meta.get("calendar", ""), "duration": meta.get("duration", ""),
            "duration_min": parse_duration_minutes(meta.get("duration", "")) or 0,
            "default_start": meta.get("default_start", ""),
            "days": meta.get("days") or DAYS,
            "cadence": meta.get("cadence", ""),
            "travel": meta.get("travel", ""),   # "pause" = skip on Travel-calendar days
        }
        (daily if meta.get("cadence") == "daily" else weekly).append(entry)

    daily.sort(key=lambda e: e["block"])
    weekly.sort(key=lambda e: e["block"])
    return {
        "week": {"monday": monday.isoformat(), "sunday": sunday.isoformat(),
                 "offset": offset_weeks},
        "bank": {"daily": daily, "weekly": weekly},
        "pool": pool.list_items(status="pooled"),
    }


def build_view(offset_weeks=0):
    """Assemble the full week view model for surfaces (web / TUI-agnostic).

    build_bank() for the local half, then one fetch per non-excluded calendar to
    fill in each block's `found` and lay out the seven days with their events.
    Same data the TUI renders — no Rich, no curses, just a dict ready for JSON.
    """
    view = build_bank(offset_weeks=offset_weeks)
    monday, sunday = week_range(offset_weeks=offset_weeks)

    cals = [c for c in list_calendars() if c not in EXCLUDE_CALENDARS]
    events_by_cal = {c: fetch_events(c, monday, sunday) for c in cals}
    tdays = travel_days(monday, sunday)   # dates on the Travel calendar → paused habits skip

    daily, weekly = view["bank"]["daily"], view["bank"]["weekly"]
    for entry in daily + weekly:
        titles = [t for _, _, _, t in events_by_cal.get(entry["calendar"], [])]
        entry["found"] = found_in_titles(entry, titles)

    # build_bank sorts by name alone (it can't know found); the real order puts
    # what still needs placing on top, so re-sort now that found is filled in.
    daily.sort(key=lambda e: (e["found"] >= e["expected"], e["block"]))
    weekly.sort(key=lambda e: (e["found"] >= e["expected"], e["block"]))

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        day_events = []
        for cal, evs in events_by_cal.items():
            for edate, stime, etime, title in evs:
                if edate == ds:
                    day_events.append({
                        "start_time": stime, "end_time": etime, "title": title,
                        "all_day": not stime, "calendar": cal,
                    })
        day_events.sort(key=lambda e: (0 if e["all_day"] else 1, e["start_time"] or ""))
        days.append({
            "date": ds, "weekday": DAYS[d.weekday()],
            "label": f"{DAYS[d.weekday()].upper()} {d.month}/{d.day}",
            "events": day_events,
            "travel": ds in tdays,
        })

    view["days"] = days
    return view


def find_block(block_name):
    """Return (system_slug, meta) for the block with this name, or (None, None)."""
    for sys_slug, meta, _ in load_blocks():
        if meta.get("block") == block_name:
            return sys_slug, meta
    return None, None


def parse_day(s, monday):
    """Accept mon|tue|…|sun or YYYY-MM-DD. Return a date in the current week."""
    s = s.strip().lower()
    if s in DAYS:
        return monday + timedelta(days=DAYS.index(s))
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_duration_minutes(s):
    """Parse '90m' / '2h' / '90' → minutes. Return None on failure."""
    if not s:
        return None
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*([mh]?)$", s)
    if not m:
        return None
    n = int(m.group(1))
    return n * 60 if m.group(2) == "h" else n


def pick_title(meta, block_name, target_date, existing_events):
    """Decide what gcal title to use, or None if no slot is free.

    existing_events: list of (date, start, end, title) tuples for the week.
    Daily blocks: one per date — collide only if an event with this title
        already exists on `target_date`.
    Weekly singletons (instances=1): one per week.
    Weekly multi-instance: pick lowest free `#N`.
    """
    cadence = meta.get("cadence", "")
    try:
        instances = int(meta.get("instances", 1) or 1)
    except (ValueError, TypeError):
        instances = 1

    if cadence == "daily":
        target = target_date.strftime("%Y-%m-%d")
        for d, _, _, t in existing_events:
            if t == block_name and d == target:
                return None
        return block_name

    if cadence == "weekly":
        if instances <= 1:
            for _, _, _, t in existing_events:
                if t == block_name:
                    return None
            return block_name
        used = {t for _, _, _, t in existing_events}
        for i in range(1, instances + 1):
            candidate = f"{block_name} #{i}"
            if candidate not in used:
                return candidate
        return None

    return block_name


def place_event(block_name, day_arg, time_arg, offset_weeks=0, duration_override=None):
    """Programmatic place. Returns {ok, msg, title?, calendar?, when?, duration?, event?}.

    duration_override (minutes) wins over the block's frontmatter duration when set.

    `event` is the created event as gcal_api.event_dict() — the same shape
    `cl agenda --month` emits, INCLUDING its id. That id is why this writes via
    the API rather than `gcalcli add`: gcalcli prints nothing identifying, so a
    caller holding a cached month grid had no way to patch the new bar in and had
    to throw the whole month away (a ~90s refetch) on every single placement.
    """
    sys_slug, meta = find_block(block_name)
    if not meta:
        return {"ok": False, "msg": f"unknown block: {block_name}"}

    cal = meta.get("calendar")
    if not cal:
        return {"ok": False, "msg": f"{block_name} has no calendar set"}

    duration_min = duration_override or parse_duration_minutes(meta.get("duration", ""))
    if not duration_min:
        return {"ok": False, "msg": f"{block_name} has no parseable duration"}

    monday, sunday = week_range(offset_weeks=offset_weeks)
    date = parse_day(day_arg, monday)
    if not date:
        return {"ok": False, "msg": f"bad day: {day_arg} (use mon|tue|…|sun or YYYY-MM-DD)"}

    if not re.match(r"^\d{1,2}:\d{2}$", time_arg):
        return {"ok": False, "msg": f"bad time: {time_arg} (use HH:MM)"}

    days = meta.get("days") or DAYS
    weekday = DAYS[date.weekday()]
    if days and weekday not in days:
        return {"ok": False, "msg": f"{block_name} doesn't run on {weekday} (days: {', '.join(days)})"}

    existing = fetch_events(cal, monday, sunday)
    title = pick_title(meta, block_name, date, existing)
    if not title:
        cadence = meta.get("cadence", "")
        if cadence == "daily":
            return {"ok": False, "msg": f"{block_name} already scheduled on {date}"}
        try:
            instances = int(meta.get("instances", 1) or 1)
        except (ValueError, TypeError):
            instances = 1
        return {"ok": False, "msg": f"all {instances} instance(s) of {block_name} already scheduled this week"}

    day_s = date.strftime("%Y-%m-%d")
    when = f"{day_s} {time_arg}"
    # HH:MM may be "9:05" (the regex above allows a 1-digit hour); the API wants
    # a real RFC3339 timestamp, so normalize through datetime rather than pasting.
    try:
        start_dt = datetime.fromisoformat(f"{day_s}T{time_arg}:00")
    except ValueError:
        return {"ok": False, "msg": f"bad time: {time_arg} (use HH:MM)"}
    end_dt = start_dt + timedelta(minutes=duration_min)

    import gcal_api    # local: events.py imports week, so a top-level import would cycle
    try:
        cal_id, tz = gcal_api.resolve_calendar(cal, writable=True)
        body = {
            "summary": title,
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz},
            "end": {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz},
            "reminders": {"useDefault": True},
        }
        res = gcal_api._wrap(lambda: gcal_api.service().events().insert(
            calendarId=cal_id, body=body, sendUpdates="none").execute())
        ev = gcal_api.event_dict(res, cal)
    except gcal_api.GcalError as e:
        return {"ok": False, "msg": f"couldn't place {title}: {e.msg}"}

    return {
        "ok": True,
        "msg": f"{title} · {cal} · {when} ({duration_min}m)",
        "title": title,
        "calendar": cal,
        "when": when,
        "duration": duration_min,
        "event": ev,
    }


def fill_day(day_arg, offset_weeks=0):
    """Auto-place every still-unplaced block that runs on `day_arg`, at its
    `default_start`. The day-planner's "schedule defaults" button — it lays
    down the routine skeleton in one shot so the user only hand-places the
    exceptions. Returns {ok, date?, placed:[…], skipped:[{block,reason}], msg}.

    Only blocks with a well-formed `default_start` are auto-placed; one without
    needs a human to pick the time, so it's reported as skipped rather than
    guessed. That makes `default_start` the de-facto fixed/mutable switch, and
    surfaces rely on it meaning exactly that.

    Both daily and weekly cadences fill. A weekly block is placed on the days it
    names (`days: [sat]`), and place_event's pick_title enforces `instances` per
    *week*, so filling both tue and thu for a 1-instance block places it once and
    reports the second as an already-scheduled skip. A weekly block with no days
    is skipped: there'd be no principled way to choose which day it lands on.
    Collision / already-placed is handled by place_event, surfacing here as a skip.
    """
    monday, sunday = week_range(offset_weeks=offset_weeks)
    date = parse_day(day_arg, monday)
    if not date:
        return {"ok": False, "msg": f"bad day: {day_arg} (use mon|tue|…|sun or YYYY-MM-DD)",
                "placed": [], "skipped": []}
    weekday = DAYS[date.weekday()]
    import trips
    trip = trips.trip_for(date.isoformat())          # traveling? which trip?
    allow = trips.allowlist(trip["key"]) if trip else None

    placed, skipped = [], []
    for sys_slug, meta, st in load_blocks():
        if st != "active":
            continue
        cadence = (meta.get("cadence") or "").strip()
        if cadence not in ("daily", "weekly"):
            continue
        name = meta.get("block", "?")
        # A daily block with no days runs every day. A *weekly* one with no days
        # has no anchor day, so filling would drop it on whichever day happened to
        # be filled first — an arbitrary choice that belongs to the planner, not us.
        days = meta.get("days") or (DAYS if cadence == "daily" else [])
        if cadence == "weekly" and not days:
            skipped.append({"block": name,
                            "reason": "weekly block has no days — set days or place by hand"})
            continue
        if weekday not in days:
            continue
        if trip and name not in allow:                # on a trip, daily is paused unless allowed
            skipped.append({"block": name, "reason": f"paused for trip — {trip['name']}",
                            "travel": True})
            continue
        start = (meta.get("default_start") or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", start):
            skipped.append({"block": name, "reason": "no default_start — place by hand"})
            continue
        res = place_event(name, date.strftime("%Y-%m-%d"), start, offset_weeks=offset_weeks)
        if res["ok"]:
            # `event` carries the created event's id — a surface holding a cached
            # month grid patches each one straight in instead of refetching.
            placed.append({"block": name, "title": res["title"], "when": res["when"],
                           "event": res.get("event")})
        else:
            skipped.append({"block": name, "reason": res["msg"]})

    n = len(placed)
    msg = f"placed {n} default{'' if n == 1 else 's'} on {date}" if n else f"nothing to place on {date}"
    return {"ok": True, "date": date.isoformat(), "placed": placed, "skipped": skipped, "msg": msg}


def fill(day_arg, offset_weeks=0, as_json=False):
    """CLI wrapper for fill_day: prints per-block result + a summary last line."""
    r = fill_day(day_arg, offset_weeks=offset_weeks)
    if as_json:
        import json
        print(json.dumps(r))
        if not r["ok"]:
            sys.exit(1)
        return
    if not r["ok"]:
        console.print(f"[red]{r['msg']}[/red]")
        sys.exit(1)
    for p in r["placed"]:
        console.print(f"[dark_sea_green4]  → {p['title']} · {p['when']}[/dark_sea_green4]")
    for s in r["skipped"]:
        console.print(f"[grey50]  · skipped {s['block']}: {s['reason']}[/grey50]")
    console.print(r["msg"])


def unplace_event(block_name, day_arg, offset_weeks=0):
    """Remove a placed block's gcal event for a day WITHOUT logging a skip.

    Different intent from skip(): a skip means "I'm intentionally not doing this"
    (and dents the week's expected count); an unplace means "wrong slot / put it
    back in the tray to re-place." The block simply returns to the day planner's
    "to place" list. Returns {ok, msg, removed_title?}.
    """
    sys_slug, meta = find_block(block_name)
    if not meta:
        return {"ok": False, "msg": f"unknown block: {block_name}"}
    cal = meta.get("calendar")
    if not cal:
        return {"ok": False, "msg": f"{block_name} has no calendar set"}
    monday, _ = week_range(offset_weeks=offset_weeks)
    date = parse_day(day_arg, monday)
    if not date:
        return {"ok": False, "msg": f"bad day: {day_arg}"}
    events = fetch_events(cal, date, date)
    title = find_event_on_date(events, block_name, date)
    if not title:
        return {"ok": False, "msg": f"{block_name} not scheduled on {date}"}
    if not delete_event_by_title(cal, title, date):
        return {"ok": False, "msg": f"failed to remove {title} from gcal"}
    return {"ok": True, "msg": f"unplaced {title} from {date}", "removed_title": title}


def move_event(block_name, day_arg, new_time, offset_weeks=0, duration_override=None):
    """Retime (and optionally re-duration) a placed block: delete the existing
    gcal event on that day and re-add it at new_time. Returns place_event's
    result. If the block wasn't actually placed, just places it fresh."""
    r = unplace_event(block_name, day_arg, offset_weeks=offset_weeks)
    if not r["ok"] and "not scheduled" not in r["msg"]:
        return r
    return place_event(block_name, day_arg, new_time, offset_weeks=offset_weeks,
                       duration_override=duration_override)


def place(block_name, day_arg, time_arg, offset_weeks=0, duration_override=None,
          as_json=False):
    """CLI wrapper: prints result, exits with non-zero on failure.

    Under --json the whole place_event dict goes out, `event` included — that's
    how a surface learns the new event's id without re-reading the calendar.
    """
    result = place_event(block_name, day_arg, time_arg, offset_weeks=offset_weeks,
                         duration_override=duration_override)
    if as_json:
        import json
        print(json.dumps(result))
        if not result["ok"]:
            sys.exit(1)
        return
    if result["ok"]:
        console.print(f"[dark_sea_green4]  → {result['msg']}[/dark_sea_green4]")
    else:
        console.print(f"[red]{result['msg']}[/red]")
        sys.exit(1)


def week_skip_counts(monday, sunday):
    """Per-block count of skips whose date falls in [monday, sunday]."""
    out = {}
    for entry in load_skips():
        block = entry.get("block")
        date_str = entry.get("date", "")
        if not block or not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if monday <= d <= sunday:
            out[block] = out.get(block, 0) + 1
    return out


def week_skip_dates(block_name, monday, sunday):
    """Set of dates this block was skipped on within [monday, sunday]."""
    out = set()
    for entry in load_skips():
        if entry.get("block") != block_name:
            continue
        try:
            d = datetime.strptime(entry.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if monday <= d <= sunday:
            out.add(d)
    return out


def load_skips():
    """Return list of skip entries: [{block, date, reason}, ...]."""
    if not SKIPS_FILE.exists():
        return []
    entries = []
    current = None
    for line in SKIPS_FILE.read_text().splitlines():
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


def append_skip(block, date_str, reason):
    """Append a skip entry to ~/kb/_state/skips.yaml."""
    STATE.mkdir(parents=True, exist_ok=True)
    safe = reason.replace('"', "'")
    entry = (
        f"- block: {block}\n"
        f"  date: {date_str}\n"
        f'  reason: "{safe}"\n'
    )
    with SKIPS_FILE.open("a") as f:
        f.write(entry)


def find_event_on_date(events, block_name, target_date):
    """First event on target_date whose title is block_name or starts with `block_name #`."""
    target = target_date.strftime("%Y-%m-%d")
    for d, _, _, t in events:
        if d != target:
            continue
        if t == block_name or t.startswith(f"{block_name} #"):
            return t
    return None


def delete_event_by_title(calendar, title, date):
    """Delete a gcal event by exact title on a specific date."""
    title = strip_display_suffix(title)
    cmd = gcalcli(
        "delete",
        "--calendar", calendar,
        "--iamaexpert",
        title,
        date.strftime("%Y-%m-%d"),
        (date + timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    # NB: gcalcli delete uses subcommand-level --calendar (correct as written above)
    result = subprocess.run(cmd, input="y\n" * 10, capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def skip_event(block_name, day_arg, reason="", offset_weeks=0):
    """Programmatic skip. Returns dict {ok, msg, deleted_title?}."""
    sys_slug, meta = find_block(block_name)
    if not meta:
        return {"ok": False, "msg": f"unknown block: {block_name}"}

    monday, _ = week_range(offset_weeks=offset_weeks)
    date = parse_day(day_arg, monday)
    if not date:
        return {"ok": False, "msg": f"bad day: {day_arg}"}

    cal = meta.get("calendar")
    deleted_title = None
    if cal:
        events = fetch_events(cal, date, date)
        title_to_del = find_event_on_date(events, block_name, date)
        if title_to_del:
            if delete_event_by_title(cal, title_to_del, date):
                deleted_title = title_to_del

    append_skip(block_name, date.strftime("%Y-%m-%d"), reason)

    msg = f"skipped {block_name} on {date}"
    if reason:
        msg += f' ("{reason}")'
    if deleted_title:
        msg += f" · removed gcal {deleted_title}"
    return {"ok": True, "msg": msg, "deleted_title": deleted_title}


def skip(block_name, day_arg, reason="", offset_weeks=0):
    """CLI wrapper: prints + exits."""
    result = skip_event(block_name, day_arg, reason, offset_weeks=offset_weeks)
    if result["ok"]:
        console.print(f"[dark_sea_green4]  → {result['msg']}[/dark_sea_green4]")
    else:
        console.print(f"[red]{result['msg']}[/red]")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="cl week")
    parser.add_argument("--next", dest="next_week", action="store_true",
                        help="operate on next week instead of current")
    parser.add_argument("--offset", type=int, default=None,
                        help="operate N weeks from now (negative = past); overrides --next")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dump", action="store_true", help="print this week's bank, headless")
    group.add_argument("--place", nargs=3, metavar=("BLOCK", "DAY", "TIME"),
                       help="schedule BLOCK on DAY at TIME (e.g. writing-block mon 10:10)")
    parser.add_argument("--duration", type=int, default=None,
                        help="with --place: override the block's duration (minutes)")
    group.add_argument("--skip", nargs="+", metavar="ARG",
                       help="--skip BLOCK DAY [REASON…] — log a skip; deletes matching gcal event if any")
    group.add_argument("--move", nargs=3, metavar=("BLOCK", "DAY", "TIME"),
                       help="retime a placed block (delete + re-add); pair with --duration to resize")
    group.add_argument("--unplace", nargs=2, metavar=("BLOCK", "DAY"),
                       help="remove a placed block's event without logging a skip (back to the tray)")
    # Outside the group so it can pair with --json (the day-planner surface reads
    # the structured result); --fill wins if combined with another mode.
    parser.add_argument("--fill", metavar="DAY",
                        help="auto-place every unplaced daily/weekly block that runs on DAY at its default_start")
    # --json is a MODIFIER, not a mode: alone it emits the week view model; with
    # --place/--fill it emits that write's result (incl. the created event + its
    # id, which is what lets a surface patch its cached grid instead of refetching).
    parser.add_argument("--json", action="store_true",
                        help="emit JSON: the week view model, or the result of --place/--fill")
    parser.add_argument("--bank", action="store_true",
                        help="with --json: emit only the bank + pool (no calendar fetch; `found` is 0)")
    args = parser.parse_args()

    offset = args.offset if args.offset is not None else (1 if args.next_week else 0)

    if args.fill:
        fill(args.fill, offset_weeks=offset, as_json=args.json)
        return
    if args.dump:
        dump(offset_weeks=offset)
        return
    if args.place:
        place(*args.place, offset_weeks=offset, duration_override=args.duration,
              as_json=args.json)
        return
    if args.json:
        import json
        view = build_bank(offset_weeks=offset) if args.bank else build_view(offset_weeks=offset)
        print(json.dumps(view))
        return
    if args.move:
        r = move_event(*args.move, offset_weeks=offset, duration_override=args.duration)
        if r["ok"]:
            console.print(f"[dark_sea_green4]  → {r['msg']}[/dark_sea_green4]")
        else:
            console.print(f"[red]{r['msg']}[/red]")
            sys.exit(1)
        return
    if args.unplace:
        r = unplace_event(*args.unplace, offset_weeks=offset)
        if r["ok"]:
            console.print(f"[dark_sea_green4]  → {r['msg']}[/dark_sea_green4]")
        else:
            console.print(f"[red]{r['msg']}[/red]")
            sys.exit(1)
        return
    if args.skip:
        if len(args.skip) < 2:
            console.print("[red]--skip needs at least BLOCK and DAY[/red]")
            sys.exit(1)
        block, day = args.skip[0], args.skip[1]
        reason = " ".join(args.skip[2:]) if len(args.skip) > 2 else ""
        skip(block, day, reason, offset_weeks=offset)
        return

    # No flag → launch TUI
    from week_tui import WeekApp
    app = WeekApp()
    if args.next_week:
        app.offset_weeks = 1
    app.run()


if __name__ == "__main__":
    main()
