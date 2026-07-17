"""orientations.py — `cl orientations` — orientation editor.

CRUD over the life *orientations* that live as markdown in
`~/kb/orientations/<slug>.md`. Orientations are the top of the feeding chain
(orientation ← goal ← system ← block): the enduring directions everything else
serves. The frontmatter is tiny (`orientation`, `status`); this command lets
Surface and the shell create, retitle, restatus, and retire them without
hand-editing. The prose body stays in nvim.

  cl orientations [list] [--json]     every orientation (name + status)
  cl orientations show NAME [--json]  one orientation's fields + what feeds it
  cl orientations new --name SLUG [--status active]
  cl orientations set NAME [--name NEW-SLUG] [--status S]
  cl orientations rm NAME [--force]

Renaming moves the file; references in systems/goals are NOT auto-repointed
(re-point them in those editors). Names are globally unique kebab slugs.
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
ORIENTATIONS = KB / "orientations"
SYSTEMS = week.SYSTEMS
GOALS = KB / "goals"

FIELD_ORDER = ["orientation", "status"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_STATUS = "active"


def path_for(slug):
    return ORIENTATIONS / f"{slug}.md"


def all_orientations():
    out = []
    if ORIENTATIONS.exists():
        for f in sorted(ORIENTATIONS.glob("*.md")):
            m = fm.read(f)
            out.append({"orientation": m.get("orientation") or f.stem,
                        "status": m.get("status", "active"),
                        "slug": f.stem})
    return out


def _fed_by(name):
    """Blocks + goals that declare this orientation in their feeding chain.
    Post-flatten, blocks own the chain directly (no systems layer)."""
    blocks, goals = [], []
    for _, m, _ in week.load_blocks():
        if name in (m.get("orientations") or []):
            blocks.append(m.get("block"))
    if GOALS.exists():
        for f in sorted(GOALS.rglob("*.md")):
            if name in fm.read(f).get("orientations", []):
                goals.append(fm.read(f).get("goal") or f.stem)
    return {"blocks": sorted(blocks), "goals": goals}


def default_body(slug):
    title = slug.replace("-", " ").title()
    return (f"# {title}\n\n"
            "## What this means\n\n"
            "## Why this orientation\n\n"
            "## What feeds this\n")


def cmd_list(args):
    items = all_orientations()
    if args.json:
        print(json.dumps({"orientations": items}))
        return
    for o in items:
        dim = "" if o["status"] == "active" else " [dim](%s)[/dim]" % o["status"]
        console.print(f"  {o['orientation']}{dim}")


def cmd_show(args):
    p = path_for(args.name)
    if not p.exists():
        _fail(f"no orientation named '{args.name}'", args.json)
        return
    m = fm.read(p)
    out = {"orientation": m.get("orientation") or args.name,
           "status": m.get("status", "active"),
           "path": str(p), "fed_by": _fed_by(args.name)}
    if args.json:
        print(json.dumps(out))
        return
    console.print(f"[bold]{out['orientation']}[/bold]  ({out['status']})")
    console.print(f"  fed by blocks   {', '.join(out['fed_by']['blocks']) or '—'}")
    console.print(f"  fed by goals    {', '.join(out['fed_by']['goals']) or '—'}")


def cmd_new(args):
    slug = args.name
    if not SLUG_RE.match(slug or ""):
        _fail(f"name '{slug}' must be lowercase-kebab (a-z0-9-)", args.json)
        return
    p = path_for(slug)
    if p.exists():
        _fail(f"orientation '{slug}' already exists", args.json)
        return
    meta = {"orientation": slug, "status": args.status or DEFAULT_STATUS}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm.render(meta, FIELD_ORDER) + "\n\n" + default_body(slug))
    _ok({"created": slug, "path": str(p)}, f"created orientation {slug}", args.json)


def cmd_set(args):
    p = path_for(args.name)
    if not p.exists():
        _fail(f"no orientation named '{args.name}'", args.json)
        return
    updates = {}
    if args.status is not None:
        updates["status"] = args.status
    new_slug = None
    if args.new_name is not None:
        new_slug = args.new_name
        if not SLUG_RE.match(new_slug):
            _fail(f"name '{new_slug}' must be lowercase-kebab (a-z0-9-)", args.json)
            return
        if new_slug != args.name and path_for(new_slug).exists():
            _fail(f"orientation '{new_slug}' already exists", args.json)
            return
        updates["orientation"] = new_slug
    if not updates:
        _fail("nothing to change (pass --status and/or --name)", args.json)
        return
    fm.set_fields(p, updates)
    if new_slug and new_slug != args.name:
        p.rename(path_for(new_slug))
        p = path_for(new_slug)
    warn = " (references in systems/goals not auto-repointed)" if new_slug and new_slug != args.name else ""
    _ok({"updated": new_slug or args.name, "path": str(p)},
        f"updated {new_slug or args.name}{warn}", args.json)


def cmd_rm(args):
    p = path_for(args.name)
    if not p.exists():
        _fail(f"no orientation named '{args.name}'", args.json)
        return
    fed = _fed_by(args.name)
    if (fed["blocks"] or fed["goals"]) and not args.force:
        refs = ", ".join(fed["blocks"] + fed["goals"])
        _fail(f"still fed by: {refs} — pass --force to delete anyway", args.json)
        return
    if not args.force and not args.json:
        if input(f"  type the name to delete '{args.name}': ").strip() != args.name:
            console.print("  aborted.")
            return
    p.unlink(missing_ok=True)
    _ok({"deleted": args.name, "path": str(p)}, f"deleted {args.name}", args.json)


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
    p = argparse.ArgumentParser(prog="cl orientations", description="orientation editor")
    sub = p.add_subparsers(dest="cmd")

    lp = sub.add_parser("list"); lp.add_argument("--json", action="store_true")
    shp = sub.add_parser("show"); shp.add_argument("name"); shp.add_argument("--json", action="store_true")
    np = sub.add_parser("new")
    np.add_argument("--name", required=True); np.add_argument("--status")
    np.add_argument("--json", action="store_true")
    stp = sub.add_parser("set")
    stp.add_argument("name"); stp.add_argument("--name", dest="new_name", help="rename (new slug)")
    stp.add_argument("--status"); stp.add_argument("--json", action="store_true")
    rp = sub.add_parser("rm")
    rp.add_argument("name"); rp.add_argument("--force", action="store_true")
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
