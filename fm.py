"""fm.py — frontmatter read/edit helpers shared by the schema editors
(`cl systems`, `cl goals`, `cl orientations`).

The guiding principle is **byte-preserving, targeted edits**: to change a field
we rewrite only the line(s) for that key and leave the markdown body plus every
key we don't touch exactly as they were. This is what lets these editors sit on
top of files that use richer YAML than our minimal parser models — a goal's
block-style `systems:`/`projects:` lists survive an edit to its `status:`
because we never re-render the whole frontmatter. Only `new`-file templates are
rendered from scratch, and those we control.

Read side is tolerant: scalars, inline `[a, b]` lists, AND block `- item` lists.
"""

import re
from pathlib import Path

FENCE = "---"
_BLOCK_ITEM = re.compile(r"^\s+-\s+(.*)$")


def split(path: Path):
    """Return (fm_lines, body, had_fm). fm_lines excludes the two fences."""
    text = path.read_text()
    if not text.startswith(FENCE):
        return [], text, False
    lines = text.splitlines()
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == FENCE:
            return lines[1:i], "\n".join(lines[i + 1:]), True
    return [], text, False


def read(path: Path) -> dict:
    """Parse frontmatter into a dict. Handles scalars, inline `[a, b]` lists,
    and block `- item` lists. Values are strings (quotes stripped) or lists."""
    fm_lines, _, ok = split(path)
    meta: dict = {}
    if not ok:
        return meta
    key = None
    for line in fm_lines:
        m = _BLOCK_ITEM.match(line)
        if m and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(m.group(1).strip().strip('"'))
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip()
        v = v.strip()
        if v == "":
            meta[key] = ""                      # may turn into a block list below
        elif v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[key] = v.strip('"')
    return meta


def _fmt(key, value, quote_keys) -> str:
    if isinstance(value, list):
        return f"{key}: [" + ", ".join(str(x) for x in value) + "]"
    s = str(value)
    if key in quote_keys or s == "":
        q = "'" if '"' in s else '"'
        return f"{key}: {q}{s}{q}"
    return f"{key}: {s}"


def set_fields(path: Path, updates: dict, *, quote_keys=()):
    """Apply {key: scalar|list} to a file's frontmatter, in place, preserving
    everything else. A key already present is replaced (and any block-list
    continuation lines under it are dropped, so list↔scalar transitions are
    safe); a missing key is inserted just before the closing fence."""
    text = path.read_text()
    trailing_nl = text.endswith("\n")
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise ValueError(f"{path} has no frontmatter fence")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == FENCE:
            end = i
            break
    if end is None:
        raise ValueError(f"{path} frontmatter is not closed")

    remaining = dict(updates)
    i = 1
    while i < end:
        k = lines[i].split(":", 1)[0].strip()
        if k in remaining:
            lines[i] = _fmt(k, remaining.pop(k), quote_keys)
            # drop any block-list continuation lines belonging to this key
            j = i + 1
            while j < end and _BLOCK_ITEM.match(lines[j]):
                del lines[j]
                end -= 1
        i += 1
    for k, v in remaining.items():              # not present → insert before fence
        lines.insert(end, _fmt(k, v, quote_keys))
        end += 1

    out = "\n".join(lines)
    path.write_text(out + "\n" if trailing_nl else out)


def render(meta: dict, order, *, quote_keys=()) -> str:
    """Render a full frontmatter block for a NEW file. `order` lists keys in
    canonical order; extras follow. Empty values are skipped."""
    lines = [FENCE]
    seen = set()
    for k in order:
        v = meta.get(k)
        if v not in (None, "", []):
            lines.append(_fmt(k, v, quote_keys))
            seen.add(k)
    for k, v in meta.items():
        if k in seen or v in (None, "", []):
            continue
        lines.append(_fmt(k, v, quote_keys))
    lines.append(FENCE)
    return "\n".join(lines)
