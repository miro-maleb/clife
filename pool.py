"""pool.py — `cl pool` — the scheduling pool (one-off items awaiting placement).

The pool is the keystone of the weekly loop. Routine blocks stay as markdown in
`~/kb/systems/*/blocks/` (human-authored, git-synced, reach every machine). The
pool DB owns only the churny, queried state that markdown is bad at:

  - one-off items ("call Jonah") that need a slot but don't recur, and
  - placement / completion state (planned → done | missed).

`cl week` reads the pool as a bank of things to place; daily review closes the
loop — a missed one-off flips back to `pooled` to be re-placed later.

DB lives OUTSIDE the kb (tower-only, not markdown, not git-synced) at
`~/.local/share/clife/pool.db`.

  cl pool add TITLE [--area A] [--project P] [--est 90m] [--deadline YYYY-MM-DD]
                    [--priority N] [--calendar CAL] [--note TEXT] [--source PATH]
  cl pool list [--status S | --all] [--area A] [--project P] [--json]
  cl pool place ITEM DATE [TIME] [--end HH:MM] [--gcal-id ID]
  cl pool done PLACEMENT
  cl pool return PLACEMENT [REASON…]
  cl pool drop ITEM
  cl pool show ITEM
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

DB_DIR = Path(os.environ.get("CLIFE_DATA_DIR", Path.home() / ".local" / "share" / "clife"))
DB_PATH = DB_DIR / "calendar-pool.db"

# item lifecycle: pooled → placed → done  (or → dropped; placed → pooled on miss)
OPEN_STATUSES = ("pooled", "placed")
ALL_STATUSES = ("pooled", "placed", "done", "dropped")

# Calendar a one-off lands on when it has none of its own (daily-life bucket).
DEFAULT_CALENDAR = os.environ.get("CLIFE_POOL_CALENDAR", "Miro-Personal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    area        TEXT,
    project     TEXT,
    source_path TEXT,
    est_minutes INTEGER DEFAULT 60,
    calendar    TEXT,
    priority    INTEGER DEFAULT 0,
    deadline    TEXT,
    status      TEXT NOT NULL DEFAULT 'pooled',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS placement (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES pool_item(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    start      TEXT,
    end        TEXT,
    status     TEXT NOT NULL DEFAULT 'planned',
    gcal_id    TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_item_status   ON pool_item(status);
CREATE INDEX IF NOT EXISTS idx_place_item    ON placement(item_id);
CREATE INDEX IF NOT EXISTS idx_place_date    ON placement(date);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    """Open the pool DB, creating the directory + schema on first use."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def parse_minutes(s, default=60):
    """'90m' / '2h' / '90' → minutes. Returns `default` on empty, None on garbage."""
    if s is None or s == "":
        return default
    s = str(s).strip().lower()
    m = re.match(r"^(\d+)\s*([mh]?)$", s)
    if not m:
        return None
    n = int(m.group(1))
    return n * 60 if m.group(2) == "h" else n


# ── data layer (pure-ish; return dicts, raise ValueError on bad input) ──────────

def add_item(title, area=None, project=None, source_path=None, est_minutes=60,
             calendar=None, priority=0, deadline=None, notes=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO pool_item
               (title, area, project, source_path, est_minutes, calendar,
                priority, deadline, status, created_at, updated_at, notes)
               VALUES (?,?,?,?,?,?,?,?, 'pooled', ?,?,?)""",
            (title, area, project, source_path, est_minutes, calendar,
             priority or 0, deadline, ts, ts, notes),
        )
        return get_item(conn, cur.lastrowid)


def get_item(conn, item_id):
    row = conn.execute("SELECT * FROM pool_item WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(status=None, area=None, project=None, conn=None):
    """List items. `status`: a status string, 'all', or None (→ open = pooled+placed)."""
    own = conn is None
    conn = conn or connect()
    try:
        where, params = [], []
        if status == "all":
            pass
        elif status:
            where.append("status = ?")
            params.append(status)
        else:
            where.append(f"status IN ({','.join('?' * len(OPEN_STATUSES))})")
            params.extend(OPEN_STATUSES)
        if area:
            where.append("area = ?")
            params.append(area)
        if project:
            where.append("project = ?")
            params.append(project)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT * FROM pool_item {clause}
                ORDER BY priority DESC,
                         CASE WHEN deadline IS NULL OR deadline='' THEN 1 ELSE 0 END,
                         deadline ASC, created_at ASC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def place_item(item_id, date, start=None, end=None, gcal_id=None):
    with connect() as conn:
        item = get_item(conn, item_id)
        if not item:
            raise ValueError(f"no pool item #{item_id}")
        if item["status"] == "dropped":
            raise ValueError(f"item #{item_id} is dropped")
        ts = now()
        cur = conn.execute(
            """INSERT INTO placement (item_id, date, start, end, status, gcal_id, created_at)
               VALUES (?,?,?,?, 'planned', ?, ?)""",
            (item_id, date, start, end, gcal_id, ts),
        )
        conn.execute("UPDATE pool_item SET status='placed', updated_at=? WHERE id=?", (ts, item_id))
        return get_placement(conn, cur.lastrowid)


def _end_time(start, minutes):
    """'14:00' + 90 → '15:30'. Returns None if start is falsy/malformed."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", start or "")
    if not m:
        return None
    total = int(m.group(1)) * 60 + int(m.group(2)) + int(minutes or 0)
    total = min(total, 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def schedule_item(item_id, date, start, calendar=None):
    """Place a pool item AND write the real gcal event (fork #1: it lands on the
    actual calendar). Records a placement; item → placed. Returns the placement.

    gcalcli add doesn't hand back an event id, so gcal_id stays NULL — the
    placement row still tracks date/time/status for the daily-review loop.
    """
    import subprocess

    with connect() as conn:
        item = get_item(conn, item_id)
    if not item:
        raise ValueError(f"no pool item #{item_id}")
    if item["status"] == "dropped":
        raise ValueError(f"item #{item_id} is dropped")
    if not re.match(r"^\d{1,2}:\d{2}$", start or ""):
        raise ValueError(f"bad time: {start} (use HH:MM)")

    cal = calendar or item.get("calendar") or DEFAULT_CALENDAR
    dur = item.get("est_minutes") or 30
    end = _end_time(start, dur)
    cmd = ["gcalcli", "add", "--calendar", cal, "--title", item["title"],
           "--when", f"{date} {start}", "--duration", str(dur), "--noprompt"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise ValueError("gcalcli not installed")
    if r.returncode != 0:
        raise ValueError(f"gcalcli add failed: {r.stderr.strip()}")
    return place_item(item_id, date, start=start, end=end)


def get_placement(conn, pid):
    row = conn.execute("SELECT * FROM placement WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def complete_placement(pid):
    with connect() as conn:
        pl = get_placement(conn, pid)
        if not pl:
            raise ValueError(f"no placement #{pid}")
        ts = now()
        conn.execute("UPDATE placement SET status='done' WHERE id=?", (pid,))
        conn.execute("UPDATE pool_item SET status='done', updated_at=? WHERE id=?", (ts, pl["item_id"]))
        return get_item(conn, pl["item_id"])


def return_placement(pid, reason=None):
    """Mark a placement missed and flip its item back to pooled (daily-review loop)."""
    with connect() as conn:
        pl = get_placement(conn, pid)
        if not pl:
            raise ValueError(f"no placement #{pid}")
        ts = now()
        conn.execute("UPDATE placement SET status='missed' WHERE id=?", (pid,))
        note = f"returned {ts}" + (f": {reason}" if reason else "")
        conn.execute(
            """UPDATE pool_item
               SET status='pooled', updated_at=?,
                   notes = TRIM(COALESCE(notes,'') || CASE WHEN notes IS NULL OR notes='' THEN '' ELSE char(10) END || ?)
               WHERE id=?""",
            (ts, note, pl["item_id"]),
        )
        return get_item(conn, pl["item_id"])


def drop_item(item_id):
    with connect() as conn:
        if not get_item(conn, item_id):
            raise ValueError(f"no pool item #{item_id}")
        ts = now()
        conn.execute("UPDATE pool_item SET status='dropped', updated_at=? WHERE id=?", (ts, item_id))
        return get_item(conn, item_id)


def item_placements(conn, item_id):
    rows = conn.execute(
        "SELECT * FROM placement WHERE item_id=? ORDER BY date, start", (item_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── CLI ─────────────────────────────────────────────────────────────────────

def _fmt_item_line(it):
    badge = {"pooled": "[yellow]○[/yellow]", "placed": "[cyan]◐[/cyan]",
             "done": "[green]✓[/green]", "dropped": "[grey42]✗[/grey42]"}.get(it["status"], "·")
    bits = [f"[grey50]#{it['id']:>3}[/grey50]", badge, f"[bold]{it['title']}[/bold]"]
    meta = []
    if it.get("area"):
        meta.append(it["area"])
    if it.get("project"):
        meta.append(it["project"])
    if it.get("est_minutes"):
        meta.append(f"{it['est_minutes']}m")
    if it.get("deadline"):
        meta.append(f"due {it['deadline']}")
    if it.get("priority"):
        meta.append(f"p{it['priority']}")
    if meta:
        bits.append(f"[grey50]{' · '.join(meta)}[/grey50]")
    return "  ".join(bits)


def cmd_add(a):
    est = parse_minutes(a.est)
    if est is None:
        console.print(f"[red]bad --est: {a.est} (use 90, 90m, or 2h)[/red]")
        sys.exit(1)
    it = add_item(a.title, area=a.area, project=a.project, source_path=a.source,
                  est_minutes=est, calendar=a.calendar, priority=a.priority,
                  deadline=a.deadline, notes=a.note)
    console.print("[dark_sea_green4]  → pooled[/dark_sea_green4] " + _fmt_item_line(it))


def cmd_list(a):
    status = "all" if a.all else a.status
    items = list_items(status=status, area=a.area, project=a.project)
    if a.json:
        print(json.dumps({"items": items}))
        return
    if not items:
        console.print("[grey50]  pool is empty[/grey50]")
        return
    label = status if status else "open"
    console.print(f"\n[bold]Pool[/bold] [grey50]({label})[/grey50]\n")
    for it in items:
        console.print(_fmt_item_line(it))
    console.print()


def cmd_place(a):
    try:
        pl = place_item(a.item, a.date, start=a.time, end=a.end, gcal_id=a.gcal_id)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    when = pl["date"] + (f" {pl['start']}" if pl["start"] else "")
    console.print(f"[dark_sea_green4]  → placed[/dark_sea_green4] item #{a.item} @ {when} "
                  f"[grey50](placement #{pl['id']})[/grey50]")


def cmd_schedule(a):
    try:
        pl = schedule_item(a.item, a.date, a.time, calendar=a.calendar)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[dark_sea_green4]  → scheduled[/dark_sea_green4] item #{a.item} @ "
                  f"{pl['date']} {pl['start']}–{pl['end']} [grey50](→ gcal)[/grey50]")


def cmd_done(a):
    try:
        it = complete_placement(a.placement)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[green]  ✓ done[/green] {it['title']}")


def cmd_return(a):
    reason = " ".join(a.reason) if a.reason else None
    try:
        it = return_placement(a.placement, reason=reason)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    msg = f"[yellow]  ↩ returned to pool[/yellow] {it['title']}"
    if reason:
        msg += f' [grey50]("{reason}")[/grey50]'
    console.print(msg)


def cmd_drop(a):
    try:
        it = drop_item(a.item)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(f"[grey42]  ✗ dropped[/grey42] {it['title']}")


def cmd_show(a):
    with connect() as conn:
        it = get_item(conn, a.item)
        if not it:
            console.print(f"[red]no pool item #{a.item}[/red]")
            sys.exit(1)
        pls = item_placements(conn, a.item)
    console.print()
    console.print(_fmt_item_line(it))
    for k in ("source_path", "calendar", "notes"):
        if it.get(k):
            console.print(f"    [grey50]{k}:[/grey50] {it[k]}")
    if pls:
        console.print("    [grey50]placements:[/grey50]")
        for p in pls:
            when = p["date"] + (f" {p['start']}" if p["start"] else "")
            console.print(f"      [grey50]#{p['id']}[/grey50] {when}  [grey50]{p['status']}[/grey50]")
    console.print()


def main():
    parser = argparse.ArgumentParser(prog="cl pool")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("add", help="add a one-off item to the pool")
    p.add_argument("title")
    p.add_argument("--area")
    p.add_argument("--project")
    p.add_argument("--source", help="kb path the item came from")
    p.add_argument("--est", default="60", help="estimated duration: 90, 90m, or 2h")
    p.add_argument("--calendar")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--deadline", help="YYYY-MM-DD")
    p.add_argument("--note")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="list pool items")
    p.add_argument("--status", help="filter to one status (pooled/placed/done/dropped)")
    p.add_argument("--all", action="store_true", help="include done + dropped")
    p.add_argument("--area")
    p.add_argument("--project")
    p.add_argument("--json", action="store_true", help="emit JSON for surfaces")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("place", help="place an item into a slot")
    p.add_argument("item", type=int)
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("time", nargs="?", help="HH:MM start (optional)")
    p.add_argument("--end", help="HH:MM end")
    p.add_argument("--gcal-id", dest="gcal_id", help="gcal event id if pushed")
    p.set_defaults(func=cmd_place)

    p = sub.add_parser("schedule", help="place an item AND write the gcal event")
    p.add_argument("item", type=int)
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("time", help="HH:MM start")
    p.add_argument("--calendar", help="override calendar (default: item's, else Miro-Personal)")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("done", help="mark a placement done")
    p.add_argument("placement", type=int)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("return", help="mark a placement missed, return item to pool")
    p.add_argument("placement", type=int)
    p.add_argument("reason", nargs="*")
    p.set_defaults(func=cmd_return)

    p = sub.add_parser("drop", help="drop an item without doing it")
    p.add_argument("item", type=int)
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("show", help="show one item + its placements")
    p.add_argument("item", type=int)
    p.set_defaults(func=cmd_show)

    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
