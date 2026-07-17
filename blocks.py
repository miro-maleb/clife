"""blocks.py — `cl blocks` — routine block editor.

CRUD over the routine block *definitions* that live as markdown in
`~/kb/systems/<system>/blocks/<block>.md`. These are the source of truth for
`cl week` placement and `cl habits` streaks; this command lets you edit their
default settings, create new daily/weekly blocks, and delete them without
hand-editing frontmatter.

  cl blocks [list] [--json]        list every block (table, or JSON envelope
                                   with blocks + systems + goals + orientations)
  cl blocks show BLOCK [--json]    one block's settings + its feeding chain
  cl blocks new --block SLUG --parent SYSTEM --cadence daily|weekly [opts]
  cl blocks set BLOCK [opts]       edit fields on an existing block
  cl blocks rm BLOCK [--force]     delete a block file
  cl blocks feed SYSTEM [--goals a,b] [--orientations x,y]
                                   edit which goals/orientations a system feeds
  cl blocks meta [--json]          systems + goals + orientations (pickers)
  cl blocks calendars [--json]     writable gcal calendar names (slow — gcalcli)

Field opts (new/set): --calendar NAME, --cadence daily|weekly, --days mon,tue
(or 'all' to clear → every day), --duration 30m|2h, --start HH:MM, --instances N,
--habit true|false, --name NEW-SLUG (rename), --parent SYSTEM (re-home).

Block name == file stem == gcal title key; renaming resets its streak (the
review DB is keyed by title). All block names are globally unique.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

import week

console = Console()

KB = week.KB
SYSTEMS = week.SYSTEMS
GOALS = KB / "goals"
ORIENTATIONS = KB / "orientations"
DAYS = week.DAYS

# Canonical frontmatter order for a block file. Unknown keys are preserved and
# appended after these. `habit` is only written when False (anchor); tracked is
# the default, so we keep those files clean by omitting it.
FIELD_ORDER = ["block", "parent", "calendar", "cadence", "habit",
               "days", "duration", "instances", "default_start", "travel",
               "status", "goals", "orientations"]

CADENCES = ("daily", "weekly")
from paths import DEFAULT_CALENDAR


# ── file read / write ────────────────────────────────────────────────────────

def read_block(path):
    """Return (meta, body). meta via week's tolerant parser; body is the prose
    after the closing frontmatter fence, preserved verbatim."""
    text = path.read_text()
    meta = week.parse_frontmatter(path)
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
    return meta, body


def _render_scalar(key, value):
    if isinstance(value, list):
        return "[" + ", ".join(str(x) for x in value) + "]"
    s = str(value)
    if key == "default_start":            # HH:MM must be quoted or YAML reads it as time
        return f'"{s}"'
    return s


def render_frontmatter(meta):
    lines = ["---"]
    seen = set()
    for k in FIELD_ORDER:
        if k in meta and meta[k] not in (None, "", []):
            lines.append(f"{k}: {_render_scalar(k, meta[k])}")
            seen.add(k)
    for k, v in meta.items():             # preserve any extra keys we don't model
        if k in seen or v in (None, "", []):
            continue
        lines.append(f"{k}: {_render_scalar(k, v)}")
    lines.append("---")
    return "\n".join(lines)


def write_block(path, meta, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = render_frontmatter(meta)
    body = (body or "").strip("\n")
    path.write_text(fm + "\n\n" + body + "\n" if body else fm + "\n")


HABITS = week.HABITS


def block_path(meta):
    """Where this block's file lives. The flat layout (KB/habits/<block>.md) is
    canonical; the legacy nested path is honored only if that file still exists
    (an unmigrated tenant). New blocks land flat."""
    name = meta["block"]
    flat = HABITS / (name + ".md")
    if flat.exists():
        return flat
    parent = meta.get("parent")
    if parent:
        nested = SYSTEMS / parent / "blocks" / (name + ".md")
        if nested.exists():
            return nested
    return flat


def default_body(slug):
    title = slug.replace("-", " ").title()
    return (f"# {title}\n\n"
            "## What this block is\n\n"
            "## \"Done\" looks like\n\n"
            "## Notes\n")


# ── lookups ──────────────────────────────────────────────────────────────────

def find_block(name):
    """Return (sys_slug, meta, sys_status, path) or (None, None, None, None)."""
    for sys_slug, meta, sys_status in week.load_blocks():
        if meta.get("block") == name:
            return sys_slug, meta, sys_status, block_path(meta)
    return None, None, None, None


def system_meta(slug):
    sf = SYSTEMS / slug / "system.md"
    return week.parse_frontmatter(sf) if sf.exists() else None


def feeding_for(parent):
    m = system_meta(parent) or {}
    return {
        "system": parent,
        "system_status": m.get("status", "active"),
        "goals": _aslist(m.get("goals")),
        "orientations": _aslist(m.get("orientations")),
    }


def all_systems():
    out = []
    if not SYSTEMS.exists():
        return out
    for d in sorted(SYSTEMS.iterdir()):
        sf = d / "system.md"
        if d.is_dir() and sf.exists():
            m = week.parse_frontmatter(sf)
            out.append({"system": d.name, "status": m.get("status", "active"),
                        "goals": _aslist(m.get("goals")),
                        "orientations": _aslist(m.get("orientations"))})
    return out


def all_goals():
    out = []
    if GOALS.exists():
        for f in sorted(GOALS.rglob("*.md")):
            m = week.parse_frontmatter(f)
            out.append(m.get("goal") or f.stem)
    return out


def all_orientations():
    out = []
    if ORIENTATIONS.exists():
        for f in sorted(ORIENTATIONS.glob("*.md")):
            m = week.parse_frontmatter(f)
            out.append(m.get("orientation") or f.stem)
    return out


def _aslist(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def block_dict(sys_slug, meta, sys_status):
    return {
        "block": meta.get("block"),
        "system": sys_slug,
        "parent": meta.get("parent", sys_slug),
        "system_status": sys_status,
        "calendar": meta.get("calendar", ""),
        "cadence": meta.get("cadence", ""),
        "habit": week.is_habit(meta),
        "days": _aslist(meta.get("days")),
        "duration": meta.get("duration", ""),
        "duration_min": week.parse_duration_minutes(meta.get("duration", "")) or 0,
        "instances": int(meta.get("instances", 1) or 1),
        "default_start": meta.get("default_start", ""),
        "travel": meta.get("travel", ""),   # "pause" = skip on Travel-calendar days
        # feeding chain — enriched onto meta by week.load_blocks(), whatever the
        # layout (block's own keys when flat, parent system's when nested)
        "goals": _aslist(meta.get("goals")),
        "orientations": _aslist(meta.get("orientations")),
    }


# ── validation ───────────────────────────────────────────────────────────────

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def validate(meta, *, is_new, original_name=None):
    """Return a list of human-readable error strings ([] == valid)."""
    errs = []
    name = meta.get("block", "")
    if not SLUG_RE.match(name or ""):
        errs.append(f"block name '{name}' must be lowercase-kebab (a-z0-9-)")
    # Flat blocks (KB/habits/) have no parent. Only validate a parent when one is
    # given — i.e. a legacy nested tenant still authoring under systems/.
    parent = meta.get("parent", "")
    if parent and not (SYSTEMS / parent / "system.md").exists():
        errs.append(f"parent system '{parent}' not found under ~/kb/systems/")
    cadence = meta.get("cadence", "")
    if cadence not in CADENCES:
        errs.append(f"cadence must be one of {CADENCES}, got '{cadence}'")
    days = _aslist(meta.get("days"))
    bad = [d for d in days if d not in DAYS]
    if bad:
        errs.append(f"unknown days {bad} (use {DAYS})")
    if cadence == "weekly" and days:
        errs.append("weekly blocks don't take days")
    dur = meta.get("duration", "")
    if dur and week.parse_duration_minutes(dur) is None:
        errs.append(f"duration '{dur}' unparseable (use 30m / 90m / 2h)")
    try:
        if int(meta.get("instances", 1) or 1) < 1:
            errs.append("instances must be >= 1")
    except (ValueError, TypeError):
        errs.append(f"instances '{meta.get('instances')}' not an integer")
    start = meta.get("default_start", "")
    if start and not TIME_RE.match(str(start)):
        errs.append(f"default_start '{start}' must be HH:MM (24h)")
    # uniqueness of the name (globally) — only if new or renamed
    if is_new or (original_name and name != original_name):
        existing = {m.get("block") for _, m, _ in week.load_blocks()}
        if name in existing:
            errs.append(f"a block named '{name}' already exists")
    return errs


def apply_fields(meta, args):
    """Mutate meta from parsed --field flags. Returns the (possibly new) name."""
    if args.calendar is not None:
        meta["calendar"] = args.calendar
    if args.cadence is not None:
        meta["cadence"] = args.cadence
    if args.duration is not None:
        meta["duration"] = args.duration
    if args.start is not None:
        meta["default_start"] = args.start
    if args.instances is not None:
        meta["instances"] = str(args.instances)
    if args.parent is not None:
        meta["parent"] = args.parent
    if args.days is not None:
        d = args.days.strip().lower()
        if d in ("", "all", "every", "everyday", "daily"):
            meta.pop("days", None)          # omit == every day
        else:
            meta["days"] = [x.strip() for x in d.split(",") if x.strip()]
    if args.habit is not None:
        if args.habit.strip().lower() in ("false", "no", "0", "off"):
            meta["habit"] = "false"         # anchor — kept out of habit tracking
        else:
            meta.pop("habit", None)         # tracked is the default; keep file clean
    if args.travel is not None:
        if args.travel.strip().lower() in ("pause", "skip"):
            meta["travel"] = "pause"
        else:                                    # keep/none/"" → clear (keep is the default)
            meta.pop("travel", None)
    if args.name is not None:
        meta["block"] = args.name
    return meta.get("block")


# ── system.md "## Blocks" bullet sync (best-effort) ──────────────────────────

def _sync_system_bullet(system, slug, add):
    """Insert/remove a `- [slug](blocks/slug.md)` bullet in a system's
    `## Blocks` section. Best-effort — never fatal."""
    try:
        sf = SYSTEMS / system / "system.md"
        if not sf.exists():
            return
        lines = sf.read_text().splitlines()
        bullet_re = re.compile(rf"^\s*-\s*\[{re.escape(slug)}\]\(blocks/{re.escape(slug)}\.md\)")
        if add:
            if any(bullet_re.match(l) for l in lines):
                return
            # find the ## Blocks heading, insert after its list (first blank after it)
            for i, l in enumerate(lines):
                if l.strip().lower().startswith("## blocks"):
                    j = i + 1
                    while j < len(lines) and lines[j].strip().startswith("- "):
                        j += 1
                    lines.insert(j, f"- [{slug}](blocks/{slug}.md)")
                    sf.write_text("\n".join(lines) + "\n")
                    return
        else:
            kept = [l for l in lines if not bullet_re.match(l)]
            if len(kept) != len(lines):
                sf.write_text("\n".join(kept) + "\n")
    except Exception:
        pass


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_list(args):
    blocks = [block_dict(s, m, st) for s, m, st in week.load_blocks()]
    blocks.sort(key=lambda b: (b["cadence"] != "daily", b["system"], b["block"]))
    if args.json:
        print(json.dumps({
            "blocks": blocks,
            "systems": all_systems(),
            "goals": all_goals(),
            "orientations": all_orientations(),
        }))
        return
    t = Table(show_header=True, header_style="bold")
    for c in ("block", "system", "cad", "days", "dur", "start", "inst", "kind"):
        t.add_column(c)
    for b in blocks:
        days = "all" if not b["days"] else ",".join(b["days"])
        kind = "habit" if b["habit"] else "anchor"
        style = None if b["system_status"] == "active" else "dim"
        t.add_row(b["block"], b["system"],
                  b["cadence"][:1].upper(), days if b["cadence"] == "daily" else "—",
                  b["duration"] or "—", b["default_start"] or "—",
                  str(b["instances"]), kind, style=style)
    console.print(t)


def cmd_show(args):
    s, meta, st, path = find_block(args.block)
    if not meta:
        _fail(f"no block named '{args.block}'", args.json)
        return
    bd = block_dict(s, meta, st)
    bd["feeding"] = {"goals": bd["goals"], "orientations": bd["orientations"]}
    bd["path"] = str(path)
    if args.json:
        print(json.dumps(bd))
        return
    console.print(f"[bold]{bd['block']}[/bold]  ({bd['system']}, {st})")
    for k in ("calendar", "cadence", "days", "duration", "default_start",
              "instances"):
        v = bd[k]
        console.print(f"  {k:15} {(', '.join(v) if isinstance(v, list) else v) or '—'}")
    console.print(f"  {'kind':15} {'habit (tracked)' if bd['habit'] else 'anchor'}")
    f = bd["feeding"]
    console.print(f"  feeds goals    {', '.join(f['goals']) or '—'}")
    console.print(f"  feeds orient.  {', '.join(f['orientations']) or '—'}")


def cmd_new(args):
    meta = {
        "block": args.block,
        "calendar": args.calendar or DEFAULT_CALENDAR,
        "cadence": args.cadence,
        "duration": args.duration or "30m",
        "instances": str(args.instances or 1),
        "status": "active",
    }
    if args.parent:                           # legacy nested tenant only
        meta["parent"] = args.parent
    if args.start:
        meta["default_start"] = args.start
    if args.days and args.cadence == "daily":
        d = args.days.strip().lower()
        if d not in ("", "all", "every", "everyday", "daily"):
            meta["days"] = [x.strip() for x in d.split(",") if x.strip()]
    if args.habit and args.habit.strip().lower() in ("false", "no", "0", "off"):
        meta["habit"] = "false"
    errs = validate(meta, is_new=True)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    path = block_path(meta)
    if path.exists():
        _fail(f"{path} already exists", args.json)
        return
    write_block(path, meta, default_body(meta["block"]))
    if meta.get("parent"):
        _sync_system_bullet(meta["parent"], meta["block"], add=True)
    _ok({"created": meta["block"], "path": str(path)},
        f"created {meta['block']} → {path}", args.json)


def cmd_set(args):
    s, meta, st, path = find_block(args.block)
    if not meta:
        _fail(f"no block named '{args.block}'", args.json)
        return
    old_name, old_parent = meta.get("block"), meta.get("parent")
    _, body = read_block(path)
    new_name = apply_fields(meta, args)
    errs = validate(meta, is_new=False, original_name=old_name)
    if errs:
        _fail("; ".join(errs), args.json)
        return
    new_path = block_path(meta)
    write_block(new_path, meta, body)
    if new_path != path:                      # renamed or re-homed: drop the old file
        path.unlink(missing_ok=True)
        if old_parent:
            _sync_system_bullet(old_parent, old_name, add=False)
        if meta.get("parent"):
            _sync_system_bullet(meta["parent"], meta["block"], add=True)
    warn = ""
    if new_name != old_name:
        warn = " (name changed — its habit streak resets, gcal title contract shifts)"
    _ok({"updated": meta["block"], "path": str(new_path), "renamed_from": old_name},
        f"updated {meta['block']}{warn}", args.json)


def cmd_rm(args):
    s, meta, st, path = find_block(args.block)
    if not meta:
        _fail(f"no block named '{args.block}'", args.json)
        return
    if not args.force and not args.json:
        console.print(f"[yellow]delete[/yellow] {path} ?  (block '{args.block}', {st})")
        if input("  type the block name to confirm: ").strip() != args.block:
            console.print("  aborted.")
            return
    path.unlink(missing_ok=True)
    _sync_system_bullet(meta.get("parent"), args.block, add=False)
    _ok({"deleted": args.block, "path": str(path)},
        f"deleted {args.block}", args.json)


def cmd_feed(args):
    sf = SYSTEMS / args.system / "system.md"
    if not sf.exists():
        _fail(f"no system named '{args.system}'", args.json)
        return
    text = sf.read_text()
    meta = week.parse_frontmatter(sf)
    updates = {}
    if args.goals is not None:
        updates["goals"] = _csv(args.goals)
    if args.orientations is not None:
        updates["orientations"] = _csv(args.orientations)
    if not updates:
        _fail("nothing to change (pass --goals and/or --orientations)", args.json)
        return
    text = _rewrite_fm_list(text, updates)
    sf.write_text(text)
    _ok({"system": args.system, **{k: updates[k] for k in updates}},
        f"updated feeding for {args.system}", args.json)


def cmd_meta(args):
    payload = {"systems": all_systems(), "goals": all_goals(),
               "orientations": all_orientations()}
    if args.json:
        print(json.dumps(payload))
        return
    console.print("[bold]systems[/bold]")
    for s in payload["systems"]:
        console.print(f"  {s['system']:28} {s['status']:11} "
                      f"goals={','.join(s['goals']) or '—'} "
                      f"orient={','.join(s['orientations']) or '—'}")
    console.print(f"[bold]goals[/bold]  {', '.join(payload['goals'])}")
    console.print(f"[bold]orientations[/bold]  {', '.join(payload['orientations'])}")


def cmd_calendars(args):
    try:
        cals = week.list_calendars(access=("owner", "writer"))
    except Exception as e:
        _fail(f"gcalcli calendar list failed: {e}", args.json)
        return
    cals = [c for c in cals if c not in week.EXCLUDE_CALENDARS]
    if args.json:
        print(json.dumps({"calendars": cals}))
        return
    for c in cals:
        console.print(f"  {c}")


# ── frontmatter list rewrite (for system.md feeding edits) ───────────────────

def _rewrite_fm_list(text, updates):
    """Replace inline `key: [...]` lists in the frontmatter block. Adds the key
    at the end of frontmatter if absent. Only touches the top fence."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    end = None
    for i, l in enumerate(lines[1:], start=1):
        if l.strip() == "---":
            end = i
            break
    if end is None:
        return text
    remaining = dict(updates)
    for i in range(1, end):
        k = lines[i].split(":", 1)[0].strip()
        if k in remaining:
            lines[i] = f"{k}: [{', '.join(remaining.pop(k))}]"
    for k, vals in remaining.items():         # keys not present — insert before fence
        lines.insert(end, f"{k}: [{', '.join(vals)}]")
        end += 1
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


# ── output helpers ───────────────────────────────────────────────────────────

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


# ── entry ────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="cl blocks", description="routine block editor")
    sub = p.add_subparsers(dest="cmd")

    def add_field_opts(sp):
        sp.add_argument("--calendar")
        sp.add_argument("--cadence", choices=CADENCES)
        sp.add_argument("--days", help="comma list (mon,tue) or 'all' to clear")
        sp.add_argument("--duration")
        sp.add_argument("--start", help="HH:MM default start")
        sp.add_argument("--instances", type=int)
        sp.add_argument("--habit", help="true|false (false = calendar anchor)")
        sp.add_argument("--travel", help="pause|keep — pause = skip this habit on Travel-calendar days")

    lp = sub.add_parser("list")
    lp.add_argument("--json", action="store_true")

    shp = sub.add_parser("show")
    shp.add_argument("block")
    shp.add_argument("--json", action="store_true")

    np = sub.add_parser("new")
    np.add_argument("--block", required=True)
    np.add_argument("--parent", required=True)
    add_field_opts(np)
    np.add_argument("--json", action="store_true")

    stp = sub.add_parser("set")
    stp.add_argument("block")
    stp.add_argument("--name", help="rename (new slug)")
    stp.add_argument("--parent", help="re-home to another system")
    add_field_opts(stp)
    stp.add_argument("--json", action="store_true")

    rp = sub.add_parser("rm")
    rp.add_argument("block")
    rp.add_argument("--force", action="store_true")
    rp.add_argument("--json", action="store_true")

    fp = sub.add_parser("feed")
    fp.add_argument("system")
    fp.add_argument("--goals", help="comma list (replaces)")
    fp.add_argument("--orientations", help="comma list (replaces)")
    fp.add_argument("--json", action="store_true")

    mp = sub.add_parser("meta")
    mp.add_argument("--json", action="store_true")

    cp = sub.add_parser("calendars")
    cp.add_argument("--json", action="store_true")
    return p


def main():
    p = build_parser()
    # bare `cl blocks` and `cl blocks --json` default to list
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        args = p.parse_args(["list"] + argv)
    else:
        args = p.parse_args(argv)
    dispatch = {
        "list": cmd_list, "show": cmd_show, "new": cmd_new, "set": cmd_set,
        "rm": cmd_rm, "feed": cmd_feed, "meta": cmd_meta, "calendars": cmd_calendars,
    }
    (dispatch.get(args.cmd) or cmd_list)(args)


if __name__ == "__main__":
    main()
