"""kbfile.py — open and save one kb markdown file, the safe way.

The one owner of "how an editor saves a kb file": the path must resolve inside
the kb, the YAML frontmatter is never handed to the editor (it is re-read from
disk and reattached on save, so an editor cannot mangle it), the write is
atomic, and a save is refused when the file changed on disk since it was
opened (someone else's edit, the router, a sync) — the caller decides what to
do with that conflict. Wikilinks and backlinks stay links.py's.

Same rules as Surface's routers/files.py, which can move onto this module.

CLI (JSON out), used by the Board hub:
  kbfile.py open PATH
  kbfile.py save PATH [--mtime M]      body on stdin
  kbfile.py resolve NAME
  kbfile.py backlinks PATH
  kbfile.py search QUERY               file names first, then contents
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import paths
import links

KB = paths.KB.resolve()


def guard(raw):
    """The path (absolute or kb-relative) if it is a .md file inside the kb, else None."""
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
        p = (p if p.is_absolute() else KB / p).resolve()
        p.relative_to(KB)
    except (ValueError, OSError):
        return None
    return p if p.suffix == ".md" else None


def split_fm(text):
    """(frontmatter block incl. both fences and a newline, body). No block → ("", text)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[:i + 1]) + "\n", "\n".join(lines[i + 1:]).lstrip("\n")
    return "", text


def atomic_write(path, text):
    """tmp file in the same dir → fsync → os.replace; keeps the file's mode."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_file(raw):
    p = guard(raw)
    if not p or not p.is_file():
        return {"error": "not a kb note"}
    fm, body = split_fm(p.read_text())
    return {"path": str(p), "rel": str(p.relative_to(KB)), "name": p.stem, "body": body,
            "has_fm": bool(fm), "mtime": str(p.stat().st_mtime_ns)}


def save_file(raw, body, mtime=None):
    """Save the body under the file's current frontmatter. With `mtime` (from
    open), refuse if the file changed since: {"conflict": True, …}."""
    p = guard(raw)
    if not p or not p.is_file():
        return {"error": "not a kb note"}
    # A string: nanoseconds overflow a JavaScript number, and a rounded copy
    # would look like someone else's edit.
    now = str(p.stat().st_mtime_ns)
    if mtime is not None and str(mtime) != now:
        return {"conflict": True, "mtime": now}
    fm, _ = split_fm(p.read_text())
    out = (fm + "\n" + body) if fm else body
    if not out.endswith("\n"):
        out += "\n"
    atomic_write(p, out)
    return {"ok": True, "path": str(p), "mtime": str(p.stat().st_mtime_ns)}


def resolve(name):
    hit = links.resolve(name)
    if hit is None or guard(str(hit)) is None:
        return {"name": name, "path": None}
    return {"name": name, "path": str(hit), "rel": str(Path(hit).resolve().relative_to(KB))}


def backlinks(raw):
    p = guard(raw)
    if not p:
        return {"error": "not a kb note"}
    seen, out = set(), []
    for r in links.backlinks(p.stem):
        if r["path"] in seen:
            continue
        seen.add(r["path"])
        out.append({"path": r["path"], "name": r["name"], "line": r["line"], "text": r["text"][:140]})
    return {"path": str(p), "backlinks": out}


def search(q, limit=30):
    """Notes whose name holds every word, then notes whose text does (ripgrep).
    archive/ and _state/ are left out; they are not reading material."""
    words = [w.lower() for w in q.split() if w]
    if not words:
        return {"notes": []}
    skip = ("archive/", "_state/", ".obsidian/", ".git/")
    names, seen = [], set()
    for f in KB.rglob("*.md"):
        rel = str(f.relative_to(KB))
        if rel.startswith(skip):
            continue
        if all(w in rel.lower() for w in words):
            names.append({"path": str(f), "rel": rel, "name": f.stem, "match": "name"})
            seen.add(str(f))
    hits = []
    try:
        r = subprocess.run(["rg", "-l", "-i", "-F", "--glob", "*.md", "--glob", "!archive/**", "--glob", "!_state/**",
                            "-e", words[0], str(KB)], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            f = Path(line)
            if str(f) in seen or str(f.relative_to(KB)).startswith(skip):
                continue
            text = f.read_text(errors="replace")
            low = text.lower()
            if not all(w in low for w in words):
                continue
            i = low.find(words[0])
            snip = " ".join(text[max(0, i - 60): i + 160].split())
            hits.append({"path": str(f), "rel": str(f.relative_to(KB)), "name": f.stem, "match": "text",
                         "snippet": ("…" if i > 60 else "") + snip, "mtime": f.stat().st_mtime})
    except (OSError, subprocess.TimeoutExpired):
        pass
    hits.sort(key=lambda h: -h["mtime"])
    return {"notes": (names + hits)[:limit]}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    if cmd == "open":
        out = open_file(rest[0])
    elif cmd == "save":
        m = rest[rest.index("--mtime") + 1] if "--mtime" in rest else None
        out = save_file(rest[0], sys.stdin.read(), m)
    elif cmd == "resolve":
        out = resolve(rest[0])
    elif cmd == "backlinks":
        out = backlinks(rest[0])
    elif cmd == "search":
        out = search(" ".join(rest))
    else:
        out = {"error": f"unknown command {cmd}"}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
