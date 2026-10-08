import argparse
import json
import os
import re
import subprocess
import sys
import tty
import termios
from datetime import datetime
from pathlib import Path

from paths import KB, STORE

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from kb_utils import capture_payload

import ai
import pool

console = Console()

# 2026-10-05: ~/kb/notes/ is gone. Every new file this module writes lands in
# working/ (paths.STORE is an alias for WORKING); nothing composes kb paths here.
notes_path = STORE
project_path = STORE
# There is no inbox FOLDER. Untagged captures live in the stream's YYYY/MM
# shards like every other note, and "inbox" is the set of notes nobody has
# placed yet — no tags in frontmatter. The folder this used to name moved twice
# on 2026-09-03 and emptied this command silently both times; a filter cannot
# desync from a path that no longer decides anything.
inbox_path = STORE
pinned_path = STORE / ".pinned"   # vestigial: no pinned items exist


def _is_unplaced(f):
    """Every file in working/ is waiting on a decision (2026-10-05: tags retired,
    working/ IS the inbox). The tag predicate below is kept only for history.

    A tag in the BODY counts (2026-10-04): this is the predicate --prune-noise
    trusts before binning a note, so it must agree with `cl stream`, which
    reads tags from frontmatter and body both. It used to read frontmatter
    only, and a note saying `#dream` in Obsidian would have looked unplaced
    here while every view showed it placed. Asks stream, never re-implements."""
    return f.is_file() and not f.name.startswith(".")
shopping_path = STORE
system_improvements_path = STORE / "system-improvements.md"

HOTKEYS = (
    "[grey50][[/grey50][steel_blue1]n[/steel_blue1][grey50]][/grey50] note  "
    "[grey50][[/grey50][steel_blue1]t[/steel_blue1][grey50]][/grey50] task  "
    "[grey50][[/grey50][steel_blue1]c[/steel_blue1][grey50]][/grey50] calendar  "
    "[grey50][[/grey50][steel_blue1]p[/steel_blue1][grey50]][/grey50] new project  "
    "[grey50][[/grey50][steel_blue1]v[/steel_blue1][grey50]][/grey50] → project  "
    "[grey50][[/grey50][steel_blue1]g[/steel_blue1][grey50]][/grey50] grocery  "
    "[grey50][[/grey50][steel_blue1]h[/steel_blue1][grey50]][/grey50] household  "
    "[grey50][[/grey50][steel_blue1]i[/steel_blue1][grey50]][/grey50] improvement  "
    "[grey50][[/grey50][grey70]s[/grey70][grey50]][/grey50] skip  "
    "[grey50][[/grey50][grey70]d[/grey70][grey50]][/grey50] delete  "
    "[grey50][[/grey50][grey70]q[/grey70][grey50]][/grey50] quit"
)


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_project_names():
    names = []
    for f in project_path.rglob("project.md"):
        names.append(f.parent.name)
    return sorted(set(names))


def get_project_areas():
    """Return area dirs (those with area.md). Post sub-project 02 migration."""
    return sorted([
        d for d in project_path.iterdir()
        if d.is_dir() and (d / "area.md").exists()
    ])


def list_route_targets():
    """Return [path-relative-to-projects/] for every routable target.

    Includes areas, projects, and sub-projects — anywhere a captured note can
    be filed. Used by the `v` (→ project) hotkey for hierarchy-aware routing.
    """
    targets = []
    for area in sorted(project_path.iterdir()):
        if not area.is_dir() or not (area / "area.md").exists():
            continue
        targets.append(area.name)
        for project in sorted(area.iterdir()):
            if not project.is_dir() or not (project / "project.md").exists():
                continue
            targets.append(f"{area.name}/{project.name}")
            for sp in sorted(project.iterdir()):
                if sp.is_dir() and (sp / "sub-project.md").exists():
                    targets.append(f"{area.name}/{project.name}/{sp.name}")
    return targets


def select_project_fzf(project_names):
    result = subprocess.run(
        ["fzf", "--prompt=  project: ", "--height=40%", "--reverse", "--no-info"],
        input="\n".join(project_names),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def select_area_fzf(areas):
    result = subprocess.run(
        ["fzf", "--prompt=  area: ", "--height=40%", "--reverse", "--no-info"],
        input="\n".join(a.name for a in areas),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def slug_from_name(name):
    return name.stem.lower().replace(" ", "-")


def restore_terminal():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    termios.tcsetattr(fd, termios.TCSADRAIN, old)


def route_pool(file):
    """[c] calendar → send this capture to the calendar pool as a one-off item.

    Replaces the old quick-gcal "calendar" route (which committed straight to
    Google Calendar and lost untimed tasks). Everything calendar-bound now lands
    in the pool; the week planner places it on the real calendar deliberately.

    AI does coarse structuring (title / area / est_minutes); the human confirms or
    edits before it lands. The item shows up next time you run the scheduler.
    """
    text = capture_payload(file) or file.stem
    console.print(f"\n  [grey50]→ calendar pool:[/grey50] [tan]{text}[/tan]")
    console.print("  [grey50]structuring…[/grey50]")
    areas = [a.name for a in get_project_areas()]
    fields = ai.pool_item_from_text(text, areas=areas) or {}
    title = fields.get("title") or text
    area = fields.get("area")
    est = fields.get("est_minutes") or 30

    console.print(f"  [grey50]title:[/grey50] [tan]{title}[/tan]")
    console.print(f"  [grey50]area: [/grey50] {area or '—'}   [grey50]est:[/grey50] {est}m")
    console.print("  [grey50]add?[/grey50] [grey50][[/grey50][steel_blue1]y[/steel_blue1][grey50]/[/grey50][grey70]e[/grey70][grey50]dit/[/grey50][grey70]n[/grey70][grey50]][/grey50] ", end="")
    key = getch()
    console.print()
    if key == "n":
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    if key == "e":
        restore_terminal()
        console.print("  [grey50]title:[/grey50] ", end="")
        try:
            title = input().strip() or title
        except EOFError:
            pass
        console.print("  [grey50]area (blank = none):[/grey50] ", end="")
        try:
            a = input().strip()
            area = a or area
        except EOFError:
            pass
        console.print(f"  [grey50]est minutes [{est}]:[/grey50] ", end="")
        try:
            e = input().strip()
            est = pool.parse_minutes(e, default=est) or est
        except EOFError:
            pass

    notes = text if text.strip() != title.strip() else None
    pool.add_item(title, area=area, est_minutes=est, notes=notes)
    file.unlink()
    console.print(f"[dark_sea_green4]  → calendar pool[/dark_sea_green4] [grey50]({est}m{', ' + area if area else ''})[/grey50]")


def _note_slugify(s):
    import re
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", s.lower())).strip("-")


def _unique_note_slug(slug):
    """Avoid clobbering an existing note of the same name."""
    if not (notes_path / f"{slug}.md").exists():
        return slug
    n = 2
    while (notes_path / f"{slug}-{n}.md").exists():
        n += 1
    return f"{slug}-{n}"


def _write_note(slug, content):
    notes_path.mkdir(parents=True, exist_ok=True)
    dest = notes_path / f"{slug}.md"
    dest.write_text(
        f"---\ncreated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n\n{content}\n"
    )
    return dest


def route_note(file):
    """[n] → notes/. The AI suggests a filename from the content (so the note
    doesn't land as a timestamp); the human accepts it or types their own."""
    content = file.read_text().strip()
    payload = capture_payload(file) or content
    console.print("  [grey50]titling…[/grey50]")
    sug = ai.title_from_text(payload) or {}
    slug = sug.get("slug") or slug_from_name(file)
    if sug.get("title"):
        console.print(f"  [grey50]title:[/grey50] [tan]{sug['title']}[/tan]")
    console.print(f"  [grey50]name:[/grey50] [tan]{slug}[/tan]  [grey50][enter = ok, or type a name][/grey50] ", end="")
    restore_terminal()
    try:
        typed = input().strip()
    except EOFError:
        typed = ""
    if typed:
        slug = _note_slugify(typed) or slug
    slug = _unique_note_slug(slug)
    _write_note(slug, content)
    file.unlink()
    console.print(f"[dark_sea_green4]  → saved to notes/{slug}.md[/dark_sea_green4]")


def route_task(file):
    payload = capture_payload(file) or file.stem
    project_names = get_project_names()
    print()
    project = select_project_fzf(project_names)
    if not project:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    project_file = next((f for f in project_path.rglob("project.md") if f.parent.name == project), None)
    if not project_file:
        console.print(f"[indian_red]  project '{project}' not found — skipping[/indian_red]")
        return
    with project_file.open("a") as f:
        f.write(f"\n- [ ] {payload}")
    file.unlink()
    console.print(f"[dark_sea_green4]  → task added to {project}[/dark_sea_green4]")


def route_new_project(file):
    """Create a new on-hold project from this inbox item.

    Uses the same slugify and frontmatter schema as `cl new` (sub-project 05),
    but defaults to area=ideas/ since this is a quick-capture flow. Press / to
    fzf-pick a different area.
    """
    from new import slugify

    content = file.read_text().strip()

    restore_terminal()
    slug = ""
    while not slug:
        console.print("\n  [grey50]project name:[/grey50] ", end="")
        try:
            raw = input().strip()
        except EOFError:
            raw = ""
        slug = slugify(raw)
        if not slug:
            console.print("[red]  name required[/red]")

    console.print(
        "\n  [grey50]area?[/grey50] [grey70](enter for ideas, / to pick)[/grey70] ",
        end="",
    )
    key = getch()
    console.print()

    if key == "/":
        areas = get_project_areas()
        area_name = select_area_fzf(areas)
        if not area_name:
            console.print("[rosy_brown]  → cancelled[/rosy_brown]")
            return
    else:
        area_name = "ideas"

    area_dir = project_path / area_name
    area_dir.mkdir(parents=True, exist_ok=True)
    project_dir = area_dir / slug
    if project_dir.exists():
        console.print(f"[indian_red]  already exists: {area_name}/{slug}[/indian_red]")
        return
    project_dir.mkdir()

    title = " ".join(w.capitalize() for w in slug.split("-"))
    today = datetime.now().strftime("%Y-%m-%d")

    (project_dir / "project.md").write_text(
        f"---\n"
        f"created: {today}\n"
        f"deadline: \n"
        f"status: on-hold\n"
        f"completed: \n"
        f"abandoned: \n"
        f"sleeping: \n"
        f"last_reviewed: \n"
        f"area: {area_name}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"## Idea\n\n"
        f"{content}\n"
    )
    file.unlink()
    console.print(f"[dark_sea_green4]  → new project: {area_name}/{slug}[/dark_sea_green4]")


def route_paste_project(file):
    """Attach this inbox file to an existing area / project / sub-project.

    Hierarchy-aware: shows flat fzf list of every routable target (areas,
    projects, sub-projects). Pick `coding` or `coding/ai-pipeline` or
    `coding/ai-pipeline/01-foo`.
    """
    targets = list_route_targets()
    if not targets:
        console.print("[indian_red]  no routable targets — projects/ is empty[/indian_red]")
        return
    print()
    result = subprocess.run(
        ["fzf", "--prompt=  → ", "--height=40%", "--reverse", "--no-info"],
        input="\n".join(targets), capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    target = result.stdout.strip()
    if not target:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")
        return
    dest_dir = project_path / target
    if not dest_dir.is_dir():
        console.print(f"[indian_red]  not a directory: {target}[/indian_red]")
        return
    dest = dest_dir / file.name
    file.rename(dest)
    console.print(f"[dark_sea_green4]  → {target}/{file.name}[/dark_sea_green4]")


def route_shopping(file, list_name):
    payload = capture_payload(file)
    shopping_path.mkdir(parents=True, exist_ok=True)
    list_file = shopping_path / f"{list_name}.md"
    if not list_file.exists():
        list_file.write_text(f"# {list_name.capitalize()}\n\n")
    with list_file.open("a") as f:
        f.write(f"- [ ] {payload}\n")
    file.unlink()
    console.print(f"[dark_sea_green4]  → added to {list_name}[/dark_sea_green4]")


def route_system_improvement(file):
    """Append the inbox capture as a bullet under '## Open ideas' in
    system-improvements.md. Used during the use-don't-build period to defer
    system tweaks to the weekly maintenance session.
    """
    if not system_improvements_path.exists():
        console.print(
            f"[indian_red]  system-improvements.md missing at {system_improvements_path}[/indian_red]"
        )
        return
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else content
    if not first_line:
        console.print("[rosy_brown]  → empty capture, skipped[/rosy_brown]")
        return
    text = system_improvements_path.read_text()
    lines = text.splitlines()
    open_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "## Open ideas"), None
    )
    if open_idx is None:
        console.print(
            "[indian_red]  no '## Open ideas' section in system-improvements.md[/indian_red]"
        )
        return
    end_idx = next(
        (i for i in range(open_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    insert_idx = end_idx
    while insert_idx > open_idx + 1 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1
    lines.insert(insert_idx, f"- {first_line}")
    system_improvements_path.write_text(
        "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    )
    file.unlink()
    console.print("[dark_sea_green4]  → system improvements[/dark_sea_green4]")


def route_delete(file):
    console.print("  delete? [grey50][[/grey50][steel_blue1]y[/steel_blue1][grey50]/[/grey50][grey70]n[/grey70][grey50]][/grey50] ", end="")
    key = getch()
    print()
    if key == "y":
        file.unlink()
        console.print("[indian_red]  → deleted[/indian_red]")
    else:
        console.print("[rosy_brown]  → cancelled[/rosy_brown]")


def process_file(file, index, total):
    console.print()
    console.print(Rule(f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{file.name}[/tan]", style="grey23"))
    content = file.read_text().strip()
    console.print(Panel(content, border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {HOTKEYS}\n")

    while True:
        key = getch()
        if key == "c":
            route_pool(file)
            return True
        elif key == "n":
            route_note(file)
            return True
        elif key == "t":
            route_task(file)
            return True
        elif key == "p":
            route_new_project(file)
            return True
        elif key == "v":
            route_paste_project(file)
            return True
        elif key == "g":
            route_shopping(file, "grocery")
            return True
        elif key == "h":
            route_shopping(file, "household")
            return True
        elif key == "i":
            route_system_improvement(file)
            return True
        elif key == "s":
            console.print("[rosy_brown]  → skipped[/rosy_brown]")
            return True
        elif key == "d":
            route_delete(file)
            return True
        elif key == "q":
            return False


# ── Non-interactive routing (web / automation surface) ──────────────────────
#
# Same routing semantics as the interactive keys, but every selection that the
# TUI gathers via fzf/keypress is passed in as a value. Each returns a result
# dict; nothing prints to stdout (callers emit JSON).


def inbox_files():
    return sorted([
        f for f in inbox_path.rglob("*.md")
        if f.is_file() and f.name != ".gitkeep" and _is_unplaced(f)
    ])


def _strip_frontmatter(text):
    """Drop a leading --- ... --- block, returning the body."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return (text[nl + 1:] if nl != -1 else "").strip()
    return text


def _fm_field(raw, key):
    m = re.search(rf"^{key}:\s*(.+)$", raw, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else ""


def _sender_name(frm):
    """'Google <google-noreply@google.com>' -> 'Google'; else the raw value."""
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', frm)
    return (m.group(1).strip() if m else frm).strip()


def _item_from(f):
    """One inbox/pinned file → the item dict the surfaces render."""
    raw = f.read_text().strip()
    body = _strip_frontmatter(raw)
    src = _fm_field(raw, "source")
    subject = _fm_field(raw, "subject")
    sender = _sender_name(_fm_field(raw, "from"))
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    headline = subject or first
    preview = headline if len(headline) <= 100 else headline[:97] + "…"
    return {"file": f.name, "text": body, "preview": preview, "source": src,
            "subject": subject, "from": sender}


def inbox_items():
    """List inbox items as dicts. For emails, subject (from frontmatter) is the
    at-a-glance headline — no AI needed. source/from carried for context."""
    return [_item_from(f) for f in inbox_files()]


def pinned_items():
    """Inbox items pinned as todos — kept in inbox/pinned/, shown at the bottom of
    the inbox until marked complete (then deleted)."""
    if not pinned_path.exists():
        return []
    return [_item_from(f) for f in sorted(pinned_path.glob("*.md")) if f.name != ".gitkeep"]


def route_targets_payload():
    """Everything the pickers need: projects, hierarchy targets, areas."""
    return {
        "projects": get_project_names(),
        "route_targets": list_route_targets(),
        "areas": [a.name for a in get_project_areas()],
    }


def ni_note(file):
    """Surface's non-interactive note route: auto-apply the AI-suggested filename
    (rename later in the editor if needed); fall back to the timestamp slug."""
    content = file.read_text().strip()
    payload = capture_payload(file) or content
    slug = (ai.title_from_text(payload) or {}).get("slug") or slug_from_name(file)
    slug = _unique_note_slug(slug)
    _write_note(slug, content)
    file.unlink()
    return {"ok": True, "msg": f"notes/{slug}.md"}


def ni_shopping(file, list_name):
    payload = capture_payload(file)
    shopping_path.mkdir(parents=True, exist_ok=True)
    list_file = shopping_path / f"{list_name}.md"
    if not list_file.exists():
        list_file.write_text(f"# {list_name.capitalize()}\n\n")
    with list_file.open("a") as f:
        f.write(f"- [ ] {payload}\n")
    file.unlink()
    return {"ok": True, "msg": list_name}


def ni_improvement(file):
    if not system_improvements_path.exists():
        return {"ok": False, "msg": "system-improvements.md missing"}
    content = file.read_text().strip()
    first_line = content.splitlines()[0] if content else ""
    if not first_line:
        return {"ok": False, "msg": "empty capture"}
    text = system_improvements_path.read_text()
    lines = text.splitlines()
    open_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "## Open ideas"), None)
    if open_idx is None:
        return {"ok": False, "msg": "no '## Open ideas' section"}
    end_idx = next((i for i in range(open_idx + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    insert_idx = end_idx
    while insert_idx > open_idx + 1 and lines[insert_idx - 1].strip() == "":
        insert_idx -= 1
    lines.insert(insert_idx, f"- {first_line}")
    system_improvements_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    file.unlink()
    return {"ok": True, "msg": "system improvements"}


def ni_task(file, project):
    payload = capture_payload(file) or file.stem
    project_file = next((f for f in project_path.rglob("project.md") if f.parent.name == project), None)
    if not project_file:
        return {"ok": False, "msg": f"project '{project}' not found"}
    with project_file.open("a") as f:
        f.write(f"\n- [ ] {payload}")
    file.unlink()
    return {"ok": True, "msg": f"task → {project}"}


def ni_paste_project(file, target):
    dest_dir = project_path / target
    if not dest_dir.is_dir():
        return {"ok": False, "msg": f"not a directory: {target}"}
    file.rename(dest_dir / file.name)
    return {"ok": True, "msg": f"{target}/{file.name}"}


def ni_new_project(file, name, area="ideas"):
    from new import slugify
    slug = slugify(name)
    if not slug:
        return {"ok": False, "msg": "name required"}
    content = file.read_text().strip()
    area = area or "ideas"
    area_dir = project_path / area
    area_dir.mkdir(parents=True, exist_ok=True)
    project_dir = area_dir / slug
    if project_dir.exists():
        return {"ok": False, "msg": f"already exists: {area}/{slug}"}
    project_dir.mkdir()
    title = " ".join(w.capitalize() for w in slug.split("-"))
    today = datetime.now().strftime("%Y-%m-%d")
    (project_dir / "project.md").write_text(
        f"---\ncreated: {today}\ndeadline: \nstatus: on-hold\ncompleted: \nabandoned: \n"
        f"sleeping: \nlast_reviewed: \narea: {area}\n---\n\n# {title}\n\n## Idea\n\n{content}\n"
    )
    file.unlink()
    return {"ok": True, "msg": f"new project: {area}/{slug}"}


def ni_pool(file, value="", area=""):
    """Web/automation counterpart of route_pool. `value` overrides the AI title;
    `area` overrides the AI area. Empty → AI structures the raw capture."""
    text = capture_payload(file) or file.stem
    fields = ai.pool_item_from_text(text, areas=[a.name for a in get_project_areas()]) or {}
    title = (value or fields.get("title") or text).strip()
    a = (area or fields.get("area") or "").strip() or None
    est = fields.get("est_minutes") or 30
    notes = text if text.strip() != title else None
    pool.add_item(title, area=a, est_minutes=est, notes=notes)
    file.unlink()
    return {"ok": True, "msg": f"pool: {title}"}


# ── AI coarse pruning → see ai.py ───────────────────────────────────────────
#
# AI's only inbox jobs: flag noise + summarize long items. Routing stays 100%
# human (slick UI), because AI-guessed routing proved unreliable and not better.


def prune_items():
    """Flag noise + summarize each inbox item via the local model (prompt: ai.py)."""
    items = inbox_items()
    if not items:
        return {"items": []}
    valid = {it["file"] for it in items}
    by_file = {it["file"]: it for it in items}
    out = []
    for s in ai.prune_inbox(items):
        f = (s.get("file") or "").strip().strip("[]").strip()
        if f not in valid:
            continue
        src = by_file.get(f, {})
        out.append({
            "file": f,
            "noise": bool(s.get("noise")),
            "confidence": _as_float(s.get("confidence"), 0.0),
            "summary": (s.get("summary") or "")[:120],
            # carried from the item, not the model: `from` was read off the
            # judgement dict here and so was always "" in the trashed record.
            "from": src.get("from", ""),
            "source": src.get("source", ""),
        })
    return {"items": out}


TRASH = KB / ".trash"


def _trash_file(file, reason="manual"):
    """Move to kb/.trash/ instead of deleting — recoverable until the AI filter is trusted.

    Records the origin relpath in triage's `trashed` sidecar on the way out.
    `cl triage restore` needs that record and refuses a file without one, so a
    path that trashed without writing it produced files nothing could put back:
    46 of 150 in .trash on 2026-09-15, every one of them from --prune-noise or
    from `ni_route delete`. The record is written HERE, at the one chokepoint
    all three callers share, rather than at each of them.
    """
    TRASH.mkdir(parents=True, exist_ok=True)
    dest = TRASH / file.name
    if dest.exists():
        dest = TRASH / f"{file.stem}-{datetime.now().strftime('%H%M%S')}{file.suffix}"
    try:
        rel = str(file.relative_to(KB))
    except ValueError:
        rel = f"notes/{file.name}"
    file.rename(dest)
    try:
        import triage                      # lazy: triage imports inbox at module level
        tr = triage.load_trashed()
        tr[dest.name] = {"from": rel, "slug": file.stem, "reason": reason,
                         "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        triage.save_slots(triage.load_slots(), tr)
    except Exception:
        pass                               # a failed record must not lose the file
    return dest


# Two guards stand between a model's opinion and a deleted file. Both are here
# because neither existed: `confidence` was computed by the judge, returned in
# the payload, and then never read — a 0.15 hunch deleted exactly as hard as a
# 0.97 certainty. And an untagged note was eligible the instant it was written,
# so a capture you were still thinking about could be judged and binned before
# you got back to it.
PRUNE_MIN_CONFIDENCE = float(os.environ.get("CL_PRUNE_MIN_CONFIDENCE", "0.85"))
PRUNE_MIN_AGE_HOURS = float(os.environ.get("CL_PRUNE_MIN_AGE_HOURS", "24"))

TRASH_LOG = KB / "_state" / "trash-log.md"


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _note_age_hours(f):
    """Hours since the note was written. Frontmatter first, mtime as the floor.

    `captured:` wins over `created:` where both exist, and `T` normalises to a
    space — older notes carry `created: YYYY-MM-DDTHH:MM:SS` and sorting the two
    formats together interleaves them by format rather than by time.
    """
    now = datetime.now()
    try:
        raw = f.read_text(errors="replace")[:400]
    except OSError:
        return 0.0
    stamp = _fm_field(raw, "captured") or _fm_field(raw, "created")
    if stamp:
        v = stamp.replace("T", " ").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return (now - datetime.strptime(v, fmt)).total_seconds() / 3600
            except ValueError:
                continue
    try:
        return (now.timestamp() - f.stat().st_mtime) / 3600
    except OSError:
        return 0.0


def _log_trash(rows):
    """Append what was eaten to a markdown ledger under _state/.

    Not JSON, and not in notes/ — a ledger in the store would itself be an
    untagged note and this function's own next run would be entitled to eat it.
    The point of the ledger is that the filter becomes reviewable: until you can
    read back what it took and why, trust never accrues and the honest advice
    stays "be careful, it deletes things".
    """
    if not rows:
        return
    try:
        TRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        new = not TRASH_LOG.exists()
        with TRASH_LOG.open("a", encoding="utf-8") as fh:
            if new:
                fh.write("# Trash log\n\nWritten by `cl inbox --prune-noise`. "
                         "Restore any line with `cl triage restore <file>`.\n")
            fh.write(f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            for r in rows:
                fh.write(f"- `{r['file']}` — conf {r['confidence']:.2f} — "
                         f"{r.get('summary') or '(no summary)'}\n")
    except OSError:
        pass


def prune_noise(dry_run=False, min_confidence=None, min_age_hours=None):
    """AI spam-filter: move high-confidence noise to trash (recoverable). Idempotent.

    This is the auto-filter — "streams heavy, output light": run it after ingest so
    obvious spam never reaches the human. Trash keeps it recoverable while trust builds.

    A note survives unless the judge says noise AND says so above the confidence
    floor AND the note is older than the age floor. `skipped` reports every near
    miss with its reason, so a run that takes nothing still tells you what it was
    looking at — the previous version reported only a `kept` count, which is why
    it read as arbitrary.
    """
    floor = PRUNE_MIN_CONFIDENCE if min_confidence is None else float(min_confidence)
    age_floor = PRUNE_MIN_AGE_HOURS if min_age_hours is None else float(min_age_hours)
    # RETIRED 2026-10-05: nothing auto-bins captures any more (working/ ages into
    # archive/ instead). Same JSON shape, nothing touched. The old body follows,
    # unreachable, for the record.
    return {"trashed": [], "skipped": [], "kept": 0, "floor": floor,
            "age_floor": age_floor, "dry_run": dry_run,
            "retired": "--prune-noise is retired (2026-10-05); nothing was judged or moved"}
    trashed, skipped, kept = [], [], 0
    for it in prune_items().get("items", []):
        f = inbox_path / it["file"]
        if not f.exists():
            continue
        conf = _as_float(it.get("confidence"), 0.0)
        rec = {"file": it["file"], "from": it.get("from", ""),
               "source": it.get("source", ""), "confidence": conf,
               "summary": it.get("summary", "")}
        if not it.get("noise"):
            kept += 1
            continue
        age = _note_age_hours(f)
        if age < age_floor:
            skipped.append({**rec, "why": f"too new ({age:.1f}h < {age_floor:g}h)"})
            continue
        if conf < floor:
            skipped.append({**rec, "why": f"low confidence ({conf:.2f} < {floor:g})"})
            continue
        if not dry_run:
            _trash_file(f, reason=f"prune-noise conf={conf:.2f}")
        trashed.append(rec)
    if not dry_run:
        _log_trash(trashed)
    return {"trashed": trashed, "skipped": skipped, "kept": kept,
            "floor": floor, "age_floor": age_floor, "dry_run": dry_run}


def ni_todo(file):
    """Pin an inbox item as a todo: move it into inbox/pinned/ so it drops out of
    the route queue and shows at the bottom of the inbox until completed."""
    pinned_path.mkdir(parents=True, exist_ok=True)
    file.rename(pinned_path / file.name)
    return {"ok": True, "msg": "pinned as todo"}


def ni_complete(filename):
    """Mark a pinned todo done — just delete it (git is the undo)."""
    f = pinned_path / Path(filename).name
    if f.exists():
        f.unlink()
    return {"ok": True, "msg": "done"}


def _resolve_inbox_file(filename):
    """Find a stream note from whatever the caller has.

    `_item_from` emits a bare BASENAME, but notes have lived in month shards
    (_stream/YYYY/MM/) since 2026-09-03. Composing inbox_path/basename stopped
    resolving that day, so every --route call returned "file gone" — silently,
    because the JSON says ok:false and the callers rendered nothing. Accept a
    path relative to the stream, an absolute path, or a basename we go find."""
    p = Path(filename)
    if p.is_absolute() and p.exists():
        return p
    direct = inbox_path / filename
    if direct.exists():
        return direct
    hits = sorted(inbox_path.rglob(p.name))
    return hits[0] if len(hits) == 1 else None


def ni_route(filename, dest, value="", area=""):
    file = _resolve_inbox_file(filename)
    if file is None:
        return {"ok": False, "msg": "file gone"}
    if dest == "todo":         return ni_todo(file)
    if dest == "note":         return ni_note(file)
    if dest == "grocery":      return ni_shopping(file, "grocery")
    if dest == "household":    return ni_shopping(file, "household")
    if dest == "improvement":  return ni_improvement(file)
    if dest == "skip":         return {"ok": True, "msg": "skipped"}
    if dest == "delete":       _trash_file(file, reason="route delete"); return {"ok": True, "msg": "trashed"}
    if dest == "task":         return ni_task(file, value)
    if dest == "project":      return ni_paste_project(file, value)
    if dest == "newproject":   return ni_new_project(file, value, area)
    if dest in ("calendar", "pool"):  return ni_pool(file, value, area)
    return {"ok": False, "msg": f"unknown dest: {dest}"}


def try_ingest_email():
    """If kb-capture is configured, sync + ingest before triage. Silent if not."""
    maildir_env = os.environ.get("CL_INGEST_MAILDIR", "")
    default_maildir = Path.home() / "mail" / "kb-capture" / "Inbox"
    if not maildir_env and not default_maildir.exists():
        return
    # Only run mbsync if it's installed and the channel exists in mbsyncrc.
    mbsyncrc = Path.home() / ".mbsyncrc"
    has_kb_capture = mbsyncrc.exists() and "kb-capture" in mbsyncrc.read_text()
    if has_kb_capture:
        subprocess.run(
            ["mbsync", "kb-capture"],
            capture_output=True,
            text=True,
        )
    # Ingest. ingest.main() prints its own status; silent if nothing to do.
    import ingest
    ingest.main()


def main():
    parser = argparse.ArgumentParser(prog="cl inbox", add_help=False)
    parser.add_argument("--list", action="store_true", help="emit inbox items as JSON")
    parser.add_argument("--pinned", action="store_true", help="emit pinned todos as JSON")
    parser.add_argument("--complete", metavar="FILE", help="delete a pinned todo (mark done)")
    parser.add_argument("--targets", action="store_true", help="emit picker targets as JSON")
    parser.add_argument("--prune", action="store_true",
                        help="AI coarse-prune: flag noise + summarize, as JSON (routing stays human)")
    parser.add_argument("--prune-noise", action="store_true",
                        help="AI spam-filter: move high-confidence noise to trash (recoverable); emits JSON")
    parser.add_argument("--dry-run", action="store_true", help="with --prune-noise: preview only")
    parser.add_argument("--min-confidence", type=float, default=None,
                        help=f"with --prune-noise: judge confidence floor (default {PRUNE_MIN_CONFIDENCE:g})")
    parser.add_argument("--min-age-hours", type=float, default=None,
                        help=f"with --prune-noise: never touch notes newer than this (default {PRUNE_MIN_AGE_HOURS:g}h)")
    parser.add_argument("--route", nargs=2, metavar=("FILE", "DEST"),
                        help="non-interactively route FILE to DEST; emits JSON")
    parser.add_argument("--value", default="", help="selection for picker routes (project/calendar/target/name)")
    parser.add_argument("--area", default="", help="area for newproject route")
    args, _ = parser.parse_known_args()

    if args.list:
        print(json.dumps({"items": inbox_items()}))
        return
    if args.pinned:
        print(json.dumps({"items": pinned_items()}))
        return
    if args.complete:
        print(json.dumps(ni_complete(args.complete)))
        return
    if args.targets:
        print(json.dumps(route_targets_payload()))
        return
    if args.prune:
        print(json.dumps(prune_items()))
        return
    if getattr(args, "prune_noise", False):
        print(json.dumps(prune_noise(dry_run=args.dry_run,
                                     min_confidence=args.min_confidence,
                                     min_age_hours=args.min_age_hours)))
        return
    if args.route:
        filename, dest = args.route
        print(json.dumps(ni_route(filename, dest, args.value, args.area)))
        return

    try_ingest_email()

    files = sorted([
        f for f in inbox_path.rglob("*.md")
        if f.is_file() and f.name != ".gitkeep" and _is_unplaced(f)
    ])

    if not files:
        console.print()
        console.print("[grey50]  inbox is empty[/grey50]")
        console.print()
        return

    total = len(files)
    console.print()
    console.print(Rule(f"[bold steel_blue1]  Inbox[/bold steel_blue1]  [grey50]{total} files[/grey50]", style="steel_blue1 dim"))
    console.print()

    for i, file in enumerate(files, 1):
        keep_going = process_file(file, i, total)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
