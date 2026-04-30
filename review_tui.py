"""
review_tui.py — TUI version of the full review pipeline.

Sequences ideas → projects → notes TUI sessions, then pushes once.
Run via: lo review --tui  (or automatically on Termux)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import ideas_tui
import projects_tui
import notes_tui
from tui_common import git_push_kb, exit_to_launcher


def main():
    ideas_tui.run()
    projects_tui.run()
    notes_tui.run()
    git_push_kb()
    exit_to_launcher()


if __name__ == "__main__":
    main()
