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
