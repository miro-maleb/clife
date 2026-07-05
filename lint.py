"""lint.py — `cl lint` — kb frontmatter linter.

Checks every structured kb file (the systems/goals/orientations/blocks spine +
projects areas/projects/sub-projects) against the canonical schema in
`schema.py`, and reports drift: legacy kebab keys, unknown keys, bad/absent
status values, non-ISO dates, template-placeholder leakage, malformed lists.

  cl lint [PATH …]        lint the whole kb (or just the given paths)
  cl lint --fix           apply the safe mechanical fixes in place
  cl lint --json          machine-readable report (for surfaces)
  cl lint --type TYPE     restrict to one type (project|system|goal|…)

`--fix` only does the unambiguous rewrites — rename legacy keys, strip inline
`# comments` off scalar values, normalize a bare `key:` list to `key: []`, and
remap known status strays. Everything needing judgment (missing required keys,
placeholder values that were never filled in, unknown keys) is reported for a
human. Fixes are byte-targeted (frontmatter only; body untouched).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

import fm
import schema
import week
from paths import DATA_DIR

console = Console()

KB = week.KB
# Persistent snapshot of the latest read-only scan — written by the weekly timer
# (`cl lint --snapshot`) and read by the Surface maintenance page as the "latest
# scan before fixing." Tower-local state, not git-synced (like the pool DB).
REPORT_PATH = DATA_DIR / "lint-report.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(:)(.*)$")
_BLOCK_ITEM = re.compile(r"^\s+-\s+")

# Files/dirs we never descend into.
SKIP_PARTS = {".git", ".trash", "__pycache__", "node_modules", "venv", ".venv"}
# Only these top-level trees are in scope (matches the agreed cleanup scope).
SCOPE_TOPS = {"systems", "goals", "orientations", "projects", "templates"}


def in_scope(p: Path) -> bool:
    parts = p.relative_to(KB).parts
    if any(x in SKIP_PARTS for x in parts):
        return False
    return bool(parts) and parts[0] in SCOPE_TOPS


def iter_files(paths):
    roots = [Path(p) for p in paths] if paths else [KB / t for t in sorted(SCOPE_TOPS)]
    for root in roots:
        root = root if root.is_absolute() else (KB / root)
        if root.is_file() and root.suffix == ".md":
            yield root
        elif root.is_dir():
            for f in sorted(root.rglob("*.md")):
                if in_scope(f):
                    yield f


def _fm_lines(text):
    """Return (pre, fm_lines, post, ok). pre is the opening fence line(s)."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines(keepends=False)
    for i, l in enumerate(lines[1:], start=1):
        if l.strip() == "---":
            return (lines[0], lines[1:i], lines[i:], True)
    return None


def _strip_comment(val: str) -> str:
    """Drop a trailing ` # comment` from an unquoted scalar value."""
    if '"' in val or "'" in val:
        return val
    return re.sub(r"\s+#.*$", "", val).rstrip()


def check_file(path: Path):
    """Return (issues, fixed_text_or_None). issues: list of (category, detail)."""
    typ = schema.classify(path)
    if typ is None:
        return [], None
    text = path.read_text()
    parsed = _fm_lines(text)
    rel = str(path.relative_to(KB))
    issues = []
    if parsed is None:
        # a document/template without frontmatter is fine; a spine file is not
        if typ in schema.SCHEMAS:
            issues.append(("no-frontmatter", rel))
        return issues, None
    fence, fmlines, post, _ = parsed

    spec = schema.SCHEMAS.get(typ)
    lenient = typ in ("template", "document")
    new_lines = []
    seen_keys = set()
    changed = False

    for i, line in enumerate(fmlines):
        m = KEY_RE.match(line)
        if not m:
            new_lines.append(line)
            continue
        indent, key, colon, rest = m.groups()
        val = rest.strip()
        # a key whose value is empty but whose next line is `- item` is a
        # block-style list header, NOT a bare/empty list — never "fix" it to []
        next_is_item = (i + 1 < len(fmlines)) and bool(_BLOCK_ITEM.match(fmlines[i + 1]))

        # 1) legacy kebab key → canonical snake (universal)
        if key in schema.KEY_ALIASES:
            canon = schema.KEY_ALIASES[key]
            issues.append(("legacy-key", f"{rel}: {key} → {canon}"))
            key = canon
            changed = True

        seen_keys.add(key)

        # 2) unknown key (schema'd types only; open_keys → just note)
        if spec and not lenient and key not in spec["known"]:
            if spec.get("open_keys"):
                issues.append(("unknown-key (allowed)", f"{rel}: {key}"))
            else:
                issues.append(("unknown-key", f"{rel}: {key}"))

        # 3) placeholder leakage: a real (non-template) value with ` | `
        if not lenient and " | " in val and not val.startswith("["):
            issues.append(("placeholder-value", f"{rel}: {key}: {val}"))

        # 4) inline comment on a scalar → strip (fixable)
        if "#" in val and not val.startswith("[") and _strip_comment(val) != val:
            val = _strip_comment(val)
            rest = f" {val}"
            issues.append(("inline-comment", f"{rel}: {key}"))
            changed = True

        # 5) status enum (schema'd, non-lenient)
        if spec and not lenient and spec.get("status") and key == "status" and val:
            v = val
            nv = schema.status_remap(typ, v)
            if nv:
                issues.append(("status-remap", f"{rel}: {v} → {nv}"))
                val, rest, changed = nv, f" {nv}", True
                v = nv
            if v not in spec["status"] and " | " not in v:
                issues.append(("bad-status", f"{rel}: status: {v}"))

        # 6) bare list key → []  (fixable) — but not a block-list header
        if spec and key in spec.get("lists", set()) and val == "" and not next_is_item:
            val, rest = "[]", " []"
            issues.append(("bare-list", f"{rel}: {key}"))
            changed = True

        # 7) date format (schema'd, non-lenient)
        if spec and not lenient and key in spec.get("dates", set()) and val:
            bare = _strip_comment(val).strip('"')
            if bare and not DATE_RE.match(bare):
                issues.append(("bad-date", f"{rel}: {key}: {val}"))

        new_lines.append(f"{indent}{key}{colon}{rest}")

    # 8) required keys present?
    if spec and not lenient:
        missing = spec["required"] - seen_keys
        for k in sorted(missing):
            issues.append(("missing-required", f"{rel}: {k}"))

    # 9) identity fields must match the file's location (report-only)
    if not lenient:
        for key, expected, actual in schema.identity_issues(typ, path, fm.read(path)):
            issues.append(("identity-mismatch", f"{rel}: {key}: {actual} ≠ {expected} (from path)"))

    fixed = None
    if changed:
        trailing_nl = text.endswith("\n")
        out = "\n".join([fence] + new_lines + post)
        fixed = out + "\n" if trailing_nl else out
    return issues, fixed


def scan(paths=None, type_filter=None, fix=False):
    """Run the linter and return (report_dict, n_fixed). When fix=True, applies
    the safe rewrites in place. The report is the machine model both --json and
    the snapshot share."""
    from collections import defaultdict
    by_cat = defaultdict(list)
    n_files = 0
    n_fixed = 0
    for f in iter_files(paths):
        if type_filter and schema.classify(f) != type_filter:
            continue
        n_files += 1
        issues, fixed = check_file(f)
        for cat, detail in issues:
            by_cat[cat].append(detail)
        if fixed is not None and fix:
            f.write_text(fixed)
            n_fixed += 1
    total = sum(len(v) for v in by_cat.values())
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": n_files, "issues": total, "fixed": n_fixed,
        "by_category": {k: v for k, v in by_cat.items()},
    }
    return report, n_fixed


def main():
    ap = argparse.ArgumentParser(prog="cl lint", description="kb frontmatter linter")
    ap.add_argument("paths", nargs="*", help="limit to these files/dirs")
    ap.add_argument("--fix", action="store_true", help="apply safe fixes in place")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--type", help="restrict to one schema type")
    ap.add_argument("--snapshot", action="store_true",
                    help="read-only scan → write the timestamped report to "
                         f"{REPORT_PATH} (for the weekly timer / maintenance page)")
    args = ap.parse_args()

    # A snapshot is always read-only — it captures drift *before* any fix.
    report, n_fixed = scan(args.paths, args.type, fix=args.fix and not args.snapshot)
    total = report["issues"]
    by_cat = report["by_category"]

    if args.snapshot:
        report["trigger"] = os.environ.get("CLIFE_LINT_TRIGGER", "manual")
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report))
        console.print(f"[green]✓[/green] snapshot written — {report['files']} files, "
                      f"{total} issue(s) → {REPORT_PATH}")
        return
    if args.json:
        print(json.dumps(report))
        return

    # severity order: blocking first, cosmetic last
    order = ["no-frontmatter", "missing-required", "identity-mismatch", "bad-status",
             "placeholder-value", "unknown-key", "bad-date", "legacy-key",
             "inline-comment", "bare-list", "status-remap", "unknown-key (allowed)"]
    cats = sorted(by_cat, key=lambda c: (order.index(c) if c in order else 99, c))
    if not total:
        console.print(f"[green]✓ clean[/green] — {report['files']} files, no frontmatter drift.")
        return
    for cat in cats:
        items = by_cat[cat]
        console.print(f"[bold yellow]⚠ {cat}[/bold yellow] [grey50]({len(items)})[/grey50]")
        for it in items[:30]:
            console.print(f"   [grey70]{it}[/grey70]")
        if len(items) > 30:
            console.print(f"   [grey50]… and {len(items) - 30} more[/grey50]")
    console.print()
    if args.fix:
        console.print(f"[green]fixed {n_fixed} file(s).[/green]  {total} issue(s) seen; "
                      "remaining need a human.")
    else:
        console.print(f"[grey50]{report['files']} files · {total} issue(s). "
                      "Re-run with --fix for the mechanical ones.[/grey50]")


if __name__ == "__main__":
    main()
