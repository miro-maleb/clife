import re
import subprocess
from datetime import datetime
from pathlib import Path

from paths import KB, STORE

# THE syncer. Not a second implementation of "stage, commit, pull, push" —
# `kb-autosync` is the one the systemd timer runs, it holds an flock so two
# syncs cannot interleave, it aborts a conflicted rebase instead of leaving it
# stopped, and it refuses to run at all on a detached HEAD or a rebase in
# progress.
#
# WHY THIS REPLACED THE INLINE VERSION. clife used to do the four git calls
# itself with check=False on every one. On 2026-07-20 the phone's `pull
# --rebase` conflicted and stopped mid-rebase (which IS a detached HEAD), the
# push failed, both return codes were discarded, and the next run's `add -A`
# staged the conflict markers the stopped rebase had written and committed
# them. Six weeks of captures never left the phone and nothing anywhere said a
# word. A sync that cannot report failure is not a sync.
KB_AUTOSYNC = Path.home() / "bin" / "kb-autosync"


def sync_kb(timeout: int = 120) -> tuple[bool, str]:
    """Sync ~/kb. Returns (ok, message) — and the caller MUST show a failure.

    Note a skip is a success: if the timer already holds the lock, kb-autosync
    exits 0 having done nothing, and the timer will carry the change.
    """
    if not KB_AUTOSYNC.exists():
        return False, f"kb-autosync not found at {KB_AUTOSYNC} — kb NOT synced"
    try:
        r = subprocess.run([str(KB_AUTOSYNC)], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"kb-autosync timed out after {timeout}s — kb NOT synced"
    except OSError as e:
        return False, f"kb-autosync could not run ({e}) — kb NOT synced"
    if r.returncode == 0:
        return True, "Synced."
    return False, (r.stderr.strip() or r.stdout.strip()
                   or f"kb-autosync exited {r.returncode}")

# The per-day work log. NOT kb/journal/ — that is reserved for the handwritten
# journal (OCR pipeline); writing dailies there would collide with it.
_journal_dir = STORE


def capture_payload(file_path):
    """Return the routable text from an inbox capture file.

    Email captures (frontmatter `source: email`) → the subject line.
    Everything else → the body, stripped. Returns "" if nothing usable.
    """
    text = Path(file_path).read_text()
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
            None,
        )
        if end is not None:
            body = "\n".join(lines[end + 1:]).strip()
            meta = {}
            for line in lines[1:end]:
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                val = val.strip()
                if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1].replace('\\"', '"')
                meta[key.strip()] = val
            if meta.get("source") == "email":
                return meta.get("subject", "").strip() or body
            return body
    return text.strip()


def today_journal():
    return _journal_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def get_journal_path(date=None):
    d = date if date is not None else datetime.now()
    return _journal_dir / d.strftime("%Y-%m-%d.md")


def insert_journal_bullet(text, journal=None):
    """Insert *text* as a bullet into the ## Log section of today's journal.

    Creates the journal file if it doesn't exist.  Falls back to appending at
    the end when there is no ## Log section.
    """
    if journal is None:
        journal = today_journal()
    bullet = f"- {text}"

    if not journal.exists():
        journal.write_text(bullet + "\n")
        return

    lines = journal.read_text().splitlines()
    section_start = None
    insert_at = None
    for i, line in enumerate(lines):
        if re.match(r'^## Log\s*$', line):
            section_start = i
        elif section_start is not None and re.match(r'^## ', line):
            insert_at = i
            break

    if section_start is None:
        lines.append(bullet)
    elif insert_at is not None:
        lines.insert(insert_at, "")
        lines.insert(insert_at, bullet)
    else:
        lines.append(bullet)

    journal.write_text("\n".join(lines) + "\n")
