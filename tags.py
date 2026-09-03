"""tags.py — `cl tags`: the daily-note block parser + inline-tag index.

The daily notes (~/kb/daily/) are a freeform longform surface. The one
convention: a line of 3+ repeated `-`, `*`, or `_` is a *block break*. A
**block** is the text between two breaks (or file start/end). Tags are `#foo`
tokens dropped anywhere inside a block. That's it — no bullets, no headings
required, tags live wherever.

This module is the single block-parser. `cl tags` reads it two ways:

  cl tags [--list]        distinct tags + how many blocks carry each
  cl tags TAG             every block carrying TAG (hierarchical: `book`
                          also matches `book/dogen-...`)

Add --json to either for machines (the Telescope picker in clife.nvim). The
same parser will later back the chopper/router (`cl sift`) — one definition of
"what a block is", reused, so the viewer and the router can never disagree.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import fm
from paths import KB

# A block break: a line that is only 3+ of the same thematic-break char.
# Matches your `---` / `***`, and also the daily header's `----------` rule.
_BREAK_CHARS = set("-*_")


def is_break(line):
    s = line.strip()
    if len(s) < 3:
        return False
    c = s[0]
    return c in _BREAK_CHARS and all(ch == c for ch in s)


# `#tag` anywhere, not mid-word, not `##heading`. Allows a/b hierarchy and -_.
_TAG_RE = re.compile(r'(?:^|(?<=[^\w#]))#([A-Za-z][\w/-]*)')


# Code is not prose: `#define`, `#include`, a `#!/bin/sh` shebang and a `#`
# comment inside a fenced block are not tags. Stripping code before matching
# fixes the INDEX as well as the frontmatter sync — one rule, so `cl tags`
# and `cl tags --sync` can never disagree about what a tag is.
_FENCE_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_code(text):
    return _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", text))


# Stripping code spans isn't enough: `#define` written in prose, and a bare hex
# colour like `#e8a34e`, both match "# then a letter". Surface's capture.py hit
# this first and excludes them by name — mirrored here so the two parsers agree.
# It decides routing now, not just the index: a note mentioning a hex colour
# would count as TAGGED and drop out of the untagged inbox view untouched.
_HEX_RE = re.compile(r'^[0-9a-fA-F]{3,8}$')
_NOT_TAGS = {"include", "define", "ifdef", "ifndef", "endif", "pragma",
             "elif", "undef", "import", "if", "else", "error", "warning"}


def tags_in(text):
    """Distinct tags in a chunk of text, order-stable, trailing /- trimmed."""
    text = strip_code(text)
    seen = []
    for m in _TAG_RE.findall(text):
        t = m.rstrip('/-')
        if not t or t.lower() in _NOT_TAGS or _HEX_RE.match(t):
            continue
        if t not in seen:
            seen.append(t)
    return seen


def _daily_date(path):
    """The YYYY-MM-DD from a daily filename, or '' if it isn't one."""
    stem = Path(path).stem
    return stem if re.fullmatch(r'\d{4}-\d{2}-\d{2}', stem) else ''


def parse_blocks(path):
    """Split one file into blocks. Returns a list of dicts:
       {path, relpath, date, line, tags, text, preview}
    line is the 1-based line of the block's first non-blank line (jump target).
    """
    path = Path(path)
    try:
        raw = path.read_text(errors='replace')
    except OSError:
        return []
    lines = raw.split('\n')
    n = len(lines)

    # Skip a leading YAML frontmatter block (only structured files have one;
    # dailies don't — this just keeps --all from choking on them).
    i = 0
    if i < n and lines[i].strip() == '---':
        j = i + 1
        while j < n and lines[j].strip() != '---':
            j += 1
        if j < n:
            i = j + 1

    try:
        rel = str(path.relative_to(KB))
    except ValueError:
        rel = str(path)
    date = _daily_date(path)

    blocks = []
    cur = []  # list of (lineno, text)

    def flush():
        b = cur[:]
        while b and not b[0][1].strip():
            b.pop(0)
        while b and not b[-1][1].strip():
            b.pop()
        if not b:
            return
        text = '\n'.join(t for _, t in b)
        tags = tags_in(text)
        preview = next((t.strip() for _, t in b if t.strip()), '')
        if len(preview) > 90:
            preview = preview[:87] + '…'
        blocks.append({
            'path': str(path),
            'relpath': rel,
            'date': date,
            'line': b[0][0],
            'tags': tags,
            'text': text,
            'preview': preview,
        })

    for idx in range(i, n):
        line = lines[idx]
        if is_break(line):
            flush()
            cur = []
        else:
            cur.append((idx + 1, line))
    flush()
    return blocks


def _skip(p):
    """Non-content files that live alongside daily notes (the folder README, lint
    state, dotfiles) — never blocks to index."""
    return p.name == 'README.md' or p.name.startswith(('_', '.'))


def _iter_files(scope):
    if scope.is_file():
        yield scope
        return
    if scope == KB:  # --all: whole kb, minus noise dirs
        skip = {'.git', '.trash', 'archive', '.obsidian', '_state', '_lint'}
        for p in sorted(scope.rglob('*.md')):
            if any(part in skip for part in p.relative_to(KB).parts) or _skip(p):
                continue
            yield p
        return
    # rglob, not glob: a --path directory was scanned only one level deep, so
    # `cl tags --path ~/kb/writing/_stream` silently found nothing once the
    # notes moved into YYYY/MM subfolders. Only the --all branch recursed.
    for p in sorted(scope.rglob('*.md')):
        if not _skip(p):
            yield p


def gather_blocks(scope):
    out = []
    for f in _iter_files(scope):
        out.extend(parse_blocks(f))
    return out


def _matches(block_tags, query):
    """Hierarchical tag match: `book` matches `book` and `book/anything`."""
    q = query.lstrip('#').rstrip('/-')
    return any(t == q or t.startswith(q + '/') for t in block_tags)


# ── writing side: fold inline tags into frontmatter ─────────────────────────
# `cl tags` has always been read-only — it indexes inline tags but nothing ever
# wrote them anywhere. That left two tag systems side by side that never agreed:
# inline `#tag` in the body, and frontmatter `tags: [...]`. It matters now that
# "the inbox" is a QUERY over untagged notes rather than a folder: a note tagged
# `#home` inline but empty in frontmatter reads as untagged forever, so tagging
# it does nothing.
#
# Union, not replace. Replace would make frontmatter purely derived (cleaner,
# always regenerable) but would delete hand-added frontmatter tags that have no
# inline twin — e.g. `tags: [network, hardware, purchase]` on a note whose body
# never says `#network`. Union's cost is that deleting an inline tag doesn't
# remove it from frontmatter; that's the imperfection we chose, deliberately,
# because it is non-destructive.
def sync_file(path: Path):
    """Union body #tags into frontmatter `tags:`. Returns (added, final) or None."""
    _, body, had_fm = fm.split(path)
    found = tags_in(body)
    if not found:
        return None

    existing = fm.read(path).get('tags') if had_fm else []
    if isinstance(existing, str):
        existing = [existing] if existing else []
    existing = list(existing or [])

    merged, added = list(existing), []
    for t in found:
        if t not in merged:
            merged.append(t)
            added.append(t)
    if not added:
        return None

    if not had_fm:
        # No frontmatter to edit — give the file a minimal block. fm.set_fields
        # refuses (correctly) to invent a fence, so do it here where the intent
        # is explicit.
        text = path.read_text()
        path.write_text("---\ntags: []\n---\n\n" + text)
    fm.set_fields(path, {'tags': merged})
    return added, merged


def main():
    ap = argparse.ArgumentParser(
        prog='cl tags',
        description='Index and view inline #tags across the daily notes.')
    ap.add_argument('tag', nargs='?',
                    help='show every block carrying this tag (hierarchical)')
    ap.add_argument('--list', action='store_true',
                    help='list distinct tags + block counts (the default)')
    ap.add_argument('--all', action='store_true',
                    help='scan the whole kb, not just daily/')
    ap.add_argument('--path', metavar='PATH',
                    help='scan a specific file or directory instead')
    ap.add_argument('--json', action='store_true', help='machine output')
    ap.add_argument('--sync', action='store_true',
                    help='fold body #tags into frontmatter tags: (union)')
    args = ap.parse_args()

    if args.sync:
        scope = Path(args.path).expanduser() if args.path else KB
        changed = 0
        for f in _iter_files(scope):
            r = sync_file(f)
            if r:
                changed += 1
                if not args.json:
                    try:
                        rel = f.relative_to(KB)
                    except ValueError:
                        rel = f          # --path may point outside the kb
                    print(f"{rel}: +{', '.join(r[0])}")
        if args.json:
            print(json.dumps({'changed': changed}))
        else:
            print(f"{changed} file(s) updated")
        return

    if args.path:
        scope = Path(args.path).expanduser()
    elif args.all:
        scope = KB
    else:
        scope = KB / 'daily'

    if not scope.exists():
        if args.json:
            print('[]')
        else:
            print(f'  nothing to scan: {scope}')
        return

    blocks = gather_blocks(scope)

    # TAG given → the blocks carrying it (newest daily first).
    if args.tag:
        hits = [b for b in blocks if _matches(b['tags'], args.tag)]
        hits.sort(key=lambda b: (b['date'], b['line']), reverse=True)
        if args.json:
            print(json.dumps(hits))
            return
        if not hits:
            print(f"  no blocks tagged #{args.tag.lstrip('#')}")
            return
        for b in hits:
            loc = f"{b['relpath']}:{b['line']}"
            tagstr = '  '.join('#' + t for t in b['tags'])
            print(f"\n── {loc}   {tagstr}")
            print(b['text'])
        print()
        return

    # No TAG → the tag index.
    counts = {}
    for b in blocks:
        for t in b['tags']:
            counts[t] = counts.get(t, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if args.json:
        print(json.dumps([{'tag': t, 'count': c} for t, c in items]))
        return
    if not items:
        print('  no tags found')
        return
    width = max((len(t) for t in counts), default=0) + 1
    for t, c in items:
        print(f"  #{t:<{width}} {c}")


if __name__ == '__main__':
    main()
