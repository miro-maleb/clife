"""schema.py — canonical kb frontmatter schemas + file-type classification.

Single source of truth for what frontmatter *should* look like, per file type,
used by `cl lint`. Complementary to the editor modules: `blocks.py`,
`systems.py`, `goals.py`, `orientations.py` own the canonical *write* order
(`FIELD_ORDER`) for the files they create; this module owns the *validation*
vocabulary (required keys, allowed status values, which keys are dates/lists)
across the whole kb spine + projects. When you add a field or status value in
an editor, mirror it here so the linter doesn't flag legitimate files.

Historical note: area/project/sub-project schema comes from sub-project 02
(schema-and-migration); the 5-tier spine (systems/goals/orientations/blocks)
was layered on top later.
"""

from pathlib import Path

import week

KB = week.KB

# Legacy / kebab-case key spellings → the canonical snake_case. Applied to EVERY
# file regardless of type: snake_case is the house style for multi-word keys.
KEY_ALIASES = {
    "depends-on": "depends_on",
    "superseded-by": "superseded_by",
    "superseded-on": "superseded_on",
    "last-reviewed": "last_reviewed",
    "follow-up": "follow_up",
    "record-date": "record_date",
    "estimated-effort": "estimated_effort",
}

# Conservative status remaps for known strays (only clearly-equivalent values).
# Global ones apply to any type; PER_TYPE ones only within that type (e.g. an
# area's "on-hold" means the same as "dormant"; a sub-project's "seed" — an
# idea-vocab leak — means "pending").
STATUS_REMAP = {
    "thinking": "active",
    "archive": "archived",
    "shipped": "complete",
}
STATUS_REMAP_BY_TYPE = {
    "area": {"on-hold": "dormant"},
    "sub-project": {"seed": "pending"},
}


def status_remap(typ, value):
    """Resolve a status stray to its canonical value, or None if no remap."""
    return STATUS_REMAP_BY_TYPE.get(typ, {}).get(value) or STATUS_REMAP.get(value)

# Per-type schema. `open_keys=True` → unknown keys only warn, never error
# (projects legitimately carry lots of one-off metadata). Keys always allowed
# everywhere: nothing — each type lists its own `known` set.
SCHEMAS = {
    "area": dict(
        required={"created", "status"},
        known={"created", "status", "tags", "type", "superseded_by", "superseded_on"},
        status={"active", "dormant", "superseded"},
        dates={"created", "superseded_on"}, lists={"tags"}),
    "project": dict(
        required={"created", "status"},
        known={"created", "deadline", "status", "completed", "abandoned", "sleeping",
               "last_reviewed", "area", "tags", "goals", "orientations", "depends_on",
               "theme", "started", "superseded_by", "superseded_on", "supersedes",
               "type", "month", "source", "priority", "estimated_effort"},
        status={"active", "on-hold", "sleeping", "complete", "abandoned", "archived",
                "superseded", "pending"},
        dates={"created", "deadline", "completed", "abandoned", "sleeping",
               "last_reviewed", "started", "superseded_on"},
        lists={"tags", "goals", "orientations", "depends_on"}, open_keys=True),
    # A sub-project is a project-within-a-project and carries the same lifecycle
    # metadata, so it inherits the project optional vocabulary on top of its own
    # dependency fields.
    "sub-project": dict(
        required={"created", "status"},
        known={"created", "status", "depends_on", "completed", "started",
               "deferred_on", "deferred_to", "blocked_by", "blocks",
               "deadline", "abandoned", "sleeping", "last_reviewed", "tags",
               "area", "theme", "priority", "estimated_effort", "type",
               "month", "source", "superseded_by", "superseded_on"},
        status={"pending", "active", "sleeping", "complete", "abandoned",
                "on-hold", "superseded"},
        dates={"created", "completed", "started", "deferred_on", "deadline",
               "abandoned", "sleeping", "last_reviewed", "superseded_on"},
        lists={"depends_on", "blocks", "tags"}),
    "block": dict(
        # Flat habit block (~/kb/habits/<block>.md) — self-contained since the
        # systems flatten: it carries its own status + feeding chain.
        required={"block", "calendar", "cadence"},
        known={"block", "calendar", "cadence", "habit", "days", "duration",
               "instances", "default_start", "travel", "status", "goals",
               "orientations", "parent"},   # parent tolerated for legacy nested tenants
        status={"active", "on-hold", "superseded", "dormant"},
        dates=set(), lists={"days", "goals", "orientations"}),
    "goal": dict(
        required={"goal", "year", "status"},
        known={"goal", "year", "status", "marker", "orientations",
               "projects", "deferred_on", "deferred_to"},
        status={"active", "paused", "done", "dropped", "deferred"},
        dates={"deferred_on"}, lists={"orientations", "projects"}),
    "orientation": dict(
        required={"orientation", "status"},
        known={"orientation", "status", "superseded_by", "superseded_on"},
        status={"active", "on-hold", "superseded"},
        dates={"superseded_on"}, lists=set()),
}


def identity_issues(typ, path, meta):
    """Report-only: identity fields that disagree with the file's location.
    A block/system/goal/orientation names itself in frontmatter AND lives at a
    path that encodes the same slug; when they drift (folder `weekly-robotics`
    but `system: robotics`) joins get slippery. Which side is authoritative
    varies (blocks key streaks off the `block:` field, the planner keys systems
    off the folder), so the linter only surfaces these — it never auto-rewrites
    a name that might be a join key. Returns [(key, expected, actual)]."""
    out = []

    def chk(key, expected):
        actual = meta.get(key)
        if actual and actual != expected:
            out.append((key, expected, actual))

    if typ == "block":
        chk("block", path.stem)                          # habits/<block>.md
    elif typ == "goal":
        chk("goal", path.stem)                           # goals/<year>/<slug>.md
    elif typ == "orientation":
        chk("orientation", path.stem)                    # orientations/<slug>.md
    return out


def classify(path: Path):
    """Map a kb file to its schema type, or None if outside lint scope. A
    special "template" type gets lenient checks (placeholder values), and
    "document" covers loose content under projects/ (universal key checks only).
    """
    p = path if path.is_absolute() else (KB / path)
    try:
        parts = p.relative_to(KB).parts
    except ValueError:
        return None
    if not parts:
        return None
    top, name = parts[0], p.name
    if top == "habits":
        if name.endswith(".md") and name != "README.md" and not name.endswith("-notes.md"):
            return "block"
        return None
    if top == "goals" and name.endswith(".md"):
        return "goal"
    if top == "orientations" and name.endswith(".md") and name != "README.md":
        return "orientation"
    if top == "templates" and name.endswith(".md"):
        return "template"
    if top == "projects":
        if name == "area.md":
            return "area"
        if name == "project.md":
            return "project"
        if name == "sub-project.md":
            return "sub-project"
        return "document"
    return None


# Which templates validate against which schema's KEY set (values are
# placeholders, so only key names are checked for templates).
TEMPLATE_TYPE = {
    "project.md": "project",
    "blog-post.md": None,      # document/essay — no strict schema
    "episode.md": None,        # podcast episode — no strict schema
    "log.md": None,
}
