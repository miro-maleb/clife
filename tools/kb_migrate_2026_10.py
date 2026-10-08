"""One-shot kb migration, 2026-10-05: flat notes/ + tags -> writing/ working/ threads/ archive/ + todo.md.

Driven by the migration map Miro approved (JSON). Lossless by construction:
  - files that become a LINE (todo, idea, dream) are still moved whole into
    archive/folded/, so nothing exists only as a one-line summary;
  - notes merged into one thread keep their full text under their own heading,
    with a `<!-- from notes/x.md -->` marker;
  - "drop" means archive/dropped/, not delete.
Wikilinks to a note whose filename changed are rewritten kb-wide, and every
old->new name is recorded in _state/moved.json.

    python3 tools/kb_migrate_2026_10.py MAP.json            # dry run, prints the plan
    python3 tools/kb_migrate_2026_10.py MAP.json --apply    # does it (no git; caller commits)
"""
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import ARCHIVE, KB, THREADS, TODO, WORKING, WRITING  # noqa: E402

NOTES = KB / "notes"
FM = re.compile(r"^---\n(.*?)\n---\n?", re.S)


def split_fm(text):
    m = FM.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


def fm_get(fm, key):
    m = re.search(rf"^{key}:\s*(.*)$", fm, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def clean_fm(fm, extra=None):
    """Drop tags/id; keep created/written/captured and anything app-owned."""
    keep = [ln for ln in fm.splitlines()
            if ln.strip() and not re.match(r"^(tags|id)\s*:", ln) and not ln.startswith("  - ")]
    for k, v in (extra or {}).items():
        keep = [ln for ln in keep if not ln.startswith(f"{k}:")] + [f"{k}: {v}"]
    return "---\n" + "\n".join(keep) + "\n---\n\n" if keep else ""


def created(fm):
    return (fm_get(fm, "captured") or fm_get(fm, "created")).replace("T", " ")[:10]


def main(map_path, apply):
    rows = json.loads(Path(map_path).read_text())
    plan, moved = [], {}
    merges = defaultdict(list)
    lines = {"todo": {"Sooner": [], "Later": []}, "ideas": [], "dreams": []}

    for r in rows:
        src = NOTES / r["file"]
        if not src.exists():
            plan.append(("MISSING", r["file"], ""))
            continue
        k, dest = r["dest_kind"], r["dest"]
        stem = src.stem
        if k in ("writing", "working", "archive"):
            target = KB / dest.split(" (")[0].strip()
            plan.append(("move", r["file"], str(target.relative_to(KB))))
            moved[stem] = target.stem
        elif k == "drop":
            target = ARCHIVE / "dropped" / src.name
            plan.append(("drop->archive", r["file"], str(target.relative_to(KB))))
        elif k == "thread":
            target = KB / dest.split(" (")[0].strip()
            merges[target].append((r, src))
            moved[stem] = target.stem
        elif k == "todo":
            m = re.match(r"todo\.md:\s*(Sooner|Later):\s*(.+)$", dest)
            band, text = (m.group(1), m.group(2)) if m else ("Later", r.get("title") or stem)
            lines["todo"][band].append(text.strip())
            plan.append((f"todo:{band}", r["file"], text.strip()))
        elif k == "ideas":
            text = dest.split(":", 1)[1].strip() if ":" in dest else (r.get("title") or stem)
            lines["ideas"].append((src, text))
            plan.append(("idea", r["file"], text[:70]))
        elif k == "dreams":
            plan.append(("dream", r["file"], "writing/dreams.md"))
            lines["dreams"].append(src)
        else:
            plan.append(("UNKNOWN", r["file"], k))
        if k in ("todo", "ideas", "dreams"):
            moved.setdefault(stem, None)

    for target, members in merges.items():
        plan.append(("thread" + (" (merge %d)" % len(members) if len(members) > 1 else ""),
                     ", ".join(m[1].name for m in members), str(target.relative_to(KB))))

    leftovers = sorted(p.name for p in NOTES.iterdir()
                       if p.is_file() and p.name not in {r["file"] for r in rows})
    for name in leftovers:
        plan.append(("unmapped->working", name, f"working/{name}"))

    for op, a, b in plan:
        print(f"{op:20} {a[:55]:55} -> {b}")
    print(f"\n{len(rows)} mapped, {len(leftovers)} unmapped (-> working/), "
          f"{sum(len(v) for v in lines['todo'].values())} todo lines, "
          f"{len(lines['ideas'])} idea notes, {len(lines['dreams'])} dreams, {len(merges)} thread files")
    if not apply:
        print("\nDRY RUN — nothing written. Add --apply.")
        return

    for d in (WRITING, WORKING, THREADS, ARCHIVE, ARCHIVE / "folded", ARCHIVE / "dropped"):
        d.mkdir(parents=True, exist_ok=True)

    def put(src, target, extra_fm=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.with_name(f"{target.stem}-{src.stem}{target.suffix}")
        if src.suffix == ".md":
            fm, body = split_fm(src.read_text())
            target.write_text(clean_fm(fm, extra_fm) + body.lstrip("\n"))
            src.unlink()
        else:
            shutil.move(src, target)
        return target

    for r in rows:
        src = NOTES / r["file"]
        if not src.exists():
            continue
        k, dest = r["dest_kind"], r["dest"]
        if k in ("writing", "working", "archive"):
            extra = {"status": "draft"} if "status: draft" in dest else None
            put(src, KB / dest.split(" (")[0].strip(), extra)
        elif k == "drop":
            put(src, ARCHIVE / "dropped" / src.name)

    # threads: one file per target; merged members keep full text under a heading
    for target, members in merges.items():
        status = "reference" if any("reference" in (m[0].get("reason", "") + m[0]["dest"]) for m in members) \
            or target.stem.startswith("recipes-") else "active"
        if len(members) == 1:
            r, src = members[0]
            put(src, target, {"status": status}); continue
        parts = [f"---\nstatus: {status}\n---\n\n# {target.stem.replace('-', ' ').title()}\n",
                 "<!-- resume header goes here: where we left off / decided / open / next -->\n"]
        for r, src in sorted(members, key=lambda m: created(split_fm(m[1].read_text())[0]) if m[1].suffix == ".md" else ""):
            if src.suffix != ".md":
                put(src, THREADS / "files" / src.name); continue
            fm, body = split_fm(src.read_text())
            title = r.get("title") or src.stem
            parts.append(f"\n## {title}\n<!-- from notes/{src.name} · created {created(fm)} -->\n\n{body.strip()}\n")
            src.unlink()
        target.write_text("\n".join(parts))

    # todo.md
    if TODO.exists():
        raise SystemExit("todo.md already exists — refusing to overwrite")
    TODO.write_text("# Todo\n\n## Sooner\n" + "".join(f"- [ ] {t}\n" for t in lines["todo"]["Sooner"])
                    + "\n## Later\n" + "".join(f"- [ ] {t}\n" for t in lines["todo"]["Later"]))
    for r in rows:
        if r["dest_kind"] == "todo" and (NOTES / r["file"]).exists():
            put(NOTES / r["file"], ARCHIVE / "folded" / r["file"])

    # ideas.md + dreams.md (originals kept in archive/folded)
    idea_md = WRITING / "ideas.md"
    with idea_md.open("a") as f:
        if idea_md.stat().st_size == 0:
            f.write("# Ideas\n\nSeeds for poems, stories and posts. One line each.\n\n")
        for src, text in lines["ideas"]:
            f.write(f"- {text}  <!-- {src.name} -->\n")
            put(src, ARCHIVE / "folded" / src.name)
    dreams = WRITING / "dreams.md"
    with dreams.open("a") as f:
        if dreams.stat().st_size == 0:
            f.write("# Dreams\n")
        for src in sorted(lines["dreams"], key=lambda p: created(split_fm(p.read_text())[0])):
            fm, body = split_fm(src.read_text())
            f.write(f"\n## {created(fm)}\n\n{body.strip()}\n")
            put(src, ARCHIVE / "folded" / src.name)

    for name in leftovers:
        put(NOTES / name, WORKING / name)

    # rewrite wikilinks whose target filename changed or was folded away
    ren = {old: new for old, new in moved.items() if new and new != old}
    pat = re.compile(r"\[\[([^\]|#]+)([^\]]*)\]\]")
    changed = 0
    for p in list(KB.glob("writing/**/*.md")) + list(KB.glob("working/*.md")) + \
            list(KB.glob("threads/*.md")) + list(KB.glob("archive/**/*.md")):
        t = p.read_text()
        n = pat.sub(lambda m: f"[[{ren.get(m.group(1).strip(), m.group(1))}{m.group(2)}]]", t)
        if n != t:
            p.write_text(n); changed += 1
    (KB / "_state").mkdir(exist_ok=True)
    (KB / "_state" / "moved.json").write_text(json.dumps(moved, indent=1, sort_keys=True))

    rest = [p.name for p in NOTES.iterdir()] if NOTES.exists() else []
    if not rest:
        NOTES.rmdir()
    print(f"\nAPPLIED. wikilinks rewritten in {changed} files. notes/ left: {rest or 'removed'}")


if __name__ == "__main__":
    main(sys.argv[1], "--apply" in sys.argv[2:])
