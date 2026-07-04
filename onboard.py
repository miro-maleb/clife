"""onboard.py — `cl init` — welcome + set up a new person's system.

Everything the daily-driver stack needs before it works, in one guided pass:
the **connect** half (knowledge base + git, calendars, email, a default
calendar) and the **populate** half (the 5-tier spine — orientations → goals →
areas → projects → systems/habits). Portable by design: it keys off `~/kb` and
the user's own gcalcli, so running it as a different person (or `HOME`) stands
up their instance. See `hearth/surface/docs/PORTABILITY.md`.

Two surfaces read the same `readiness()` model:
  - `cl init`            the interactive wizard (interview → create files)
  - `cl init --status --json`   the machine report (Surface's /setup page)

The connect steps are **detect + guide** — gcalcli's Google OAuth and a Gmail
app-password can't (and shouldn't) be driven from here, so the wizard checks
each and prints the exact next command. The populate steps interview you and
write real files via the same templates `cl new` uses (kept in sync by hand —
if the frontmatter here drifts from new.py, the linter will flag it).
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from rich.console import Console

import blocks
import week
from new import slugify, title_of

console = Console()

KB = week.KB
ORIENTATIONS = KB / "orientations"
GOALS = KB / "goals"
SYSTEMS = KB / "systems"
PROJECTS = KB / "projects"
MSMTPRC = Path.home() / ".msmtprc"


# ── readiness model ──────────────────────────────────────────────────────────
# Shared by the wizard and Surface. Each step: key, label, group (connect|
# populate), status (ok|todo), detail (what's there now), how (the fix).

def _calendars():
    """The user's discoverable calendars, or [] if gcalcli isn't configured."""
    try:
        return list(week.list_calendars())
    except Exception:
        return []


def _count_glob(pattern):
    return sum(1 for _ in KB.glob(pattern)) if KB.is_dir() else 0


def _habit_count():
    """Active daily/weekly routine blocks that are tracked habits."""
    n = 0
    try:
        for _slug, meta, st in week.load_blocks():
            if st == "active" and meta.get("cadence") in ("daily", "weekly") \
                    and week.is_habit(meta):
                n += 1
    except Exception:
        pass
    return n


def readiness():
    """Inspect the system → a list of steps + an overall completeness figure."""
    cals = _calendars()
    year = datetime.now().year
    steps = []

    def step(key, label, group, ok, detail, how):
        steps.append({"key": key, "label": label, "group": group,
                      "status": "ok" if ok else "todo", "detail": detail, "how": how})

    # ── connect ──
    step("kb", "Knowledge base", "connect", KB.is_dir(),
         str(KB) if KB.is_dir() else "not created",
         f"mkdir -p {KB}")
    step("git", "Version control", "connect", (KB / ".git").is_dir(),
         "git repo" if (KB / ".git").is_dir() else "not a git repo",
         f"cd {KB} && git init")
    step("calendars", "Calendars", "connect", bool(cals),
         f"{len(cals)} calendar(s): {', '.join(cals[:4])}" if cals else "gcalcli not configured",
         "gcalcli init   # sign in with your Google account")
    step("default_calendar", "Default calendar", "connect",
         bool(cals) and blocks.DEFAULT_CALENDAR in cals,
         f"{blocks.DEFAULT_CALENDAR}" + ("" if not cals or blocks.DEFAULT_CALENDAR in cals
                                         else " — not among your calendars"),
         f"set DEFAULT_CALENDAR in clife/blocks.py to one of: {', '.join(cals) or '(connect calendars first)'}")
    step("email", "Email (send)", "connect", MSMTPRC.exists(),
         "~/.msmtprc present" if MSMTPRC.exists() else "no ~/.msmtprc",
         "configure ~/.msmtprc → Gmail via an app password (passwordeval cat …)")

    # ── populate ──
    n_or = _count_glob("orientations/*.md")
    step("orientations", "Orientations", "populate", n_or > 0,
         f"{n_or} defined", "cl init  → orientations, or  cl orientations new NAME")
    n_go = _count_glob(f"goals/{year}/*.md")
    step("goals", f"Goals for {year}", "populate", n_go > 0,
         f"{n_go} for {year}", "cl init  → goals, or  cl new --goal NAME")
    n_ar = _count_glob("projects/*/area.md")
    step("areas", "Areas", "populate", n_ar > 0,
         f"{n_ar} defined", "cl init  → areas, or  cl new --area NAME")
    n_pr = _count_glob("projects/*/*/project.md")
    step("projects", "Projects", "populate", n_pr > 0,
         f"{n_pr} defined", "cl init  → projects, or  cl new --project NAME")
    n_hb = _habit_count()
    step("habits", "Habits / systems", "populate", n_hb > 0,
         f"{n_hb} tracked", "cl init  → habits, or  cl new --system NAME")

    done = sum(1 for s in steps if s["status"] == "ok")
    return {"steps": steps, "done": done, "total": len(steps),
            "pct": round(done / len(steps) * 100) if steps else 0,
            "complete": done == len(steps)}


# ── file writers (programmatic — no editor, return path or None if it exists) ──
# Frontmatter mirrors new.py / orientations.py; if you change one, change both
# (the linter guards the vocabulary).

def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return None
    path.write_text(text)
    return path


def new_orientation(name):
    slug = slugify(name)
    return _write(ORIENTATIONS / f"{slug}.md",
                  f"---\norientation: {slug}\nstatus: active\n---\n\n"
                  f"# {title_of(name)}\n\n"
                  f"## What this means\n\n## Why it matters to me\n")


def new_goal(name, year=None, orientation=None):
    slug = slugify(name)
    year = year or datetime.now().year
    orients = f"[{orientation}]" if orientation else "[]"
    return _write(GOALS / str(year) / f"{slug}.md",
                  f"---\ngoal: {slug}\nyear: {year}\nstatus: active\n"
                  f'marker: ""\nsystems: []\norientations: {orients}\nprojects: []\n---\n\n'
                  f"# {title_of(name)}\n\n## Why now\n\n"
                  f"## End-of-year marker\n\n*Concrete numbers — the more specific, "
                  f"the harder to lie to yourself about.*\n\n## Notes\n")


def new_area(name):
    slug = slugify(name)
    today = datetime.now().strftime("%Y-%m-%d")
    return _write(PROJECTS / slug / "area.md",
                  f"---\ncreated: {today}\nstatus: active\ntags: []\n---\n\n"
                  f"# {title_of(name)}\n\n")


def new_project(name, area):
    slug = slugify(name)
    today = datetime.now().strftime("%Y-%m-%d")
    return _write(PROJECTS / area / slug / "project.md",
                  f"---\ncreated: {today}\ndeadline: \nstatus: active\ncompleted: \n"
                  f"abandoned: \nsleeping: \nlast_reviewed: \narea: {area}\ntags: []\n---\n\n"
                  f"# {title_of(name)}\n\n## Goal\n\n## Tasks\n\n## Notes\n")


def new_habit(name, cadence="daily", start="", days=None, travel=False):
    """Scaffold a one-block system with the interviewed schedule filled in."""
    slug = slugify(name)
    system_dir = SYSTEMS / slug
    if system_dir.exists():
        return None
    block_name = re.sub(r"^(daily|weekly|monthly)-", "", slug)
    day_list = days if days is not None else (week.DAYS if cadence == "daily" else [])
    (system_dir / "blocks").mkdir(parents=True)
    (system_dir / "system.md").write_text(
        f"---\nsystem: {slug}\nstatus: active\ngoals: []\norientations: []\n---\n\n"
        f"# {title_of(name)}\n\n## Why it exists\n\n"
        f"## Blocks\n\n- [{block_name}](blocks/{block_name}.md)\n")
    travel_line = "travel: pause\n" if travel else ""
    (system_dir / "blocks" / f"{block_name}.md").write_text(
        f"---\nblock: {block_name}\nparent: {slug}\ncalendar: {blocks.DEFAULT_CALENDAR}\n"
        f"cadence: {cadence}\ndays: [{', '.join(day_list)}]\nduration: 30m\ninstances: 1\n"
        f'default_start: "{start}"\n{travel_line}---\n\n'
        f"# {title_of(name)} — {block_name}\n\n## What this block is\n\n"
        f'## "Done" looks like\n')
    return system_dir / "system.md"


# ── the interview wizard ─────────────────────────────────────────────────────

def _ask(prompt):
    try:
        return input(f"  {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _yn(prompt, default=False):
    d = "Y/n" if default else "y/N"
    a = _ask(f"{prompt} [{d}]").lower()
    if not a:
        return default
    return a.startswith("y")


def _add_loop(what, create, extra=None):
    """Repeatedly prompt for a name and create, until blank. `extra(name)`
    gathers any per-item follow-ups and returns kwargs for `create`."""
    made = 0
    while True:
        name = _ask(f"{what} name (blank to finish):")
        if not name:
            break
        kwargs = extra(name) if extra else {}
        path = create(name, **kwargs)
        if path:
            console.print(f"    [dark_sea_green4]✓ {Path(path).relative_to(KB)}[/dark_sea_green4]")
            made += 1
        else:
            console.print("    [rosy_brown]· already exists — skipped[/rosy_brown]")
    return made


def _connect_phase(rep):
    console.print("\n[bold]Connect[/bold]  [grey50]— the plumbing[/grey50]")
    for s in rep["steps"]:
        if s["group"] != "connect":
            continue
        if s["status"] == "ok":
            console.print(f"  [green]✓[/green] {s['label']}  [grey50]{s['detail']}[/grey50]")
        else:
            console.print(f"  [yellow]•[/yellow] {s['label']}  [grey50]{s['detail']}[/grey50]")
            console.print(f"      [grey54]→ {s['how']}[/grey54]")
    console.print("  [grey50]These need a browser sign-in / a password, so do them in a "
                  "shell when ready.[/grey50]")


def _populate_phase():
    console.print("\n[bold]Populate[/bold]  [grey50]— your spine, top-down[/grey50]")

    console.print("\n[bold cyan]Orientations[/bold cyan] — the enduring directions your life "
                  "points in (the 'why' behind everything).")
    if _yn("Add some now?", default=True):
        _add_loop("Orientation", new_orientation)

    year = datetime.now().year
    console.print(f"\n[bold cyan]Goals for {year}[/bold cyan] — what you want to be true by "
                  "year-end. Concrete beats vague.")
    if _yn("Add some now?", default=True):
        def goal_extra(_n):
            o = _ask("  ↳ which orientation does it serve? (blank = none):")
            return {"year": year, "orientation": slugify(o) if o else None}
        _add_loop("Goal", new_goal, goal_extra)

    console.print("\n[bold cyan]Areas[/bold cyan] — ongoing domains of life that never 'finish' "
                  "(health, writing, home…).")
    if _yn("Add some now?", default=True):
        _add_loop("Area", new_area)

    areas = [p.parent.name for p in PROJECTS.glob("*/area.md")] if PROJECTS.is_dir() else []
    if areas:
        console.print("\n[bold cyan]Projects[/bold cyan] — discrete efforts with a 'done' state, "
                      "each living under an area.")
        console.print(f"  [grey50]areas: {', '.join(areas)}[/grey50]")
        if _yn("Add some now?", default=True):
            def proj_extra(_n):
                a = _ask(f"  ↳ under which area? ({'/'.join(areas)}):") or areas[0]
                return {"area": slugify(a)}
            _add_loop("Project", new_project, proj_extra)
    else:
        console.print("\n[grey50]Projects live under an area — add an area first, then re-run "
                      "`cl init` for projects.[/grey50]")

    console.print("\n[bold cyan]Habits / systems[/bold cyan] — the daily/weekly routine blocks "
                  "that become your agenda and habit tracker.")
    if _yn("Add some now?", default=True):
        def habit_extra(_n):
            cadence = (_ask("  ↳ cadence [daily/weekly]:") or "daily").lower()
            cadence = "weekly" if cadence.startswith("w") else "daily"
            start = _ask("  ↳ default start time [HH:MM, blank = place by hand]:")
            travel = _yn("  ↳ pause this habit while traveling?", default=False)
            return {"cadence": cadence, "start": start, "travel": travel}
        _add_loop("Habit", new_habit, habit_extra)


def _report(rep):
    console.print(f"\n[bold]Readiness[/bold]  [grey50]{rep['done']}/{rep['total']} "
                  f"· {rep['pct']}%[/grey50]")
    for s in rep["steps"]:
        mark = "[green]✓[/green]" if s["status"] == "ok" else "[yellow]•[/yellow]"
        console.print(f"  {mark} {s['label']}  [grey50]{s['detail']}[/grey50]")
    if rep["complete"]:
        console.print("\n  [green]Everything's in place — open Surface and go.[/green]")
    else:
        console.print("\n  [grey54]Re-run `cl init` anytime — it picks up where you left off.[/grey54]")


def wizard():
    console.print("\n  [bold]Welcome.[/bold] Let's set up your system.\n"
                  "  [grey50]Everything lives as markdown in ~/kb and reads from your own "
                  "calendar.\n  Nothing here is permanent — you can edit or delete any of it "
                  "later.[/grey50]")
    if not KB.is_dir():
        if _yn(f"\n  Create your knowledge base at {KB}?", default=True):
            KB.mkdir(parents=True, exist_ok=True)
            console.print(f"  [dark_sea_green4]✓ created {KB}[/dark_sea_green4]")

    _connect_phase(readiness())
    _populate_phase()
    _report(readiness())


def main():
    parser = argparse.ArgumentParser(prog="cl init")
    parser.add_argument("--status", action="store_true",
                        help="print the readiness report and exit (no prompts)")
    parser.add_argument("--json", action="store_true", help="with --status: emit JSON")
    args = parser.parse_args()

    if args.status:
        rep = readiness()
        if args.json:
            print(json.dumps(rep))
        else:
            _report(rep)
        return
    wizard()


if __name__ == "__main__":
    main()
