import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

TERMUX = "com.termux" in os.environ.get("PREFIX", "")
TUI_COMMANDS = {"inbox", "notes", "projects", "capture", "review"}

COMMANDS = {
    "capture":     "capture",
    "ingest":      "ingest",
    "week":        "week",
    "inbox":       "inbox",
    "projects":    "projects",
    "notes":       "notes",
    "review":      "review",
    "tree":        "tree",
    "new":         "new",
    "new-project": "new_project",  # back-compat alias for muscle memory
    "dashboard":   "dashboard_tui",
}

HELP = """
  cl — CLIfe

  cl capture                  quick text capture — one line per item → inbox
  cl capture --voice          voice capture — say 'break' between items, Ctrl+C to finish
  cl capture --journal        capture directly to today's journal (text or voice)
  cl ingest [--dry-run]       pull new email from kb-capture maildir → inbox
  cl inbox [--tui]            route inbox files — j/n/t/p/v/g/h/s/d
  cl notes                    flat fzf browser over every note
  cl notes --orphans          only orphan notes (no project home, no inbound link)
  cl notes --stale            only notes unmodified > 90 days
  cl notes --area NAME        filter to one area
  cl notes --project NAME     filter to one project
  cl notes --tag NAME         filter to one tag
  cl projects [--tui]         project review — active + on-hold (default)
  cl projects --sleeping      show sleeping projects
  cl projects --active|--on-hold|--complete|--abandoned|--all
  cl review                   full review pipeline — projects (incl. sleeping) → notes
  cl tree [--full] [--active] bird's-eye view: areas → projects [→ sub-projects]
  cl week                     weekly planner
  cl new                      scaffold area / project / sub-project (interactive or with flags)
  cl new --area NAME          create a new area
  cl new --project NAME       create a new project (fzf prompts for area)
  cl new --project NAME --in AREA
  cl new --sub-project NAME [--in PROJECT]
  cl new-project              alias for `cl new --project` (back-compat)
  cl dashboard                persistent dashboard — journal, calendar, projects, capture

  On Termux, inbox/notes/projects automatically use the TUI.
"""

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(HELP)
        return

    cmd = sys.argv[1]

    if cmd not in COMMANDS:
        print(f"  unknown command: {cmd}\n  run 'cl --help' for available commands")
        sys.exit(1)

    if cmd == "dashboard":
        module = __import__("dashboard_tui")
        module.main()
        return

    use_tui = "--tui" in sys.argv or (TERMUX and cmd in TUI_COMMANDS)
    if cmd == "capture" and any(f in sys.argv for f in ("--journal", "-j")):
        use_tui = False

    if use_tui and cmd in TUI_COMMANDS:
        sys.argv = [a for a in sys.argv if a != "--tui"]
        module = __import__(f"{COMMANDS[cmd]}_tui")
        module.main()
        return

    sys.argv = sys.argv[1:]
    module = __import__(COMMANDS[cmd])
    module.main()

if __name__ == "__main__":
    main()
