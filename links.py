#!/usr/bin/env python3
"""links.py — the wikilink layer. ONE owner for what `[[...]]` means in this kb.

The store is flat (2026-09-07), so a wikilink resolves by NAME — its last path
segment — and any directory part is decoration left over from the old tree. That
rule, the regex that finds a link, and the masking that keeps a `[[ -f ]]` shell
guard from counting as one all live here, and every reader calls in:

    kb_lint              broken-link / dissolved-dir checks
    nvim-write           gf, <CR>, <leader>b, `[[` completion
    Surface              renders [[x]] as an anchor in the writing lens

That list is the whole point. The kb has already paid for the alternative: four
hand-rolled "does this note have tags?" readers disagreed inside one day, which
is why `cl stream` owns every tag question. A wikilink is the same kind of fact,
and it now has the same kind of owner.

    cl links resolve NAME      the file a link points at (exit 1 if nothing)
    cl links out PATH          the links going out of one note
    cl links back NAME         the notes linking in (backlinks)
    cl links orphans           notes with no link in and none out
    cl links index             every name a link can resolve to
    cl links map               name -> path, as JSON, for an editor to cache
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fm                        # noqa: E402
from paths import KB, WRITING, WORKING, THREADS, ARCHIVE, JOURNAL  # noqa: E402

# Resolver tie-break when one name exists in two places (2026-10-05 layout):
# lower is better. writing/ is the finished work, threads/ the living docs,
# working/ the desk, archive/ the past; anything else (outbox, root) last.
DIR_RANK = ((WRITING, 0), (THREADS, 1), (WORKING, 2), (ARCHIVE, 3))
ORPHAN_DIRS = (WRITING, WORKING, THREADS)


def dir_rank(p: Path) -> int:
    for d, r in DIR_RANK:
        if p.is_relative_to(d):
            return r
    return 9

# Neither scanned nor counted as link targets. `_lint` is our own output; `outbox`
# and `_state` are generated; `.trash` is prune-noise's bin and `recovery` a rescue
# copy; templates carry intentional placeholder links; `oil:` is an oil.nvim accident.
SKIP_DIRS = {".git", ".obsidian", "_lint", "oil:", "templates", "outbox", "_state",
             ".trash", "recovery", "log"}

# Claude's memory files. Claude-written notes link them as [[slug]], which renders
# like any wikilink and names nothing in the kb. No directory (another machine) just
# means nothing gets this label.
MEMORY_DIR = (Path.home() / ".claude" / "projects"
              / str(Path.home()).replace("/", "-") / "memory")

WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODESPAN = re.compile(r"`[^`\n]*`")


def skip(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def md_files():
    for md in sorted(KB.rglob("*.md")):
        if not skip(md.relative_to(KB)):
            yield md


def live_lines(text: str):
    """Yield (lineno, line-with-code-masked) for every line that is PROSE.

    Fenced blocks are dropped whole and inline code spans are blanked, because a
    `[[ -f x ]]` bash guard in a Linux-course unit is a shell test and a backticked
    `[[slug]]` in a spec is an example. Both render as text, and rendering is the
    only thing that makes a link a link. Every scanner here shares this generator
    so none of them can disagree about what counts.
    """
    fenced = False
    for i, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        yield i, _CODESPAN.sub("", line)


def target(raw: str) -> str:
    """The bare name a link body points at: strip `|alias`, `#anchor`, `./` `../`."""
    t = raw.split("|")[0].split("#")[0].strip()
    while t.startswith(("./", "../")):
        t = t.split("/", 1)[1]
    return t


def alias(raw: str) -> str | None:
    """The display text of `[[note|shown as this]]`, if it has one."""
    return raw.split("|", 1)[1].strip() if "|" in raw else None


def _keys(name: str):
    """The names one filename answers to, lowercased and least-specific last."""
    f = name.lower()
    return [f, f.removesuffix(".md"), f.split(".")[0]]


def build_index() -> set[str]:
    """Every name a wikilink can resolve to, lowercased. The store is flat, so a
    link resolves by NAME — its last path segment — and any directory part is
    decoration at best."""
    names: set[str] = set()
    for root, dirs, files in os.walk(KB):
        rel_root = Path(root).relative_to(KB)
        dirs[:] = [d for d in dirs if not skip(rel_root / d)]
        for f in files:
            names.update(_keys(f))
    return names


def build_map() -> dict[str, Path]:
    """name -> the file it opens. Same names as build_index(), but resolved to a
    real path, which is what an editor needs to actually follow a link.

    Two tie-breaks, because names are not unique across the whole kb: the folder
    rank (writing > threads > working > archive > elsewhere), and a more
    specific key beats a vaguer one (`hearth-map.excalidraw.md` claims its own
    full name before it claims the bare `hearth-map`). Without those, a link
    could open an archived copy while the note you meant sat in writing/.
    """
    out: dict[str, Path] = {}
    claim: dict[str, tuple[int, int]] = {}      # key -> (-dir rank, -specificity)
    for root, dirs, files in os.walk(KB):
        rel_root = Path(root).relative_to(KB)
        dirs[:] = [d for d in dirs if not skip(rel_root / d)]
        for f in files:
            p = Path(root) / f
            where = dir_rank(p)
            for rank, key in enumerate(_keys(f)):
                # Higher is better: the better folder first, then the more specific
                # key (rank 0 is the full filename, rank 2 the bare prefix).
                bid = (-where, -rank)
                if key not in out or bid > claim[key]:
                    out[key], claim[key] = p, bid
    return out


def resolves(t: str, names: set[str]) -> bool:
    if not t:                                   # a pure same-file #anchor
        return True
    last = t.split("/")[-1].lower()
    return bool({last, last.removesuffix(".md"), last.split(".")[0]} & names)


def resolve(t: str, mapping: dict[str, Path] | None = None) -> Path | None:
    """The file a link target opens, or None. Takes a raw target or a link body."""
    m = build_map() if mapping is None else mapping
    t = target(t)
    if not t:
        return None
    last = t.split("/")[-1].lower()
    for key in (last, last.removesuffix(".md"), last.split(".")[0]):
        if key in m:
            return m[key]
    return None


def outgoing(path: Path, mapping: dict[str, Path] | None = None) -> list[dict]:
    """Every link leaving one note, in document order."""
    m = build_map() if mapping is None else mapping
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out = []
    for i, line in live_lines(text):
        for mt in WIKILINK.finditer(line):
            body = mt.group(1)
            t = target(body)
            hit = resolve(t, m)
            out.append({"target": t, "alias": alias(body), "line": i,
                        "raw": mt.group(0), "text": line.strip(),
                        "path": str(hit) if hit else None})
    return out


def backlinks(name: str, mapping: dict[str, Path] | None = None) -> list[dict]:
    """Every note whose links land on `name`. Derived, never stored.

    Nothing writes a reverse edge into frontmatter and nothing should: a stored
    backlink is a second copy of a fact the body already carries, and it goes
    stale the moment a link is edited. 225 files is a grep.
    """
    m = build_map() if mapping is None else mapping
    want = resolve(name, m)
    if want is None:
        # An unwritten note still has backlinks — that is how you find out it
        # wants writing. Fall back to matching the bare name.
        want = None
        bare = target(name).split("/")[-1].lower().removesuffix(".md")
    else:
        bare = None
    hits = []
    for md in md_files():
        if want is not None and md == want:
            continue                            # a note linking itself is not a backlink
        try:
            text = md.read_text(errors="replace")
        except OSError:
            continue
        for i, line in live_lines(text):
            for mt in WIKILINK.finditer(line):
                t = target(mt.group(1))
                if want is not None:
                    if resolve(t, m) != want:
                        continue
                elif t.split("/")[-1].lower().removesuffix(".md") != bare:
                    continue
                hits.append({"path": str(md), "name": md.name, "line": i,
                             "raw": mt.group(0), "text": line.strip()})
    return hits


def orphans(include_journal=False) -> list[dict]:
    """writing/ working/ threads/ notes with no link out and no link in — the
    interlinking worklist. archive/ is never on it.

    Journal entries (writing/journal/, by folder) are excluded by default — not because they can't carry links
    (they can; `cl links suggest` proposes them) but because 90-odd of them would
    bury the notes this worklist exists to surface.
    """
    m = build_map()
    linked_to: set[Path] = set()
    has_out: set[Path] = set()
    for md in md_files():
        try:
            text = md.read_text(errors="replace")
        except OSError:
            continue
        for _, line in live_lines(text):
            for mt in WIKILINK.finditer(line):
                hit = resolve(target(mt.group(1)), m)
                if hit is not None and hit != md:
                    linked_to.add(hit)
                    has_out.add(md)
    out = []
    for md in sorted(p for d in ORPHAN_DIRS if d.is_dir() for p in d.rglob("*.md")):
        if md in linked_to or md in has_out:
            continue
        if not include_journal and md.is_relative_to(JOURNAL):
            continue
        if md.name == "INDEX.md":
            continue                      # threads/INDEX.md is the map, not a note
        out.append({"path": str(md), "name": md.name, "stem": md.stem,
                    "dir": str(md.parent.relative_to(KB)), "tags": [],
                    "bytes": md.stat().st_size})
    return out


def memory_stems() -> set[str]:
    if not MEMORY_DIR.is_dir():
        return set()
    return {p.stem.lower() for p in MEMORY_DIR.glob("*.md") if p.name != "MEMORY.md"}


def scan_links(names: set[str]) -> dict[str, list[tuple]]:
    """{broken_target: [(file, line, raw), ...]} — kb_lint's broken-link pass."""
    broken: dict[str, list[tuple]] = {}
    for md in md_files():
        rel = str(md.relative_to(KB))
        try:
            text = md.read_text(errors="replace")
        except OSError:
            continue
        for i, line in live_lines(text):
            for mt in WIKILINK.finditer(line):
                t = target(mt.group(1))
                if not resolves(t, names):
                    broken.setdefault(t, []).append((rel, i, mt.group(0)))
    return broken


def dissolved(t: str, stems: set[str]) -> tuple[bool, str | None]:
    """For a path-style target: (is its directory gone, the flat note it meant).

    Gone means gone from disk — `outbox/ai-rss/latest` points into a directory that
    still exists, and the ai-rss NOTE is not what it wanted, so it gets no hint.
    For a dissolved path the note is usually named by a segment —
    projects/personal-tasks/project meant personal-tasks — and only a segment that
    IS a note now counts: the suggestion is a file that exists, never a guess."""
    parts = t.split("/")
    if len(parts) < 2 or (KB / "/".join(parts[:-1])).is_dir():
        return False, None
    for seg in reversed(parts[:-1]):
        if seg.lower() in stems:
            return True, seg
    return True, None


# ── CLI ───────────────────────────────────────────────────────────────────────
def _emit(payload, as_json: bool, text: str):
    if as_json:
        print(json.dumps(payload, indent=2 if sys.stdout.isatty() else None))
    else:
        print(text)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cl links",
        description="the wikilink layer — resolve, follow, and find what links back.")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("resolve", help="the file a link points at")
    r.add_argument("name")
    r.add_argument("--json", action="store_true")

    o = sub.add_parser("out", help="links going out of one note")
    o.add_argument("path")
    o.add_argument("--json", action="store_true")

    b = sub.add_parser("back", help="notes linking in (backlinks)")
    b.add_argument("name")
    b.add_argument("--json", action="store_true")

    orp = sub.add_parser("orphans", help="writing/working/threads notes with no link in and none out")
    orp.add_argument("--include-journal", action="store_true",
                     help="don't exclude writing/journal/")
    orp.add_argument("--json", action="store_true")

    i = sub.add_parser("index", help="every name a link can resolve to")
    i.add_argument("--json", action="store_true")

    sg = sub.add_parser("suggest", help="related notes not yet linked (embedding similarity)")
    sg.add_argument("--tag", default="journal")
    sg.add_argument("--min", type=float, default=0.80, help="similarity floor")
    sg.add_argument("--gap-days", type=int, default=7, help="skip pairs written this close together")
    sg.add_argument("--per-note", type=int, default=3)
    sg.add_argument("--write", action="store_true", help="append to the newer note's ## Related")
    sg.add_argument("--json", action="store_true")

    mp = sub.add_parser("map", help="name -> path, for an editor to cache")
    mp.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    if args.cmd == "resolve":
        hit = resolve(args.name)
        if hit is None:
            _emit({"name": args.name, "path": None}, args.json, "")
            return 1
        _emit({"name": args.name, "path": str(hit)}, args.json, str(hit))
        return 0

    if args.cmd == "out":
        p = Path(args.path).expanduser()
        rows = outgoing(p)
        _emit({"path": str(p), "links": rows}, args.json,
              "\n".join(f"{r['line']:>5}  {r['raw']}"
                        + ("" if r["path"] else "   ← unresolved")
                        for r in rows) or "no links")
        return 0

    if args.cmd == "back":
        rows = backlinks(args.name)
        _emit({"name": args.name, "backlinks": rows}, args.json,
              "\n".join(f"{r['name']}:{r['line']}  {r['text'][:90]}"
                        for r in rows) or "no backlinks")
        return 0

    if args.cmd == "orphans":
        rows = orphans(include_journal=args.include_journal)
        _emit({"orphans": rows, "count": len(rows)}, args.json,
              "\n".join(f"{r['stem']:<44} {r['dir']}"
                        for r in rows)
              + f"\n\n{len(rows)} orphan(s)")
        return 0

    if args.cmd == "suggest":
        import related
        rows = related.suggest(args.tag, args.min, args.gap_days, args.per_note)
        wrote = related.write(rows) if args.write and rows else 0
        _emit({"suggestions": rows, "written": wrote}, args.json,
              "\n".join(f"{r['score']:.3f}  {r['from_date']} {Path(r['from']).stem}  ->  "
                        f"[[{r['link']}]] ({r['to_date']})\n        {r['from_text'][:70]}\n"
                        f"        {r['to_text'][:70]}" for r in rows)
              + f"\n\n{len(rows)} suggestion(s)" + (f", written into {wrote} note(s)" if args.write else " — dry run"))
        return 0

    if args.cmd == "map":
        m = {k: str(v) for k, v in sorted(build_map().items())}
        _emit({"map": m, "count": len(m)}, args.json,
              "\n".join(f"{k}\t{v}" for k, v in m.items()))
        return 0

    if args.cmd == "index":
        names = sorted(build_index())
        _emit({"names": names, "count": len(names)}, args.json, "\n".join(names))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
