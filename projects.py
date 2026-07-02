import argparse
import json
import os
import re
import shutil
import sys
import tty
import termios
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

import fm

console = Console()

KB = Path.home() / "kb"
project_path = KB / "projects"
archive_path = KB / "archive"
goals_path = KB / "goals"
orientations_path = KB / "orientations"

# Top-level dirs whose projects don't belong in routine review.
# - infrastructure: clife, life-os — system tooling, not goal-oriented projects.
# - personal-life: ongoing systems (budget, food, etc.) — show with --all if needed.
EXCLUDED_TOP = {"infrastructure", "personal-life"}

STATUS_ORDER = {
    "active": 0, "on-hold": 1, "sleeping": 2,
    "complete": 3, "abandoned": 4, "archived": 5, "superseded": 6,
}

# Default project review: active + on-hold + complete (so 'complete' bubbles
# up for the archive prompt).
DEFAULT_STATUSES = {"active", "on-hold", "complete"}
# Full review (cl review) widens to include sleeping.
REVIEW_STATUSES = {"active", "on-hold", "sleeping", "complete"}

REVIEW_FRESH_DAYS = 7  # skip projects reviewed within this many days unless --force


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_top_folder(md_file):
    parts = md_file.parts
    idx = parts.index("projects")
    return parts[idx + 1]


def get_field(content, field):
    for line in content.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def get_status(content):
    return get_field(content, "status") or "unknown"


def get_goal(content):
    in_goal = False
    for line in content.splitlines():
        if line.strip() == "## Goal":
            in_goal = True
            continue
        if in_goal:
            if line.startswith("##"):
                break
            if line.strip():
                return line.strip()

    past_frontmatter = False
    dash_count = 0
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if not past_frontmatter:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def open_task_count(md_file):
    count = 0
    for f in md_file.parent.rglob("*.md"):
        count += f.read_text().count("- [ ]")
    return count


def days_since_reviewed(content):
    """Return int days since last_reviewed, or None if never reviewed."""
    val = get_field(content, "last_reviewed")
    if not val or val in ("~", "null"):
        return None
    try:
        d = datetime.strptime(val, "%Y-%m-%d")
        return (datetime.now() - d).days
    except ValueError:
        return None


def last_activity_ts(md_file):
    """Return the most recent unix timestamp signaling activity on a project.

    Combines:
    - max mtime of any .md file under the project/sub-project tree
    - the last_reviewed: frontmatter date (if set)

    Used by review's stale-detection — catches sub-project work AND formal
    review cadence, and resists transient mtime noise from one signal.
    """
    project_dir = md_file.parent
    mtimes = [f.stat().st_mtime for f in project_dir.rglob("*.md")]
    last_mtime = max(mtimes) if mtimes else md_file.stat().st_mtime

    content = md_file.read_text()
    val = get_field(content, "last_reviewed")
    last_reviewed_ts = 0.0
    if val and val not in ("~", "null"):
        try:
            last_reviewed_ts = datetime.strptime(val, "%Y-%m-%d").timestamp()
        except ValueError:
            pass

    return max(last_mtime, last_reviewed_ts)


def set_field(md_file, field, value):
    """Set or add a frontmatter field. Idempotent."""
    content = md_file.read_text()
    lines = content.splitlines()
    in_frontmatter = False
    fm_start = None
    fm_end = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                fm_start = i
            else:
                fm_end = i
                break
    if fm_start is None or fm_end is None:
        return  # no frontmatter

    found = False
    for i in range(fm_start + 1, fm_end):
        if lines[i].startswith(f"{field}:"):
            lines[i] = f"{field}: {value}"
            found = True
            break
    if not found:
        lines.insert(fm_end, f"{field}: {value}")

    md_file.write_text("\n".join(lines) + "\n")


def set_status(md_file, new_status):
    today = datetime.now().strftime("%Y-%m-%d")
    set_field(md_file, "status", new_status)
    if new_status == "complete":
        set_field(md_file, "completed", today)
    elif new_status == "abandoned":
        set_field(md_file, "abandoned", today)
    elif new_status == "sleeping":
        set_field(md_file, "sleeping", today)


def mark_reviewed(md_file):
    set_field(md_file, "last_reviewed", datetime.now().strftime("%Y-%m-%d"))


def archive_project(md_file):
    """Move project dir to ~/kb/archive/<area>/<project>/.

    Preserves the area folder structure. Status flipped to 'archived' before
    the move. Returns the new path, or None if move was skipped.
    """
    set_status(md_file, "archived")
    project_dir = md_file.parent
    rel = project_dir.relative_to(project_path)
    dest = archive_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        console.print(f"[indian_red]  archive target exists: {dest}[/indian_red]")
        return None
    shutil.move(str(project_dir), str(dest))
    return dest


def open_in_nvim(file):
    os.spawnlp(os.P_WAIT, "nvim", "nvim", str(file))


def status_color(status):
    from tui_common import STATUS_COLORS, MUTED
    return STATUS_COLORS.get(status, MUTED)


def build_hotkeys(status):
    def key(k, label, color="steel_blue1"):
        return f"[grey50][[/grey50][{color}]{k}[/{color}][grey50]][/grey50] {label}"

    parts = [key("k", "keep")]

    if status == "complete":
        # primary action is archive
        parts.append(key("A", "archive", "gold3"))
        parts.append(key("a", "re-activate", "dark_sea_green4"))
    elif status == "sleeping":
        parts.append(key("w", "wake", "rosy_brown"))
    elif status == "active":
        parts.append(key("h", "hold", "rosy_brown"))
        parts.append(key("z", "sleep", "grey50"))
        parts.append(key("c", "complete", "dark_sea_green4"))
    elif status == "on-hold":
        parts.append(key("a", "activate", "dark_sea_green4"))
        parts.append(key("z", "sleep", "grey50"))
        parts.append(key("c", "complete", "dark_sea_green4"))

    parts.append(key("x", "abandon", "indian_red"))
    parts.append(key("o", "open"))
    parts.append(key("q", "quit", "grey70"))

    return "  ".join(parts)


def get_all_projects(statuses=None, force=False):
    """Return list of project.md paths for review.

    Filters:
    - Skips areas (we only list project.md, never area.md)
    - Skips top-level dirs in EXCLUDED_TOP
    - Skips projects with last_reviewed within REVIEW_FRESH_DAYS, unless force
    """
    if statuses is None:
        statuses = DEFAULT_STATUSES

    items = []
    for md_file in sorted(project_path.rglob("project.md")):
        if get_top_folder(md_file) in EXCLUDED_TOP:
            continue
        content = md_file.read_text()
        if get_status(content) not in statuses:
            continue
        if not force:
            days = days_since_reviewed(content)
            if days is not None and days < REVIEW_FRESH_DAYS:
                continue
        items.append(md_file)

    items.sort(key=lambda f: (
        STATUS_ORDER.get(get_status(f.read_text()), 99),
        str(f),
    ))
    return items


def review_item(md_file, index, total):
    name = md_file.parent.name
    content = md_file.read_text()
    status = get_status(content)
    goal = get_goal(content)
    task_count = open_task_count(md_file)
    days = days_since_reviewed(content)

    status_str = f"[{status_color(status)}]{status}[/{status_color(status)}]"
    rel_path = md_file.parent.relative_to(project_path)

    lines = [f"[grey50]{rel_path}[/grey50]  {status_str}"]
    if goal:
        lines.append(f"\n[grey50]goal[/grey50]  [grey80]{goal}[/grey80]")
    lines.append(f"[grey50]open[/grey50]  [steel_blue1]{task_count}[/steel_blue1] [grey50]tasks[/grey50]")
    if days is None:
        lines.append("[grey50]reviewed[/grey50]  [grey50]never[/grey50]")
    else:
        lines.append(f"[grey50]reviewed[/grey50]  [grey70]{days}d ago[/grey70]")

    console.print()
    console.print(Rule(
        f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{name}[/tan]",
        style="grey23",
    ))
    console.print(Panel("\n".join(lines), border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {build_hotkeys(status)}\n")

    while True:
        key = getch()
        if key == "k":
            mark_reviewed(md_file)
            console.print("[steel_blue1]  → kept[/steel_blue1]")
            return True
        elif key == "a" and status != "active":
            set_status(md_file, "active")
            mark_reviewed(md_file)
            console.print("[dark_sea_green4]  → activated[/dark_sea_green4]")
            return True
        elif key == "h" and status == "active":
            set_status(md_file, "on-hold")
            mark_reviewed(md_file)
            console.print("[rosy_brown]  → on hold[/rosy_brown]")
            return True
        elif key == "z" and status in ("active", "on-hold"):
            set_status(md_file, "sleeping")
            mark_reviewed(md_file)
            console.print("[grey50]  → sleeping[/grey50]")
            return True
        elif key == "w" and status == "sleeping":
            set_status(md_file, "on-hold")
            mark_reviewed(md_file)
            console.print("[rosy_brown]  → woken to on-hold[/rosy_brown]")
            return True
        elif key == "c" and status in ("active", "on-hold"):
            set_status(md_file, "complete")
            mark_reviewed(md_file)
            console.print("[dark_sea_green4]  → marked complete[/dark_sea_green4]")
            return True
        elif key == "A" and status == "complete":
            dest = archive_project(md_file)
            if dest:
                console.print(f"[gold3]  → archived to {dest.relative_to(KB)}[/gold3]")
            return True
        elif key == "x":
            set_status(md_file, "abandoned")
            mark_reviewed(md_file)
            console.print("[indian_red]  → abandoned[/indian_red]")
            return True
        elif key == "o":
            open_in_nvim(md_file)
            mark_reviewed(md_file)
            return True
        elif key == "q":
            return False


# ── JSON-first project editor (`cl projects list|show|set|new|archive`) ───────
# Sits alongside the review TUI: mirrors `cl systems`/`cl goals` so Surface can
# render a Projects tab + the universal /ed/{kind}/{slug} modal over the same
# area/goals/orientations feeding chain projects already carry. Writes go through
# fm.set_fields (byte-preserving) so project bodies + untouched keys survive.

EDITOR_CMDS = {"list", "show", "set", "new", "archive"}
PROJECT_STATUSES = ["active", "on-hold", "sleeping", "complete", "abandoned",
                    "archived", "superseded", "pending"]
FIELD_ORDER = ["created", "deadline", "status", "completed", "abandoned",
               "sleeping", "last_reviewed", "area", "goals", "orientations", "tags"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def known_goals():
    out = []
    if goals_path.exists():
        for f in sorted(goals_path.rglob("*.md")):
            if f.name == "README.md":
                continue
            out.append(fm.read(f).get("goal") or f.stem)
    return out


def known_orientations():
    out = []
    if orientations_path.exists():
        for f in sorted(orientations_path.glob("*.md")):
            if f.name == "README.md":
                continue
            out.append(fm.read(f).get("orientation") or f.stem)
    return out


def known_areas():
    if not project_path.exists():
        return []
    return sorted(d.name for d in project_path.iterdir()
                  if d.is_dir() and (d / "area.md").exists())


def find_project(slug):
    """Locate a project.md by its folder name (unique across the tree)."""
    for md in project_path.rglob("project.md"):
        if md.parent.name == slug:
            return md
    return None


def _project_title(content, slug):
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug.replace("-", " ").title()


def project_row(md):
    content = md.read_text()
    meta = fm.read(md)
    return {
        "slug": md.parent.name,
        "title": _project_title(content, md.parent.name),
        "area": get_top_folder(md),
        "status": meta.get("status") or "unknown",
        "goal": get_goal(content),
        "deadline": meta.get("deadline") or "",
        "goals": meta.get("goals") or [],
        "orientations": meta.get("orientations") or [],
        "tags": meta.get("tags") or [],
        "open_tasks": open_task_count(md),
        "reviewed_days": days_since_reviewed(content),
        "created": meta.get("created") or "",
        "path": str(md),
        "dir": str(md.parent),
    }


def _all_project_rows():
    rows = [project_row(md) for md in project_path.rglob("project.md")]
    rows.sort(key=lambda r: (r["area"], STATUS_ORDER.get(r["status"], 99), r["slug"]))
    return rows


def _validate_refs(goals, orients, as_json):
    kg, ko = set(known_goals()), set(known_orientations())
    errs = []
    bad_g = [g for g in (goals or []) if g not in kg]
    bad_o = [o for o in (orients or []) if o not in ko]
    if bad_g:
        errs.append(f"unknown goals {bad_g}")
    if bad_o:
        errs.append(f"unknown orientations {bad_o}")
    return errs


def _ok(payload, msg, as_json):
    if as_json:
        print(json.dumps({"ok": True, **payload}))
    else:
        console.print(f"[green]✓[/green] {msg}")


def _fail(msg, as_json):
    if as_json:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        console.print(f"[red]✗[/red] {msg}")
    sys.exit(0 if as_json else 1)


def cmd_list(args):
    rows = _all_project_rows()
    if args.json:
        print(json.dumps({"projects": rows, "areas": known_areas(),
                          "goals": known_goals(), "orientations": known_orientations(),
                          "statuses": PROJECT_STATUSES}))
        return
    area = None
    for r in rows:
        if r["area"] != area:
            area = r["area"]
            console.print(f"\n[bold]{area}[/bold]")
        badge = "" if r["status"] == "active" else f" [dim]({r['status']})[/dim]"
        console.print(f"  {r['slug']:32}{badge}  {r['open_tasks']} open")


def cmd_show(args):
    md = find_project(args.slug)
    if not md:
        _fail(f"no project named '{args.slug}'", args.json)
        return
    row = project_row(md)
    if args.json:
        print(json.dumps(row))
        return
    console.print(f"[bold]{row['title']}[/bold]  ({row['status']})  [dim]{row['area']}[/dim]")
    if row["goal"]:
        console.print(f"  goal           {row['goal']}")
    console.print(f"  open tasks     {row['open_tasks']}")
    console.print(f"  goals          {', '.join(row['goals']) or '—'}")
    console.print(f"  orientations   {', '.join(row['orientations']) or '—'}")
    console.print(f"  deadline       {row['deadline'] or '—'}")


def cmd_set(args):
    md = find_project(args.slug)
    if not md:
        _fail(f"no project named '{args.slug}'", args.json)
        return
    updates = {}
    if args.deadline is not None:
        if args.deadline and not DATE_RE.match(args.deadline):
            _fail(f"deadline '{args.deadline}' must be YYYY-MM-DD", args.json)
            return
        updates["deadline"] = args.deadline
    if args.goals is not None:
        updates["goals"] = _csv(args.goals)
    if args.orientations is not None:
        updates["orientations"] = _csv(args.orientations)
    if args.tags is not None:
        updates["tags"] = _csv(args.tags)
    if args.status is not None:
        if args.status not in PROJECT_STATUSES:
            _fail(f"status '{args.status}' not one of {PROJECT_STATUSES}", args.json)
            return
        updates["status"] = args.status
        today = datetime.now().strftime("%Y-%m-%d")
        if args.status == "complete":
            updates["completed"] = today
        elif args.status == "abandoned":
            updates["abandoned"] = today
        elif args.status == "sleeping":
            updates["sleeping"] = today
    if args.reviewed:
        updates["last_reviewed"] = datetime.now().strftime("%Y-%m-%d")
    if not updates:
        _fail("nothing to change", args.json)
        return
    errs = _validate_refs(updates.get("goals"), updates.get("orientations"), args.json)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    # Drop no-op writes so re-saving an unchanged form doesn't churn the file
    # (e.g. flip a bare `deadline: ` into `deadline: ""`). The Surface form posts
    # every field on every save, so this keeps git quiet unless something moved.
    cur = fm.read(md)

    def _same(k, v):
        c = cur.get(k)
        return (c or []) == v if isinstance(v, list) else (c or "") == (v or "")
    updates = {k: v for k, v in updates.items() if not _same(k, v)}
    if not updates:
        _ok({"updated": args.slug, "path": str(md), "noop": True}, "no changes", args.json)
        return
    fm.set_fields(md, updates)
    _ok({"updated": args.slug, "path": str(md)}, f"updated {args.slug}", args.json)


def cmd_new(args):
    slug = args.project
    if not SLUG_RE.match(slug or ""):
        _fail(f"project '{slug}' must be lowercase-kebab (a-z0-9-)", args.json)
        return
    if args.area not in known_areas():
        _fail(f"unknown area '{args.area}' (have: {', '.join(known_areas())})", args.json)
        return
    if find_project(slug):
        _fail(f"project '{slug}' already exists", args.json)
        return
    goals = _csv(args.goals)
    orients = _csv(args.orientations)
    errs = _validate_refs(goals, orients, args.json)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    d = project_path / args.area / slug
    if d.exists():
        _fail(f"path already exists: {d}", args.json)
        return
    meta = {"created": datetime.now().strftime("%Y-%m-%d"),
            "status": args.status or "active", "area": args.area,
            "goals": goals, "orientations": orients, "tags": _csv(args.tags)}
    title = args.title or slug.replace("-", " ").title()
    body = (f"# {title}\n\n## Goal\n\n## Tasks\n\n- [ ] \n\n## Notes / Links\n")
    d.mkdir(parents=True)
    p = d / "project.md"
    p.write_text(fm.render(meta, FIELD_ORDER) + "\n\n" + body)
    _ok({"created": slug, "path": str(p)}, f"created project {slug} in {args.area}", args.json)


def cmd_archive(args):
    md = find_project(args.slug)
    if not md:
        _fail(f"no project named '{args.slug}'", args.json)
        return
    dest = archive_path / md.parent.relative_to(project_path)
    if dest.exists():
        _fail(f"archive target exists: {dest}", args.json)
        return
    dest = archive_project(md)          # flips status → archived, moves the dir
    if dest is None:
        _fail("archive failed", args.json)
        return
    _ok({"archived": args.slug, "path": str(dest)},
        f"archived {args.slug} → {dest.relative_to(KB)}", args.json)


def _editor_parser():
    p = argparse.ArgumentParser(prog="cl projects")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("list"); lp.add_argument("--json", action="store_true")
    shp = sub.add_parser("show"); shp.add_argument("slug"); shp.add_argument("--json", action="store_true")

    stp = sub.add_parser("set")
    stp.add_argument("slug")
    stp.add_argument("--status"); stp.add_argument("--deadline")
    stp.add_argument("--goals"); stp.add_argument("--orientations"); stp.add_argument("--tags")
    stp.add_argument("--reviewed", action="store_true")
    stp.add_argument("--json", action="store_true")

    np = sub.add_parser("new")
    np.add_argument("--project", required=True); np.add_argument("--area", required=True)
    np.add_argument("--status"); np.add_argument("--goals"); np.add_argument("--orientations")
    np.add_argument("--tags"); np.add_argument("--title")
    np.add_argument("--json", action="store_true")

    ap = sub.add_parser("archive"); ap.add_argument("slug"); ap.add_argument("--json", action="store_true")
    return p


def editor_main(argv):
    args = _editor_parser().parse_args(argv)
    {"list": cmd_list, "show": cmd_show, "set": cmd_set,
     "new": cmd_new, "archive": cmd_archive}[args.cmd](args)


def main(statuses=None, force=False):
    argv = sys.argv[1:]
    if argv and argv[0] in EDITOR_CMDS:
        editor_main(argv)
        return
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--active",    action="store_true")
    parser.add_argument("--on-hold",   action="store_true", dest="on_hold")
    parser.add_argument("--sleeping",  action="store_true")
    parser.add_argument("--complete",  action="store_true")
    parser.add_argument("--abandoned", action="store_true")
    parser.add_argument("--archived",  action="store_true")
    parser.add_argument("--all",       action="store_true")
    parser.add_argument("--force",     action="store_true",
                        help="don't skip recently-reviewed projects")
    parser.add_argument("--json",      action="store_true",
                        help="emit projects as JSON (for surfaces / the hot bar)")
    args, _ = parser.parse_known_args()
    force = force or args.force

    if statuses is not None:
        pass
    elif args.all:
        statuses = {"active", "on-hold", "sleeping", "complete", "abandoned", "archived"}
    elif any([args.active, args.on_hold, args.sleeping, args.complete, args.abandoned, args.archived]):
        statuses = set()
        if args.active:    statuses.add("active")
        if args.on_hold:   statuses.add("on-hold")
        if args.sleeping:  statuses.add("sleeping")
        if args.complete:  statuses.add("complete")
        if args.abandoned: statuses.add("abandoned")
        if args.archived:  statuses.add("archived")
    else:
        statuses = DEFAULT_STATUSES

    if args.json:
        import json as _json
        # The hot bar wants the live set; default to active + on-hold unless the
        # caller narrowed it, and never skip on review-freshness.
        if not any([args.active, args.on_hold, args.sleeping, args.complete,
                    args.abandoned, args.archived, args.all]):
            statuses = {"active", "on-hold"}
        out = []
        for md in get_all_projects(statuses, force=True):
            content = md.read_text()
            out.append({
                "name": md.parent.name,
                "area": get_top_folder(md),
                "status": get_status(content),
                "goal": (get_goal(content) or "").strip(),
                "open_tasks": open_task_count(md),
                "path": str(md),
                "dir": str(md.parent),
            })
        print(_json.dumps({"projects": out}))
        return

    items = get_all_projects(statuses, force=force)

    if not items:
        console.print()
        if not force:
            console.print("[grey50]  nothing to review[/grey50]  [grey35](use --force to include recently-reviewed)[/grey35]")
        else:
            console.print("[grey50]  nothing to review[/grey50]")
        console.print()
        return

    counts = {}
    for f in items:
        s = get_status(f.read_text())
        counts[s] = counts.get(s, 0) + 1

    status_summary = "  ".join(
        f"[{status_color(s)}]{counts[s]} {s}[/{status_color(s)}]"
        for s in ("active", "on-hold", "sleeping", "complete", "abandoned", "archived")
        if counts.get(s, 0) > 0
    )

    console.print()
    console.print(Rule(
        f"[bold steel_blue1]  Project Review[/bold steel_blue1]  [grey50]—[/grey50]  {status_summary}",
        style="steel_blue1 dim",
    ))
    console.print()

    total = len(items)
    for i, md_file in enumerate(items, 1):
        keep_going = review_item(md_file, i, total)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
