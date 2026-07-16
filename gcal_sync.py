"""gcal_sync.py — mirror Google Calendar into a local SQLite DB.

Heavy once, cheap forever. The first run does a FULL sync: it crawls a bounded
window (±1 year — see WINDOW_* below) of every calendar and stores the Calendar
API's `nextSyncToken`. Every run after that is INCREMENTAL — the token makes the
API hand back only what changed or was deleted since last time, so a quiet sync
is a couple of fast HTTP calls. Reads then come from SQLite (~1ms) instead of
gcalcli (~90s).

Recurring events are EXPANDED (singleEvents=true), so every row is a real dated
instance and "what's on day X" is a plain indexed query — no rrule math at read
time. The window is fixed at full-sync time (the sync token encapsulates it, and
the API forbids pairing a syncToken with timeMin/timeMax); rolling it forward is
just another full sync (`--full`), which is cheap.

No new auth — reuses gcal_api's client (gcalcli's OAuth token). Standalone:

    python gcal_sync.py            # incremental where possible, else full
    python gcal_sync.py --full     # force a full re-sync (e.g. to roll the window)
    python gcal_sync.py --stats    # just print what's mirrored

Read API (import me): events_between(lo, hi), get_event(id), day_events(date).
"""

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import paths
from gcal_api import GcalError, calendar_map, event_dict, service

DB_PATH = paths.DATA_DIR / "calendar-mirror.db"

# The mirror covers [today - BACK, today + AHEAD]. Together these bound it to a
# 2-year window. Widen either (e.g. both to 730) and re-run with --full to roll.
WINDOW_BACK_DAYS = 365
WINDOW_AHEAD_DAYS = 365
PAGE_SIZE = 2500          # Calendar API per-page max


# ── window ───────────────────────────────────────────────────────────────────

def _window():
    """RFC3339 UTC bounds for the full-sync crawl."""
    today = date.today()
    lo = today - timedelta(days=WINDOW_BACK_DAYS)
    hi = today + timedelta(days=WINDOW_AHEAD_DAYS)
    return (datetime(lo.year, lo.month, lo.day, tzinfo=timezone.utc).isoformat(),
            datetime(hi.year, hi.month, hi.day, tzinfo=timezone.utc).isoformat())


# ── db ───────────────────────────────────────────────────────────────────────

def connect():
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # concurrent readers during a sync
    _init(conn)
    return conn


def _init(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS events (
      id                 TEXT NOT NULL,
      calendar           TEXT NOT NULL,
      title              TEXT,
      start_date         TEXT,      -- 'YYYY-MM-DD'
      end_date           TEXT,      -- inclusive (event_dict converts gcal's exclusive)
      start_time         TEXT,      -- 'HH:MM' or NULL (all-day)
      end_time           TEXT,      -- 'HH:MM' or NULL
      all_day            INTEGER,
      recurring          INTEGER,
      recurring_event_id TEXT,
      status             TEXT,
      html_link          TEXT,
      updated            TEXT,      -- API 'updated' stamp (debug/audit)
      PRIMARY KEY (id, calendar)
    );
    CREATE INDEX IF NOT EXISTS ix_events_span ON events(start_date, end_date);
    CREATE INDEX IF NOT EXISTS ix_events_cal  ON events(calendar);
    CREATE TABLE IF NOT EXISTS sync_state (
      calendar   TEXT PRIMARY KEY,
      sync_token TEXT,
      window_min TEXT,
      window_max TEXT,
      last_full  TEXT,
      last_sync  TEXT
    );
    """)
    conn.commit()


def _upsert(conn, ev, cal_name):
    d = event_dict(ev, cal_name)
    conn.execute("""
      INSERT INTO events (id, calendar, title, start_date, end_date, start_time,
        end_time, all_day, recurring, recurring_event_id, status, html_link, updated)
      VALUES (:id,:calendar,:title,:start_date,:end_date,:start_time,:end_time,
        :all_day,:recurring,:recurring_event_id,:status,:html_link,:updated)
      ON CONFLICT(id, calendar) DO UPDATE SET
        title=excluded.title, start_date=excluded.start_date, end_date=excluded.end_date,
        start_time=excluded.start_time, end_time=excluded.end_time, all_day=excluded.all_day,
        recurring=excluded.recurring, recurring_event_id=excluded.recurring_event_id,
        status=excluded.status, html_link=excluded.html_link, updated=excluded.updated
    """, {**d, "all_day": int(bool(d["all_day"])), "recurring": int(bool(d["recurring"])),
          "updated": ev.get("updated")})


def _delete(conn, eid, cal_name):
    conn.execute("DELETE FROM events WHERE id=? AND calendar=?", (eid, cal_name))


# ── sync ─────────────────────────────────────────────────────────────────────

def _http_status(e):
    return getattr(getattr(e, "resp", None), "status", None)


def sync_calendar(conn, cal_name, cal_id, *, force_full=False):
    """Sync one calendar. Incremental if we hold a token (and aren't forced),
    else a full windowed crawl. Returns (upserts, deletes, mode)."""
    from googleapiclient.errors import HttpError

    row = conn.execute(
        "SELECT sync_token, window_min, window_max FROM sync_state WHERE calendar=?",
        (cal_name,)).fetchone()
    token = None if force_full else (row["sync_token"] if row else None)
    wmin, wmax = _window()
    mode = "incremental" if token else "full"

    # syncToken can't be combined with timeMin/timeMax — the token already
    # encapsulates the window from the initial full sync. singleEvents/showDeleted
    # are kept identical across full+incremental so the token stays valid.
    params = {"singleEvents": True, "showDeleted": True, "maxResults": PAGE_SIZE}
    params.update({"syncToken": token} if token else {"timeMin": wmin, "timeMax": wmax})

    up = dl = 0
    page_token = None
    new_token = None
    while True:
        p = dict(params, **({"pageToken": page_token} if page_token else {}))
        try:
            res = service().events().list(calendarId=cal_id, **p).execute()
        except HttpError as e:
            if _http_status(e) == 410:               # token expired → restart full
                return sync_calendar(conn, cal_name, cal_id, force_full=True)
            raise
        for ev in res.get("items", []):
            if ev.get("status") == "cancelled":
                _delete(conn, ev["id"], cal_name); dl += 1
            else:
                _upsert(conn, ev, cal_name); up += 1
        new_token = res.get("nextSyncToken") or new_token
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    now = datetime.now(timezone.utc).isoformat()
    keep_min = wmin if mode == "full" else (row["window_min"] if row else wmin)
    keep_max = wmax if mode == "full" else (row["window_max"] if row else wmax)
    conn.execute("""
      INSERT INTO sync_state (calendar, sync_token, window_min, window_max, last_full, last_sync)
      VALUES (?,?,?,?,?,?)
      ON CONFLICT(calendar) DO UPDATE SET
        sync_token=excluded.sync_token,
        window_min=excluded.window_min, window_max=excluded.window_max,
        last_full=CASE WHEN ?='full' THEN excluded.last_full ELSE sync_state.last_full END,
        last_sync=excluded.last_sync
    """, (cal_name, new_token, keep_min, keep_max,
          now, now, mode))
    conn.commit()
    return up, dl, mode


def sync(*, force_full=False, quiet=False):
    """Sync every calendar. One bad calendar is reported, not fatal."""
    conn = connect()
    try:
        cals = calendar_map()
    except GcalError as e:
        print(f"gcal-sync: {e.msg}", file=sys.stderr)
        conn.close()
        return 1
    t0 = time.time()
    tot_up = tot_dl = 0
    rows = []
    for name, meta in cals.items():
        try:
            up, dl, mode = sync_calendar(conn, name, meta["id"], force_full=force_full)
            tot_up += up; tot_dl += dl
            rows.append((name, up, dl, mode, None))
        except Exception as e:                        # keep going; log the offender
            rows.append((name, 0, 0, "error", f"{type(e).__name__}: {e}"))
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()

    if not quiet:
        for name, up, dl, mode, err in rows:
            if err:
                print(f"  ✗ {name:28} {err}")
            else:
                print(f"  · {name:28} {mode:11} +{up} -{dl}")
        print(f"gcal-sync: {tot_up} upserts, {tot_dl} deletes across {len(cals)} "
              f"calendars in {time.time()-t0:.1f}s — {total} events mirrored")
    return 0


# ── read API ─────────────────────────────────────────────────────────────────

def events_between(lo, hi, conn=None):
    """Every mirrored event whose span touches the inclusive date range
    [lo, hi] (ISO 'YYYY-MM-DD'). All-day first, then by start time."""
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute("""
          SELECT * FROM events
          WHERE status IS NOT 'cancelled' AND start_date <= ? AND end_date >= ?
          ORDER BY all_day DESC, (start_time IS NULL), start_time, start_date
        """, (hi, lo)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def day_events(day, conn=None):
    """Events on a single ISO date."""
    return events_between(day, day, conn=conn)


def get_event(eid, conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        r = conn.execute("SELECT * FROM events WHERE id=? AND status IS NOT 'cancelled'",
                         (eid,)).fetchone()
        return dict(r) if r else None
    finally:
        if own:
            conn.close()


def stats():
    conn = connect()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    per = conn.execute(
        "SELECT calendar, COUNT(*) n, MIN(start_date) lo, MAX(start_date) hi "
        "FROM events GROUP BY calendar ORDER BY n DESC").fetchall()
    st = conn.execute("SELECT calendar, last_full, last_sync FROM sync_state").fetchall()
    last = {r["calendar"]: r for r in st}
    conn.close()
    print(f"{total} events in {DB_PATH}")
    for r in per:
        s = last.get(r["calendar"])
        synced = (s["last_sync"][:19] if s and s["last_sync"] else "never")
        print(f"  {r['calendar']:28} {r['n']:5}  {r['lo']}…{r['hi']}  synced {synced}")


def main():
    ap = argparse.ArgumentParser(description="Mirror Google Calendar into SQLite.")
    ap.add_argument("--full", action="store_true", help="force a full re-sync (rolls the window)")
    ap.add_argument("--stats", action="store_true", help="print what's mirrored and exit")
    ap.add_argument("--quiet", action="store_true", help="no per-calendar output")
    args = ap.parse_args()
    if args.stats:
        stats()
        return 0
    return sync(force_full=args.full, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
