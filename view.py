#!/usr/bin/env python3
"""cl view — open an HTML view of kb/ in a native window.

  cl view                 open the default view (projects)
  cl view projects        open the projects view
  cl view --list          list available views

Views live in views/<name>/ (scan.py + view.html). The GTK/pywebview window
runs under the system python (which has pywebview installed), so this module
just resolves the view and hands off to views/_launcher.py via subprocess.
"""
import os
import subprocess
import sys

VIEWS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "views")
LAUNCHER = os.path.join(VIEWS_DIR, "_launcher.py")
SYSTEM_PYTHON = "/usr/bin/python3"  # has pywebview + GTK; the venv does not
DEFAULT_VIEW = "projects"


def available_views():
    if not os.path.isdir(VIEWS_DIR):
        return []
    views = []
    for entry in sorted(os.listdir(VIEWS_DIR)):
        if entry.startswith("_"):
            continue
        view_dir = os.path.join(VIEWS_DIR, entry)
        if os.path.isfile(os.path.join(view_dir, "view.html")):
            views.append(entry)
    return views


def main():
    args = [a for a in sys.argv[1:] if a != "view"]  # tolerate stray dispatch arg

    if "--list" in args or "-l" in args:
        views = available_views()
        print("  available views:" if views else "  no views found")
        for v in views:
            print(f"    {v}")
        return

    target = next((a for a in args if not a.startswith("-")), DEFAULT_VIEW)
    views = available_views()
    if target not in views:
        print(f"  unknown view: {target}")
        print(f"  available: {', '.join(views) or '(none)'}")
        sys.exit(1)

    python = SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else "python3"
    sys.exit(subprocess.call([python, LAUNCHER, target]))


if __name__ == "__main__":
    main()
