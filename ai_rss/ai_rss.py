#!/usr/bin/env python3
"""ai-rss — a personal AI newspaper.

State your interests as columns + queries (config.yaml); local qwen on the tower
searches the web (DuckDuckGo), scrapes + cleans the articles, selects the most
significant non-sensational stories, and writes a quiet multi-column digest.

All judgment (select + write) runs on the local model. Only dumb page-fetching
touches the network. See ~/kb/projects/infrastructure/hearth/ai-rss/sub-project.md.

Usage:
    ai_rss.py --dry-run                 # all columns -> ./out/, no kb/seen writes
    ai_rss.py --dry-run --column Dharma # iterate on one column (fast)
    ai_rss.py                           # full run -> <outbox>/ai-rss/ + seen-state

Output contract (the web app reads latest.json):
    <outbox>/ai-rss/latest.json  ·  issue-<date>.json  ·  latest.md  ·  issue-<date>.md
    JSON: {"date","generated_at","columns":[{"name","stories":[
           {"headline","summary","url","source"}],
           "recommendation":{"verdict","notes":[…]}   # optional; omitted if the pass
          }]}                                         # is off or returns nothing
Same-day reruns re-pick freely (seen-dedupe is against previous days only).

Runs as an overnight batch (systemd `ai-rss.timer`, Mondays 00:00), so nothing here
optimises for latency: thinking is on and the model gets whole articles to read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import trafilatura
import yaml
from ddgs import DDGS
from urllib.parse import urlparse

import sources

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")


def domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


# Path segments that mark an index/feed/bio page rather than a single article.
_INDEX_SEGMENTS = {"author", "authors", "tag", "tags", "category", "categories",
                   "topic", "topics", "archive", "archives"}


def is_indexish(url: str) -> bool:
    """True for homepages, author/tag/category feeds — not single articles."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if not segs:  # bare domain / homepage
        return True
    return any(s.lower() in _INDEX_SEGMENTS for s in segs)

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
SEEN_FILE = STATE_DIR / "seen.json"
OUT_DIR = HERE / "out"


def log(msg: str) -> None:
    print(f"  · {msg}", file=sys.stderr, flush=True)


# ── config + state ────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(HERE / "config.yaml") as f:
        return yaml.safe_load(f)


def load_seen() -> dict[str, str]:
    """Map of url -> ISO date first delivered. Dedupe is against PREVIOUS days only,
    so a same-day 'regenerate' can re-pick today's candidates instead of going empty."""
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text())
        if isinstance(data, list):  # migrate legacy set-format → treat as long-seen
            return {u: "1970-01-01" for u in data}
        return data
    return {}


def save_seen(seen: dict[str, str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))


# ── local model ───────────────────────────────────────────────────────────────
def llm(cfg: dict, system: str, user: str, json_mode: bool = False,
        temperature: float = 0.3) -> str:
    """One non-streaming chat call to the tower's ollama.

    This runs as an overnight batch job, so reasoning time is free — `think: true`
    buys better judgment at no cost anyone is awake to notice. With thinking on,
    ollama returns the reasoning in a separate `thinking` field and leaves `content`
    clean, so JSON mode is unaffected (the <think> strip below stays as a fallback
    for models that inline it instead).
    """
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": cfg.get("think", False),
        "options": {"temperature": temperature, "num_ctx": cfg["num_ctx"]},
    }
    if json_mode:
        payload["format"] = "json"
    r = requests.post(f"{cfg['ollama_host']}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


# ── pipeline stages ───────────────────────────────────────────────────────────
def search_column(cfg: dict, queries: list[str]) -> list[dict]:
    """search -> dedupe by url. Returns candidate dicts: title, url, snippet."""
    s = cfg["search"]
    out: dict[str, dict] = {}
    with DDGS() as ddgs:
        for q in queries:
            try:
                hits = ddgs.text(q, region=s["region"],
                                 timelimit=s["timelimit"],
                                 max_results=s["results_per_query"])
            except Exception as e:  # rate limit, network, parse
                log(f"search failed for {q!r}: {e}")
                hits = []
            for h in hits:
                url = h.get("href") or h.get("url", "")
                if url and url not in out and not is_indexish(url):
                    out[url] = {"title": h.get("title", ""), "url": url,
                                "snippet": h.get("body", "")}
            time.sleep(s["sleep_between"])
    return list(out.values())


def drop_excluded(column: dict, candidates: list[dict]) -> list[dict]:
    """Filter candidates a column can never use, by regex over title + snippet.

    This is deliberately NOT the model's job. Whether this GPU has FP4 tensor cores
    is a fact, not a judgment call, and the brief-only version kept selecting NVFP4
    checkpoints anyway (and writing up their Blackwell-only speedups as if they
    applied). Same lesson the inbox learned: let AI prune coarsely and judge, but
    keep hard rules deterministic.
    """
    pats = [re.compile(p, re.I) for p in column.get("exclude", [])]
    if not pats:
        return candidates
    kept, dropped = [], []
    for c in candidates:
        # Title only, deliberately. Matching the snippet too killed a real
        # release-news post ("Kimi K3 in the next few hours…") because someone
        # mentioned fp4 downthread. The format is named in a model id or a post
        # title when the item IS that artifact; in body text it's just chatter.
        if any(p.search(c.get("title", "")) for p in pats):
            dropped.append(c["title"][:60])
        else:
            kept.append(c)
    if dropped:
        log(f"[{column['name']}] excluded {len(dropped)}: {', '.join(dropped[:4])}"
            + (" …" if len(dropped) > 4 else ""))
    return kept


def select(cfg: dict, column: str, candidates: list[dict],
           brief: str | None = None) -> list[dict]:
    """Local LLM picks the most significant, reputable, non-sensational stories.

    A column's optional `brief` is its editorial standing order — what this section
    is *for* — and overrides generic newsworthiness when the two disagree.
    """
    n = cfg["select"]["per_column"]
    menu = "\n".join(
        f"[{i}] {c['title']}\n     {c['snippet'][:200]}\n     {c['url']}"
        for i, c in enumerate(candidates)
    )
    system = (
        "You are the editor of a calm, intelligent personal newspaper. This is a "
        "NEWS section: every pick must report a recent, datable DEVELOPMENT — a "
        "release, announcement, event, ruling, or a newly published book/essay/"
        "article. REJECT anything evergreen or timeless: teacher/author bios, "
        "perennial explainers, course or 'about' pages, reference material, and "
        "feed/index pages. Also reject clickbait, sensationalism, listicles, SEO "
        "spam, and near-duplicates of the same event. "
        "Quality over quota: it is BETTER to return FEWER items — even an empty "
        "list — than to include something that is not actually news. Reply ONLY as JSON."
    )
    brief_block = f"\nEditorial brief for this section (overrides all else):\n{brief}\n" if brief else ""
    user = (
        f"Section: {column}\n{brief_block}\nCandidates:\n{menu}\n\n"
        f"Select at most {n} — only the ones that are genuinely recent news"
        f"{' AND satisfy the editorial brief' if brief else ''}. "
        "Fewer (or none) is fine. Return JSON: "
        '{"selected": [<indices>], "reason": "<one line>"}'
    )
    try:
        data = json.loads(llm(cfg, system, user, json_mode=True))
        idxs = [i for i in data.get("selected", []) if isinstance(i, int)
                and 0 <= i < len(candidates)]
    except (json.JSONDecodeError, KeyError) as e:
        log(f"select parse failed ({e}); falling back to first {n}")
        idxs = list(range(min(n, len(candidates))))
    return [candidates[i] for i in idxs[:n]]


def fetch_clean(cfg: dict, url: str) -> str | None:
    """Scrape + extract clean main text. Returns None if it can't get usable text.

    trafilatura first; if that's blocked, retry once with a browser User-Agent.
    """
    html = None
    try:
        html = trafilatura.fetch_url(url)
    except Exception:
        pass
    if not html:  # bot-blocked or empty — retry as a browser
        try:
            r = requests.get(url, timeout=cfg["write"]["fetch_timeout"],
                             headers={"User-Agent": UA})
            if r.ok and r.text:
                html = r.text
        except Exception as e:
            log(f"fetch failed {url}: {e}")
    if not html:
        return None
    return trafilatura.extract(html, include_comments=False,
                               include_tables=False, favor_precision=True)


def write_story(cfg: dict, column: str, story: dict, body: str) -> dict | None:
    """Local LLM writes the dispatch. Returns a structured story dict, or None."""
    system = (
        "You write short, calm newspaper dispatches. Plain, precise, "
        "non-sensational. No hype words, no 'breaking', no editorializing. "
        "Do not invent facts beyond the text you are given. Reply ONLY as JSON."
    )
    user = (
        f"Section: {column}\nSource title: {story['title']}\n\n"
        f"Article text:\n{body[:cfg['write']['max_article_chars']]}\n\n"
        'Return JSON: {"headline": "<plain headline, <=12 words, no hype, no '
        'markdown>", "summary": "<2-3 calm sentences: what happened and why it '
        'matters>"}'
    )
    try:
        d = json.loads(llm(cfg, system, user, json_mode=True, temperature=0.4))
        headline = str(d["headline"]).strip().lstrip("# ")
        summary = str(d["summary"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log(f"  write parse failed: {e}")
        return None
    if not headline or not summary:
        return None
    return {"headline": headline, "summary": summary,
            "url": story["url"], "source": domain(story["url"])}


def verify_story(cfg: dict, story: dict, body: str) -> dict | None:
    """Re-read a written story against its source. Fix it, or throw it out.

    The writer is the only stage that can invent facts, and nothing downstream could
    catch it: the recommender trusts the column, and the column is all the reader
    sees. Observed inventions include "compatible with Azure deployment endpoints"
    (conjured from tag metadata) — plausible, specific, and wholly absent from the
    source.

    So this pass is adversarial by construction: a separate call, told to assume the
    summary is wrong and to hunt for claims the source doesn't support. Asking the
    writer "are you sure?" just gets agreement; asking a fresh critic "what did they
    make up?" gets findings. One rewrite attempt, then drop — a story we can't state
    accurately is worth less than the silence it leaves.
    """
    v = cfg.get("verify", {})
    if not v.get("enabled"):
        return story
    system = (
        "You fact-check newspaper copy against its source text. Assume the writer "
        "hallucinated: your job is to catch claims the source does not support. "
        "A claim is unsupported if the source does not state it — plausible, "
        "well-known, or probably-true is NOT supported. Watch especially for "
        "invented specifics: numbers, hardware, platforms, benchmarks, dates, "
        "compatibility claims. Do not object to compression, ordinary paraphrase, "
        "or omission — only to statements the source cannot back. Reply ONLY as JSON."
    )
    user = (
        f"SOURCE TEXT:\n{body[:cfg['write']['max_article_chars']]}\n\n"
        f"HEADLINE: {story['headline']}\nSUMMARY: {story['summary']}\n\n"
        'Return JSON: {"supported": <true if EVERY claim is backed by the source>, '
        '"problems": ["<each unsupported claim, quoted>"], '
        '"fixed_headline": "<the headline, corrected if needed>", '
        '"fixed_summary": "<the summary rewritten using ONLY what the source '
        'supports; keep it 2-3 calm sentences>"}'
    )
    try:
        d = json.loads(llm(cfg, system, user, json_mode=True, temperature=0.1))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log(f"  verify parse failed ({e}); keeping story as written")
        return story
    if d.get("supported"):
        return story
    problems = [str(p) for p in d.get("problems", []) if str(p).strip()]
    fixed_h = str(d.get("fixed_headline", "")).strip().lstrip("# ")
    fixed_s = str(d.get("fixed_summary", "")).strip()
    if not fixed_s or not fixed_h:
        log(f"  ✗ dropped (unfixable): {story['headline'][:52]}")
        return None
    log(f"  ~ corrected: {story['headline'][:44]} — {problems[0][:60] if problems else '?'}")
    return {**story, "headline": fixed_h, "summary": fixed_s, "corrected": True}


def recommend(cfg: dict, column: dict, stories: list[dict]) -> dict | None:
    """The paper reads its column back and says what, if anything, to DO about it.

    This is the actual payload. The reader doesn't want news, he wants to know
    whether it's time to act — so a second pass re-reads the finished stories
    against the same rig facts the selector used, and answers that directly.

    Two failure modes it is explicitly steered away from, both learned the hard way
    on 2026-07-15: manufacturing urgency when the honest answer is "nothing changed
    this week", and repeating a vendor's headline number without checking it applies
    to THIS hardware (an NVFP4 checkpoint got recommended as an upgrade for a card
    with no FP4 tensor cores).
    """
    if not stories or not cfg.get("recommend", {}).get("enabled"):
        return None
    brief = column.get("brief", "")
    menu = "\n\n".join(
        f"- {s['headline']}\n  {s['summary']}\n  ({s['source']})" for s in stories)
    system = (
        "You advise ONE reader whose exact setup is given. Your only job: say "
        "whether anything in this section is worth ACTING on, for him, now.\n"
        "Rules:\n"
        "1. Doing nothing is a real, common, and respectable answer. Most weeks "
        "nothing genuinely changes. Say so plainly rather than inventing a reason "
        "to act. Never manufacture urgency.\n"
        "2. Never recommend anything whose benefit depends on hardware or a file "
        "format he does not have. If a claimed speedup needs a different GPU "
        "architecture, or a format his runtime cannot load, it is NOT an upgrade "
        "for him — say that explicitly instead of repeating the vendor's number.\n"
        "3. If an item is promising but you cannot tell from the text whether it "
        "fits his rig, say what would have to be checked. Do not guess.\n"
        "4. Be concrete and specific. Name the thing. No vague 'keep an eye on AI'.\n"
        "Reply ONLY as JSON."
    )
    user = (
        f"The reader's setup and standing editorial brief:\n{brief}\n\n"
        f"This week's '{column['name']}' section, as written:\n\n{menu}\n\n"
        'Return JSON: {"verdict": "<one line: the bottom line, e.g. \'Nothing to '
        'do this week.\' or \'One thing worth a look: X\'>", "notes": ["<a specific '
        'point, naming the item and why it does or does not matter for HIS rig>", '
        '"<another, if warranted>"]}'
    )
    try:
        d = json.loads(llm(cfg, system, user, json_mode=True, temperature=0.2))
        verdict = str(d.get("verdict", "")).strip()
        notes = [str(n).strip() for n in d.get("notes", []) if str(n).strip()]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log(f"  recommend parse failed: {e}")
        return None
    if not verdict:
        return None
    log(f"[{column['name']}] verdict: {verdict[:70]}")
    return {"verdict": verdict, "notes": notes}


# ── assembly ──────────────────────────────────────────────────────────────────
def generate_issue(cfg: dict, only: str | None, seen: dict[str, str],
                   update_seen: bool) -> dict:
    """Run the pipeline and return the structured issue (the app's data contract)."""
    today = dt.date.today().isoformat()
    columns_out = []
    for col in cfg["columns"]:
        if only and col["name"].lower() != only.lower():
            continue
        # A column either names its sources (curated: high signal, no scrape) or
        # states queries (open-web search). Curated wins where both are present.
        if col.get("sources"):
            log(f"[{col['name']}] collecting curated sources…")
            raw = sources.collect(col["sources"])
        else:
            log(f"[{col['name']}] searching…")
            raw = search_column(cfg, col["queries"])
        # dedupe against PREVIOUS days only (today's URLs stay eligible for regenerate)
        candidates = [c for c in raw if seen.get(c["url"], today) >= today]
        candidates = drop_excluded(col, candidates)
        log(f"[{col['name']}] {len(candidates)} fresh candidates")
        if not candidates:
            continue
        chosen = select(cfg, col["name"], candidates, col.get("brief"))
        log(f"[{col['name']}] selected {len(chosen)}")
        stories = []
        for st in chosen:
            # Curated sources hand us the text (release notes, post body) or point
            # at plain text; those URLs (reddit, HF) resist scraping anyway. Only
            # search hits get the trafilatura treatment.
            body = st.get("body")
            if not body and st.get("raw_url"):
                body = sources.fetch_raw(st["raw_url"])
                if body:  # keep the stats — the card alone won't say it's trending
                    body = f"{st.get('meta', '')}\n\n{body}"
            provided = bool(body)
            if not body:
                body = fetch_clean(cfg, st["url"])
            if not body or len(body) < (80 if provided else 250):
                log(f"  skip (no text): {st['url']}")
                continue
            log(f"  writing: {st['title'][:60]}")
            s = write_story(cfg, col["name"], st, body)
            if s:
                s = verify_story(cfg, s, body)   # may correct it, or drop it entirely
            if s:
                stories.append(s)
                if update_seen:
                    seen[st["url"]] = today
        if stories:
            out_col = {"name": col["name"], "stories": stories}
            rec = recommend(cfg, col, stories)
            if rec:
                out_col["recommendation"] = rec
            columns_out.append(out_col)
    return {
        "date": today,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "columns": columns_out,
    }


def story_count(issue: dict) -> int:
    return sum(len(c["stories"]) for c in issue["columns"])


def render_markdown(issue: dict) -> str:
    """The human/Obsidian view of the same structured issue."""
    parts = [f"# The Daily Tower — {issue['date']}\n",
             "*Curated and written by local qwen on miro-tower.*\n"]
    for col in issue["columns"]:
        parts.append(f"\n## {col['name']}\n")
        blocks = [f"### {s['headline']}\n\n{s['summary']}\n\n"
                  f"[{s['source']}]({s['url']})" for s in col["stories"]]
        parts.append("\n\n---\n\n".join(blocks))
        rec = col.get("recommendation")
        if rec:
            parts.append(f"\n### Recommendations\n\n**{rec['verdict']}**\n")
            if rec.get("notes"):
                parts.append("\n".join(f"- {n}" for n in rec["notes"]))
    return "\n".join(parts) + "\n"


def deliver(issue: dict, base: Path) -> Path:
    """Write dated issue-<date>.{json,md} + stable latest.{json,md}. Returns latest.json."""
    base.mkdir(parents=True, exist_ok=True)
    md = render_markdown(issue)
    data = json.dumps(issue, indent=2, ensure_ascii=False)
    (base / f"issue-{issue['date']}.json").write_text(data)
    (base / f"issue-{issue['date']}.md").write_text(md)
    latest = base / "latest.json"
    latest.write_text(data)                     # <- what the web app reads
    (base / "latest.md").write_text(md)
    return latest


def main() -> None:
    ap = argparse.ArgumentParser(description="ai-rss personal newspaper")
    ap.add_argument("--dry-run", action="store_true",
                    help="write to ./out/, don't touch kb or seen-state")
    ap.add_argument("--column", help="run only this column (by name)")
    args = ap.parse_args()

    cfg = load_config()
    update_seen = not args.dry_run
    seen = load_seen()

    log(f"model={cfg['model']}  dry_run={args.dry_run}  "
        f"column={args.column or 'ALL'}")
    issue = generate_issue(cfg, args.column, seen, update_seen)

    if args.dry_run:
        base = OUT_DIR
    else:
        base = Path(os.path.expanduser(cfg["deliver"]["outbox"])) / "ai-rss"
    out = deliver(issue, base)
    if not args.dry_run:
        save_seen(seen)

    log(f"done — {story_count(issue)} stories -> {out}")
    print(out)


if __name__ == "__main__":
    main()
