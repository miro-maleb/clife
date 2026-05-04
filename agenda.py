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


def fetch_day_events(target_date: _date) -> list[dict]:
    """Fetch all events on target_date from every accessible calendar.

    Returns rows ordered by (start_time, title). All-day events sort first
    (empty start string).
    """
    cals = list_calendars()
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


def dump(target_date: _date) -> None:
    weekday = DAYS[target_date.weekday()]
    console.print(f"\n[bold]cl agenda — {weekday.title()} {target_date}[/bold]\n")

    events = fetch_day_events(target_date)
    rows = annotate_with_status(events, target_date.strftime("%Y-%m-%d"))

    if not rows:
        console.print("  [dim]nothing on the calendar today[/dim]\n")
        return

    all_day = [r for r in rows if not r["start"]]
    timed = [r for r in rows if r["start"]]

    if all_day:
        for r in all_day:
            console.print(f"  [grey50]all-day[/grey50]    {r['title']}  [grey42]({r['calendar']})[/grey42]")
        console.print()

    done = partial = skipped = pending = 0
    for r in timed:
        st = (r["status"] or {}).get("status") if r["status"] else None
        marker = _MARKERS.get(st, _MARKERS[None])
        if r["meta"]:
            tag = f"({r['system']})"
            color = "white"
        else:
            marker = "   "
            tag = f"[grey50](gcal — {r['calendar']})[/grey50]"
            color = "grey50"
        title = r["title"]
        if st == "done":
            title_md = f"[strike grey50]{title}[/strike grey50]"
        elif st == "skip":
            title_md = f"[grey50]{title}[/grey50]"
        elif st == "partial":
            title_md = f"[strike grey50]{title}[/strike grey50] [dim](partial)[/dim]"
        else:
            title_md = f"[{color}]{title}[/{color}]"

        time_col = f"{r['start']}–{r['end']}" if r["end"] else r["start"]
        console.print(f"  {marker} {time_col:13s} {title_md:40s} {tag}")

        note = (r["status"] or {}).get("note") if r["status"] else ""
        if note:
            console.print(f"          [dim]note: {note}[/dim]")

        if r["meta"]:
            if st == "done":
                done += 1
            elif st == "partial":
                partial += 1
            elif st == "skip":
                skipped += 1
            else:
                pending += 1

    total_blocks = done + partial + skipped + pending
    console.print()
    console.print(
        f"[bold]{done}/{total_blocks} done[/bold]"
        f"  ·  {partial} partial  ·  {skipped} skipped  ·  {pending} pending\n"
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_date_arg(s: str) -> _date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cl agenda")
    parser.add_argument("--dump", action="store_true",
                        help="print today's items + status, headless")
    parser.add_argument("--date", type=parse_date_arg, default=None,
                        help="operate on this date (YYYY-MM-DD); default today")
    args = parser.parse_args()

    target = args.date or datetime.now().date()

    if args.dump:
        dump(target)
        return

    from agenda_tui import AgendaApp
    app = AgendaApp(start_date=target)
    app.run()


if __name__ == "__main__":
    main()
