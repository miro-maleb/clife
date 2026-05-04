import re
from datetime import datetime
from pathlib import Path

_journal_dir = Path.home() / "kb" / "journal"


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
