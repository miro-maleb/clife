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
