"""habits.py — `cl habits` — the habit dashboard.

Every routine block IS a habit (the block definitions in ~/kb/systems/ are the
source of truth for what habits exist). This surface reads the daily-review
verdicts back out of the pool DB (pool.review_mark) and shows, per habit, a
recent done/missed strip plus the current streak and totals.

A day with no verdict is left blank, not counted as a miss — review is opt-in, so
an unmarked day means "unknown", not "failed". Only explicit done/missed count.

  cl habits                    launch (no TUI) — prints the dump
  cl habits --dump             print the dashboard, headless
  cl habits --json             emit JSON (for Surface /habits)
  cl habits --days N           window width in days (default 14)
"""

import argparse
import json
from datetime import date as _date, timedelta

from rich.console import Console

import pool
from week import is_habit, load_blocks, travel_days

console = Console()

WINDOW = 14


_RANK = {"done": 3, "partial": 2, "missed": 1}   # best verdict wins a mixed day


def _merge_instances(name, instances, history):
    """Fold a block's instance titles ('X', 'X #1', 'X #2', …) into one
    date → status map. On a day with mixed instances the best verdict wins
    (done > partial > missed)."""
    titles = [name] + [f"{name} #{i}" for i in range(1, (instances or 1) + 1)]
    merged = {}
    for t in titles:
        for d, s in history.get(t, {}).items():
            if _RANK.get(s, 0) > _RANK.get(merged.get(d), 0):
                merged[d] = s
    return merged


def _streak_and_counts(merged):
    items = sorted(merged.items())  # by date asc
    done = sum(1 for _, s in items if s == "done")
    partial = sum(1 for _, s in items if s == "partial")
    streak = 0
    for _, s in reversed(items):   # newest → oldest; missed breaks, else continues
        if s == "missed":
            break
        streak += 1
    return streak, done, partial, len(items)


def build(days=WINDOW):
    today = _date.today()
    start = today - timedelta(days=days - 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    history = pool.review_history()  # all-time, so streak/totals are complete
    tdays = travel_days(start, today)   # dates you were traveling → paused habits show ✈
    daily, weekly = [], []
    for sys_slug, meta, sys_status in load_blocks():
        if sys_status != "active":   # matches cl week — retired systems drop off
            continue
        cadence = meta.get("cadence", "")
        if cadence not in ("daily", "weekly"):
            continue
        if not is_habit(meta):   # calendar anchor (lunch/dinner), not a tracked habit
            continue
        name = meta["block"]
        try:
            instances = int(meta.get("instances", 1) or 1)
        except (ValueError, TypeError):
            instances = 1
        merged = _merge_instances(name, instances, history)
        streak, done, partial, marked = _streak_and_counts(merged)
        paused_when_travel = meta.get("travel") == "pause"

        def cell(d):
            if paused_when_travel and d in tdays:
                return "paused"
            return merged.get(d)

        row = {
            "block": name,
            "system": sys_slug,
            "cadence": cadence,
            "streak": streak,
            "done": done,
            "partial": partial,
            "marked": marked,
            "strip": [{"date": d, "status": cell(d)} for d in dates],
        }
        (daily if cadence == "daily" else weekly).append(row)

    daily.sort(key=lambda r: (-r["streak"], r["block"]))
    weekly.sort(key=lambda r: (-r["streak"], r["block"]))
    return {
        "window": {"start": start.isoformat(), "end": today.isoformat(), "days": dates},
        "daily": daily,
        "weekly": weekly,
    }


# ── surfaces ─────────────────────────────────────────────────────────────────

def _cell(status):
    return {"done": "[green]■[/green]", "partial": "[yellow]■[/yellow]",
            "missed": "[red]■[/red]"}.get(status, "[grey30]·[/grey30]")


def dump(days=WINDOW):
    data = build(days)
    console.print(f"\n  [bold]habits[/bold]  [grey50]{data['window']['start']} → "
                  f"{data['window']['end']}[/grey50]\n")
    for group, label in (("daily", "Daily"), ("weekly", "Weekly")):
        rows = data[group]
        if not rows:
            continue
        console.print(f"  [bold]{label}[/bold]")
        for r in rows:
            strip = "".join(_cell(c["status"]) for c in r["strip"])
            flame = f"[dark_orange]🔥{r['streak']}[/dark_orange]" if r["streak"] else "[grey42]  ·[/grey42]"
            console.print(f"    {strip}  {flame:>6}  {r['block']:26s} "
                          f"[grey50]{r['done']}/{r['marked']} done[/grey50]")
        console.print()


def main():
    parser = argparse.ArgumentParser(prog="cl habits")
    parser.add_argument("--dump", action="store_true", help="print the dashboard")
    parser.add_argument("--json", action="store_true", help="emit JSON (for Surface)")
    parser.add_argument("--days", type=int, default=WINDOW, help="window width (default 14)")
    args = parser.parse_args()

    if args.json:
        print(json.dumps(build(args.days)))
        return
    dump(args.days)


if __name__ == "__main__":
    main()
