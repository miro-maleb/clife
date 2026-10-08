"""paths.py — the per-tenant seams for clife, in one place.

Everything used to hardcode `~/kb` and a bare `gcalcli`. For a second tenant on
the same machine (a different `~/kb`, a different Google account) those become
env-driven — but the defaults are Miro's exact old values, so an un-env'd run is
byte-identical. Zero clife imports here, so anything can import it cycle-free.

  CLIFE_KB                 knowledge-base root        (default ~/kb)
  CLIFE_DATA_DIR           tower-local state (pool DB, lint report)  (default ~/.local/share/clife)
  CLIFE_GCALCLI_CONFIG     gcalcli --config-folder    (default: ambient config)
  CLIFE_GCAL_OAUTH         gcalcli's OAuth token pickle (default $XDG_DATA_HOME/gcalcli/oauth)
  CLIFE_DEFAULT_CALENDAR   default calendar for new blocks (default Miro-Personal)

Note: gcalcli's OAuth *token* lives in the XDG data dir, not --config-folder, so
a second tenant also needs its own XDG_DATA_HOME to isolate its Google login.
"""
import os
from pathlib import Path

KB = Path(os.environ.get("CLIFE_KB", str(Path.home() / "kb"))).expanduser()

# THE LAYOUT (2026-10-05 redesign — replaces the 2026-09-07 flat tag store).
# Folders say WHO a note is for and where it is in its life, never what it is:
#   writing/  the only part Miro browses: poems/ stories/ blog/ letters/ journal/,
#             plus dreams.md and ideas.md (append-only)
#   working/  the capture desk: every new capture lands here, one file each; the
#             router files it out or it stays; 60 days untouched -> archive/
#   threads/  written FOR CLAUDE to resume a conversation or project; INDEX.md first
#   archive/  finished or gone quiet; nothing is deleted
#   todo.md   the todo list: `## Sooner` / `## Later` checkbox lines (clife/todo.py)
# Tags are retired. Nothing composes these paths itself; import them.
WRITING = KB / "writing"
WORKING = KB / "working"
THREADS = KB / "threads"
ARCHIVE = KB / "archive"
TODO = KB / "todo.md"
RHYTHM = KB / "rhythm.md"                  # the shape of the week; never a checklist
PROJECTS = THREADS / "INDEX.md"            # project/idea status, in bands: Active · Ideas · Asleep · Reference
DREAMS = WRITING / "dreams.md"
IDEAS = WRITING / "ideas.md"
JOURNAL = WRITING / "journal"
NOTE_DIRS = (WRITING, WORKING, THREADS, ARCHIVE)   # everything a reader may scan

# Legacy name. Every pre-redesign module imports STORE as "where new notes go";
# pointing it at working/ keeps those writers correct until each is rewritten.
# Do NOT use it for new code, and do not scan it expecting the whole kb.
STORE = WORKING

# Tower-local state (not git-synced): the calendar-pool DB, the lint report, etc.
DATA_DIR = Path(os.environ.get("CLIFE_DATA_DIR", str(Path.home() / ".local" / "share" / "clife"))).expanduser()

GCALCLI_CONFIG = os.environ.get("CLIFE_GCALCLI_CONFIG")   # None → gcalcli's own default

# gcalcli's OAuth token lives in the XDG *data* dir, not --config-folder. `cl events`
# unpickles it to talk to the Calendar API directly (gcalcli can't edit by id).
GCAL_OAUTH = Path(os.environ.get(
    "CLIFE_GCAL_OAUTH",
    os.path.join(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")),
                 "gcalcli", "oauth"))).expanduser()

DEFAULT_CALENDAR = os.environ.get("CLIFE_DEFAULT_CALENDAR", "Miro-Personal")


def gcalcli(*args):
    """Build a gcalcli argv, injecting --config-folder for the active tenant so
    each tenant reads/writes its own Google account."""
    base = ["gcalcli"]
    if GCALCLI_CONFIG:
        base += ["--config-folder", GCALCLI_CONFIG]
    return base + list(args)
