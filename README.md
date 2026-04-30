# CLIfe

A personal CLI operating system. Plain markdown, git-backed, terminal-native.

The system you inhabit, not an app you open. Would have worked in 1995, can also be wired to AI.

## Install

```bash
git clone https://github.com/miro-maleb/clife.git ~/clife
cd ~/clife
./install.sh
```

This creates a venv, installs Python dependencies, and adds `~/clife` to your PATH.

After install, you'll still need:

- **`~/kb/`** — the knowledge base this tool operates on. Either clone your existing kb repo, or `mkdir ~/kb && git init`.
- **GROQ_API_KEY** — for voice transcription. Save to `~/.config/life-os/secrets.env`:
  ```
  GROQ_API_KEY=your-key-here
  ```
- **External tools** — `fzf`, `gcalcli` (auto-installed via pip), `nvim`, optional `whisper.cpp` for offline transcription. Install via your package manager.

## Usage

```bash
cl --help
```

## Design

CLIfe is the successor to `lo` (life-os). Same philosophy, leaner surface area, codified hierarchy.

The full design lives in the kb at `~/kb/projects/clife/project.md`, with sub-projects scoped under `~/kb/projects/clife/01-foundation/` etc.

## Decision filter

In order:

1. Can a script do it deterministically? → Script it.
2. Does it need a decision? → User makes it.
3. Does it need genuine synthesis or thinking? → AI.

Claude is the exception handler, not the runtime.
