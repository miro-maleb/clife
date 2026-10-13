"""projects.py — the ONE owner of ~/kb/threads/INDEX.md (2026-10-08).

INDEX is the project list and the only place a project's state lives:

    ## Active
    - **garden** — raised beds; next: buy lumber
    ## Ideas
    - **shed** — no thread yet — build a shed
    ## Asleep
    ## Reference

Ideas and projects are one thing in two states; moving the line changes it.
An Active project keeps 2-3 lines in todo.md, tied to it by ending with
`(threads/<name>.md)`; the rest of its plan lives in its thread. A name with
no threads/<name>.md file is fine until someone needs one.

THE QUEUE (2026-10-08): a thread's first `## Next` section is a plain list of
upcoming steps, in order. `refill()` tops an Active project back up to
REFILL_TO lines by moving steps off the front of its queue into Sooner, so
crossing a step off anywhere (phone, Desk, gemma, nvim) brings the next one
without anyone remembering to. The router timer calls `refill_all()` every
5 minutes; Hermes' todo_done calls `refill()` at once.

Every reader and writer (Hermes' MCP tools, the Desk, Bridge) goes through
here, so the format has exactly one parser — the lesson of four hand-rolled
"has tags?" readers disagreeing in one day.
"""
import difflib
import re
import sys

import todo
from paths import PROJECTS, THREADS

BANDS = ("Active", "Ideas", "Asleep", "Reference")
TODO_LINES = 3          # an Active project's share of todo.md, at most
REFILL_TO = 2           # refill() tops an Active project up to this many lines
_LINE = re.compile(r"^- \*\*(.+?)\*\* — (.*)$")


def _read():
    return PROJECTS.read_text().splitlines()


def _write(lines):
    PROJECTS.write_text("\n".join(lines).rstrip("\n") + "\n")


def items():
    """[{band, name, note, line}] in file order. `line` is the 0-based index."""
    band, out = None, []
    for i, ln in enumerate(_read()):
        h = re.match(r"^##\s+(\w+)", ln)
        if h:
            band = h.group(1).capitalize() if h.group(1).capitalize() in BANDS else None
            continue
        m = _LINE.match(ln)
        if m and band:
            out.append({"band": band, "name": m.group(1), "note": m.group(2).strip(), "line": i})
    return out


def find(name):
    """The project called `name` (case-insensitive), or None."""
    key = (name or "").strip().lower()
    return next((p for p in items() if p["name"].lower() == key), None)


def suggest(name):
    return difflib.get_close_matches((name or "").lower(), [p["name"] for p in items()], n=3, cutoff=0.4)


def tag(name):
    """What a todo line ends with to belong to `name`."""
    return f"(threads/{name}.md)"


def todo_lines(name):
    """This project's open todo lines: [{band, text, ...}]."""
    t = f"threads/{name}.md"
    return [it for it in todo.open_items() if t in it["text"]]


_NEXT_HEAD = re.compile(r"^##\s+Next\s*$", re.I)
_QITEM = re.compile(r"^- (?:\[( |x|X)\] )?(.+?)\s*$")


def _queue(lines):
    """[(first, last, text)] open items of the first `## Next` section. An item
    is a `- x` or `- [ ] x` line plus any indented lines under it; checked
    items are skipped. `first`/`last` are 0-based line indexes, inclusive."""
    try:
        h = next(i for i, ln in enumerate(lines) if _NEXT_HEAD.match(ln))
    except StopIteration:
        return []
    out, i = [], h + 1
    while i < len(lines) and not lines[i].startswith("## "):
        m = _QITEM.match(lines[i])
        if not m:
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and lines[j + 1][:1] in (" ", "\t") and lines[j + 1].strip():
            j += 1
        text = " ".join([m.group(2)] + [ln.strip() for ln in lines[i + 1:j + 1]])
        if m.group(1) in (None, " "):
            out.append((i, j, text))
        i = j + 1
    return out


def queue(name):
    """A project's upcoming steps, in order (its thread's `## Next` list)."""
    f = THREADS / f"{name}.md"
    return [t for _, _, t in _queue(f.read_text().splitlines())] if f.is_file() else []


def enqueue(name, text):
    """Add a step to the end of a project's queue (its thread's first `## Next`
    list), making the section under the header if there is none. Returns the
    step. An Active project's refill() may then move it to todo.md."""
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty step")
    p = find(name)
    if not p:
        raise KeyError(name)
    f = THREADS / f"{p['name']}.md"
    lines = f.read_text().splitlines() if f.is_file() else [f"# {p['name']}", ""]
    h = next((i for i, ln in enumerate(lines) if _NEXT_HEAD.match(ln)), None)
    if h is None:
        at = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        lines[at:at] = ["## Next", f"- {text}", ""]
    else:
        end = h + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        last = end - 1
        while last > h and not lines[last].strip():
            last -= 1
        lines.insert(last + 1, f"- {text}")
    f.write_text("\n".join(lines).rstrip("\n") + "\n")
    return text


def refill(name):
    """Top an ACTIVE project up to REFILL_TO todo lines from its queue, into
    Sooner. Each step is added to todo.md first, then cut from the thread, so a
    crash in between duplicates a step rather than losing one. Returns the
    steps added."""
    p = find(name)
    if not p or p["band"] != "Active":
        return []
    f = THREADS / f"{p['name']}.md"
    added = []
    while len(todo_lines(p["name"])) < REFILL_TO and f.is_file():
        lines = f.read_text().splitlines()
        q = _queue(lines)
        if not q:
            break
        first, last, text = q[0]
        todo.add(f"{text} {tag(p['name'])}", sooner=True)
        del lines[first:last + 1]
        f.write_text("\n".join(lines).rstrip("\n") + "\n")
        added.append(text)
    return added


def refill_all():
    """{name: [steps added]} for every Active project that needed topping up."""
    out = {}
    for p in items():
        if p["band"] == "Active":
            got = refill(p["name"])
            if got:
                out[p["name"]] = got
    return out


def has_thread(name):
    return (THREADS / f"{name}.md").exists()


def _band_end(lines, band):
    """Index to insert a new line at the end of `band`, creating the heading if missing."""
    try:
        h = next(i for i, ln in enumerate(lines) if re.match(rf"^##\s+{band}\b", ln, re.I))
    except StopIteration:
        lines += ["", f"## {band}"]
        return len(lines)
    at, j = h + 1, h + 1
    while j < len(lines) and not lines[j].startswith("## "):
        if _LINE.match(lines[j]):
            at = j + 1
        j += 1
    return at


def _band(b):
    b = (b or "").strip().capitalize()
    b = {"Idea": "Ideas", "Sleep": "Asleep", "Sleeping": "Asleep", "Parked": "Asleep",
         "Paused": "Asleep", "Wake": "Active", "Awake": "Active", "Ref": "Reference"}.get(b, b)
    if b not in BANDS:
        raise ValueError(f"band must be one of {', '.join(BANDS)}")
    return b


def move(name, band):
    """Move a project's line to the end of `band`. Returns {name, from, to}."""
    band = _band(band)
    p = find(name)
    if not p:
        raise KeyError(name)
    if p["band"] == band:
        return {"name": p["name"], "from": band, "to": band, "unchanged": True}
    lines = _read()
    text = lines.pop(p["line"])
    lines.insert(_band_end(lines, band), text)
    _write(lines)
    return {"name": p["name"], "from": p["band"], "to": band}


def set_note(name, note):
    """Replace the note after a project's name (its state in a sentence)."""
    note = " ".join(str(note).split())
    if not note:
        raise ValueError("empty note")
    p = find(name)
    if not p:
        raise KeyError(name)
    lines = _read()
    lines[p["line"]] = f"- **{p['name']}** — {note}"
    _write(lines)
    return {"name": p["name"], "band": p["band"], "note": note}


def add(name, note, band="Ideas"):
    """A new project or idea. `name` becomes lowercase-kebab."""
    band = _band(band)
    name = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    note = " ".join(str(note).split())
    if not name or not note:
        raise ValueError("a project needs a name and a note")
    if find(name):
        raise FileExistsError(name)
    lines = _read()
    lines.insert(_band_end(lines, band), f"- **{name}** — {note}")
    _write(lines)
    return {"name": name, "band": band, "note": note}


if __name__ == "__main__":
    import json
    cmd, *rest = sys.argv[1:] or ["list"]
    if cmd == "list":
        b = _band(rest[0]) if rest else None
        out = [dict(p, todo=[t["text"] for t in todo_lines(p["name"])], queue=queue(p["name"]))
               for p in items() if b is None or p["band"] == b]
        print(json.dumps(out, indent=1, ensure_ascii=False))
    elif cmd == "move":
        print(json.dumps(move(rest[0], rest[1])))
    elif cmd == "note":
        print(json.dumps(set_note(rest[0], " ".join(rest[1:]))))
    elif cmd == "add":
        print(json.dumps(add(rest[0], " ".join(rest[2:]), rest[1])))
    elif cmd == "queue":
        print(json.dumps(queue(rest[0]), ensure_ascii=False))
    elif cmd == "refill":
        print(json.dumps(refill(rest[0]) if rest else refill_all(), ensure_ascii=False))
    else:
        sys.exit("usage: projects.py list [band] | move NAME BAND | note NAME TEXT | add NAME BAND NOTE"
                 " | queue NAME | refill [NAME]")
