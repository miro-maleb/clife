"""daily.py — `cl daily`: the writable day model for the block writing surface.

A daily note (~/kb/daily/YYYY-MM-DD.md) is a 2-line header (`Mon  27 Jul` + rule)
followed by blocks separated by thematic-break rules (---/***/___), with `#tags`
dropped anywhere inside a block. This is the header-aware, *writable* view of that
file — read it as ordered blocks, append a block, or replace/delete one. Block
boundaries + tag extraction come from tags.py, so the lens, `cl tags`, and nvim
share one definition of "what a block is".

  cl daily [--date D] [--json]      the day as ordered blocks
  cl daily [--date D] --append      append a block (text on stdin)
  cl daily [--date D] --set N [--if-mtime M]   replace block N (text on stdin;
                                               empty text deletes it)

Writes take block text on STDIN, not argv, so a block that starts with "- " or
"#" can't be mistaken for a flag.
"""
import argparse
import json
import sys
from datetime import date as _date
from pathlib import Path

from paths import KB, STORE
from tags import is_break, tags_in

# Daily notes live in the writing stream, sharded by month like every other
# note — ~/kb/daily was retired 2026-09-03 (its entries were moved into the
# shards) and nine code references were still writing into the empty stub.
DAILY = STORE


def day_path(date=None):
    d = date or _date.today().isoformat()
    return DAILY / d[:4] / d[5:7] / f"{d}.md", d


def split_frontmatter(raw):
    """(frontmatter, rest). Daily notes carry `tags: [journal]` so they do NOT
    show up in the inbox view, which is the set of UNTAGGED notes. That block is
    delimited by `---`, the same marker this file uses to separate blocks, so it
    has to come off before any block parsing — and be put back on write, or an
    append would silently strip the tag and park the note in the routing queue."""
    if not raw.startswith("---\n"):
        return "", raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return "", raw
    return raw[:end + 5], raw[end + 5:].lstrip("\n")


def split_header(raw):
    """(header, body). Header = a first text line immediately underlined by a rule
    (the `Mon  27 Jul` / `------` daily header), else ''."""
    lines = raw.split("\n")
    if len(lines) >= 2 and lines[0].strip() and is_break(lines[1]):
        return "\n".join(lines[:2]), "\n".join(lines[2:])
    return "", raw


def split_blocks(body):
    """Body → list of trimmed block texts, split on rule lines."""
    blocks, cur = [], []

    def flush():
        b = cur[:]
        while b and not b[0].strip():
            b.pop(0)
        while b and not b[-1].strip():
            b.pop()
        if b:
            blocks.append("\n".join(b))

    for line in body.split("\n"):
        if is_break(line):
            flush()
            cur.clear()
        else:
            cur.append(line)
    flush()
    return blocks


def _default_header(d):
    dt = _date.fromisoformat(d)
    line = f"{dt.strftime('%A')}     {dt.day} {dt.strftime('%b')}"
    return line + "\n" + "-" * len(line)


def model(date=None):
    path, d = day_path(date)
    raw = path.read_text(errors="replace") if path.exists() else ""
    fm, raw = split_frontmatter(raw)
    header, body = split_header(raw)
    blocks = split_blocks(body)
    return {
        "date": d,
        "path": str(path),
        "exists": path.exists(),
        "mtime": (path.stat().st_mtime if path.exists() else 0.0),
        "frontmatter": fm,
        "header": header,
        "blocks": [{"i": i, "tags": tags_in(t), "text": t}
                   for i, t in enumerate(blocks)],
    }


def _reassemble(header, blocks, frontmatter=""):
    body = "\n\n---\n\n".join(b.strip() for b in blocks if b.strip())
    text = (header.rstrip("\n") + "\n\n" + body) if header else body
    text = text.rstrip("\n") + "\n"
    return (frontmatter.rstrip("\n") + "\n\n" + text) if frontmatter else text


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def append_block(date, text):
    path, d = day_path(date)
    m = model(date)
    header = m["header"] or _default_header(d)
    blocks = [b["text"] for b in m["blocks"]]
    blocks.append(text.strip())
    _write(path, _reassemble(header, blocks, m.get("frontmatter", "")))
    return model(date)


def set_block(date, index, text, if_mtime=None):
    path, d = day_path(date)
    if if_mtime not in (None, "") and path.exists():
        cur = path.stat().st_mtime
        if abs(cur - float(if_mtime)) > 0.001:
            return {"ok": False, "error": "stale", "mtime": cur}
    m = model(date)
    header = m["header"] or _default_header(d)
    blocks = [b["text"] for b in m["blocks"]]
    if index < 0 or index >= len(blocks):
        return {"ok": False, "error": "bad index"}
    if text.strip():
        blocks[index] = text.strip()
    else:
        del blocks[index]          # a blanked-out edit deletes the block
    _write(path, _reassemble(header, blocks, m.get("frontmatter", "")))
    return {"ok": True, **model(date)}


def main():
    ap = argparse.ArgumentParser(
        prog="cl daily",
        description="The block-based daily writing surface.")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--json", action="store_true", help="emit the day as JSON")
    ap.add_argument("--append", action="store_true",
                    help="append a block (text on stdin)")
    ap.add_argument("--set", type=int, metavar="N",
                    help="replace block N (text on stdin; empty deletes)")
    ap.add_argument("--if-mtime", help="reject --set if the file changed since")
    a = ap.parse_args()

    if a.append:
        print(json.dumps(append_block(a.date, sys.stdin.read())))
        return
    if a.set is not None:
        print(json.dumps(set_block(a.date, a.set, sys.stdin.read(), a.if_mtime)))
        return

    m = model(a.date)
    if a.json:
        print(json.dumps(m))
        return
    if m["header"]:
        print(m["header"])
    for b in m["blocks"]:
        tagstr = "  ".join("#" + t for t in b["tags"])
        print(f"\n[{b['i']}] {tagstr}".rstrip())
        print(b["text"])


if __name__ == "__main__":
    main()
