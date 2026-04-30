"""
kb_migrate.py — one-shot migration of ~/kb/projects/ to CLIfe schema.

Idempotent. Safe to run multiple times.

Performs:
  1. Renames 6 life-os/* project.md → sub-project.md with sub-project schema
  2. Adds missing frontmatter fields to all area/project/sub-project files
  3. Adds depends_on to robocart sub-projects from parent-table mapping
  4. Writes proper frontmatter for coding/area.md
  5. Normalizes 'on hold' → 'on-hold'
"""

import sys
from pathlib import Path

KB = Path.home() / "kb"
PROJECTS = KB / "projects"

SKIP_NAMES = {"__pycache__", "node_modules", "venv", ".venv", ".git"}

PROJECT_FIELDS = ["created", "deadline", "status", "completed", "abandoned",
                  "sleeping", "last_reviewed", "area", "tags"]
AREA_FIELDS = ["created", "status", "tags"]
SUBPROJECT_FIELDS = ["created", "status", "depends_on"]

DEFAULT_CREATED = "2026-04-29"

LIFE_OS_SUBS = ["claude-optimization", "core-scripts", "mobile-phase-1",
                "remote-server", "tui", "voice-pipeline"]

# Robocart sub-project deps from parent project.md table
ROBOCART_DEPS = {
    "01-chassis-and-frame":              "[]",
    "02-drive-system":                   "[01]",
    "03-power-system":                   "[02]",
    "04-control-electronics":            "[02, 03]",
    "05-beacon-system":                  "[]",
    "06-following-software":             "[04, 05]",
    "07-collision-avoidance":            "[04, 06]",
    "08-manual-control-mode":            "[04]",
    "09-safety-systems":                 "[]",
    "10-integration-and-prototyping":    "[01, 02, 03, 04, 05, 06, 07, 08, 09]",
    "11-enclosure-and-weatherproofing":  "[10]",
    "12-branding-and-marketing":         "[10]",
    "13-sales-and-business":             "[11, 12]",
}


def parse_frontmatter(text):
    """Return (fm_dict, fm_keys_in_order, body, had_frontmatter)."""
    if not text.startswith("---"):
        return {}, [], text, False
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, [], text, False
    fm, order = {}, []
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            fm[k] = v.strip()
            order.append(k)
    body = "\n".join(lines[end+1:])
    if not body.endswith("\n"):
        body += "\n"
    return fm, order, body, True


def render_frontmatter(fm, key_order):
    lines = ["---"]
    for k in key_order:
        if k in fm:
            v = fm[k]
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def default_for(field, path):
    if field == "tags":
        return "[]"
    if field == "depends_on":
        return "[]"
    if field == "created":
        return DEFAULT_CREATED
    if field == "status":
        return "active"
    if field == "area":
        rel = path.relative_to(PROJECTS)
        parts = rel.parts
        # parts[-1] = "project.md", parts[-2] = project_slug
        # If parts has area parent: parts[-3] = area_slug
        if len(parts) >= 3:
            return parts[-3]
        return ""
    return ""


def normalize_status(fm):
    s = fm.get("status", "")
    if s == "on hold":
        fm["status"] = "on-hold"


def ensure_fields(path, fm, order, schema_fields):
    """Mutate fm/order to ensure all schema_fields exist."""
    for f in schema_fields:
        if f not in fm:
            fm[f] = default_for(f, path)
            order.append(f)


def migrate_marker(path, schema_fields):
    """Generic frontmatter normalizer for area/project/sub-project marker files."""
    text = path.read_text()
    fm, order, body, had_fm = parse_frontmatter(text)
    if not had_fm:
        # No frontmatter at all — build one from scratch
        for f in schema_fields:
            fm[f] = default_for(f, path)
            order.append(f)
        new_text = render_frontmatter(fm, order) + "\n" + body.lstrip()
        path.write_text(new_text)
        return "created-frontmatter"

    normalize_status(fm)
    before = dict(fm)
    ensure_fields(path, fm, order, schema_fields)

    # Reorder so canonical fields come first in canonical order, then anything else
    canonical = [k for k in schema_fields if k in fm]
    extra = [k for k in order if k not in schema_fields]
    new_order = canonical + extra
    # de-dup preserving order
    seen = set()
    new_order = [k for k in new_order if not (k in seen or seen.add(k))]

    new_text = render_frontmatter(fm, new_order) + "\n" + body.lstrip()
    path.write_text(new_text)
    return "ok" if before == fm else "updated"


def rename_life_os_subprojects():
    """Convert 6 life-os/* project.md → sub-project.md with sub-project schema."""
    life_os = PROJECTS / "life-os"
    if not life_os.exists():
        return []

    moves = []
    for sub in LIFE_OS_SUBS:
        d = life_os / sub
        old = d / "project.md"
        new = d / "sub-project.md"
        if not old.exists():
            continue
        if new.exists():
            continue  # already migrated
        text = old.read_text()
        fm, order, body, had_fm = parse_frontmatter(text)
        normalize_status(fm)

        # Rebuild as sub-project schema. Preserve created, status. depends_on default [].
        new_fm = {
            "created": fm.get("created", DEFAULT_CREATED),
            "status": fm.get("status", "active"),
            "depends_on": "[]",
        }
        new_text = render_frontmatter(new_fm, SUBPROJECT_FIELDS) + "\n" + body.lstrip()
        new.write_text(new_text)
        old.unlink()
        moves.append(str(d.relative_to(PROJECTS)))
    return moves


def fix_robocart_depends():
    """Set depends_on field on each robocart sub-project from parent table."""
    base = PROJECTS / "lofty-ventures" / "robocart"
    if not base.exists():
        return []

    fixed = []
    for sub_name, deps in ROBOCART_DEPS.items():
        sub_path = base / sub_name / "sub-project.md"
        if not sub_path.exists():
            continue
        text = sub_path.read_text()
        fm, order, body, had_fm = parse_frontmatter(text)
        if not had_fm:
            continue
        if fm.get("depends_on", "") == deps:
            continue  # already correct
        fm["depends_on"] = deps
        if "depends_on" not in order:
            order.append("depends_on")
        new_order = [k for k in SUBPROJECT_FIELDS if k in fm]
        new_text = render_frontmatter(fm, new_order) + "\n" + body.lstrip()
        sub_path.write_text(new_text)
        fixed.append(sub_name)
    return fixed


def fix_coding_area():
    """coding/area.md has no frontmatter. Build one."""
    p = PROJECTS / "coding" / "area.md"
    if not p.exists():
        return False
    text = p.read_text()
    fm, _, body, had_fm = parse_frontmatter(text)
    if had_fm and "created" in fm and "status" in fm:
        return False
    new_fm = {
        "created": "2026-03-27",  # roughly when life-os started — coding area predates
        "status": "active",
        "tags": "[infrastructure, coding, software]",
    }
    new_text = render_frontmatter(new_fm, AREA_FIELDS)
    if body.strip():
        new_text += "\n" + body.lstrip()
    else:
        new_text += "\n# Coding\n\nSoftware projects — applications, infrastructure, and tools the user is building.\n"
    p.write_text(new_text)
    return True


def is_skipped(d):
    if any(part in SKIP_NAMES for part in d.parts):
        return True
    if (d / "pyvenv.cfg").exists():
        return True
    return False


def walk_markers():
    """Yield (path, type) for every area.md / project.md / sub-project.md."""
    for d in PROJECTS.rglob("*"):
        if not d.is_dir() or is_skipped(d):
            continue
        for marker in ("area.md", "project.md", "sub-project.md"):
            p = d / marker
            if p.exists():
                yield p, marker[:-3]


def main():
    print("==> Migrating life-os/* project.md → sub-project.md")
    moves = rename_life_os_subprojects()
    for m in moves:
        print(f"   {m}")
    if not moves:
        print("   (already migrated)")

    print()
    print("==> Fixing coding/area.md frontmatter")
    if fix_coding_area():
        print("   wrote frontmatter")
    else:
        print("   (already ok)")

    print()
    print("==> Normalizing all area/project/sub-project frontmatter")
    n_files = 0
    for path, kind in walk_markers():
        if kind == "area":
            schema = AREA_FIELDS
        elif kind == "project":
            schema = PROJECT_FIELDS
        else:
            schema = SUBPROJECT_FIELDS
        migrate_marker(path, schema)
        n_files += 1
    print(f"   touched {n_files} files")

    print()
    print("==> Setting depends_on on robocart sub-projects")
    fixed = fix_robocart_depends()
    for f in fixed:
        print(f"   {f}")
    if not fixed:
        print("   (already set)")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
