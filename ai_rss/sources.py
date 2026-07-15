"""Curated sources — the candidate producers for a column.

Why this exists: open-web search (DDG) for "new LLM release" returns mostly
AI-generated SEO spam (markaicode.com, insiderllm.com, benchlm.ai…). The local
model then spends its judgment rejecting sludge instead of ranking real news.
For a topic whose good sources are few, known, and stable, naming them beats
searching for them. The AI's job moves to where it's actually good: deciding
which of these developments matter for *this* reader's hardware, and writing
them up.

Every fetcher returns the same candidate contract as ai_rss.search_column():

    {"title": str, "url": str, "snippet": str, "body": str | None}

`body` is the story text when the source already hands it to us (a release note,
a reddit post). When present the pipeline uses it and skips the scrape entirely —
these URLs (reddit, HF) are hostile to trafilatura anyway. When None, the
pipeline scrapes the URL as usual.

`raw_url` is the middle case: plain text exists but is a whole extra request
(a HF model card). Selection only needs the metadata in `snippet`, so the fetch
is deferred to write time and pays for the ~6 chosen, not the ~30 collected.
"""
from __future__ import annotations

import html as html_mod
import re
import time

import feedparser
import requests

# Reddit serves its SPA shell (or a 429) to anything that doesn't look like a
# browser; its .rss is fine with one. The .json API is blocked outright.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

TIMEOUT = 20


def _log(msg: str) -> None:
    print(f"  · {msg}", flush=True)


def _strip_html(s: str) -> str:
    """Feed summaries are HTML fragments; we want plain text for the model."""
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def _get(url: str, retries: int = 3, **kw) -> requests.Response | None:
    """GET with backoff on 429. Reddit rate-limits by IP and recovers in ~30s."""
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             headers={"User-Agent": UA}, **kw)
        except Exception as e:
            _log(f"fetch error {url}: {e}")
            return None
        if r.status_code == 429:
            wait = 20 * (attempt + 1)
            _log(f"429 from {url} — backing off {wait}s")
            time.sleep(wait)
            continue
        if not r.ok:
            _log(f"HTTP {r.status_code} from {url}")
            return None
        return r
    _log(f"gave up after {retries} tries (rate-limited): {url}")
    return None


# ── fetchers ──────────────────────────────────────────────────────────────────
def from_rss(spec: dict) -> list[dict]:
    """Any Atom/RSS feed. Covers r/LocalLLaMA (top.rss) and blogs alike."""
    url, label = spec["url"], spec.get("label", "rss")
    limit = spec.get("limit", 25)
    r = _get(url)
    if not r:
        return []
    feed = feedparser.parse(r.text)
    out = []
    for e in feed.entries[:limit]:
        link = e.get("link", "")
        if not link:
            continue
        raw = e.get("summary", "") or (e.get("content") or [{}])[0].get("value", "")
        text = _strip_html(raw)
        title = _strip_html(e.get("title", ""))
        # Title + discussion text is the story for link-posts, where the body is
        # just "submitted by u/…". Keep both so the writer has something to chew.
        body = f"{title}\n\n{text}" if text else title
        out.append({"title": title, "url": link, "snippet": text[:300],
                    "body": body})
    _log(f"{label}: {len(out)} items")
    return out


def from_github_releases(spec: dict) -> list[dict]:
    """Release notes for an inference engine — the body IS the story."""
    repo = spec["repo"]
    limit = spec.get("limit", 3)
    r = _get(f"https://api.github.com/repos/{repo}/releases?per_page={limit}")
    if not r:
        return []
    out = []
    for rel in r.json():
        if rel.get("draft") or rel.get("prerelease"):
            continue
        name = rel.get("name") or rel.get("tag_name") or ""
        notes = (rel.get("body") or "").strip()
        title = f"{repo} {name}"
        out.append({
            "title": title,
            "url": rel.get("html_url", ""),
            "snippet": notes[:300],
            "body": f"{title} released {rel.get('published_at','')[:10]}\n\n{notes}",
        })
    _log(f"{repo}: {len(out)} releases")
    return out


def from_hf_trending(spec: dict) -> list[dict]:
    """Trending models on the Hub — the leading edge of what's downloadable."""
    limit = spec.get("limit", 30)
    r = _get("https://huggingface.co/api/models"
             f"?sort=trendingScore&direction=-1&limit={limit}")
    if not r:
        return []
    out = []
    for m in r.json():
        mid = m.get("id", "")
        if not mid:
            continue
        tags = [t for t in m.get("tags", []) if not t.startswith(("license:", "region:"))]
        meta = (f"{mid} — trending {m.get('trendingScore')}, "
                f"{m.get('likes', 0)} likes, {m.get('downloads', 0)} downloads. "
                f"Tags: {', '.join(tags[:12])}")
        out.append({
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "snippet": meta,
            # The model card is the story; metadata alone yields "gains traction
            # on Hugging Face" filler. Fetched only if this model is selected.
            "raw_url": f"https://huggingface.co/{mid}/raw/main/README.md",
            "meta": meta,
        })
    _log(f"hf trending: {len(out)} models")
    return out


FETCHERS = {
    "rss": from_rss,
    "github_releases": from_github_releases,
    "hf_trending": from_hf_trending,
}


def fetch_raw(url: str) -> str | None:
    """Plain-text fetch for a `raw_url` (no scraping/extraction involved)."""
    r = _get(url, retries=2)
    return r.text if r and r.text.strip() else None


def collect(specs: list[dict]) -> list[dict]:
    """Run every source for a column; dedupe by URL, preserving first-seen order."""
    out: dict[str, dict] = {}
    for spec in specs:
        fn = FETCHERS.get(spec.get("type", ""))
        if not fn:
            _log(f"unknown source type: {spec.get('type')!r} — skipped")
            continue
        try:
            items = fn(spec)
        except Exception as e:
            _log(f"source {spec.get('type')} failed: {e}")
            items = []
        for it in items:
            out.setdefault(it["url"], it)
    return list(out.values())
