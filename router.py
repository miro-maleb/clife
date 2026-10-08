"""router.py — files new captures off the working desk. `cl route` (timer: kb-router.timer).

2026-10-05 redesign. Every capture lands in ~/kb/working/; this decides where it
goes, without asking (Miro: "I want to make this work so that I don't need to
confirm and I can just trust the bot to route it"). Trust comes from three things,
not from the model being perfect:

  1. Prefixes are rules, not guesses — 100% when you want certainty:
       todo: / sooner: / buy: / grocery: / dream:
  2. Coarse destinations, and "unsure" is a valid answer: anything not clearly a
     todo, dream or finished piece of writing STAYS in working/ (always safe).
  3. Nothing is destroyed: the capture file itself moves to archive/folded/ when it
     becomes a line, every action is a line in _state/routing-log.md, and git has
     the rest. `cl route --undo FILE` puts one back.

Measured 2026-10-05 on 109 of Miro's own notes (resident gemma4:12b, these
few-shot examples, 2-fold): todo 97%, writing 95%, dream 100%, overall ~91%.
Examples are his approved migration choices: router_examples.json.
"""
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from paths import ARCHIVE, DREAMS, KB, WORKING, WRITING
import todo

MODEL = "resident"
OLLAMA = "http://localhost:11434/api/generate"
STATE = KB / "_state" / "router.json"
LOG = KB / "_state" / "routing-log.md"
EXAMPLES = Path(__file__).with_name("router_examples.json")
SETTLE_SEC = 10 * 60          # a capture younger than this may still be being written
KINDS = {
    "todo": "something Miro has to DO: a task, errand, chore, purchase, or person to contact",
    "dream": "a dream he had",
    "writing": "his own writing worth keeping: a poem, story, letter, blog post or journal reflection",
    "working": "everything else: an idea, a link, a place, a loose thought, notes for a project, a wish for his computer setup",
}
FORMS = {"poems": "a poem", "stories": "a story or fiction", "blog": "a blog post or essay draft",
         "letters": "a letter to someone", "journal": "a personal reflection or journal entry"}
PREFIX = re.compile(r"^\s*(todo|sooner|buy|grocery|groceries|dream)\s*:\s*", re.I)


def _fm_split(t):
    m = re.match(r"^---\n(.*?)\n---\n?", t, re.S)
    return (m.group(1), t[m.end():]) if m else ("", t)


def _body(p):
    return _fm_split(p.read_text(errors="replace"))[1].strip()


def _ask(prompt):
    body = {"model": MODEL, "prompt": prompt, "stream": False, "format": "json",
            "think": False, "keep_alive": -1, "options": {"temperature": 0}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(json.load(r).get("response") or "{}")


def classify(text):
    # The examples are his own notes, so they stay out of this public repo
    # (.gitignore); without the file the model files from the descriptions alone.
    ex = json.loads(EXAMPLES.read_text()) if EXAMPLES.exists() else {}
    shots = "\n".join(f'Note: "{s}"\n→ {k}' for k, v in ex.items() for s in v)
    kinds = "\n".join(f"- {k}: {v}" for k, v in KINDS.items())
    d = _ask(f"""You file Miro's personal notes. Choose exactly one destination:
{kinds}

When unsure, choose "working" — it is always safe.

Examples of how Miro files his own notes:
{shots}

Now file this one.
Note: "{" ".join(text.split())[:1500]}"

Return ONLY JSON: {{"kind": "todo|dream|writing|working", "todo_text": "<if todo: the task as a short imperative line, else empty>"}}""")
    k = d.get("kind")
    return (k if k in KINDS else "working"), (d.get("todo_text") or "").strip()


def form_of(text):
    forms = "\n".join(f"- {k}: {v}" for k, v in FORMS.items())
    d = _ask(f"""Which kind of writing is this? Choose one:\n{forms}\n\nText: "{" ".join(text.split())[:1500]}"\n\nReturn ONLY JSON: {{"form": "<one of {', '.join(FORMS)}>"}}""")
    return d.get("form") if d.get("form") in FORMS else "journal"


def grocery_add(items):
    tok = (Path.home() / ".config" / "kitchenowl" / "token").read_text().strip()
    for it in items:
        req = urllib.request.Request(
            "http://127.0.0.1:8803/api/shoppinglist/1/add-item-by-name",
            data=json.dumps({"name": it}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        urllib.request.urlopen(req, timeout=15).read()


def _log(fname, what):
    LOG.parent.mkdir(exist_ok=True)
    head = "" if LOG.exists() else "# Routing log\n\nWhat the router did with each capture. Undo: `cl route --undo FILE`.\n\n"
    with LOG.open("a") as f:
        f.write(f"{head}- {datetime.now():%Y-%m-%d %H:%M} `{fname}` → {what}\n")


def _fold(p):
    dest = ARCHIVE / "folded" / p.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(p, dest)
    return dest


def route_one(p, dry=False):
    text = _body(p)
    if not text:
        return "skip (empty)"
    m = PREFIX.match(text)
    if m:
        verb, rest = m.group(1).lower(), text[m.end():].strip()
        if verb in ("todo", "sooner"):
            what = f"todo ({'Sooner' if verb == 'sooner' else 'Later'}): {rest}"
            if not dry:
                todo.add(rest, sooner=verb == "sooner"); _fold(p)
        elif verb in ("buy", "grocery", "groceries"):
            items = [i.strip() for i in re.split(r",|\band\b|\n", rest) if i.strip()]
            what = f"groceries: {', '.join(items)}"
            if not dry:
                grocery_add(items); _fold(p)
        else:
            what = "dream"
            if not dry:
                _append_dream(p, rest)
        return what
    kind, todo_text = classify(text)
    if kind == "todo":
        line = todo_text or " ".join(text.split())[:120]
        if not dry:
            todo.add(line); _fold(p)
        return f"todo (Later): {line}"
    if kind == "dream":
        if not dry:
            _append_dream(p, text)
        return "dream"
    if kind == "writing":
        form = form_of(text)
        dest = WRITING / form / p.name
        if not dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(p, dest)
        return f"writing/{form}/"
    return "stays in working/"


def _append_dream(p, text):
    fm, _ = _fm_split(p.read_text(errors="replace"))
    day = (re.search(r"^(?:captured|created):\s*(\S+)", fm, re.M) or [None, f"{datetime.now():%Y-%m-%d}"])[1][:10]
    DREAMS.parent.mkdir(parents=True, exist_ok=True)
    with DREAMS.open("a") as f:
        f.write(f"\n## {day}\n\n{text.strip()}\n")
    _fold(p)


def run(dry=False, all_=False):
    # Ticked boxes (Obsidian, phone) mean done; done means gone. Sweep them here so
    # the 5-minute timer is the one place that happens.
    if not dry:
        try:
            n = todo.sweep()
            if n:
                _log("todo.md", f"swept {n} checked item(s)")
        except Exception:
            pass
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": []}
    seen = set(state["seen"])
    now = time.time()
    out = []
    for p in sorted(WORKING.glob("*.md"), key=lambda q: q.stat().st_mtime):
        if p.name in seen and not all_:
            continue
        if now - p.stat().st_mtime < SETTLE_SEC and not all_:
            continue
        try:
            what = route_one(p, dry)
        except Exception as e:                     # one bad capture must not stop the rest
            what = f"error ({e.__class__.__name__}: {e}) — left in working/"
        out.append((p.name, what))
        if not dry:
            seen.add(p.name)
            if not what.startswith("stays") and not what.startswith("skip"):
                _log(p.name, what)
    if not dry:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({"seen": sorted(seen), "at": datetime.now().isoformat(timespec="seconds")}))
    return out


def undo(fname):
    """Put a routed capture back on the working desk. Lines it added (a todo,
    a dream section) are left for you to delete — the log says which."""
    for cand in [ARCHIVE / "folded" / fname, *WRITING.glob(f"*/{fname}")]:
        if cand.exists():
            shutil.move(cand, WORKING / fname)
            _log(fname, "UNDONE — back in working/ (remove any line it added by hand)")
            return f"restored {fname} to working/"
    return f"not found: {fname}"


def main(argv):
    if argv[:1] == ["--undo"] and len(argv) > 1:
        print(undo(argv[1])); return
    dry = "--dry-run" in argv
    for name, what in run(dry=dry, all_="--all" in argv):
        print(f"{name:32} {what}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main(sys.argv[1:])
