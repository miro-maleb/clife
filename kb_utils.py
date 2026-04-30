import re
from datetime import datetime
from pathlib import Path

_journal_dir = Path.home() / "kb" / "journal"


def today_journal():
    return _journal_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def get_journal_path(date=None):
    d = date if date is not None else datetime.now()
    return _journal_dir / d.strftime("%Y-%m-%d.md")


def insert_journal_bullet(text, journal=None):
    """Insert *text* as a bullet into the ## Journal section of today's journal.

    Creates the journal file if it doesn't exist.  Falls back to appending at
    the end when there is no ## Journal section.
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
        if re.match(r'^## Journal\s*$', line):
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
