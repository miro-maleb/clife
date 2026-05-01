"""
ingest.py — pull new messages from the kb-capture mailbox into ~/kb/inbox/.

Architecture: a dedicated capture email address → Gmail filter labels everything
"kb-capture" → mbsync syncs the label as a local maildir folder → this script
reads unread messages → writes one timestamped markdown file per message into
kb/inbox/ → marks the maildir message as seen.

Configuration: set CL_INGEST_MAILDIR to point at the maildir directory.
Defaults to ~/mail/kb-capture/Inbox/. Set in ~/.config/life-os/secrets.env or
shell env.
"""

import email
import email.policy
import os
import re
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from rich.console import Console
from rich.rule import Rule

console = Console()

INBOX_DIR = Path.home() / "kb" / "inbox"
DEFAULT_MAILDIR = Path.home() / "mail" / "kb-capture" / "Inbox"


def load_maildir():
    """Return the maildir path from env, secrets file, or default."""
    p = os.environ.get("CL_INGEST_MAILDIR", "")
    if not p:
        secrets = Path.home() / ".config" / "life-os" / "secrets.env"
        if secrets.exists():
            for raw in secrets.read_text().splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line.startswith("CL_INGEST_MAILDIR="):
                    p = line.split("=", 1)[1].strip().strip("\"'")
                    # Expand $HOME and ~ for shell-style values
                    p = os.path.expandvars(p)
                    break
    return Path(p).expanduser() if p else DEFAULT_MAILDIR


def is_unseen(maildir_file):
    """Maildir flag 'S' = Seen. Files without S are new/unread."""
    name = maildir_file.name
    if ":2," not in name:
        return True
    flags = name.split(":2,")[1]
    return "S" not in flags


def mark_seen(maildir_file):
    """Add 'S' flag to maildir filename, preserving any others."""
    name = maildir_file.name
    if ":2," in name:
        base, flags = name.rsplit(":2,", 1)
        if "S" in flags:
            return
        new_flags = "".join(sorted(set(flags + "S")))
        new_name = f"{base}:2,{new_flags}"
    else:
        new_name = f"{name}:2,S"
    maildir_file.rename(maildir_file.parent / new_name)


def strip_html(html):
    """Crude HTML → text fallback when no plain part is present."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


LONG_URL_THRESHOLD = 80


def clean_text(s):
    """Normalize email body text for kb inbox display.

    Pipeline:
    - CRLF → LF
    - strip trailing whitespace per line (kills email's 76-char wrap artifacts)
    - collapse 3+ blank lines to 2
    - unwrap <https://...> autolinks, inserting spaces if smushed against text
    - footnote any URL > 80 chars: replace inline with [link N], append at end
    """
    s = s.replace("\r\n", "\n").replace("\r", "")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)

    # Unwrap <URL> autolinks (Gmail-style), inserting spaces when smushed
    s = re.sub(r"(?<=\S)<(https?://[^\s>]+)>(?=\S)", r" \1 ", s)
    s = re.sub(r"<(https?://[^\s>]+)>(?=\S)", r"\1 ", s)
    s = re.sub(r"(?<=\S)<(https?://[^\s>]+)>", r" \1", s)
    s = re.sub(r"<(https?://[^\s>]+)>", r"\1", s)

    # Footnote long URLs to keep the body readable
    long_urls = []

    def shorten(m):
        url = m.group(0).rstrip(".,;:)]")
        trailing = m.group(0)[len(url):]
        if len(url) <= LONG_URL_THRESHOLD:
            return m.group(0)
        long_urls.append(url)
        return f"[link {len(long_urls)}]" + trailing

    s = re.sub(r"https?://[^\s<>\[\]()]+", shorten, s)

    if long_urls:
        s = s.rstrip() + "\n\n---\n"
        for i, url in enumerate(long_urls, 1):
            s += f"[link {i}]: {url}\n"

    return s.strip()


def get_text_body(msg):
    """Extract a clean text body, preferring text/plain over stripped HTML."""

    def _decode(part):
        try:
            return part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            return payload.decode("utf-8", errors="replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                return clean_text(_decode(part))
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return clean_text(strip_html(_decode(part)))
        return ""
    return clean_text(_decode(msg))


def safe_quote(s):
    """Quote a string for YAML frontmatter, collapsing newlines."""
    cleaned = (s or "").replace('"', '\\"').replace("\n", " ").strip()
    return f'"{cleaned}"'


def message_to_inbox_md(msg, captured_at):
    from_ = msg.get("From", "").strip()
    subject = msg.get("Subject", "").strip()
    received_raw = msg.get("Date", "")
    try:
        received = parsedate_to_datetime(received_raw).isoformat() if received_raw else ""
    except Exception:
        received = received_raw

    body = get_text_body(msg)

    return (
        "---\n"
        f"captured: {captured_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "source: email\n"
        f"from: {safe_quote(from_)}\n"
        f"subject: {safe_quote(subject)}\n"
        f"received: {received}\n"
        "---\n\n"
        f"{body}\n"
    )


def unique_inbox_path(stamp):
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    base = INBOX_DIR / f"{stamp}-email.md"
    if not base.exists():
        return base
    i = 1
    while True:
        p = INBOX_DIR / f"{stamp}-email-{i}.md"
        if not p.exists():
            return p
        i += 1


def main():
    dry_run = "--dry-run" in sys.argv
    maildir = load_maildir()

    console.print()
    console.print(Rule(
        "[bold steel_blue1]  cl ingest[/bold steel_blue1]  [grey50]email → inbox[/grey50]",
        style="steel_blue1 dim",
    ))
    console.print()

    if not maildir.exists():
        console.print(f"  [red]maildir not found:[/red] {maildir}")
        console.print(f"  [grey50]set CL_INGEST_MAILDIR or place messages at the default path[/grey50]")
        sys.exit(1)

    candidates = []
    for sub in ("new", "cur"):
        d = maildir / sub
        if d.exists():
            candidates.extend(d.iterdir())

    unseen = [p for p in candidates if p.is_file() and is_unseen(p)]

    if not unseen:
        console.print(
            f"  [grey50]nothing to ingest[/grey50]  "
            f"({len(candidates)} total messages, all seen)"
        )
        console.print()
        return

    n_ok, n_err = 0, 0
    for f in unseen:
        try:
            with f.open("rb") as fp:
                msg = email.message_from_binary_file(fp, policy=email.policy.default)
            captured = datetime.now()
            md = message_to_inbox_md(msg, captured)
            stamp = captured.strftime("%Y-%m-%d-%H%M%S")
            out = unique_inbox_path(stamp)
            if dry_run:
                subject = msg.get("Subject", "<no subject>")[:60]
                console.print(f"  [yellow]dry-run[/yellow]  {subject}  →  {out.name}")
            else:
                out.write_text(md)
                mark_seen(f)
                subject = msg.get("Subject", "<no subject>")[:60]
                console.print(f"  [green]✓[/green]  {subject}  →  {out.name}")
            n_ok += 1
        except Exception as e:
            console.print(f"  [red]✗[/red]  {f.name}  {e}")
            n_err += 1

    console.print()
    if dry_run:
        console.print(f"  [grey70]dry run — {n_ok} would be ingested, {n_err} errors[/grey70]")
    else:
        console.print(f"  [grey70]ingested {n_ok}, errors {n_err}[/grey70]")
    console.print()


if __name__ == "__main__":
    main()
