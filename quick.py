"""quick.py — quick capture as a tiny throwaway assistant chat. `cl quick`.

Miro, 2026-10-13: "firing off little notes to my assistant who files them away
for me … you're typing and things are just happening around you." So a capture
is no longer only saved: it is acted on at once, and what was done comes back
as receipts, each with an undo.

The rules (agreed 2026-10-13):
  1. His words are saved first, always: kb-inbox writes the capture before any
     model is asked. If the model is down or slow, nothing else happens and the
     router files it on its next pass (offline = plain capture).
  2. Prefixes are rules (todo: sooner: grocery: dream:), exactly as the router
     treats them. `buy:` asks the model only to split food from other purchases.
  3. Otherwise the resident model picks tools, one call per thing the note holds.
     It acts — no confirm — and every action can be undone.
  4. A kept note is his text verbatim; the model never rewrites what is saved.
  5. When every action is a line elsewhere, the capture file folds into
     archive/folded/ (as the router does); the feed keeps his words beside what
     was done. Every action is also a line in _state/routing-log.md.

Not full Hermes: a direct model call with five tools was 0.3-0.5 s per note
and right on 8 of 10 of his notes (2026-10-13); Hermes would add its whole
prompt and tool list for no gain here.

State: _state/quick.json (the last 300 captures, with what undo needs).

CLI (JSON out), used by the Board hub:
  quick.py file TEXT...            capture + act
  quick.py follow ID TEXT...       a follow-up in that capture's tiny chat
  quick.py undo ID ACTION          undo one action
  quick.py feed [N]                newest first: quick captures + router lines
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from paths import ARCHIVE, DREAMS, KB, WORKING
import router
import todo

STATE = KB / "_state" / "quick.json"
LOCK = KB / "_state" / ".quick.lock"
CHAT = "http://localhost:11434/api/chat"
REMIND = Path.home() / "bin" / "remind"
KB_INBOX = shutil.which("kb-inbox") or str(Path.home() / ".local" / "bin" / "kb-inbox")   # the hub runs with systemd's PATH
KEEP = 300
# Tests set this: KitchenOwl and phone reminders are shared, real things.
FAKE_EXT = os.environ.get("QUICK_FAKE_EXT") == "1"

SYSTEM = """You are Miro's assistant. He fires off a quick note; you file it with your tools, one call per separate thing the note contains. Never chat.

- todo_add: something he has to DO (task, errand, person to contact, a purchase that is not groceries — write it as "Buy …"). The line is a short imperative in his words. sooner=true only when he says it's urgent, today, tomorrow or this week.
- grocery_add: food or household supplies from the grocery store, for the shared shopping list. A breadboard, a book or a tool is NOT a grocery: that is todo_add "Buy …".
- remind: he names a time ("remind me thursday at 4", "in 20 minutes"). A reminder has a time; a todo does not.
- dream: a dream he had.
- keep_note: an idea, a thought, a link, a wish for his computer setup, notes for a project, a piece of writing — anything that is not one of the above. It keeps his words exactly; you pass nothing.

When unsure, keep_note. It is always safe."""

FOLLOW = """Earlier, for this note, you did:
{done}

Now he says: "{text}"
If he is correcting an earlier action, undo(action) takes it back by its id; then do what he wants instead. If he is only adding something, just file the new thing. Never undo or repeat an action he did not mention. Moving a todo ("can wait", "not urgent", "make it sooner") is move_todo — never undo for that; undo is only for "no", "scrap that", "wrong"."""


def _t(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(req)}}}


S = {"type": "string"}
TOOLS = [
    _t("todo_add", "Add a line to his todo list.", {"text": S, "sooner": {"type": "boolean"}}, ["text"]),
    _t("grocery_add", "Add food or household supplies to the shared grocery list.",
       {"items": {"type": "array", "items": S}}, ["items"]),
    _t("remind", "Set a phone reminder at a time.", {"text": S, "when": S}, ["text", "when"]),
    _t("dream", "File the note as a dream.", {}),
    _t("keep_note", "Keep the note, his exact words, on his desk.", {}),
]
UNDO_TOOL = _t("undo", "Take back an earlier action by its id (a1, a2, …).", {"action": S}, ["action"])
MOVE_TOOL = _t("move_todo", "Move an earlier todo to Sooner or Later: 'can wait', 'not urgent', 'make it sooner'.",
               {"action": S, "band": {"type": "string", "enum": ["Sooner", "Later"]}}, ["action", "band"])


# ---------- state ----------

class _Locked:
    def __enter__(self):
        LOCK.parent.mkdir(exist_ok=True)
        self.f = LOCK.open("w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.recs = json.loads(STATE.read_text()) if STATE.exists() else []
        return self

    def save(self):
        router_tmp = STATE.with_suffix(".tmp")
        router_tmp.write_text(json.dumps(self.recs[-KEEP:], ensure_ascii=False, indent=1))
        os.replace(router_tmp, STATE)

    def find(self, cid):
        return next((r for r in self.recs if r["id"] == cid), None)

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _mark_seen(name):
    """Tell the router this capture is handled, so its next pass leaves it be."""
    st = json.loads(router.STATE.read_text()) if router.STATE.exists() else {"seen": []}
    if name not in st["seen"]:
        st["seen"] = sorted(set(st["seen"]) | {name})
        tmp = router.STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, router.STATE)


def _snip(text, n=50):
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


# ---------- the things it can do ----------

def _kitchenowl(method, path, body=None):
    tok = (Path.home() / ".config" / "kitchenowl" / "token").read_text().strip()
    req = urllib.request.Request(f"http://127.0.0.1:8803/api/shoppinglist/1/{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _grocery(item):
    """Add one item; remember whether it was already on the list, so undo never
    removes something someone else put there."""
    if FAKE_EXT:
        return {"kind": "grocery", "item": item, "item_id": None, "existed": False}
    on = {i.get("name", "").lower() for i in _kitchenowl("GET", "items")}
    got = _kitchenowl("POST", "add-item-by-name", {"name": item})
    return {"kind": "grocery", "item": item, "item_id": got.get("id"), "existed": item.lower() in on}


_REL = re.compile(r"(?:in\s+)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)")


def _remind(text, when):
    """Same reading of `when` as Hermes' remind tool (project-mcp/server.py)."""
    if FAKE_EXT:
        return {"kind": "remind", "text": text, "when": when, "rid": None}
    raw = " ".join((when or "").split()).lower()
    m = _REL.fullmatch(raw)
    if m:
        raw = f"+{m.group(1)}{m.group(2)[0]}"
    run = lambda args: json.loads(subprocess.run([str(REMIND), text, *args, "--json"], capture_output=True,
                                                 text=True, timeout=30).stdout or "{}")
    r = run(raw.split())
    if not r.get("ok"):
        p = subprocess.run(["date", "-d", raw, "+%s"], capture_output=True, text=True)
        if p.returncode != 0:
            raise ValueError(f"can't read “{when}” as a time")
        ts = int(p.stdout.strip())
        ts += (60 - ts % 60) % 60
        if ts <= time.time():
            raise ValueError(f"“{when}” is already past")
        r = run(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M").split())
        if not r.get("ok"):
            raise ValueError(r.get("error") or "remind refused it")
    return {"kind": "remind", "text": text, "when": r.get("when") or when, "rid": r.get("id")}


def _dream(rec):
    day = rec["at"][:10]
    m = router.PREFIX.match(rec["text"])
    body = rec["text"][m.end():] if m and m.group(1).lower() == "dream" else rec["text"]
    chunk = f"\n## {day}\n\n{body.strip()}\n"
    DREAMS.parent.mkdir(parents=True, exist_ok=True)
    with DREAMS.open("a") as f:
        f.write(chunk)
    return {"kind": "dream", "chunk": chunk}


def label(a):
    k = a["kind"]
    return ({"todo": lambda: f"todo · {a['band']} · {a['text']}",
             "grocery": lambda: f"KitchenOwl · {a['item']}" + (" (was already on the list)" if a.get("existed") else ""),
             "remind": lambda: f"reminder · {a['when']} · {a['text']}",
             "dream": lambda: "dreams",
             "note": lambda: "kept on your desk (working/)"}[k])()


def _do(rec, name, args):
    """Run one tool call; returns the action record (with an id) or raises."""
    if name == "todo_add":
        text = " ".join(str(args.get("text") or "").split()) or _snip(rec["text"], 120)
        r = todo.add(text, sooner=bool(args.get("sooner")))
        a = {"kind": "todo", "band": r["band"], "text": r["text"]}
    elif name == "grocery_add":
        items = [str(i).strip() for i in args.get("items") or [] if str(i).strip()]
        if not items:
            raise ValueError("no items")
        return [_grocery(i) for i in items]
    elif name == "remind":
        a = _remind(" ".join(str(args.get("text") or "").split()), str(args.get("when") or ""))
    elif name == "dream":
        a = _dream(rec)
    elif name == "keep_note":
        a = {"kind": "note"}
    else:
        raise ValueError(f"unknown tool {name}")
    return [a]


def _undo_action(rec, a):
    k = a["kind"]
    if k == "todo":
        r = todo.take_back(a["text"], a["band"])
        if not r.get("ok"):
            raise ValueError("that todo line isn't there any more")
    elif k == "grocery":
        if not a.get("existed") and a.get("item_id") and not FAKE_EXT:
            _kitchenowl("DELETE", "item", {"item_id": a["item_id"]})
    elif k == "remind":
        if a.get("rid") and not FAKE_EXT:
            subprocess.run([str(REMIND), "--remove", str(a["rid"]), "--json"], capture_output=True, timeout=30)
    elif k == "dream":
        t = DREAMS.read_text()
        i = t.rfind(a["chunk"])
        if i >= 0:
            DREAMS.write_text(t[:i] + t[i + len(a["chunk"]):])
    a["undone"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    router._log(rec["name"], f"quick “{_snip(rec['text'], 40)}” UNDONE: {label(a)}")


# ---------- the model ----------

def _chat(messages, tools):
    body = {"model": router.MODEL, "messages": messages, "tools": tools, "stream": False,
            "think": False, "keep_alive": -1, "options": {"temperature": 0}}
    req = urllib.request.Request(CHAT, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(os.environ.get("QUICK_TIMEOUT", "25"))) as r:
        msg = json.load(r).get("message") or {}
    return [(c["function"]["name"], c["function"].get("arguments") or {}) for c in msg.get("tool_calls") or []]


def _prefix_calls(text):
    """The router's prefix rules, as tool calls. None = no prefix."""
    m = router.PREFIX.match(text)
    if not m:
        return None
    verb, rest = m.group(1).lower(), text[m.end():].strip()
    if verb in ("todo", "sooner"):
        return [("todo_add", {"text": rest, "sooner": verb == "sooner"})]
    if verb in ("grocery", "groceries"):
        return [("grocery_add", {"items": [i.strip() for i in re.split(r",|\band\b|\n", rest) if i.strip()]})]
    if verb == "dream":
        return [("dream", {})]
    return "buy"          # buy: food → groceries, anything else → todo "Buy …"


def _file_calls(text):
    p = _prefix_calls(text)
    if p is None or p == "buy":
        msgs = [{"role": "system", "content": SYSTEM}]
        if p == "buy":
            msgs.append({"role": "system", "content": "Every item in this note is something to buy: groceries go to grocery_add, anything else to todo_add as “Buy …”."})
        msgs.append({"role": "user", "content": text})
        calls = _chat(msgs, TOOLS)
        return calls or [("keep_note", {})]
    return p


def _apply(rec, calls):
    """Run calls in order; one failure is a receipt, not a crash."""
    out = []
    for name, args in calls:
        if name == "move_todo":
            a = next((x for x in rec["actions"] if x["id"] == args.get("action") and x["kind"] == "todo" and not x.get("undone")), None)
            band = "Sooner" if str(args.get("band", "")).lower().startswith("s") else "Later"
            if a and a["band"] != band:
                # The exact line this capture added (take_back), not a word match: a
                # second "Call the dentist" elsewhere must not make it ambiguous.
                r = todo.take_back(a["text"], a["band"])
                if r.get("ok"):
                    todo.add(a["text"], sooner=band == "Sooner")
                    router._log(rec["name"], f"quick “{_snip(rec['text'], 40)}” moved: {label(a)} → {band}")
                    a["band"] = band
                else:
                    out.append({"kind": "error", "error": f"couldn't move {label(a)}"})
            continue
        if name == "undo":
            a = next((x for x in rec["actions"] if x["id"] == args.get("action") and not x.get("undone")), None)
            if a:
                try:
                    _undo_action(rec, a)
                except Exception as e:
                    out.append({"kind": "error", "error": f"undo {label(a)}: {e}"})
            continue
        try:
            for a in _do(rec, name, args):
                if a["kind"] == "note" and any(x["kind"] == "note" and not x.get("undone") for x in rec["actions"]):
                    continue
                a["id"] = f"a{len(rec['actions']) + 1}"
                rec["actions"].append(a)
                router._log(rec["name"], f"quick “{_snip(rec['text'], 40)}” → {label(a)}")
                out.append(a)
        except Exception as e:
            out.append({"kind": "error", "error": f"{name}: {e}"})
    return out


def _settle_file(rec):
    """Fold the capture when everything it said now lives elsewhere; keep it on
    the desk (unfolded) when a note was kept or everything was undone."""
    live = [a for a in rec["actions"] if not a.get("undone")]
    keep = not live or any(a["kind"] == "note" for a in live)
    here, folded = WORKING / rec["name"], ARCHIVE / "folded" / rec["name"]
    if keep and folded.exists() and not here.exists():
        shutil.move(folded, here)
    elif not keep and here.exists():
        router._fold(here)
    rec["file"] = str((here if keep else folded).relative_to(KB))


def _view(rec, fresh=()):
    return {"id": rec["id"], "at": rec["at"], "text": rec["text"], "file": rec.get("file"),
            "state": rec.get("state"), "followups": rec.get("followups", []),
            "actions": [{"id": a["id"], "kind": a["kind"], "label": label(a), "undone": bool(a.get("undone"))}
                        for a in rec["actions"]],
            "errors": [a["error"] for a in fresh if a["kind"] == "error"]}


# ---------- the doors ----------

def file(text):
    text = text.strip()
    if not text:
        return {"error": "empty"}
    # 1. His words, saved before anything else is tried.
    p = Path(subprocess.run([str(KB_INBOX), "-s", "quick", text], capture_output=True, text=True,
                            check=True, env={**os.environ, "CLIFE_KB": str(KB)}).stdout.strip().splitlines()[-1])
    with _Locked() as st:
        # kb-inbox only checks working/ for a clash; a capture from the same
        # second may already sit folded in archive/ under that name.
        taken = {r["name"] for r in st.recs}
        n = 1
        while p.name in taken or (ARCHIVE / "folded" / p.name).exists():
            q = p.with_name(f"{p.stem.split('~')[0]}~{n}.md"); n += 1
            if not q.exists():
                p = p.rename(q)
    rec = {"id": p.stem, "name": p.name, "at": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text,
           "actions": [], "file": str(p.relative_to(KB)), "state": "saved"}
    with _Locked() as st:
        st.recs.append(rec); st.save()
    # 2. Act. If the model can't be reached, it stays a plain capture for the router.
    try:
        calls = _file_calls(text)
    except Exception as e:
        rec["state"] = "offline"
        with _Locked() as st:
            st.find(rec["id"]).update(rec); st.save()
        return {**_view(rec), "note": f"saved; the assistant didn't answer ({e.__class__.__name__}), the router will file it"}
    with _Locked() as st:
        r = st.find(rec["id"])
        fresh = _apply(r, calls)
        if not r["actions"]:                          # every call failed: keep it as a note
            _apply(r, [("keep_note", {})])
        _settle_file(r)
        r["state"] = "filed"
        _mark_seen(r["name"])
        st.save()
        return _view(r, fresh)


def follow(cid, text):
    with _Locked() as st:
        rec = st.find(cid)
        if not rec:
            return {"error": "no such capture"}
        done = "\n".join(f"[{a['id']}] {label(a)}" + (" (undone)" if a.get("undone") else "") for a in rec["actions"]) or "(nothing)"
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": rec["text"]},
                {"role": "user", "content": FOLLOW.format(done=done, text=text.strip())}]
        try:
            calls = _chat(msgs, TOOLS + [UNDO_TOOL, MOVE_TOOL])
        except Exception as e:
            return {**_view(rec), "errors": [f"the assistant didn't answer ({e.__class__.__name__})"]}
        rec.setdefault("followups", []).append(text.strip())
        fresh = _apply(rec, calls)
        _settle_file(rec)
        st.save()
        return _view(rec, fresh)


def undo(cid, aid):
    with _Locked() as st:
        rec = st.find(cid)
        a = rec and next((x for x in rec["actions"] if x["id"] == aid), None)
        if not a:
            return {"error": "no such action"}
        if not a.get("undone"):
            try:
                _undo_action(rec, a)
            except Exception as e:
                return {**_view(rec), "errors": [str(e)]}
        _settle_file(rec)
        st.save()
        return _view(rec)


_LOGLINE = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) `([^`]+)` → (.*)$")


def feed(n=60):
    """Newest first: quick captures (with undo), then what the router filed on
    its own, so the feed has a history from day one."""
    with _Locked() as st:
        mine = {r["name"] for r in st.recs}
        out = [{"type": "quick", **_view(r)} for r in st.recs]
    if router.LOG.exists():
        for ln in router.LOG.read_text().splitlines():
            m = _LOGLINE.match(ln)
            if m and m.group(2) not in mine and not m.group(3).startswith("quick "):
                out.append({"type": "router", "at": m.group(1), "file": m.group(2), "what": m.group(3)})
    out.sort(key=lambda x: (x["at"], x.get("id") or x.get("file")), reverse=True)
    return {"feed": out[:n]}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    if cmd == "file":
        out = file(" ".join(rest) if rest else sys.stdin.read())
    elif cmd == "follow":
        out = follow(rest[0], " ".join(rest[1:]))
    elif cmd == "undo":
        out = undo(rest[0], rest[1])
    elif cmd == "feed":
        out = feed(int(rest[0]) if rest else 60)
    else:
        out = {"error": f"unknown command {cmd}"}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
