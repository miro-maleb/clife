"""The priority lane: background model work waits while Miro is dictating.

Dictation gets the model first (phone-flow,
2026-10-16). The tower's dictate service keeps $XDG_RUNTIME_DIR/dictating fresh
(touched every second of audio, and once more when a dictation ends). A
background job calls wait() before it asks the resident model for anything; it
returns at once unless dictation is live, then waits until it has been quiet
for QUIET_S, up to MAX_S, and goes ahead anyway — background jobs are small and
can be late, but never stuck.

Measured 2026-10-16: the clean-up of a dictated paragraph takes 0.46-0.7 s
alone, but 10.8 s if three long background prompts start reading at the same
moment. Jobs already running aren't stopped; mid-answer they cost ~0.5 s.
"""
import os
import time
from pathlib import Path

LEASE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "dictating"
QUIET_S = 8
MAX_S = 120


def dictating():
    try:
        return time.time() - LEASE.stat().st_mtime < QUIET_S
    except OSError:
        return False


def wait(max_s=MAX_S):
    """Block while dictation is live. Returns the seconds waited."""
    t0 = time.time()
    while dictating() and time.time() - t0 < max_s:
        time.sleep(0.5)
    return time.time() - t0
