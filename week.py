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

KB = Path.home() / "kb"
SYSTEMS = KB / "systems"
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


def load_blocks():
    """Return [(system_slug, block_meta, system_status)] for every block."""
    out = []
    if not SYSTEMS.exists():
        return out
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
            if meta.get("block"):
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
    cmd = ["gcalcli", "--nocolor", "list"]
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
    cmd = [
        "gcalcli",
        "agenda",
        "--calendar", calendar,
        start.strftime("%Y-%m-%d"),
        (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        "--tsv",
    ]
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


def build_view(offset_weeks=0):
    """Assemble the full week view model for surfaces (web / TUI-agnostic).

    Fetches every non-excluded calendar once, computes the routine bank
    (daily/weekly with found/expected), pulls the calendar pool, and lays out
    the seven days with their events. Same data the TUI renders — no Rich, no
    curses, just a dict ready for JSON.
    """
    import pool

    monday, sunday = week_range(offset_weeks=offset_weeks)
    blocks = load_blocks()
    active = [(s, m, st) for s, m, st in blocks if st == "active"]
    skips = week_skip_counts(monday, sunday)

    cals = [c for c in list_calendars() if c not in EXCLUDE_CALENDARS]
    events_by_cal = {c: fetch_events(c, monday, sunday) for c in cals}

    daily, weekly = [], []
    for sys_slug, meta, _ in active:
        block_name = meta.get("block", "?")
        cal = meta.get("calendar", "")
        expected_raw = expected_count(meta)
        if expected_raw == 0:
            continue
        expected = max(0, expected_raw - skips.get(block_name, 0))
        try:
            instances = int(meta.get("instances", 1) or 1)
        except (ValueError, TypeError):
            instances = 1
        events = events_by_cal.get(cal, [])
        if instances <= 1:
            found = sum(1 for _, _, _, t in events if t == block_name)
        else:
            titles = {t for _, _, _, t in events}
            found = sum(1 for i in range(1, instances + 1) if f"{block_name} #{i}" in titles)
        entry = {
            "block": block_name, "system": sys_slug,
            "found": found, "expected": expected,
            "calendar": cal, "duration": meta.get("duration", ""),
            "duration_min": parse_duration_minutes(meta.get("duration", "")) or 0,
            "default_start": meta.get("default_start", ""),
            "days": meta.get("days") or DAYS,
            "cadence": meta.get("cadence", ""),
        }
        (daily if meta.get("cadence") == "daily" else weekly).append(entry)

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
        })

    return {
        "week": {"monday": monday.isoformat(), "sunday": sunday.isoformat(),
                 "offset": offset_weeks},
        "bank": {"daily": daily, "weekly": weekly},
        "pool": pool.list_items(status="pooled"),
        "days": days,
    }


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
    """Programmatic place. Returns dict {ok, msg, title?, calendar?, when?, duration?}.

    duration_override (minutes) wins over the block's frontmatter duration when set.
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

    when = f"{date.strftime('%Y-%m-%d')} {time_arg}"
    cmd = [
        "gcalcli", "add",
        "--calendar", cal,
        "--title", title,
        "--when", when,
        "--duration", str(duration_min),
        "--noprompt",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "msg": f"gcalcli add failed: {result.stderr.strip()}"}

    return {
        "ok": True,
        "msg": f"{title} · {cal} · {when} ({duration_min}m)",
        "title": title,
        "calendar": cal,
        "when": when,
        "duration": duration_min,
    }


def fill_day(day_arg, offset_weeks=0):
    """Auto-place every still-unplaced *daily* block that runs on `day_arg`, at
    its `default_start`. The day-planner's "schedule defaults" button — it lays
    down the routine skeleton in one shot so the user only hand-places the
    exceptions. Returns {ok, date?, placed:[…], skipped:[{block,reason}], msg}.

    Only daily blocks with a well-formed `default_start` are auto-placed; a
    daily block without one needs a human to pick the time, so it's reported as
    skipped rather than guessed. Collision / already-placed is handled by
    place_event (via pick_title), and surfaces here as a skip reason.
    """
    monday, sunday = week_range(offset_weeks=offset_weeks)
    date = parse_day(day_arg, monday)
    if not date:
        return {"ok": False, "msg": f"bad day: {day_arg} (use mon|tue|…|sun or YYYY-MM-DD)",
                "placed": [], "skipped": []}
    weekday = DAYS[date.weekday()]

    placed, skipped = [], []
    for sys_slug, meta, st in load_blocks():
        if st != "active" or meta.get("cadence") != "daily":
            continue
        name = meta.get("block", "?")
        days = meta.get("days") or DAYS
        if weekday not in days:
            continue
        start = (meta.get("default_start") or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", start):
            skipped.append({"block": name, "reason": "no default_start — place by hand"})
            continue
        res = place_event(name, date.strftime("%Y-%m-%d"), start, offset_weeks=offset_weeks)
        if res["ok"]:
            placed.append({"block": name, "title": res["title"], "when": res["when"]})
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


def place(block_name, day_arg, time_arg, offset_weeks=0, duration_override=None):
    """CLI wrapper: prints result, exits with non-zero on failure."""
    result = place_event(block_name, day_arg, time_arg, offset_weeks=offset_weeks,
                         duration_override=duration_override)
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
    cmd = [
        "gcalcli", "delete",
        "--calendar", calendar,
        "--iamaexpert",
        title,
        date.strftime("%Y-%m-%d"),
        (date + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
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
    group.add_argument("--json", action="store_true", help="emit the full week view model as JSON (surfaces)")
    group.add_argument("--place", nargs=3, metavar=("BLOCK", "DAY", "TIME"),
                       help="schedule BLOCK on DAY at TIME (e.g. writing-block mon 10:10)")
    parser.add_argument("--duration", type=int, default=None,
                        help="with --place: override the block's duration (minutes)")
    group.add_argument("--skip", nargs="+", metavar="ARG",
                       help="--skip BLOCK DAY [REASON…] — log a skip; deletes matching gcal event if any")
    # Outside the group so it can pair with --json (the day-planner surface reads
    # the structured result); --fill wins if combined with another mode.
    parser.add_argument("--fill", metavar="DAY",
                        help="auto-place every unplaced daily block that runs on DAY at its default_start")
    args = parser.parse_args()

    offset = args.offset if args.offset is not None else (1 if args.next_week else 0)

    if args.fill:
        fill(args.fill, offset_weeks=offset, as_json=args.json)
        return
    if args.dump:
        dump(offset_weeks=offset)
        return
    if args.json:
        import json
        print(json.dumps(build_view(offset_weeks=offset)))
        return
    if args.place:
        place(*args.place, offset_weeks=offset, duration_override=args.duration)
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
