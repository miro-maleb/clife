import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

TERMUX = "com.termux" in os.environ.get("PREFIX", "")
TUI_COMMANDS = {"inbox", "notes", "projects", "capture", "review"}

COMMANDS = {
    "capture":     "capture",
    "week":        "week",
    "inbox":       "inbox",
    "projects":    "projects",
    "notes":       "notes",
    "review":      "review",
    "new-project": "new_project",
    "dashboard":   "dashboard_tui",
}

HELP = """
  cl — CLIfe

  cl capture                  quick text capture — one line per item → inbox
  cl capture --voice          voice capture — say 'break' between items, Ctrl+C to finish
  cl capture --journal        capture directly to today's journal (text or voice)
  cl inbox [--tui]            route inbox files — j/n/t/p/v/g/h/s/d
  cl notes [--tui]            notes browser — by area, project, tag, orphans
  cl projects [--tui]         project review — active + on-hold (default)
  cl projects --sleeping      show sleeping projects
  cl projects --active|--on-hold|--complete|--abandoned|--all
  cl review                   full review pipeline — projects (incl. sleeping) → notes
  cl week                     weekly planner
  cl new-project              create a new project directly (skip inbox)
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
