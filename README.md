# CLIfe

A personal CLI operating system. Plain markdown, git-backed, terminal-native.

The system you inhabit, not an app you open. Would have worked in 1995, can also be wired to AI.

## What it is

CLIfe is a thin Python toolset over a markdown knowledge base at `~/kb/`. It captures voice notes, ingests email, routes inbox items into projects/notes/calendar, and surfaces project review.

Commands:

| | |
|---|---|
| `cl capture` | text or voice → inbox |
| `cl ingest` | pull email from kb-capture maildir → inbox |
| `cl inbox` | route inbox items with hotkeys |
| `cl projects` | active project pulse |
| `cl review` | full review |
| `cl week` | weekly planner |
| `cl notes` | notes browser |
| `cl new-project` | scaffold a new project |
| `cl dashboard` | persistent dashboard TUI |

## Architecture

```
sources (voice / email / SMS via share-to-email / etc.)
  ↓
~/kb/inbox/ (one markdown file per capture)
  ↓
cl inbox  →  routes to journal / task / project / calendar / grocery
  ↓
~/kb/projects/<area>/<project>/  (area → project → sub-project hierarchy)
```

Email goes through `mbsync → ~/mail/kb-capture/ → cl ingest → ~/kb/inbox/`. The maildir is upstream pipeline data; only the processed markdown lives in kb.

Design docs live in the kb itself at `~/kb/projects/infrastructure/clife/` — top-level `project.md` + 10 sub-projects.

## Install on a new machine

```bash
git clone https://github.com/miro-maleb/clife.git ~/clife
cd ~/clife
./install.sh
```

`install.sh` is idempotent. It will:

- Create a Python venv at `~/clife/venv` and install requirements
- Add `~/clife` to your `$PATH` in `.zshrc` / `.bashrc`
- Wire up zsh tab-completion in `~/.zsh/completions/`
- Check for required system packages (`mbsync`, `fzf`, `nvim`) and tell you how to install missing ones for your distro
- Seed `~/.config/life-os/secrets.env` and `~/.mbsyncrc` from templates if they don't exist

After it runs, the printed "next steps" are:

1. **Reload your shell.**

2. **Fill in `~/.config/life-os/secrets.env`.** This file holds your Groq key (for voice transcription) and the maildir path. The other API key vars are optional.

3. **Configure mbsync** (only if you want email capture):

   - Create or pick a dedicated Gmail account (e.g. `<you>.kb@gmail.com`). Don't publish this address.
   - Enable 2FA, generate an app password at https://myaccount.google.com/apppasswords
   - Edit `~/.mbsyncrc` — replace `<YOUR-CAPTURE-ADDRESS>` with your address
   - Save the app password (no spaces stripped — paste it as Gmail shows it):
     ```bash
     echo -n 'xxxx xxxx xxxx xxxx' > ~/.config/mbsync/kb-capture-password
     chmod 600 ~/.config/mbsync/kb-capture-password
     ```
   - First sync: `mbsync kb-capture`

4. **Clone `~/kb/`** if not already present. Or `mkdir ~/kb && git init` for a fresh start.

5. **Test:** `cl --help`, `cl <tab>`, and (if email is configured) `mbsync kb-capture && cl ingest`.

6. **Optional one-time:** `gcalcli init` to authorize Google Calendar (used by `cl week`, `cl dashboard`).

## Decision filter

In order:

1. Can a script do it deterministically? → Script it.
2. Does it need a decision? → User makes it.
3. Does it need genuine synthesis or thinking? → AI.

Claude is the exception handler, not the runtime.

## Repo layout

```
~/clife/
├── cl                  # bash entrypoint (resolves its own dir → execs venv python)
├── cl.py               # Python entrypoint (dispatches to subcommands)
├── capture.py          # text + voice capture
├── ingest.py           # email maildir → inbox markdown
├── inbox.py / .._tui   # inbox triage
├── projects.py / ..    # project review
├── notes.py / ..       # notes browser
├── review.py / ..      # full review pipeline
├── week.py             # weekly planner
├── new_project.py      # scaffold a project
├── dashboard_tui.py    # persistent dashboard TUI
├── kb_utils.py         # shared journal/path helpers
├── tui_common.py       # shared TUI widgets
├── completions/_cl     # zsh tab-completion
├── templates/          # config seeds for new machines
├── tools/              # one-off utilities (kb_audit.py, kb_migrate.py)
├── install.sh
└── requirements.txt
```

## Predecessor

Successor to [`lo`](https://github.com/miro-maleb/kb/tree/main/projects/infrastructure/life-os) (life-os). Same philosophy, leaner surface area, codified area→project→sub-project hierarchy. See `~/kb/projects/infrastructure/clife/project.md` for the full design.
