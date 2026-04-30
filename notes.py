import os
import re
import sys
import tty
import termios
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

console = Console()

kb_path = Path.home() / "kb"
ideas_path = kb_path / "ideas"

EXCLUDE_FROM_CANDIDATES = {
    "journal", "projects", "ideas", "inbox", "outbox",
    "templates", "__pycache__", "scratch.md", ".trash",
}

GRADUATED_STATUSES = {"mature"}


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def build_link_index():
    link_pattern = re.compile(r'\[\[([^\]|#]+)')
    referenced = set()
    for md_file in kb_path.rglob("*.md"):
        try:
            content = md_file.read_text(errors="ignore")
        except OSError:
            continue
        for match in link_pattern.finditer(content):
            slug = match.group(1).strip().lower()
            referenced.add(slug)
            referenced.add(Path(slug).stem)
    return referenced


def get_candidate_notes():
    candidates = []
    for md_file in kb_path.rglob("*.md"):
        try:
            rel = md_file.relative_to(kb_path)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) == 1:
            continue
        if any(p in EXCLUDE_FROM_CANDIDATES for p in parts):
            continue
        candidates.append(md_file)
    return candidates


def get_frontmatter_field(content, field):
    for line in content.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def get_title(content, fallback):
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def get_snippet(content):
    past_frontmatter = False
    dash_count = 0
    lines = []
    for line in content.splitlines():
        if line.strip() == "---":
            dash_count += 1
            if dash_count >= 2:
                past_frontmatter = True
            continue
        if not past_frontmatter:
            continue
        if line.startswith("#"):
            continue
        if line.strip():
            lines.append(line.strip())
        if len(lines) >= 2:
            break
    return " ".join(lines)


def is_orphan(md_file, link_index):
    stem = md_file.stem.lower()
    rel = str(md_file.relative_to(kb_path)).lower().replace(".md", "")
    return stem not in link_index and rel not in link_index


def is_unprocessed(content):
    tags = get_frontmatter_field(content, "tags")
    status = get_frontmatter_field(content, "status")
    return tags in ("[]", "") or status in ("seed", "")


def get_reasons(md_file, content, link_index):
    reasons = []
    if is_orphan(md_file, link_index):
        reasons.append("orphan")
    if is_unprocessed(content):
        reasons.append("unprocessed")
    return reasons


def open_in_nvim(file):
    os.spawnlp(os.P_WAIT, "nvim", "nvim", str(file))


def move_to_ideas(md_file):
    ideas_path.mkdir(parents=True, exist_ok=True)
    dest = ideas_path / md_file.name
    # avoid collision
    if dest.exists():
        dest = ideas_path / f"{md_file.stem}-note{md_file.suffix}"
    md_file.rename(dest)
    console.print(f"[dark_sea_green4]  → ideas/{dest.name}[/dark_sea_green4]")


def review_note(md_file, index, total, link_index):
    content = md_file.read_text(errors="ignore")
    title = get_title(content, md_file.stem)
    status = get_frontmatter_field(content, "status")
    tags = get_frontmatter_field(content, "tags")
    created = get_frontmatter_field(content, "created")
    snippet = get_snippet(content)
    reasons = get_reasons(md_file, content, link_index)

    rel = str(md_file.relative_to(kb_path))

    meta_parts = []
    if status:
        sc = {"seed": "grey70", "growing": "steel_blue1"}.get(status, "grey70")
        meta_parts.append(f"[{sc}]{status}[/{sc}]")
    if created:
        meta_parts.append(f"[grey50]{created}[/grey50]")
    meta_parts.append(f"[grey35]{rel}[/grey35]")

    reason_str = "  ".join(f"[indian_red]{r}[/indian_red]" for r in reasons)

    lines = ["  ".join(meta_parts)]
    if reason_str:
        lines.append(reason_str)
    if tags and tags not in ("[]", ""):
        lines.append(f"[grey50]tags[/grey50]  [gold3]{tags}[/gold3]")
    if snippet:
        lines.append(f"\n[grey80]{snippet}[/grey80]")

    hotkeys = (
        "[grey50][[/grey50][steel_blue1]k[/steel_blue1][grey50]][/grey50] keep  "
        "[grey50][[/grey50][steel_blue1]o[/steel_blue1][grey50]][/grey50] open  "
        "[grey50][[/grey50][dark_sea_green4]i[/dark_sea_green4][grey50]][/grey50] → ideas  "
        "[grey50][[/grey50][indian_red]d[/indian_red][grey50]][/grey50] discard  "
        "[grey50][[/grey50][grey70]q[/grey70][grey50]][/grey50] quit"
    )

    console.print()
    console.print(Rule(
        f"[grey50]{index}[/grey50][grey35] of {total}[/grey35]  [tan]{title}[/tan]",
        style="grey23"
    ))
    console.print(Panel("\n".join(lines), border_style="grey30", padding=(1, 3)))
    console.print(f"\n  {hotkeys}\n")

    while True:
        key = getch()
        if key == "k":
            console.print("[steel_blue1]  → keeping[/steel_blue1]")
            return True
        elif key == "o":
            open_in_nvim(md_file)
            return True
        elif key == "i":
            move_to_ideas(md_file)
            return True
        elif key == "d":
            md_file.unlink()
            console.print("[indian_red]  → discarded[/indian_red]")
            return True
        elif key == "q":
            return False


def main():
    console.print()
    console.print("[grey50]  scanning notes…[/grey50]")

    link_index = build_link_index()
    candidates = get_candidate_notes()

    flagged = []
    for md_file in candidates:
        content = md_file.read_text(errors="ignore")
        status = get_frontmatter_field(content, "status")
        if status in GRADUATED_STATUSES:
            continue
        reasons = get_reasons(md_file, content, link_index)
        if reasons:
            flagged.append(md_file)

    # Sort: orphan+unprocessed first, then orphan-only, then unprocessed-only
    def sort_key(f):
        c = f.read_text(errors="ignore")
        r = get_reasons(f, c, link_index)
        return (-len(r), f.name)

    flagged.sort(key=sort_key)

    if not flagged:
        console.print("[grey50]  all notes look good[/grey50]")
        console.print()
        return

    total = len(flagged)
    console.print()
    console.print(Rule(
        f"[bold steel_blue1]  Notes Review[/bold steel_blue1]  [grey50]{total} need attention[/grey50]",
        style="steel_blue1 dim"
    ))
    console.print()

    for i, md_file in enumerate(flagged, 1):
        keep_going = review_note(md_file, i, total, link_index)
        if not keep_going:
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
