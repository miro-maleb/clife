"""ai.py — every local-LLM (ollama) call lives here, one tweakable function each.

This is THE place to tune prompts. Each function that hits the model keeps its
full prompt inline and visible; the shared transport is at the bottom. Add a new
capability = add a new function here, don't scatter prompts through the codebase.

Model: local qwen via ollama on the tower. Override with CL_AI_MODEL.
"""
import json
import os
import urllib.request

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
# One model for everything on this box, deliberately. The tower has a single
# 24 GB card and a 27B at 64k holds ~21.7 GiB of it, so a second model is not a
# second option — it is a full evict-and-reload (~25 s) every time the caller
# changes. qwen3.8-112k resident beats qwen3:8b plus thrash even on the small,
# frequent calls this default serves. See the Surface /ai lens for what is
# actually on the card. Override with CL_AI_MODEL.
MODEL = os.environ.get("CL_AI_MODEL", "qwen3.8-112k")

# ── inbox coarse pruning ─────────────────────────────────────────────────────
#
# AI's ONLY jobs on the inbox: flag noise (so junk deletes fast) and summarize
# long items (so the human can scan). Routing stays 100% human — AI-guessed
# routing proved unreliable and not better than deterministic cl flows.

def prune_inbox(items):
    """Flag noise + summarize. Does NOT route, categorize, or guess projects.

    items: [{"file","text",...}]  →  [{"file","noise":bool,"confidence":float,"summary":str}]
    """
    def blob(it):
        parts = []
        if it.get("from"):
            parts.append(f'from {it["from"]}')
        if it.get("subject"):
            parts.append(f'subject: {it["subject"]}')
        parts.append(it["text"][:500])
        return " | ".join(parts)
    listing = "\n".join(f'{i+1}. [{it["file"]}] {blob(it)}' for i, it in enumerate(items))
    prompt = f"""
You help triage a personal inbox. A HUMAN does all the routing — do NOT route,
categorize, or guess where an item should go. Two jobs per item:

1. summary: what the item is ACTUALLY about, plain words, <= 10 words, identifiable at a glance.
   - Read the whole body and say what it's about. NEVER answer with just a brand or logo
     name — "Google Logo" is wrong; "Google updating privacy/settings policy" is right.
   - Ignore logo lines, sender addresses, greetings ("Hello Miro"), and links.
   - For a short personal note/task: the text itself is usually already the summary — keep it.

2. noise: true if it's promotional / automated / a notification / a marketing, policy, or
   security email / a receipt / a newsletter — anything from a company with no personal action.
   Be DECISIVE: obvious company or automated emails are noise=true with high confidence.
   false only for things the user personally wrote or that need the user's action.

Items:
{listing}

Return ONLY JSON:
{{"items": [{{"file": "<filename, no brackets>", "noise": <true|false>, "confidence": <0.0-1.0>, "summary": "<= 10 words"}}]}}"""
    return _generate_json(prompt).get("items", [])


# ── inbox → calendar pool ────────────────────────────────────────────────────
#
# Coarse structuring only (per the AI-role rule): turn a freeform capture into a
# tidy pool item. The human still confirms/edits and decides when to schedule it.

def pool_item_from_text(text, areas=None):
    """Structure a freeform capture ("call jonah re podcast fri") into a pool item.

    text: the capture. areas: optional list of valid area tags to pick from.
    Returns {"title","area","est_minutes"} — {} on failure (caller falls back to raw text).
    """
    area_hint = ""
    if areas:
        area_hint = (
            "\nPick `area` from this list if one clearly fits, else \"\":\n"
            + ", ".join(areas)
        )
    prompt = f"""
Turn ONE freeform personal capture into a schedulable to-do item.

Capture: {text[:400]}

- title: a short imperative task, <= 8 words, no dates/times in it. Clean up
  dictation artifacts. E.g. "call jonah re podcast fri" -> "Call Jonah re: podcast".
- area: one short lowercase tag for life-area, or "" if unclear.{area_hint}
- est_minutes: rough integer minutes to DO the task (not when). Quick call ~15,
  errand ~30, focused work ~60-90. Default 30 if you truly can't tell.

Return ONLY JSON:
{{"title": "<title>", "area": "<area or empty>", "est_minutes": <integer>}}"""
    out = _generate_json(prompt)
    if not isinstance(out, dict) or not out.get("title"):
        return {}
    try:
        est = int(out.get("est_minutes") or 30)
    except (TypeError, ValueError):
        est = 30
    return {"title": str(out["title"]).strip(),
            "area": (str(out.get("area") or "").strip() or None),
            "est_minutes": max(5, est)}


def title_from_text(text):
    """Suggest a filename slug (+ title) for a captured note headed to notes/.
    Returns {"slug","title"} — {} on failure so the caller falls back to the
    inbox timestamp. Keeps the AI coarse: it names the file, the human/editor
    can always rename later."""
    import re as _re
    # The note is fenced and the model is told not to ask for more. qwen3.8 is
    # markedly more literal than 3.6/8b: given a bare short line under "Note:" it
    # judges the input to be a *description* of a note rather than a note, and
    # returns {"error": "content is empty or missing"} instead of a name. Measured
    # 2026-08-26 — 3/3 failures before this wording, 3/3 clean names after.
    prompt = f"""
Name a personal note file from its content.

The note's full text is between the markers. It may be short, fragmentary, or
read like a description — that is normal for a captured note. Name it from
whatever text is there, and never ask for more content.

<note>
{text[:600]}
</note>

- title: a concise topic title, <= 8 words, sentence case, no quotes or dates.
- slug: lowercase-kebab (a-z, 0-9, hyphens only) from the title, <= 6 words.

Return ONLY JSON:
{{"title": "<title>", "slug": "<slug>"}}"""
    out = _generate_json(prompt)
    if not isinstance(out, dict) or not out.get("slug"):
        return {}
    slug = _re.sub(r"-{2,}", "-", _re.sub(r"[^a-z0-9-]+", "-", str(out["slug"]).lower())).strip("-")
    if not slug:
        return {}
    return {"slug": slug, "title": str(out.get("title") or "").strip()}


_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
             "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
             "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}


def _resolve_when(phrase, today):
    """Deterministically turn a day PHRASE ("friday", "tomorrow", "") into a
    date, anchored on `today` (a date). Returns YYYY-MM-DD or "". Local models
    are bad at date math, so we do it here and only trust the model to lift the
    phrase out of the text (or return "" when there's no day)."""
    import datetime
    p = (phrase or "").strip().lower()
    if not p:
        return ""
    if p in ("today", "tonight", "tonite"):
        return today.isoformat()
    if p in ("tomorrow", "tmrw", "tmr"):
        return (today + datetime.timedelta(days=1)).isoformat()
    nxt = p.startswith("next ")
    key = p[5:].strip() if nxt else p
    key = key.split()[0] if key else key
    if key in _WEEKDAYS:
        delta = (_WEEKDAYS[key] - today.weekday()) % 7
        if delta == 0 and nxt:      # "next friday" when today is friday → +7
            delta = 7
        if nxt and delta < 7:       # "next X" means the following week's X
            delta += 7 if delta == 0 else 0
        return (today + datetime.timedelta(days=delta)).isoformat()
    return ""


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clean_title(t):
    """Deterministic tidy: strip any day/time phrase the model left dangling on
    the title, then sentence-case it. (The model is inconsistent about removing
    them; a regex is reliable.)"""
    import re as _re
    t = (t or "").strip()
    day = r"(next\s+)?(mon|tue|tues|wed|wednes|thu|thur|thurs|fri|sat|satur|sun)(day)?"
    tod = r"(morning|afternoon|evening|night)"
    for pat in (rf"\s+{day}(\s+{tod})?\s*$", r"\s+(today|tonight|tomorrow|tmrw)\s*$",
                r"\s+(for\s+)?\d+\s*(mins?|minutes?|hrs?|hours?)\s*$",
                r"\s+(at\s+)?\d{1,2}(:\d{2})?\s*(am|pm)?\s*$"):
        t = _re.sub(pat, "", t, flags=_re.I).strip()
    return (t[:1].upper() + t[1:]) if t else t


def event_from_text(text, today=None):
    """Structure a freeform capture into a schedulable item so a surface can route
    it: dated → the calendar, undated → the pool.

    Two focused model calls beat one do-everything prompt for a small local model:
    (1) a yes/no classifier — does the note name a day? — then (2) a branch-specific
    extractor. The model never does date math: the "dated" branch only lifts the
    day PHRASE, which we resolve deterministically here (`today` = a date).
    Returns {title, date, time, duration_min, est_minutes}; date/time "" when absent.
    """
    import datetime
    import re as _re
    today = today or datetime.date.today()
    text = (text or "").strip()
    if not text:
        return {}

    # ── 1) classify: is a day/date named? ──
    gate = _generate_json(f"""
Does this note name a specific day or date to do the thing on? A weekday
("friday"), "today"/"tomorrow", or an explicit date = yes. No day mentioned = no.

Note: {text[:400]}

Return ONLY JSON: {{"dated": true or false}}""")
    dated = bool(isinstance(gate, dict) and gate.get("dated"))

    if dated:
        # ── 2a) extract a dated item (phrase only; we resolve the date) ──
        out = _generate_json(f"""
This note names a day. Extract its parts. Do NOT compute a date; copy the day phrase.

Note: {text[:400]}

- title: short imperative title, <= 8 words, NO day/time words in it.
- when: the day phrase EXACTLY as written ("friday", "tomorrow", "next tue").
- time: "HH:MM" (24h) if a clock time is named, else "".
- duration_min: integer minutes if a length is stated, else 0.

Return ONLY JSON:
{{"title":"<t>","when":"<phrase>","time":"<HH:MM or empty>","duration_min":<int>}}""")
        if not isinstance(out, dict) or not out.get("title"):
            return {}
        time = str(out.get("time") or "").strip()
        if not _re.match(r"^\d{1,2}:\d{2}$", time):
            time = ""
        return {
            "title": _clean_title(out["title"]),
            "date": _resolve_when(out.get("when"), today),
            "time": time,
            "duration_min": max(0, _int(out.get("duration_min"), 0)),
            "est_minutes": 30,
        }

    # ── 2b) extract an undated item (→ pool) ──
    out = _generate_json(f"""
This note has no day/date. Turn it into a to-do.

Note: {text[:400]}

- title: short imperative title, <= 8 words. Clean up dictation.
- est_minutes: rough minutes to DO it (quick call ~15, errand ~30, focused ~60-90). Default 30.

Return ONLY JSON: {{"title":"<t>","est_minutes":<int>}}""")
    if not isinstance(out, dict) or not out.get("title"):
        return {}
    return {
        "title": _clean_title(out["title"]),
        "date": "", "time": "", "duration_min": 0,
        "est_minutes": max(5, _int(out.get("est_minutes"), 30)),
    }


# ── watchdog event triage ────────────────────────────────────────────────────
#
# Coarse triage only (per the AI-role rule): when the tower's health watchdog
# changes state, the model writes a plain-English read on WHAT changed and WHY,
# plus ONE *suggested* next step. It never acts — the human (or a later, opted-in
# autonomous phase) decides. Surface shows this note; the alert email carries it.

def triage_watchdog(report, previous=None):
    """Explain a watchdog snapshot in plain words + suggest a next step.

    report:   the current snapshot dict (status, checks[], culprits[]).
    previous: the prior snapshot dict, if any, so the model can say what CHANGED.
    Returns {"headline","explanation","suggested_action","confidence"} — {} on failure.
    """
    def brief(r):
        if not r:
            return "(none)"
        parts = [f'status={r.get("status")}']
        for c in r.get("checks", []):
            parts.append(f'{c["name"]}:{c["status"]}={c["detail"]}')
        for cp in r.get("culprits", []):
            parts.append(f'culprit pid {cp["pid"]} {cp.get("cpu")}%cpu '
                         f'{cp.get("etime")} {cp.get("cmd", "")}')
        return " | ".join(parts)

    prompt = f"""
You triage a Linux tower's health-watchdog events for its owner. You do NOT fix
anything — you explain what changed and suggest ONE next step for the human.

Prior snapshot:   {brief(previous)}
Current snapshot: {brief(report)}

Each check is ok/warn/crit. Culprits are flagged runaway/stray processes.
Common benign case: a service reads warn only because it was caught mid-restart
(deactivating/activating) — call that out as likely-transient, not a real outage.
Be concise and decisive; never invent causes the snapshot doesn't support.

Return ONLY JSON:
{{"headline": "<= 8 words: the current state>",
  "explanation": "<= 2 sentences: what changed and the most likely cause>",
  "suggested_action": "<= 1 sentence; 'none needed' if healthy or self-resolving>",
  "confidence": <0.0-1.0>}}"""
    return _generate_json(prompt)


def plan_remediation(problem, allowed_actions):
    """Pick remediation actions for a watchdog incident — from a FIXED menu only.

    problem: {"status", "checks":[{name,status,detail}], "culprits":[{pid,cmd,kind,killable}]}
    allowed_actions: [{"action","description","args_hint"}] — the ONLY actions permitted.
    Returns {"assessment": str, "actions": [{"action","args","why"}]}. An empty
    actions list means "nothing safe to do — leave it for a human". {} on failure.

    The model never gets a shell; it only chooses from allowed_actions. The human
    already authorized this run by pressing dispatch — keep choices conservative.
    """
    checks = " | ".join(f'{c["name"]}:{c["status"]}={c["detail"]}'
                        for c in problem.get("checks", []))
    culprits = " | ".join(f'pid {c["pid"]} {c.get("kind","")} '
                          f'killable={c.get("killable")} {c.get("cmd","")}'
                          for c in problem.get("culprits", [])) or "(none)"
    menu = "\n".join(f'- {a["action"]}({a.get("args_hint","")}): {a["description"]}'
                     for a in allowed_actions)
    prompt = f"""
You are the remediation step of a Linux tower's health watchdog. The owner just
pressed "fix it", authorizing you to act — but ONLY through the fixed action menu
below. You cannot run arbitrary commands. Choose the smallest set of actions that
resolves the problem; if nothing on the menu safely applies, return an empty
actions list and say what a human should do.

Problem status: {problem.get("status")}
Checks: {checks}
Flagged processes: {culprits}

Action menu (the ONLY actions you may use):
{menu}

Rules:
- Only kill a process that is flagged killable. Use its exact pid.
- Only restart a service that is actually failing per the checks.
- A service that is warn merely from a mid-restart blip needs no action.
- Prefer doing nothing over a risky guess.

Return ONLY JSON:
{{"assessment": "<= 1 sentence: what's wrong and your plan (or why nothing to do)>",
  "actions": [{{"action": "<name from the menu>", "args": {{...}}, "why": "<short>"}}]}}"""
    out = _generate_json(prompt)
    if not isinstance(out, dict):
        return {}
    out.setdefault("assessment", "")
    if not isinstance(out.get("actions"), list):
        out["actions"] = []
    return out


# ── the mail/message composer ────────────────────────────────────────────────
#
# Four calls, all EXPLICITLY invoked from the composer's leader menu — nothing
# ambient, nothing that suggests while you type. That constraint is the whole
# design: an assistant that interrupts writing has been tried here twice and
# rejected twice. These run when asked and show you a result you accept or throw
# away; the draft is never edited behind your back.
#
# Historically a BIGGER model than the default: proofreading and rewriting are
# exactly where 8b starts inventing, and 27b-without-thinking beats
# 8b-with-thinking on this kind of work. As of 2026-08-26 the default IS the 27b,
# so this is the same model — kept as its own knob because composing is the one
# job that would justify reaching for something heavier again, and because a
# separate name documents which calls are quality-critical.
# Override with CL_COMPOSE_MODEL.
COMPOSE_MODEL = os.environ.get("CL_COMPOSE_MODEL", "qwen3.8-112k")


def proofread(text):
    """Spelling/grammar only. Returns [{"before","after","why"}] — never a rewrite.

    Deliberately narrow: the model is told it is NOT an editor. Every suggestion
    has to be a literal substring of the draft so the UI can apply it exactly and
    the user can see precisely what changes."""
    text = (text or "").strip()
    if not text:
        return []
    prompt = f"""
You are a proofreader for something a person is writing to someone they know —
an email or a text message. Find ONLY objective errors:
misspellings, wrong/missing punctuation, subject-verb disagreement, doubled
words, obvious typos.

You are NOT an editor. Do NOT improve style, tone, word choice, or structure.
Do NOT rephrase anything that is merely informal — this is a person writing to
someone they know. Contractions, sentence fragments, lowercase 'i' in casual
writing, and starting a sentence with 'And' are all FINE, not errors.

Each fix corrects exactly ONE error. Never bundle two changes into one fix — a
misspelling and a capitalization are two fixes, because the person may want one
and not the other, and a bundled fix can only be taken whole.

If there are no real errors, return an empty list. An empty list is a good
answer; inventing corrections is not.

Each fix's "before" MUST be copied EXACTLY from the draft, character for
character, and must be long enough to appear only once (include a word or two of
surrounding context if the mistake is a short word).

DRAFT:
---
{text[:6000]}
---

Return ONLY JSON:
{{"fixes": [{{"before": "<exact text from the draft>", "after": "<corrected>", "why": "<3-6 words>"}}]}}"""
    out = _generate_json(prompt, model=COMPOSE_MODEL)
    fixes = out.get("fixes") if isinstance(out, dict) else None
    if not isinstance(fixes, list):
        return []
    # Drop anything that isn't literally present, or that changes nothing. A fix
    # the UI can't apply exactly is worse than no fix at all.
    clean = []
    for f in fixes:
        if not isinstance(f, dict):
            continue
        b, a = (f.get("before") or "").strip(), (f.get("after") or "").strip()
        if not b or not a or b == a or b not in text:
            continue
        clean.append({"before": b, "after": a, "why": (f.get("why") or "").strip()[:40]})
    return clean[:20]


def revise(text, instruction):
    """Rewrite the draft to an instruction ('tighten', 'warmer'). Returns text."""
    text = (text or "").strip()
    if not text:
        return ""
    prompt = f"""
Rewrite the draft below according to this instruction: {instruction}

Rules:
- Keep the writer's voice. This is a real person writing to someone they know,
  not a corporate email. Do not make it more formal unless asked. If the draft is
  a text message, keep it a text message — do not grow it into a letter.
- Keep every fact, name, date, number and commitment exactly as written.
- Do not add greetings, sign-offs, or pleasantries that aren't already there.
- Return the rewritten draft and NOTHING else — no preamble, no explanation,
  no quotes around it.

DRAFT:
---
{text[:6000]}
---"""
    return _generate_text(prompt, model=COMPOSE_MODEL, num_predict=900)


def summarize_thread(messages):
    """A reading aid for a long thread: what happened, what's being asked of you.

    messages: [{"who","body"}] oldest first. Never touches the draft."""
    if not messages:
        return {}
    convo = "\n\n".join(f'{m.get("who", "?")}: {(m.get("body") or "")[:1200]}'
                         for m in messages[-14:])
    prompt = f"""
Summarize this conversation for the person about to reply to it.

Be concrete and short. Name people and specifics rather than describing the
thread abstractly. If nothing is actually being asked of the reader, say so
plainly instead of inventing a task.

CONVERSATION:
---
{convo[:9000]}
---

Return ONLY JSON:
{{"gist": "<1-2 sentences: what this thread is about>",
  "asks": ["<something the reader is being asked to do or answer>", ...],
  "open": "<what is still unresolved, or empty string>"}}"""
    out = _generate_json(prompt, model=COMPOSE_MODEL)
    if not isinstance(out, dict):
        return {}
    asks = out.get("asks")
    return {"gist": (out.get("gist") or "").strip(),
            "asks": [str(a).strip() for a in asks][:6] if isinstance(asks, list) else [],
            "open": (out.get("open") or "").strip()}


def draft_reply(messages, style_samples=None):
    """An opening draft in the user's voice. A starting point to edit, not a send.

    style_samples: things the user has actually written, so the draft sounds like
    them rather than like an assistant."""
    if not messages:
        return ""
    convo = "\n\n".join(f'{m.get("who", "?")}: {(m.get("body") or "")[:1000]}'
                         for m in messages[-10:])
    samples = [s.strip() for s in (style_samples or []) if s and s.strip()][:12]
    style = "\n".join(f'- "{s[:300]}"' for s in samples) or "(no samples available)"
    prompt = f"""
Write the opening draft of a reply to the email thread below. The person will
edit it before sending — a decent starting point beats a polished wrong one.

Match the voice in these samples of how this person actually writes:
{style}

Rules:
- Answer what was actually asked. Do not pad.
- Never invent facts, commitments, dates or numbers. If something needs a
  detail the thread doesn't contain, leave an obvious [bracket] for it.
- No corporate filler ("I hope this email finds you well", "Please don't
  hesitate"). No sign-off unless the samples show one.
- Return the draft body only — no subject line, no preamble, no quotes.

THREAD:
---
{convo[:8000]}
---"""
    return _generate_text(prompt, model=COMPOSE_MODEL, num_predict=700, temperature=0.6)


# ── (room for more calls: propose_blocks(), weekly_review(), … ) ──


# ── shared transport ─────────────────────────────────────────────────────────
#
# Thinking is off everywhere here, set once via the "think" field below — the
# supported switch. Prompts used to ALSO open with a literal `/no_think` line,
# the Qwen3-era soft switch; that is now removed. Measured 2026-08-25: qwen3:8b
# still swallows the token, but qwen3.6:27b (the qwen3.5 renderer) echoes it back
# verbatim as the first line of the prompt — so on COMPOSE_MODEL it was not a
# switch at all, just noise at the top of every proofread and rewrite. With the
# field alone, both models return an empty `thinking` and no <think> block.
#
# Off, not on, is the measured choice: the ai-rss eval scored think on/off at
# 18/18 = 18/18 across six frozen cases for ~5x the runtime (see
# ai_rss/config.yaml). Proofreading is further from thinking's strength than any
# of those stages — a reasoning pass here invents edits rather than finding them.

def _generate_json(prompt, timeout=180, model=None):
    """POST to ollama with JSON-constrained output; return parsed dict ({} on failure)."""
    body = json.dumps({
        "model": model or MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": False,
        # -1, not a duration: this is the ONE resident model on a 24GB card, and
        # ollama takes the last keep_alive it was given. A "30m" here silently
        # DOWNGRADES the boot-time pin (ollama-pin.service) every time a
        # background job runs, so the assistant would quietly expire overnight
        # and the next request would pay a ~25s cold load of 21.7 GiB.
        "keep_alive": -1,
        "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.load(r).get("response", "")
        return json.loads(raw)
    except Exception:
        return {}


def _generate_text(prompt, timeout=180, model=None, num_predict=600, temperature=0.3):
    """Same transport, free text out. Returns "" on any failure — every caller
    treats an empty answer as "the model had nothing", which is a fine outcome."""
    body = json.dumps({
        "model": model or MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        # -1, not a duration: this is the ONE resident model on a 24GB card, and
        # ollama takes the last keep_alive it was given. A "30m" here silently
        # DOWNGRADES the boot-time pin (ollama-pin.service) every time a
        # background job runs, so the assistant would quietly expire overnight
        # and the next request would pay a ~25s cold load of 21.7 GiB.
        "keep_alive": -1,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.load(r).get("response") or "").strip()
    except Exception:
        return ""
