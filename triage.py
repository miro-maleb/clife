"""triage.py — `cl triage`: the unplaced queue, with a slot per note.

The scaffolding half of tags-as-routing. This owns the QUEUE and the SLOTS;
`cl stream set` owns the writing. Nothing here decides anything.

  cl triage                       the queue: every unplaced note + its slot
  cl triage --json                the same, for the TUI / Surface / a model
  cl triage suggest SLUG --tags a,b [--note "..."] [--flag dup]
                                  fill one slot
  cl triage clear SLUG | --all    empty a slot (or all of them)

WHY A SLOT AND NOT A CONVERSATION
---------------------------------
The queue works with nothing in the slots. That is the whole design: an empty
slot renders as a note with no suggestion, exactly the list you would triage by
hand today, and every slot that gets filled makes one row faster to answer. So
the app is never blocked on a model being up, warm, correct, or willing to pick
its tools — and Hermes filling slots is an improvement to a working thing
rather than a dependency of a broken one.

It also means the producer is interchangeable. Hermes fills these today; a
better local model, a nightly batch, or you at a keyboard fill them the same
way through the same door.

WHY THE SLOTS LIVE OUTSIDE THE NOTES
------------------------------------
A suggestion is not a fact about the note — it is a machine's guess, pending.
Writing it into frontmatter would make `tags:` mean two things (placed, and
maybe-placed), and the inbox view is exactly the query "is tags empty". One
speculative write and a note silently leaves the queue it is still waiting in.
So slots live in ~/kb/_state/, which is where clife's other machine state
already lives, and the notes stay clean.

THE GUARD RUNS AT SUGGEST TIME, NOT APPLY TIME
----------------------------------------------
`suggest` puts every proposed tag through `stream.resolve_tags` — the same
vocabulary guard `cl stream set` uses. A model that proposes `bridges` when
`bridge` exists gets the existing spelling written into the slot; one that
proposes a near-duplicate gets refused with the candidates and has to choose.
That is what makes "look for existing tags before proposing new ones" a
property of the system rather than a line in a prompt: the instruction can be
ignored or fall out of the model's attention as the vocabulary grows, and this
cannot be.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from paths import KB

import stream

STATE = KB / "_state"
SLOTS = STATE / "triage.json"
VERSION = 1

FLAGS = ("dup", "junk", "ask", "done")


# ── the slot file ──────────────────────────────────────────────────────────

def load_slots() -> dict:
    """Never raises. A corrupt or missing slot file must degrade to "no
    suggestions", never to a broken queue — the queue is the part that has to
    work when everything else is down."""
    try:
        data = json.loads(SLOTS.read_text())
    except Exception:
        return {}
    slots = data.get("notes")
    return slots if isinstance(slots, dict) else {}


def save_slots(slots: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    payload = {"version": VERSION,
               "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "notes": slots}
    tmp = SLOTS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(SLOTS)                      # atomic: a half-written slot file
                                            # would read as "no suggestions"


# ── the queue ──────────────────────────────────────────────────────────────

def queue(items=None) -> list:
    """Every unplaced note, oldest first, each with its slot attached.

    Oldest first and not newest: the backlog is the problem. A queue that opens
    on today's captures is a queue whose March end never gets looked at."""
    items = items if items is not None else stream.load()
    slots = load_slots()
    rows = []
    for it in items:
        if it["tags"]:
            continue
        s = slots.get(it["slug"]) or {}
        rows.append({
            "slug": it["slug"],
            "title": it["title"],
            "relpath": it["relpath"],
            "created": it["created"],
            "age_days": it["age_days"],
            "suggested": list(s.get("tags") or []),
            "note": s.get("note") or "",
            "flag": s.get("flag") or "",
            "by": s.get("by") or "",
            "at": s.get("at") or "",
        })
    rows.sort(key=lambda r: r["created"] or "")
    return rows


def render(rows, color=False) -> str:
    c = stream._color(color)
    filled = sum(1 for r in rows if r["suggested"] or r["note"])
    out = [c(f"TRIAGE ({len(rows)} unplaced · {filled} with suggestions)", "bold")]
    if not rows:
        out.append(c("  nothing unplaced — every note in the stream is tagged",
                     "dim"))
    for r in rows:
        # An undated note renders as "—", not as blank: a gap in this column
        # reads as a rendering bug, and the missing date is itself the thing
        # worth seeing.
        age = f"{r['age_days']}d" if r["age_days"] is not None else "—"
        out.append("")
        out.append(f"  {c(age.rjust(4), 'dim')}  {r['title'][:64]}")
        out.append(f"        {c(r['slug'], 'dim')}")
        if r["suggested"]:
            out.append(f"        {c('tags?', 'dim')} "
                       f"{c(' '.join(r['suggested']), 'cyan')}")
        if r["note"]:
            mark = "!" if r["flag"] in ("dup", "junk") else "?"
            out.append(f"        {c(mark, 'yellow')} {r['note'][:78]}")
    if rows:
        out.append("")
        out.append(c("  apply:  cl stream set <slug> --tag a,b", "dim"))
    return "\n".join(out)


# ── filling a slot ─────────────────────────────────────────────────────────

def suggest(slug, tags=(), note="", flag="", by="hermes", allow_new=False):
    """Fill one slot. Returns a result dict; never writes a tag the vocabulary
    guard rejected."""
    try:
        it = stream.find(slug)
    except SystemExit as e:              # find() raises with the candidates
        return {"ok": False, "error": str(e)}

    vocab = stream.vocabulary(stream.load(include_daily=True))
    keep, notes, blocked = stream.resolve_tags(tags, vocab, allow_new=allow_new)
    if blocked:
        return {"ok": False, "error": "near-duplicate tag(s)",
                "blocked": blocked,
                "hint": "use an existing tag from `cl stream tags`, "
                        "or pass --new if it really is a different idea"}

    slots = load_slots()
    slots[it["slug"]] = {"tags": keep, "note": note or "", "flag": flag or "",
                         "by": by, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    save_slots(slots)
    return {"ok": True, "slug": it["slug"], "tags": keep, "note": note,
            "flag": flag, "vocabulary_notes": notes}


def clear(slug=None, all_=False):
    slots = load_slots()
    if all_:
        n = len(slots)
        save_slots({})
        return {"ok": True, "cleared": n}
    try:
        it = stream.find(slug)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    had = slots.pop(it["slug"], None) is not None
    save_slots(slots)
    return {"ok": True, "cleared": 1 if had else 0, "slug": it["slug"]}


# ── cli ────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cl triage",
        description="the unplaced queue, one suggestion slot per note")
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--json", action="store_true")

    g = sub.add_parser("suggest", help="fill one note's slot")
    g.add_argument("slug")
    g.add_argument("--tags", default="", help="comma-separated proposed tags")
    g.add_argument("--note", default="", help="a question or comment for him")
    g.add_argument("--flag", default="", choices=("",) + FLAGS)
    g.add_argument("--by", default="hermes")
    g.add_argument("--new", action="store_true",
                   help="allow a tag the vocabulary guard flagged")
    g.add_argument("--json", action="store_true")

    k = sub.add_parser("clear", help="empty a slot")
    k.add_argument("slug", nargs="?")
    k.add_argument("--all", action="store_true")
    k.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    as_json = getattr(args, "json", False)

    if args.cmd == "suggest":
        tags = [t for t in args.tags.split(",") if t.strip()]
        res = suggest(args.slug, tags, args.note, args.flag, args.by, args.new)
        if as_json:
            print(json.dumps(res, indent=2 if sys.stdout.isatty() else None))
        elif res.get("ok"):
            bits = " ".join(res["tags"]) or "(no tags)"
            print(f"  {res['slug']}: {bits}")
            for n in res.get("vocabulary_notes") or []:
                print(f"  · {n}")
        else:
            print(f"  {res['error']}")
            for b in res.get("blocked") or []:
                print(f"    '{b['input']}' looks like: {', '.join(b['candidates'])}")
            if res.get("hint"):
                print(f"  {res['hint']}")
        raise SystemExit(0 if res.get("ok") else 2)

    if args.cmd == "clear":
        if not args.slug and not args.all:
            raise SystemExit("cl triage clear: pass a slug or --all")
        res = clear(args.slug, args.all)
        print(json.dumps(res) if as_json else f"  cleared {res.get('cleared', 0)}")
        raise SystemExit(0 if res.get("ok") else 2)

    rows = queue()
    if as_json:
        print(json.dumps(rows, indent=2 if sys.stdout.isatty() else None))
    else:
        color = sys.stdout.isatty() and not __import__("os").environ.get("NO_COLOR")
        print(render(rows, color=color))


if __name__ == "__main__":
    main()
