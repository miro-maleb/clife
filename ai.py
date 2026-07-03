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
MODEL = os.environ.get("CL_AI_MODEL", "qwen3:8b")

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
    prompt = f"""/no_think
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
    prompt = f"""/no_think
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
    prompt = f"""/no_think
Name a personal note file from its content.

Note:
{text[:600]}

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
    gate = _generate_json(f"""/no_think
Does this note name a specific day or date to do the thing on? A weekday
("friday"), "today"/"tomorrow", or an explicit date = yes. No day mentioned = no.

Note: {text[:400]}

Return ONLY JSON: {{"dated": true or false}}""")
    dated = bool(isinstance(gate, dict) and gate.get("dated"))

    if dated:
        # ── 2a) extract a dated item (phrase only; we resolve the date) ──
        out = _generate_json(f"""/no_think
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
    out = _generate_json(f"""/no_think
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


# ── (room for more calls: draft_reply(), propose_blocks(), weekly_review(), … ) ──


# ── shared transport ─────────────────────────────────────────────────────────

def _generate_json(prompt, timeout=180):
    """POST to ollama with JSON-constrained output; return parsed dict ({} on failure)."""
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": False,
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
