#!/usr/bin/env python3
"""Write the SAME source articles with thinking on vs off, side by side.

The eval harness scores correctness (did a banned claim leak?), not writing. This
answers the other half: given identical source text, does thinking produce better
prose? Same bodies both ways, so the only variable is `think`. Not delivered anywhere
— pure inspection.

    ./compare_prose.py            # a few representative sources
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ai_rss           # noqa: E402
import sources          # noqa: E402

COL = "Local AI & Open Models"

# One of each source shape, so we see prose across the range: a release note, a
# reddit discussion post, and a Hugging Face model card.
SPECS = [
    {"type": "github_releases", "repo": "ollama/ollama", "limit": 1},
    {"type": "rss", "label": "r/LocalLLaMA",
     "url": "https://www.reddit.com/r/LocalLLaMA/top.rss?t=week", "limit": 6},
    {"type": "hf_trending", "limit": 6},
]


def pick_bodies() -> list[dict]:
    """Grab a real body per source type (fetch HF card if needed)."""
    out = []
    gh = sources.from_github_releases(SPECS[0])
    if gh:
        out.append(gh[0])
    rss = sources.from_rss(SPECS[1])
    if rss:
        out.append(rss[0])
    hf = sources.from_hf_trending(SPECS[2])
    if hf:
        h = hf[0]
        card = sources.fetch_raw(h["raw_url"]) if h.get("raw_url") else None
        if card:
            h = {**h, "body": f"{h.get('meta','')}\n\n{card}"}
        out.append(h)
    return out


def main() -> None:
    cfg = ai_rss.load_config()
    items = pick_bodies()
    print(f"comparing {len(items)} sources, thinking OFF vs ON\n" + "=" * 78)

    for it in items:
        body = it.get("body") or ""
        story = {"title": it["title"], "url": it["url"],
                 "source": ai_rss.domain(it["url"])}
        print(f"\n### SOURCE: {it['title'][:70]}\n({story['source']}, {len(body)} chars of source text)\n")
        for label, think in (("OFF", False), ("ON", True)):
            cfg["think"] = think
            s = ai_rss.write_story(cfg, COL, story, body)
            print(f"── think {label} " + "─" * 62)
            if not s:
                print("  (write returned nothing)\n")
                continue
            print(f"  HEADLINE: {s['headline']}")
            print(textwrap.fill(s["summary"], width=76,
                                initial_indent="  ", subsequent_indent="  "))
            print()


if __name__ == "__main__":
    main()
