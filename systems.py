"""systems.py — `cl systems` — system (routine) editor.

CRUD over the *systems* that live as `~/kb/systems/<slug>/system.md`. A system
is a routine that owns one or more blocks; its frontmatter declares `status`
and the feeding chain it serves (`goals`, `orientations`). This command edits
those fields, scaffolds new systems, and retires or deletes them. Block CRUD
stays in `cl blocks`; the feeding chain is editable from both (`cl blocks feed`
is the same write).

  cl systems [list] [--json]          every system (status, feeding, #blocks)
  cl systems show SLUG [--json]       one system's fields + its blocks
  cl systems new --system SLUG [--status active] [--goals a,b] [--orientations x,y]
  cl systems set SLUG [--status S] [--goals ...] [--orientations ...]
                       [--superseded-by SLUG] [--superseded-on YYYY-MM-DD]
  cl systems rm SLUG [--force]        delete the whole system tree (blocks too!)

`--goals`/`--orientations` REPLACE the list (comma-separated, or empty to
clear). Only `status: active` systems are live in the planner/dashboard; set a
non-active status (e.g. superseded) to retire a routine without deleting it.
Renaming a system slug is intentionally not supported here (it cascades to every
block's `parent` and the folder name).
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from rich.console import Console

import fm
import week

console = Console()

KB = week.KB
SYSTEMS = week.SYSTEMS
GOALS = KB / "goals"
ORIENTATIONS = KB / "orientations"

FIELD_ORDER = ["system", "status", "goals", "orientations",
               "superseded_by", "superseded_on"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_STATUS = "active"


def sys_path(slug):
    return SYSTEMS / slug / "system.md"


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def block_names(slug):
    d = SYSTEMS / slug / "blocks"
    if not d.exists():
        return []
    return sorted(f.stem for f in d.glob("*.md"))


def known_goals():
    out = []
    if GOALS.exists():
        for f in sorted(GOALS.rglob("*.md")):
            out.append(fm.read(f).get("goal") or f.stem)
    return out


def known_orientations():
    out = []
    if ORIENTATIONS.exists():
        for f in sorted(ORIENTATIONS.glob("*.md")):
            out.append(fm.read(f).get("orientation") or f.stem)
    return out


def system_row(slug):
    m = fm.read(sys_path(slug))
    return {
        "system": m.get("system") or slug,
        "status": m.get("status", "active"),
        "goals": m.get("goals") or [],
        "orientations": m.get("orientations") or [],
        "superseded_by": m.get("superseded_by", ""),
        "superseded_on": m.get("superseded_on", ""),
        "blocks": block_names(slug),
    }


def all_systems():
    out = []
    if SYSTEMS.exists():
        for d in sorted(SYSTEMS.iterdir()):
            if d.is_dir() and (d / "system.md").exists():
                out.append(system_row(d.name))
    return out


def default_body(slug):
    title = slug.replace("-", " ").title()
    return (f"# {title}\n\n"
            "## Why it exists\n\n"
            "## Blocks\n\n"
            "## Notes\n")


def _validate_refs(goals, orientations, as_json):
    kg, ko = set(known_goals()), set(known_orientations())
    bad_g = [g for g in (goals or []) if g not in kg]
    bad_o = [o for o in (orientations or []) if o not in ko]
    errs = []
    if bad_g:
        errs.append(f"unknown goals {bad_g}")
    if bad_o:
        errs.append(f"unknown orientations {bad_o}")
    return errs


def cmd_list(args):
    items = all_systems()
    if args.json:
        print(json.dumps({"systems": items,
                          "goals": known_goals(),
                          "orientations": known_orientations()}))
        return
    for s in items:
        badge = "" if s["status"] == "active" else f" [dim]({s['status']})[/dim]"
        feed = ", ".join(s["goals"] + s["orientations"]) or "—"
        console.print(f"  {s['system']:28}{badge}  {len(s['blocks'])} blk  → {feed}")


def cmd_show(args):
    p = sys_path(args.slug)
    if not p.exists():
        _fail(f"no system named '{args.slug}'", args.json)
        return
    row = system_row(args.slug)
    row["path"] = str(p)
    if args.json:
        print(json.dumps(row))
        return
    console.print(f"[bold]{row['system']}[/bold]  ({row['status']})")
    console.print(f"  goals          {', '.join(row['goals']) or '—'}")
    console.print(f"  orientations   {', '.join(row['orientations']) or '—'}")
    console.print(f"  blocks         {', '.join(row['blocks']) or '—'}")
    if row["superseded_by"]:
        console.print(f"  superseded_by  {row['superseded_by']} ({row['superseded_on'] or '?'})")


def cmd_new(args):
    slug = args.system
    if not SLUG_RE.match(slug or ""):
        _fail(f"system '{slug}' must be lowercase-kebab (a-z0-9-)", args.json)
        return
    if (SYSTEMS / slug).exists():
        _fail(f"system '{slug}' already exists", args.json)
        return
    goals = _csv(args.goals) if args.goals is not None else []
    orients = _csv(args.orientations) if args.orientations is not None else []
    errs = _validate_refs(goals, orients, args.json)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    meta = {"system": slug, "status": args.status or DEFAULT_STATUS,
            "goals": goals, "orientations": orients}
    p = sys_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    (SYSTEMS / slug / "blocks").mkdir(exist_ok=True)
    p.write_text(fm.render(meta, FIELD_ORDER) + "\n\n" + default_body(slug))
    _ok({"created": slug, "path": str(p)}, f"created system {slug}", args.json)


def cmd_set(args):
    p = sys_path(args.slug)
    if not p.exists():
        _fail(f"no system named '{args.slug}'", args.json)
        return
    updates = {}
    if args.status is not None:
        updates["status"] = args.status
    if args.goals is not None:
        updates["goals"] = _csv(args.goals)
    if args.orientations is not None:
        updates["orientations"] = _csv(args.orientations)
    if args.superseded_by is not None:
        updates["superseded_by"] = args.superseded_by
    if args.superseded_on is not None:
        if args.superseded_on and not DATE_RE.match(args.superseded_on):
            _fail(f"superseded_on '{args.superseded_on}' must be YYYY-MM-DD", args.json)
            return
        updates["superseded_on"] = args.superseded_on
    if not updates:
        _fail("nothing to change", args.json)
        return
    errs = _validate_refs(updates.get("goals"), updates.get("orientations"), args.json)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    fm.set_fields(p, updates)
    _ok({"updated": args.slug, "path": str(p)}, f"updated {args.slug}", args.json)


def cmd_rm(args):
    d = SYSTEMS / args.slug
    if not (d / "system.md").exists():
        _fail(f"no system named '{args.slug}'", args.json)
        return
    blks = block_names(args.slug)
    if blks and not args.force:
        _fail(f"has {len(blks)} block(s): {', '.join(blks)} — their streaks reset. "
              f"pass --force to delete the whole system", args.json)
        return
    if not args.force and not args.json:
        if input(f"  type the slug to delete system '{args.slug}': ").strip() != args.slug:
            console.print("  aborted.")
            return
    shutil.rmtree(d)
    _ok({"deleted": args.slug, "blocks_deleted": blks, "path": str(d)},
        f"deleted system {args.slug} ({len(blks)} block(s))", args.json)


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
    p = argparse.ArgumentParser(prog="cl systems", description="system editor")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("list"); lp.add_argument("--json", action="store_true")
    shp = sub.add_parser("show"); shp.add_argument("slug"); shp.add_argument("--json", action="store_true")

    np = sub.add_parser("new")
    np.add_argument("--system", required=True)
    np.add_argument("--status"); np.add_argument("--goals"); np.add_argument("--orientations")
    np.add_argument("--json", action="store_true")

    stp = sub.add_parser("set")
    stp.add_argument("slug")
    stp.add_argument("--status"); stp.add_argument("--goals"); stp.add_argument("--orientations")
    stp.add_argument("--superseded-by", dest="superseded_by")
    stp.add_argument("--superseded-on", dest="superseded_on")
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
