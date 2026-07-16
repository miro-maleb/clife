#!/usr/bin/env python3
"""eval — does ai-rss actually do its job, and is `think:true` worth 5x the runtime?

Every config choice in ai-rss so far (thinking on, whole-article reads, the verify
pass) was justified by argument, not measurement. This turns that around: it feeds
FROZEN inputs into a single pipeline stage and asserts checkable properties of the
output — no live web, so a run is reproducible and a regression is visible.

Scope, deliberately narrow (see ~/kb/ideas/overnight-local-ai-jobs.md): ai-rss only,
graded by deterministic assertions. No LLM-as-judge — we do not trust a model to score
a model. Every check here is plain code over the stage's output.

The model is the only nondeterministic part, so each case runs N times and reports k/N
passes; a real behaviour should be robust, not a coin flip. `--think both` runs the whole
suite twice (on/off) side by side — that comparison is the headline deliverable.

    ./run_eval.py                       # think as configured, 3 runs each
    ./run_eval.py --think both          # the on-vs-off scoreboard
    ./run_eval.py --think off --runs 1  # fast smoke test of the machinery
    ./run_eval.py --case verify-catches-hallucination
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # import the pipeline under test
import ai_rss                                  # noqa: E402

CASES_DIR = HERE / "cases"


# ── the check vocabulary — plain code, one function, no model ──────────────────
def _blob(stage: str, out) -> str:
    """The searchable text an output reduces to, per stage."""
    if out is None:
        return ""
    if stage == "verify":
        return f"{out.get('headline','')} {out.get('summary','')}"
    if stage == "recommend":
        return f"{out.get('verdict','')} " + " ".join(out.get("notes", []))
    if stage == "select":
        return " ".join(f"{c.get('title','')} {c.get('url','')}" for c in out)
    return ""


def run_checks(checks: dict, stage: str, out, dropped: list[dict]) -> list[tuple]:
    """Return [(check_name, ok, detail)]. `dropped` is populated for the exclude
    stage (candidates the filter removed) so kept/dropped assertions can see both."""
    results = []
    blob = _blob(stage, out).lower()

    def add(name, ok, detail=""):
        results.append((name, ok, detail))

    for name, arg in checks.items():
        if name == "is_none":
            add(name, (out is None) == bool(arg), f"out={'None' if out is None else 'set'}")
        elif name == "corrected":
            got = bool(out and out.get("corrected"))
            add(name, got == bool(arg), f"corrected={got}")
        elif name == "text_excludes":
            bad = [s for s in arg if s.lower() in blob]
            add(name, not bad, f"leaked: {bad}" if bad else "clean")
        elif name == "text_includes":
            miss = [s for s in arg if s.lower() not in blob]
            add(name, not miss, f"missing: {miss}" if miss else "all present")
        elif name == "text_includes_any":
            hit = [s for s in arg if s.lower() in blob]
            add(name, bool(hit), f"matched: {hit}" if hit else f"none of {arg}")
        elif name == "count_max":
            n = len(out or [])
            add(name, n <= arg, f"n={n}")
        elif name == "count_min":
            n = len(out or [])
            add(name, n >= arg, f"n={n}")
        elif name == "url_excluded":
            urls = " ".join(c.get("url", "") for c in (out or []))
            add(name, arg not in urls, f"chosen urls: {urls[:80]}")
        elif name == "url_included":
            urls = " ".join(c.get("url", "") for c in (out or []))
            add(name, arg in urls, "present" if arg in urls else "absent")
        elif name == "dropped_includes":
            titles = " ".join(c["title"] for c in dropped).lower()
            miss = [s for s in arg if s.lower() not in titles]
            add(name, not miss, f"not dropped: {miss}" if miss else "dropped ok")
        elif name == "kept_includes":
            titles = " ".join(c["title"] for c in (out or [])).lower()
            miss = [s for s in arg if s.lower() not in titles]
            add(name, not miss, f"not kept: {miss}" if miss else "kept ok")
        else:
            add(name, False, "UNKNOWN CHECK")
    return results


# ── dispatch a case to the real stage function ────────────────────────────────
def run_stage(cfg: dict, case: dict):
    """Call the pipeline stage under test with the case's frozen input.
    Returns (output, dropped_list)."""
    stage = case["stage"]
    inp = case["input"]
    col = {"name": case.get("column", "Local AI & Open Models"),
           "brief": case.get("brief", ""),
           "exclude": case.get("exclude", cfg["columns"][0].get("exclude", []))}
    if stage == "exclude":
        cands = inp["candidates"]
        kept = ai_rss.drop_excluded(col, cands)
        dropped = [c for c in cands if c not in kept]
        return kept, dropped
    if stage == "select":
        return ai_rss.select(cfg, col["name"], inp["candidates"],
                             col["brief"] or None), []
    if stage == "verify":
        cfg.setdefault("verify", {})["enabled"] = True
        return ai_rss.verify_story(cfg, inp["story"], inp["body"]), []
    if stage == "recommend":
        cfg.setdefault("recommend", {})["enabled"] = True
        return ai_rss.recommend(cfg, col, inp["stories"]), []
    raise ValueError(f"unknown stage: {stage}")


def eval_case(cfg: dict, case: dict, runs: int) -> dict:
    """Run one case `runs` times; a run passes only if every check passes."""
    passes, first_fail = 0, None
    for _ in range(runs):
        try:
            out, dropped = run_stage(cfg, case)
        except Exception as e:
            first_fail = first_fail or f"stage raised: {e}"
            continue
        checks = run_checks(case["assert"], case["stage"], out, dropped)
        if all(ok for _, ok, _ in checks):
            passes += 1
        elif first_fail is None:
            first_fail = "; ".join(f"{n}({d})" for n, ok, d in checks if not ok)
    return {"passes": passes, "runs": runs, "detail": first_fail or ""}


def load_cases(only: str | None) -> list[dict]:
    out = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
        c = yaml.safe_load(f.read_text())
        c["name"] = f.stem
        if only and only not in c["name"]:
            continue
        out.append(c)
    return out


def run_suite(think: bool, cases: list[dict], runs: int) -> list[dict]:
    cfg = ai_rss.load_config()
    cfg["think"] = think
    rows = []
    for c in cases:
        r = eval_case(cfg, c, runs)
        rows.append({"name": c["name"], "stage": c["stage"], **r})
        mark = "✓" if r["passes"] == r["runs"] else ("·" if r["passes"] else "✗")
        print(f"  {mark} [{c['stage']:9}] {c['name']:36} {r['passes']}/{r['runs']}"
              + (f"  — {r['detail'][:70]}" if r["passes"] < r["runs"] else ""),
              flush=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="ai-rss eval harness")
    ap.add_argument("--think", choices=["on", "off", "both"], default="config",
                    help="override cfg.think; 'both' = the on-vs-off scoreboard")
    ap.add_argument("--runs", type=int, default=3, help="repeats per case")
    ap.add_argument("--case", help="substring filter on case name")
    args = ap.parse_args()

    cases = load_cases(args.case)
    if not cases:
        print("no cases found", file=sys.stderr)
        sys.exit(1)

    modes = ([("think:off", False), ("think:on", True)] if args.think == "both"
             else [("think:off", False)] if args.think == "off"
             else [("think:on", True)] if args.think == "on"
             else [("think:config", ai_rss.load_config().get("think", False))])

    results = {}
    for label, think in modes:
        print(f"\n═══ {label} ({args.runs} run{'s' if args.runs > 1 else ''} each) ═══")
        results[label] = run_suite(think, cases, args.runs)

    total = {label: sum(r["passes"] for r in rows) for label, rows in results.items()}
    denom = len(cases) * args.runs
    print("\n─── totals ───")
    for label, score in total.items():
        print(f"  {label:14} {score}/{denom}")
    if args.think == "both":
        d = total["think:on"] - total["think:off"]
        verdict = ("thinking helped" if d > 0 else
                   "thinking hurt" if d < 0 else "no measurable difference")
        print(f"\n  → {verdict} ({d:+d} checks). Runtime cost of thinking is ~5x.")


if __name__ == "__main__":
    main()
