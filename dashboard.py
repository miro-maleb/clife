"""
dashboard.py — `cl dashboard` — primary always-on desktop surface.

A tmux session named `dashboard` with a 30/40/30 horizontal layout:
  - Left   (30%): `cl tree --pane`   — Textual TUI, clickable project tree
  - Center (40%): a shell — run nvim, claude, whatever
  - Right  (30%): `cl agenda --pane`  — interactive agenda; `o`/Enter on a
                                        block dispatches `nvim <file>` into
                                        the center pane

`cl dashboard` always rebuilds the session from scratch. If one already
exists it's killed first, so a quit-and-relaunch always gets you back to
the clean three-pane layout. The one exception: running from inside the
dashboard session would kill the script mid-flight, so we detect that and
ask the user to detach first.

Pane IDs (not indexes) are captured at creation and stashed as session
env vars (`CLIFE_LEFT_PANE`, `CLIFE_CENTER_PANE`, `CLIFE_RIGHT_PANE`) so
cross-pane targeting (e.g. tree_pane.py dispatching `cl show <path>` into
the center) survives different user pane-base-index settings.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from tui_common import ACCENT, BORDER

SESSION = "dashboard"
CL = str(Path(__file__).parent / "cl")

LEFT_CMD  = f"{CL} tree --pane --active"
RIGHT_CMD = f"{CL} agenda --pane"


def session_exists() -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", SESSION],
        capture_output=True,
    ).returncode == 0


def inside_dashboard() -> bool:
    """True if the current shell is attached to the dashboard tmux session.
    Used to bail out before kill-session would terminate us mid-rebuild."""
    if "TMUX" not in os.environ:
        return False
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == SESSION


def _new_pane(*tmux_args: str) -> str:
    """Run a tmux command that creates a pane; return its pane_id (e.g. %3)."""
    result = subprocess.run(
        ["tmux", *tmux_args, "-P", "-F", "#{pane_id}"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _set_env(name: str, value: str) -> None:
    subprocess.run(
        ["tmux", "set-environment", "-t", SESSION, name, value],
        check=False,
    )


def create_session():
    pane_left = _new_pane(
        "new-session", "-d", "-s", SESSION, "-n", "main",
    )

    # Split horizontally: left keeps 30%, new pane gets 70% (will be split again)
    pane_center = _new_pane(
        "split-window", "-h", "-l", "70%", "-t", pane_left,
    )

    # Split the right 70%: center keeps 57% (= 40% of total),
    # new pane gets 43% (= 30% of total) on the far right.
    pane_right = _new_pane(
        "split-window", "-h", "-l", "43%", "-t", pane_center,
    )

    _set_env("CLIFE_LEFT_PANE",   pane_left)
    _set_env("CLIFE_CENTER_PANE", pane_center)
    _set_env("CLIFE_RIGHT_PANE",  pane_right)

    # Replace the default shells in the side panes with the TUIs.
    # respawn-pane is reliable; send-keys races with the new shell's startup
    # and the first few characters routinely get eaten by the void.
    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", pane_left, LEFT_CMD],
        check=True,
    )

    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", pane_right, RIGHT_CMD],
        check=True,
    )

    # Focus the center pane — where work happens
    subprocess.run(
        ["tmux", "select-pane", "-t", pane_center],
        check=True,
    )

    apply_chrome()


def apply_chrome():
    """Session-scoped tmux styling. Thin pane borders, warm amber active pane,
    dim borders elsewhere, no status bar. Doesn't touch global tmux."""
    window = f"{SESSION}:main"
    options = [
        (["-w", "-t", window],  "pane-border-lines",        "single"),
        (["-w", "-t", window],  "pane-border-style",        f"fg={BORDER}"),
        (["-w", "-t", window],  "pane-active-border-style", f"fg={ACCENT}"),
        (["-t", SESSION],       "status",                   "off"),
    ]
    for flags, opt, val in options:
        subprocess.run(
            ["tmux", "set-option", *flags, opt, val],
            check=False,
        )


def attach():
    if "TMUX" in os.environ:
        subprocess.run(["tmux", "switch-client", "-t", SESSION], check=True)
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", SESSION])


def main():
    if not shutil.which("tmux"):
        print("error: tmux not found in PATH", file=sys.stderr)
        sys.exit(1)
    if session_exists():
        if inside_dashboard():
            print(
                "already inside the dashboard session; detach first "
                "(Prefix+d) and re-run `cl dashboard` for a fresh rebuild.",
                file=sys.stderr,
            )
            sys.exit(1)
        subprocess.run(["tmux", "kill-session", "-t", SESSION], check=False)
    create_session()
    attach()
