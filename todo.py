"""todo.py — the ONE owner of ~/kb/todo.md (2026-10-05 redesign).

The todo list is a plain markdown file, two sections, checkbox lines:

    ## Sooner
    - [ ] call the plumber
    ## Later
    - [ ] research patio and windows

Done means GONE: `done()` deletes the line (no done log — Miro, 2026-10-05: "buy
eggs" doesn't need a timestamp). Git history is the record if one is ever needed.
Every reader and writer (cl, Hermes' MCP tools, nudge, Surface, the router) goes
through here, so the format has exactly one parser.
"""
import re
import sys

from paths import TODO

BANDS = ("Sooner", "Later")
_ITEM = re.compile(r"^\s*- \[( |x|X)\] (.+?)\s*$")


def _read():
    try:
        return TODO.read_text().splitlines()
    except FileNotFoundError:
        return ["# Todo", "", "## Sooner", "", "## Later", ""]


def _write(lines):
    TODO.write_text("\n".join(lines).rstrip("\n") + "\n")


def items():
    """[{band, text, done, line}] in file order. `line` is the 0-based index."""
    band, out = None, []
    for i, ln in enumerate(_read()):
        h = re.match(r"^##\s+(\w+)", ln)
        if h:
            band = h.group(1).capitalize() if h.group(1).capitalize() in BANDS else None
            continue
        m = _ITEM.match(ln)
        if m and band:
            out.append({"band": band, "text": m.group(2), "done": m.group(1) != " ", "line": i})
    return out


def open_items(band=None):
    return [it for it in items() if not it["done"] and (band is None or it["band"] == band.capitalize())]


def add(text, sooner=False):
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty todo")
    lines = _read()
    band = "Sooner" if sooner else "Later"
    try:
        h = next(i for i, ln in enumerate(lines) if re.match(rf"^##\s+{band}\b", ln, re.I))
    except StopIteration:
        lines += ["", f"## {band}"]
        h = len(lines) - 1
    # insert after the last item of this band (or right under the heading)
    at, j = h + 1, h + 1
    while j < len(lines) and not lines[j].startswith("## "):
        if _ITEM.match(lines[j]):
            at = j + 1
        j += 1
    lines.insert(at, f"- [ ] {text}")
    _write(lines)
    return {"band": band, "text": text}


def _find(match):
    words = [w for w in re.findall(r"\w+", match.lower()) if len(w) > 1]
    hits = [it for it in open_items() if all(w in it["text"].lower() for w in words)]
    # The whole line, typed or passed back exactly, is never ambiguous: "Email Sam"
    # must not refuse because "Email Sam about the kitchen" also exists.
    exact = [it for it in hits if it["text"].strip().lower() == str(match).strip().lower()]
    return exact or hits


def done(match):
    """Delete the one open item matching every word of `match`. Refuses ambiguity."""
    hits = _find(match)
    if len(hits) != 1:
        return {"ok": False, "matches": [h["text"] for h in hits]}
    lines = _read()
    del lines[hits[0]["line"]]
    _write(lines)
    return {"ok": True, "removed": hits[0]["text"]}


def sweep():
    """Drop any checked lines (Obsidian/phone check-offs). Returns how many."""
    lines = _read()
    keep = [ln for ln in lines if not (_ITEM.match(ln) and _ITEM.match(ln).group(1) != " ")]
    if len(keep) != len(lines):
        _write(keep)
    return len(lines) - len(keep)


if __name__ == "__main__":
    import json
    cmd, *rest = sys.argv[1:] or ["list"]
    if cmd == "list":
        print(json.dumps(open_items(rest[0] if rest else None), indent=1))
    elif cmd == "add":
        sooner = "--sooner" in rest
        print(json.dumps(add(" ".join(r for r in rest if r != "--sooner"), sooner)))
    elif cmd == "done":
        print(json.dumps(done(" ".join(rest))))
    elif cmd == "sweep":
        print(sweep())
