# CLAUDE — clife code repo

You're working in `~/clife/` — the code for the CLIfe CLI tool.

**Companion design docs live in the kb at `~/kb/projects/infrastructure/clife/`.** That folder is now FLAT — `project.md`, `system-improvements.md`, `activity-log.md` (deprecated 2026-06-23; day logs live in `~/kb/log/`). The numbered sub-project folders described below were flattened in the 2026 cleanups and no longer exist; don't go looking for `04-inbox-triage/sub-project.md`. Code lives here; the *plan* lives in `project.md`.

## Things that drift if you don't watch them

When you change behavior of a `cl` subcommand, **four places** likely need to stay in sync:

1. The Python module (e.g. `inbox.py`)
2. `cl.py` — `COMMANDS` dict and `HELP` text
3. `completions/_cl` — zsh tab-completion
4. `~/kb/projects/infrastructure/clife/project.md` — if the change alters what the system IS, not just how a flag behaves

When you add a *new* subcommand, also touch:

5. `README.md` — the command table
6. `nvim/lua/clife/init.lua` — if it should have an `:Cl <name>` ex-command

Paths are the thing that actually drifts here, not docs. The inbox is a VIEW
(notes with empty `tags:`), not a folder, and daily notes live in the stream —
so any code that composes `~/kb/inbox/...`, `~/kb/daily/...` or
`~/kb/journal/...` is broken by construction. Call the doors instead:
`kb-inbox` to drop a capture, `new-daily-note` for today's note, `cl stream
set` to change a note's tags/status/when. Three components in this repo were
found composing retired paths on 2026-09-06 (`clife.nvim` capture and journal
openers, and `cl tags`' default scan root).

## What lives elsewhere

- User data → `~/kb/` (separate repo). Don't write kb files from here unless explicitly working on tooling.
- Secrets → `~/.config/life-os/secrets.env` (not tracked).
- venv → `~/clife/venv/` (gitignored).

## Install + reload

`./install.sh` is idempotent. Re-run it anytime — it skips done steps.

## Multi-machine sync

The user runs clife on multiple devices. Known peers (Tailscale MagicDNS):

- `miro-thinkpad` (100.123.213.85)
- `miro-terminal` (100.99.166.56)

**Whenever you make a change that affects daily-driver behavior — clife code, nvim config, i3 bindings, shell config, dotfiles** — proactively offer to propagate it to the other machine(s) over SSH. Don't wait to be asked.

What syncs how:

- **clife repo (`~/clife/`)** — public GitHub remote (`miro-maleb/clife`). Commit + push, then `git pull` on the peer.
- **i3 config (`~/.config/i3/config`)** — not in any repo. Patch in place over SSH (back up first: `cp config config.bak.$(date +%s)`).
- **nvim config (`~/.config/nvim/`)** — not in any repo. Same — patch in place over SSH.
- **`~/.zshrc` and other dotfiles** — not in any repo. Same.

Reload patterns:

- i3: SSH and `i3-msg reload`. The remote i3 socket is reachable if you grab `DISPLAY` + `XAUTHORITY` from `/proc/$(pgrep -u miro -x i3)/environ`. Skip reload if i3 isn't running there.
- nvim: no reload needed; new buffers pick up changes.
- zsh: tell the user to `exec zsh` or open a new shell.

Failure mode to watch: if a peer's file has diverged locally, a blind patch can clobber. Always grep first to confirm anchor lines exist and the change isn't already applied.

## Sub-project map (HISTORICAL — these folders are gone; kept as a map of intent)

| # | Folder | What |
|---|---|---|
| 01 | foundation | `cl` entrypoint, install.sh, basic plumbing |
| 02 | schema-and-migration | area / project / sub-project schema |
| 03 | capture-ingestion | `cl ingest` + email-bus setup |
| 04 | inbox-triage | `cl inbox` |
| 05 | new-command | `cl new` |
| 06 | review-system | `cl projects`, `cl review`, `cl week`, archive flow |
| 07 | notes-zettelkasten | `cl notes` (pass 1 = browser; pass 2 = AI organizer, deferred) |
| 08 | desktop-dashboard | `cl dashboard` — tmux tree/shell/agenda layout |
| 09 | mobile-capture | MOBILE.md (MVP shipped); native Android app deferred |
| 10 | user-manual | end-state docs (pending) |
| 11 | editor-integration | clife.nvim |

When the user asks about something in this list, the matching sub-project folder is where the design history and open questions live.
