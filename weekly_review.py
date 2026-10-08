"""weekly_review.py — the Saturday-morning look back at the week, as a report.

Publishes weekly-review-YYYY-MM-DD through `kb-report issue`. A new file every week
is deliberate: Surface's report watcher announces new files, and the `series:`
front matter folds the issues into one card on /reports.

Built to be iterated on. Each section is one function in SECTIONS that returns
markdown (or None to skip itself), so "it would have helped to hear about X"
is one new function and one line in the list — nothing else to touch.

Todos and notes go through `cl stream ... --json` (agenda reads todo.md via
todo.py since the 2026-10-05 redesign; tags are retired); the calendar comes from gcal_sync's local mirror. The only
model call is the short descriptive summary in section 1, on the resident
model with keep_alive -1 (via ai._generate_text).

    python weekly_review.py              # write this week's review
    python weekly_review.py --stdout     # print it instead, write nothing
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ai
import fm
import gcal_sync
from paths import KB

REPORTS = KB / "outbox" / "reports"
TODAY = date.today()
WEEK_AGO = TODAY - timedelta(days=7)

SOONER_FLAG_DAYS = 3      # a Sooner line older than this gets a flag
DUST_DAYS = 14            # a Later line older than this is "gathering dust"
WORKING_STALE_DAYS = 14   # a working/ capture older than this wants filing
SUMMARY_BODY_CHARS = 1500  # per note, fed to the summary


# ── data, all through the owners ─────────────────────────────────────────────

def stream(*args):
    out = subprocess.run([str(HERE / "cl"), "stream", *args, "--json"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def tag_added(path, tag):
    """Date the tag last appeared in this note's history, or None.

    `git log -S` finds commits that changed how often the string occurs; the
    newest one is when it was (last) added. History only reaches back to the
    2026-09-07 flatten, so older todos all read as ~Sep 8 — still truer than
    `created:` for "how long has this been marked sooner"."""
    try:
        out = subprocess.run(
            ["git", "-C", str(KB), "log", "--format=%cs", f"-S{tag}", "--",
             str(Path(path).relative_to(KB))],
            capture_output=True, text=True, timeout=20).stdout.split()
        return date.fromisoformat(out[0]) if out else None
    except Exception:
        return None


def days(d):
    return (TODAY - d).days


def created_date(item):
    try:
        return date.fromisoformat((item.get("created") or "")[:10])
    except ValueError:
        return None


# ── sections ─────────────────────────────────────────────────────────────────

def written_this_week():
    notes = [n for n in stream("chrono")
             if n.get("on") and WEEK_AGO < date.fromisoformat(n["on"]) <= TODAY]
    if not notes:
        return "## What you wrote\n\nNothing new this week."

    # Grouped by FOLDER (tags retired 2026-10-05): writing/journal, threads, ...
    by_tag = {}
    for n in notes:
        by_tag.setdefault(str(Path(n["relpath"]).parent), []).append(n)
    lines = ["## What you wrote", "", f"{len(notes)} notes since {WEEK_AGO:%a %d %b}.", ""]
    for t in sorted(by_tag):
        lines.append(f"**{t}**")
        for n in by_tag[t]:
            lines.append(f"- {n['on']} — {n['title']}")
        lines.append("")

    # The journal is his: listed above by date, never summarised.
    readable = [n for n in notes if not n["relpath"].startswith("writing/journal/")]
    summary = summarize(readable) if readable else ""
    if summary:
        lines += ["*In brief (local model, descriptive only):*", "", summary, ""]
    return "\n".join(lines).rstrip()


def summarize(notes):
    blocks = []
    for n in notes:
        _, body, _ = fm.split(Path(n["path"]))
        blocks.append(f"### {n['title']}  [{Path(n['relpath']).parent}]\n"
                      f"{body.strip()[:SUMMARY_BODY_CHARS]}")
    prompt = (
        "Below are the notes one person wrote this week. Write 2-4 plain sentences "
        "saying what topics and threads they cover — what the writing was ABOUT.\n"
        "Rules: descriptive only. No advice, no encouragement, no interpretation of "
        "the person's feelings, character, or spiritual practice. No preamble, no "
        "bullet points, no headings. Third person is unnecessary — just name the "
        "topics.\n\n" + "\n\n".join(blocks))
    return ai._generate_text(prompt, num_predict=300, temperature=0.2)


def todo_sooner():
    items = [i for g in stream("agenda")["groups"] if g["key"] == "sooner"
             for i in g["items"]]
    if not items:
        return "## Sooner\n\nEmpty."
    rows = [(i.get("age_days", -1), i, None) for i in items]
    rows.sort(key=lambda r: -r[0])
    stale = sum(1 for a, _, _ in rows if a > SOONER_FLAG_DAYS)
    lines = ["## Sooner", "",
             f"{len(rows)} items, {stale} sooner for more than {SOONER_FLAG_DAYS} days.", ""]
    for age, i, since in rows:
        flag = " ⚠" if age > SOONER_FLAG_DAYS else ""
        when = f"≥{age}d" if age >= 0 else "?"
        lines.append(f"- {i['title']} — {when}{flag}")
    lines += ["", "*Age = days since the line was written into todo.md (git blame). "
              "History starts at the 2026-10-05 redesign, so older items read young.*"]
    return "\n".join(lines)


def todo_dust():
    items = [i for g in stream("agenda")["groups"] if g["key"] != "sooner"
             for i in g["items"]]
    old = [(i.get("age_days", 0), i) for i in items if i.get("age_days", 0) > DUST_DAYS]
    if not old:
        return None
    old.sort(key=lambda r: -r[0])
    lines = ["## Later, gathering dust", "",
             f"{len(old)} of {len(items)} Later items are older than {DUST_DAYS} days. "
             "Do, move to Sooner, or delete the line (`cl todo done`).", ""]
    lines += [f"- {i['title']} — {age}d" for age, i in old]
    lines += ["", "*Age = days since the line was written into todo.md (git blame).*"]
    return "\n".join(lines)


def inbox():
    items = stream("inbox")          # = working/ since 2026-10-05
    old = [i for i in items if (i.get("age_days") or 0) >= WORKING_STALE_DAYS]
    if not old:
        return f"## Working\n\n{len(items)} capture(s) in working/, none older than {WORKING_STALE_DAYS} days."
    lines = ["## Working", "",
             f"{len(old)} of {len(items)} captures in working/ are older than "
             f"{WORKING_STALE_DAYS} days — file them out or let them go to archive.", ""]
    for i in sorted(old, key=lambda i: i.get("created") or ""):
        lines.append(f"- {i['title']} — {i.get('age_days', '?')}d")
    return "\n".join(lines)


def next_week():
    lo, hi = TODAY + timedelta(days=1), TODAY + timedelta(days=7)
    try:
        events = gcal_sync.events_between(lo.isoformat(), hi.isoformat())
    except Exception as e:
        return f"## Next 7 days\n\nCalendar mirror unreadable: {e}"
    # Calendars that aren't the user's schedule (an organisation's public
    # calendar, say). Named in the environment, not here — this repo is public.
    ignored = {c.strip() for c in os.environ.get("CLIFE_IGNORE_CALENDARS", "").split(",") if c.strip()}
    events = [e for e in events if e["calendar"] not in ignored]
    if not events:
        return "## Next 7 days\n\nNothing on the calendar."
    lines = ["## Next 7 days", ""]
    day = None
    for e in sorted(events, key=lambda e: (max(e["start_date"], lo.isoformat()),
                                           not e["all_day"], e["start_time"] or "")):
        d = max(e["start_date"], lo.isoformat())
        if d != day:
            day = d
            lines += ["", f"**{date.fromisoformat(d):%a %d %b}**"]
        if e["all_day"]:
            span = (f" (through {date.fromisoformat(e['end_date']):%a})"
                    if e["end_date"] > e["start_date"] else "")
            lines.append(f"- all day — {e['title']}{span} · {e['calendar']}")
        else:
            t = (e["start_time"] or "")[11:16] or e["start_time"]
            lines.append(f"- {t} — {e['title']} · {e['calendar']}")
    return "\n".join(lines).replace("\n\n\n", "\n\n")


def footer():
    return ("---\n\n*What would have helped this week? Say so and it becomes a "
            "section — `~/clife/weekly_review.py`, one function each.*")


SECTIONS = [written_this_week, todo_sooner, todo_dust, inbox, next_week, footer]


# ── assembly ─────────────────────────────────────────────────────────────────

def build():
    parts = []
    for section in SECTIONS:
        try:
            out = section()
        except Exception as e:                      # one bad section never sinks the issue
            out = f"## {section.__name__}\n\n*Section failed: {e}*"
        if out:
            parts.append(out)
    head = (f"# Weekly review — {TODAY:%A %d %B %Y}\n\n"
            f"**[Start the review →](/review)** — todos, working captures, related "
            f"journal entries and email, one tap each.\n\n"
            f"*Generated {datetime.now():%Y-%m-%d %H:%M} by weekly_review.py.*\n")
    return head + "\n" + "\n\n".join(parts) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--stdout", action="store_true", help="print, write nothing")
    a = p.parse_args()
    text = build()
    if a.stdout:
        print(text)
        return
    # kb-report is the one writer for outbox/reports (~/hearth/kbfm/reportfmt.py
    # owns the front matter and refuses malformed output); series issue slug is
    # weekly-review-<date>, same name this job always wrote.
    r = subprocess.run([str(Path.home() / ".local" / "bin" / "kb-report"), "issue",
                        "-t", f"Weekly review — {TODAY:%a %d %b}", "-e", "🪷",
                        "-b", "The week back, the week ahead — tap to review",
                        "--series", "weekly-review", "--series-title", "Weekly review",
                        "--date", TODAY.isoformat()],
                       input=text, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"kb-report refused: {r.stderr or r.stdout}")
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
