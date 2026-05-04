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


def fetch_events(calendar, start, end):
    """Run gcalcli agenda for one calendar; return list of (date, start_time, end_time, title).

    start_time and end_time are HH:MM strings, or "" for all-day events.
    Header row is filtered out.
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
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        if parts[0] == "start_date":  # header row
            continue
        events.append((parts[0], parts[1], parts[3], parts[4]))
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


def place_event(block_name, day_arg, time_arg, offset_weeks=0):
    """Programmatic place. Returns dict {ok, msg, title?, calendar?, when?, duration?}."""
    sys_slug, meta = find_block(block_name)
    if not meta:
        return {"ok": False, "msg": f"unknown block: {block_name}"}

    cal = meta.get("calendar")
    if not cal:
        return {"ok": False, "msg": f"{block_name} has no calendar set"}

    duration_min = parse_duration_minutes(meta.get("duration", ""))
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


def place(block_name, day_arg, time_arg, offset_weeks=0):
    """CLI wrapper: prints result, exits with non-zero on failure."""
    result = place_event(block_name, day_arg, time_arg, offset_weeks=offset_weeks)
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dump", action="store_true", help="print this week's bank, headless")
    group.add_argument("--place", nargs=3, metavar=("BLOCK", "DAY", "TIME"),
                       help="schedule BLOCK on DAY at TIME (e.g. writing-block mon 10:10)")
    group.add_argument("--skip", nargs="+", metavar="ARG",
                       help="--skip BLOCK DAY [REASON…] — log a skip; deletes matching gcal event if any")
    args = parser.parse_args()

    offset = 1 if args.next_week else 0

    if args.dump:
        dump(offset_weeks=offset)
        return
    if args.place:
        place(*args.place, offset_weeks=offset)
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
