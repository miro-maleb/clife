"""goals.py — `cl goals` — goal editor.

CRUD over the year *goals* that live as `~/kb/goals/<year>/<slug>.md`. A goal
sits between orientations and systems in the feeding chain: it names a concrete
outcome for the year and declares which orientations it serves. This command
edits the modeled scalar/inline fields (`year`, `status`, `marker`,
`orientations`) and creates/retires/deletes goals.

  cl goals [list] [--json]            every goal (year, status, orientations)
  cl goals show SLUG [--json]         one goal's fields + its systems/projects
  cl goals new --goal SLUG --year YYYY [--status active] [--marker "…"] [--orientations a,b]
  cl goals set SLUG [--name NEW] [--year YYYY] [--status S] [--marker "…"] [--orientations a,b]
  cl goals rm SLUG [--force]

A goal's `systems:` and `projects:` lists are block-style cross-references
maintained from the system/project side; this editor preserves them verbatim
and does not edit them. Changing `--year` moves the file to that year's folder.
Renaming is safe (goals aren't streak-keyed) but does not repoint references.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from rich.console import Console

import fm
import week

console = Console()

KB = week.KB
GOALS = KB / "goals"
ORIENTATIONS = KB / "orientations"

FIELD_ORDER = ["goal", "year", "status", "marker", "orientations",
               "systems", "projects"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
YEAR_RE = re.compile(r"^\d{4}$")
DEFAULT_STATUS = "active"


def find_path(slug):
    """Locate a goal file by slug across year folders (matches stem or `goal:`)."""
    if not GOALS.exists():
        return None
    for f in GOALS.rglob("*.md"):
        if f.stem == slug or fm.read(f).get("goal") == slug:
            return f
    return None


def known_orientations():
    out = []
    if ORIENTATIONS.exists():
        for f in sorted(ORIENTATIONS.glob("*.md")):
            out.append(fm.read(f).get("orientation") or f.stem)
    return out


def goal_row(path):
    m = fm.read(path)
    return {
        "goal": m.get("goal") or path.stem,
        "year": str(m.get("year", "") or path.parent.name),
        "status": m.get("status", "active"),
        "marker": m.get("marker", ""),
        "orientations": m.get("orientations") or [],
        "systems": m.get("systems") or [],
        "projects": m.get("projects") or [],
        "slug": path.stem,
    }


def all_goals():
    out = []
    if GOALS.exists():
        for f in sorted(GOALS.rglob("*.md")):
            out.append(goal_row(f))
    return out


def default_body(slug):
    title = slug.replace("-", " ").title()
    return (f"# {title}\n\n"
            "## Why now\n\n"
            "## What done looks like\n\n"
            "## Notes\n")


def _bad_orients(orients):
    ko = set(known_orientations())
    return [o for o in (orients or []) if o not in ko]


def cmd_list(args):
    items = all_goals()
    if args.json:
        print(json.dumps({"goals": items, "orientations": known_orientations()}))
        return
    for g in items:
        badge = "" if g["status"] == "active" else f" [dim]({g['status']})[/dim]"
        console.print(f"  {g['year']}  {g['goal']:28}{badge}  "
                      f"→ {', '.join(g['orientations']) or '—'}")


def cmd_show(args):
    p = find_path(args.slug)
    if not p:
        _fail(f"no goal named '{args.slug}'", args.json)
        return
    row = goal_row(p)
    row["path"] = str(p)
    if args.json:
        print(json.dumps(row))
        return
    console.print(f"[bold]{row['goal']}[/bold]  ({row['year']}, {row['status']})")
    if row["marker"]:
        console.print(f"  marker         {row['marker']}")
    console.print(f"  orientations   {', '.join(row['orientations']) or '—'}")
    console.print(f"  systems        {', '.join(row['systems']) or '—'}")
    console.print(f"  projects       {', '.join(row['projects']) or '—'}")


def cmd_new(args):
    slug = args.goal
    if not SLUG_RE.match(slug or ""):
        _fail(f"goal '{slug}' must be lowercase-kebab (a-z0-9-)", args.json)
        return
    if not YEAR_RE.match(args.year or ""):
        _fail(f"year '{args.year}' must be YYYY", args.json)
        return
    if find_path(slug):
        _fail(f"goal '{slug}' already exists", args.json)
        return
    orients = _csv(args.orientations) if args.orientations is not None else []
    bad = _bad_orients(orients)
    if bad:
        _fail(f"unknown orientations {bad}", args.json)
        return
    meta = {"goal": slug, "year": args.year, "status": args.status or DEFAULT_STATUS,
            "orientations": orients}
    if args.marker:
        meta["marker"] = args.marker
    p = GOALS / args.year / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm.render(meta, FIELD_ORDER, quote_keys={"marker"})
                 + "\n\n" + default_body(slug))
    _ok({"created": slug, "path": str(p)}, f"created goal {slug} ({args.year})", args.json)


def cmd_set(args):
    p = find_path(args.slug)
    if not p:
        _fail(f"no goal named '{args.slug}'", args.json)
        return
    updates = {}
    if args.status is not None:
        updates["status"] = args.status
    if args.marker is not None:
        updates["marker"] = args.marker
    if args.orientations is not None:
        orients = _csv(args.orientations)
        bad = _bad_orients(orients)
        if bad:
            _fail(f"unknown orientations {bad}", args.json)
            return
        updates["orientations"] = orients
    new_slug = args.new_name
    if new_slug is not None:
        if not SLUG_RE.match(new_slug):
            _fail(f"name '{new_slug}' must be lowercase-kebab (a-z0-9-)", args.json)
            return
        updates["goal"] = new_slug
    new_year = args.year
    if new_year is not None:
        if not YEAR_RE.match(new_year):
            _fail(f"year '{new_year}' must be YYYY", args.json)
            return
        updates["year"] = new_year
    if not updates:
        _fail("nothing to change", args.json)
        return
    fm.set_fields(p, updates, quote_keys={"marker"})
    # move file if slug or year changed
    dest_year = new_year or p.parent.name
    dest_slug = new_slug or p.stem
    dest = GOALS / dest_year / f"{dest_slug}.md"
    if dest != p:
        if dest.exists():
            _fail(f"target {dest} already exists", args.json)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        p.rename(dest)
        p = dest
    _ok({"updated": dest_slug, "path": str(p)}, f"updated {dest_slug}", args.json)


def cmd_rm(args):
    p = find_path(args.slug)
    if not p:
        _fail(f"no goal named '{args.slug}'", args.json)
        return
    if not args.force and not args.json:
        if input(f"  type the slug to delete goal '{args.slug}': ").strip() != args.slug:
            console.print("  aborted.")
            return
    p.unlink(missing_ok=True)
    _ok({"deleted": args.slug, "path": str(p)}, f"deleted {args.slug}", args.json)


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


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


def build_parser():
    p = argparse.ArgumentParser(prog="cl goals", description="goal editor")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("list"); lp.add_argument("--json", action="store_true")
    shp = sub.add_parser("show"); shp.add_argument("slug"); shp.add_argument("--json", action="store_true")

    np = sub.add_parser("new")
    np.add_argument("--goal", required=True); np.add_argument("--year", required=True)
    np.add_argument("--status"); np.add_argument("--marker"); np.add_argument("--orientations")
    np.add_argument("--json", action="store_true")

    stp = sub.add_parser("set")
    stp.add_argument("slug")
    stp.add_argument("--name", dest="new_name", help="rename (new slug)")
    stp.add_argument("--year"); stp.add_argument("--status")
    stp.add_argument("--marker"); stp.add_argument("--orientations")
    stp.add_argument("--json", action="store_true")

    rp = sub.add_parser("rm")
    rp.add_argument("slug"); rp.add_argument("--force", action="store_true")
    rp.add_argument("--json", action="store_true")
    return p


def main():
    p = build_parser()
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        args = p.parse_args(["list"] + argv)
    else:
        args = p.parse_args(argv)
    {"list": cmd_list, "show": cmd_show, "new": cmd_new,
     "set": cmd_set, "rm": cmd_rm}.get(args.cmd, cmd_list)(args)


if __name__ == "__main__":
    main()
