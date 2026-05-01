# CLAUDE — clife code repo

You're working in `~/clife/` — the code for the CLIfe CLI tool.

**Companion design docs live in the kb at `~/kb/projects/infrastructure/clife/`.** Each sub-project has its own folder (`01-foundation/sub-project.md`, etc.) with goal, status, tasks, open questions. Those docs are the source of truth for what's shipped vs pending. Code lives here; the *plan* lives in kb.

## Things that drift if you don't watch them

When you change behavior of a `cl` subcommand, **four places** likely need to stay in sync:

1. The Python module (e.g. `inbox.py`)
2. `cl.py` — `COMMANDS` dict and `HELP` text
3. `completions/_cl` — zsh tab-completion
4. The relevant sub-project doc in kb (`~/kb/projects/infrastructure/clife/<NN>-<name>/sub-project.md`) — update Tasks list, mark items complete

When you add a *new* subcommand, also touch:

5. `README.md` — the command table
6. `nvim/lua/clife/init.lua` — if it should have an `:Cl <name>` ex-command

When a sub-project finishes a task, mark it complete in its own doc AND check whether the parent `project.md` table needs a status bump.

## What lives elsewhere

- User data → `~/kb/` (separate repo). Don't write kb files from here unless explicitly working on tooling.
- Secrets → `~/.config/life-os/secrets.env` (not tracked).
- venv → `~/clife/venv/` (gitignored).

## Install + reload

`./install.sh` is idempotent. Re-run it anytime — it skips done steps.

## Sub-project map

| # | Folder | What |
|---|---|---|
| 01 | foundation | `cl` entrypoint, install.sh, basic plumbing |
| 02 | schema-and-migration | area / project / sub-project schema |
| 03 | capture-ingestion | `cl ingest` + email-bus setup |
| 04 | inbox-triage | `cl inbox` |
| 05 | new-command | `cl new` |
| 06 | review-system | `cl projects`, `cl review`, `cl week`, archive flow |
| 07 | notes-zettelkasten | `cl notes` (pass 1 = browser; pass 2 = AI organizer, deferred) |
| 08 | desktop-dashboard | `cl dashboard` (paused) |
| 09 | mobile-capture | MOBILE.md (MVP shipped); native Android app deferred |
| 10 | user-manual | end-state docs (pending) |
| 11 | editor-integration | clife.nvim |

When the user asks about something in this list, the matching sub-project folder is where the design history and open questions live.
