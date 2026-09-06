"""stream.py — `cl stream` — the view engine over ~/kb/writing/_stream.

The stream has good capture and no way to LOOK at what's in it. Tags accumulate
and nothing groups them. This is the missing half: one query engine, several
faces (terminal here, nvim buffer, Surface, Hermes) — so the grouping logic
lives in exactly one place and every surface agrees.

The design commitment: a view is a QUERY, never a file. Nothing here writes a
derived copy of your notes that could drift from them. `render` emits a
read-only artifact to outbox/ for eyeballing, and that is explicitly derived —
regenerate it, never edit it.

An agenda item is a stream note with `status: todo`.

  tags:    what it is ABOUT      (home, network, blog/x)  — orthogonal
  status:  what STATE it is in   (todo / done / archived)
  when:    the day you MEAN to do it (not a deadline — see cl stream --help)

Roll-forward is free and deliberate: nothing is ever moved or rewritten when a
day passes. An undone item dated Tuesday stays an undone item dated Tuesday;
the TODAY view simply chooses to surface it under `overdue`. That is why this
is a view — a file would need a morning sweep, and a sweep is a rewrite, and a
rewrite is where a week's planning goes missing.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fm  # noqa: E402

KB = Path(os.environ.get("KB_DIR", str(Path.home() / "kb")))
STREAM = KB / "writing" / "_stream"
OUTBOX = KB / "outbox" / "reports"

TODO, DONE, ARCHIVED = "todo", "done", "archived"

_DAILY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$")
# A line that is nothing but inline tags — `#home`, `#update/daily #idea`.
# These lead a lot of older captures and make a terrible title.
_TAGLINE = re.compile(r"^\s*(?:#[\w/\-]+\s*)+$")
_WEEKDAYS = {d: i for i, d in enumerate(
    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


# ── reading ────────────────────────────────────────────────────────────────

def _norm_dt(raw):
    """Frontmatter timestamps come in three shapes across the stream's history:
    `2026-09-01T11:57:43`, `2026-09-01 11:57`, and bare `2026-09-01`. Normalise
    or the same day interleaves by FORMAT instead of by time when sorted."""
    if not raw:
        return None
    s = str(raw).strip().strip('"').strip("'").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_date(raw):
    dt = _norm_dt(raw)
    return dt.date() if dt else None


def _title(body: str) -> str:
    """First markdown heading, else the first line that carries actual words.

    Skipping pure-tag lines matters: `bondo-flashing-bear-damage.md` opens with
    a bare `#home`, and a view titled "#home" four times over is useless."""
    for line in body.splitlines():
        s = line.strip()
        if not s or s == "---":
            continue
        m = _HEADING.match(s)
        if m:
            s = m.group(1).strip()
        elif _TAGLINE.match(s):
            continue
        s = s.strip()
        if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()      # a matched pair only — `"Bondo" plus …`
        return s if len(s) <= 90 else s[:87] + "…"
    return "(empty)"


def _tags(meta) -> list:
    t = meta.get("tags", [])
    if isinstance(t, str):
        t = [x.strip() for x in t.strip("[]").split(",") if x.strip()]
    return [str(x).strip().lstrip("#") for x in t if str(x).strip()]


def load(include_daily=False) -> list:
    """Every stream note as a plain dict. Cheap enough to do on every call —
    the stream is a few hundred small files — which keeps every face reading
    live state instead of a cache that can be stale or wrong."""
    items = []
    if not STREAM.is_dir():
        return items
    today = date.today()
    for path in sorted(STREAM.rglob("*.md")):
        if path.name.startswith((".", "_")) or path.name == "README.md":
            continue
        if not include_daily and _DAILY.match(path.stem):
            continue          # the daily note is a writing surface, not an item
        try:
            meta = fm.read(path)
            _, body, _ = fm.split(path)
        except OSError:
            continue
        # `captured:` wins over `created:` where both exist — the stream's own
        # rule, and some notes carry both with different values.
        created = _norm_dt(meta.get("captured") or meta.get("created"))
        status = str(meta.get("status", "") or "").strip().lower()
        when = _norm_date(meta.get("when"))
        items.append({
            "slug": path.stem,
            "path": str(path),
            "relpath": str(path.relative_to(KB)),
            "title": _title(body),
            "tags": _tags(meta),
            "status": status,
            "when": when.isoformat() if when else None,
            "created": created.strftime("%Y-%m-%d %H:%M") if created else None,
            "age_days": (today - created.date()).days if created else None,
        })
    return items


# ── grouping ───────────────────────────────────────────────────────────────

def agenda_groups(items, today=None):
    """Bucket todo items by intent date. Order is the order you read them in."""
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=6)
    groups = {k: [] for k in
              ("overdue", "today", "tomorrow", "week", "later", "pool")}
    for it in items:
        if it["status"] != TODO:
            continue
        w = date.fromisoformat(it["when"]) if it["when"] else None
        if w is None:
            groups["pool"].append(it)
        elif w < today:
            groups["overdue"].append(it)
        elif w == today:
            groups["today"].append(it)
        elif w == tomorrow:
            groups["tomorrow"].append(it)
        elif w <= week_end:
            groups["week"].append(it)
        else:
            groups["later"].append(it)
    for k in ("overdue", "week", "later"):
        groups[k].sort(key=lambda i: i["when"] or "")
    groups["pool"].sort(key=lambda i: -(i["age_days"] or 0))
    return groups


# ── writing ────────────────────────────────────────────────────────────────

def resolve_when(text, today=None):
    """`today` `tomorrow` `fri` `+3d` `+2w` `2026-09-08` `none`.

    A weekday resolves to TODAY when it matches, otherwise the next one — you
    say 'put it on friday' on a Friday and you mean today, not next week."""
    today = today or date.today()
    s = (text or "").strip().lower()
    if s in ("", "none", "clear", "-"):
        return None
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"\+(\d+)([dw])", s)
    if m:
        n = int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2) == "w" else 1))
    key = s[:3]
    if key in _WEEKDAYS:
        delta = (_WEEKDAYS[key] - today.weekday()) % 7
        return today + timedelta(days=delta)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"cl stream: can't read a date from '{text}'")


def find(slug, items=None):
    """Exact stem, else unique substring. Ambiguity is an error, never a guess —
    the whole point of file-as-identity is that it can't pick the wrong note."""
    items = items if items is not None else load()
    exact = [i for i in items if i["slug"] == slug]
    if exact:
        return exact[0]
    part = [i for i in items if slug.lower() in i["slug"].lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise SystemExit(f"cl stream: no stream note matching '{slug}'")
    names = "\n  ".join(i["slug"] for i in part[:10])
    raise SystemExit(f"cl stream: '{slug}' matches {len(part)} notes:\n  {names}")


def apply_set(item, *, status=None, when="__keep__", add_tags=None):
    """One targeted frontmatter edit. fm.set_fields rewrites only the keys named
    and leaves the body plus untouched keys byte-identical — which is why no
    surface here ever needs to re-emit a whole note."""
    path = Path(item["path"])
    updates = {}
    if status is not None:
        updates["status"] = status
    if when != "__keep__":
        updates["when"] = when.isoformat() if when else ""
    if add_tags:
        merged = list(dict.fromkeys(item["tags"] + list(add_tags)))
        updates["tags"] = merged
    if not updates:
        return {}
    fm.set_fields(path, updates)
    return updates


# ── rendering ──────────────────────────────────────────────────────────────

_LABELS = {"overdue": "overdue", "today": "today", "tomorrow": "tomorrow",
           "week": "this week", "later": "upcoming", "pool": "pool"}


def _color(on):
    if not on:
        return lambda s, _c: s
    codes = {"dim": "2", "red": "31", "yellow": "33", "cyan": "36", "bold": "1"}
    return lambda s, c: f"\033[{codes[c]}m{s}\033[0m"


def render_agenda(groups, today=None, color=False, show_pool=True):
    today = today or date.today()
    c = _color(color)
    out = [c(f"  AGENDA{' ' * 34}{today.strftime('%-d %b, %A')}", "bold"), ""]
    for key in ("overdue", "today", "tomorrow", "week", "later", "pool"):
        rows = groups[key]
        if not rows or (key == "pool" and not show_pool):
            continue
        label = _LABELS[key]
        if key == "pool":
            label = f"pool ({len(rows)})"
        out.append(c(f"  ── {label} " + "─" * max(0, 56 - len(label)), "dim"))
        for it in rows:
            when = ""
            if key in ("overdue", "week", "later") and it["when"]:
                d = date.fromisoformat(it["when"])
                when = d.strftime("%-d %b" if key == "later" else "%a")
            mark = c("⚠", "red") if key == "overdue" else c("·", "dim")
            tags = " ".join("#" + t for t in it["tags"])
            line = f"  {mark} {when:<7} {it['title']}"
            if tags:
                pad = max(1, 66 - len(f"  · {when:<7} {it['title']}"))
                line += " " * pad + c(tags, "cyan")
            out.append(line)
        out.append("")
    if not any(groups[k] for k in groups):
        out.append(c("  nothing marked `status: todo` yet.", "dim"))
        out.append(c("  mark one:  cl stream set <slug> --todo --when fri", "dim"))
        out.append("")
    return "\n".join(out)


def render_list(rows, color=False, header=""):
    c = _color(color)
    out = ([c(f"  {header}", "bold"), ""] if header else [])
    for it in rows:
        tags = " ".join("#" + t for t in it["tags"])
        age = f"{it['age_days']}d" if it["age_days"] is not None else ""
        out.append(f"  {c(age.rjust(5), 'dim')}  {it['title'][:58]:<58} {c(tags, 'cyan')}")
    if not rows:
        out.append(c("  (nothing)", "dim"))
    return "\n".join(out) + "\n"


# ── cli ────────────────────────────────────────────────────────────────────

def _emit(payload, args, text):
    if args.json:
        print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
    else:
        print(text)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cl stream",
        description="views over the capture stream. a view is a query, not a file.")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("agenda", help="todo items grouped by intent date")
    a.add_argument("--json", action="store_true")
    a.add_argument("--no-pool", action="store_true", help="hide the undated pool")

    i = sub.add_parser("inbox", help="untagged notes — nobody has decided yet")
    i.add_argument("--json", action="store_true")

    t = sub.add_parser("tag", help="every note carrying a tag (hierarchical)")
    t.add_argument("tag")
    t.add_argument("--json", action="store_true")

    l = sub.add_parser("ls", help="filter the whole stream")
    l.add_argument("--status", default=None)
    l.add_argument("--tag", default=None)
    l.add_argument("--stale", type=int, default=None, metavar="DAYS")
    l.add_argument("--json", action="store_true")

    s = sub.add_parser("set", help="edit one note's status / when / tags")
    s.add_argument("slug")
    s.add_argument("--todo", action="store_true")
    s.add_argument("--done", action="store_true")
    s.add_argument("--archive", action="store_true")
    s.add_argument("--status", default=None)
    s.add_argument("--when", default=None, help="today|tomorrow|fri|+3d|2026-09-08|none")
    s.add_argument("--tag", default=None, help="comma-separated tags to ADD")
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("render", help="write the read-only agenda artifact to outbox/")
    r.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd, args.json, args.no_pool = "agenda", False, False

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    items = load()

    if args.cmd == "agenda":
        g = agenda_groups(items)
        _emit({"today": date.today().isoformat(),
               "groups": [{"key": k, "label": _LABELS[k], "items": g[k]}
                          for k in ("overdue", "today", "tomorrow", "week", "later", "pool")]},
              args, render_agenda(g, color=color, show_pool=not args.no_pool))

    elif args.cmd == "inbox":
        rows = [i for i in items if not i["tags"]]
        rows.sort(key=lambda i: i["created"] or "")
        _emit(rows, args, render_list(rows, color, f"INBOX ({len(rows)})"))

    elif args.cmd == "tag":
        q = args.tag.lstrip("#").rstrip("/")
        rows = [i for i in items
                if any(t == q or t.startswith(q + "/") for t in i["tags"])]
        rows.sort(key=lambda i: i["created"] or "", reverse=True)
        _emit(rows, args, render_list(rows, color, f"#{q} ({len(rows)})"))

    elif args.cmd == "ls":
        rows = items
        if args.status is not None:
            rows = [i for i in rows if i["status"] == args.status]
        if args.tag:
            q = args.tag.lstrip("#")
            rows = [i for i in rows
                    if any(t == q or t.startswith(q + "/") for t in i["tags"])]
        if args.stale is not None:
            rows = [i for i in rows if (i["age_days"] or 0) >= args.stale]
        rows.sort(key=lambda i: i["created"] or "", reverse=True)
        _emit(rows, args, render_list(rows, color, f"STREAM ({len(rows)})"))

    elif args.cmd == "set":
        it = find(args.slug, items)
        status = args.status
        if args.todo:
            status = TODO
        if args.done:
            status = DONE
        if args.archive:
            status = ARCHIVED
        when = "__keep__" if args.when is None else resolve_when(args.when)
        tags = [x.strip().lstrip("#") for x in (args.tag or "").split(",") if x.strip()]
        got = apply_set(it, status=status, when=when, add_tags=tags or None)
        changed = ", ".join(f"{k}: {v or '(cleared)'}" for k, v in got.items())
        _emit({"slug": it["slug"], "path": it["path"], "applied": got}, args,
              f"  {it['title']}\n  → {changed or 'nothing to change'}")

    elif args.cmd == "render":
        g = agenda_groups(items)
        OUTBOX.mkdir(parents=True, exist_ok=True)
        dest = OUTBOX / "agenda.md"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        head = ("---\n"
                f"created: {stamp}\n"
                "tags: [report]\n"
                "---\n\n"
                "# Agenda\n\n"
                f"*Derived view, regenerated {stamp}. Do not edit — "
                "edit the notes, or use the agenda view.*\n\n")
        body = []
        for key in ("overdue", "today", "tomorrow", "week", "later", "pool"):
            if not g[key]:
                continue
            body.append(f"## {_LABELS[key]}\n")
            for it in g[key]:
                w = f"`{it['when']}` " if it["when"] else ""
                tg = " ".join("#" + t for t in it["tags"])
                body.append(f"- {w}{it['title']} {tg}".rstrip())
            body.append("")
        dest.write_text(head + "\n".join(body) + "\n")
        _emit({"wrote": str(dest), "items": sum(len(v) for v in g.values())},
              args, f"  wrote {dest}")


if __name__ == "__main__":
    main()
