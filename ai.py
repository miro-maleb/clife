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
