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
           {"headline","summary","url","source"}]}]}
The "regenerate" button just re-runs this script; same-day reruns re-pick freely.
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
    """One non-streaming chat call to the tower's ollama. Thinking stripped."""
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
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


def select(cfg: dict, column: str, candidates: list[dict]) -> list[dict]:
    """Local LLM picks the most significant, reputable, non-sensational stories."""
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
    user = (
        f"Section: {column}\n\nCandidates:\n{menu}\n\n"
        f"Select at most {n} — only the ones that are genuinely recent news. "
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


# ── assembly ──────────────────────────────────────────────────────────────────
def generate_issue(cfg: dict, only: str | None, seen: dict[str, str],
                   update_seen: bool) -> dict:
    """Run the pipeline and return the structured issue (the app's data contract)."""
    today = dt.date.today().isoformat()
    columns_out = []
    for col in cfg["columns"]:
        if only and col["name"].lower() != only.lower():
            continue
        log(f"[{col['name']}] searching…")
        # dedupe against PREVIOUS days only (today's URLs stay eligible for regenerate)
        candidates = [c for c in search_column(cfg, col["queries"])
                      if seen.get(c["url"], today) >= today]
        log(f"[{col['name']}] {len(candidates)} fresh candidates")
        if not candidates:
            continue
        chosen = select(cfg, col["name"], candidates)
        log(f"[{col['name']}] selected {len(chosen)}")
        stories = []
        for st in chosen:
            body = fetch_clean(cfg, st["url"])
            if not body or len(body) < 250:
                log(f"  skip (no text): {st['url']}")
                continue
            log(f"  writing: {st['title'][:60]}")
            s = write_story(cfg, col["name"], st, body)
            if s:
                stories.append(s)
                if update_seen:
                    seen[st["url"]] = today
        if stories:
            columns_out.append({"name": col["name"], "stories": stories})
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
