# clife.nvim

Neovim integration for [CLIfe](https://github.com/miro-maleb/clife). Lives in the clife repo for now; may promote to its own repo later.

## Install

Add to your `init.lua`:

```lua
vim.opt.rtp:prepend("/home/miro/clife/nvim")
require("clife").setup()
```

(`install.sh` will offer to add this for you.)

## Commands

All commands are namespaced under `:Cl`:

| Command | Default keymap | What it does |
|---|---|---|
| `:Cl capture` | `<leader>cc` | popup, type to capture, lands in `~/kb/inbox/` |
| `:Cl capture-selection` | `<leader>cv` (visual) | selected text → inbox, with `[from path:line]` reference |
| `:Cl new-note` | `<leader>cn` | new note inside the current project's `notes/` folder (auto-detects project from buffer; bootstraps `notes/` if needed) |
| `:Cl new-project` | `<leader>cp` | run `cl new --project` interactively |
| `:Cl new-sub-project` | `<leader>cs` | new sub-project inside the project of the current buffer |
| `:Cl inbox` | `<leader>ci` | open `cl inbox` in a terminal split |
| `:Cl projects` | `<leader>cP` | Telescope picker over project.md files |
| `:Cl notes` | `<leader>cN` | Telescope picker over notes |
| `:Cl week` | `<leader>cw` | open this week's plan |
| `:Cl journal` | `<leader>cj` | today's journal |
| `:Cl review` | `<leader>cr` | run `cl review` in a terminal split |
| `:Cl template` | `<leader>t` | Telescope picker over `~/kb/templates/` → insert chosen template at cursor with `{{day}}/{{month}}/{{date}}/{{year}}/{{today}}/{{title}}` substituted |

## Override / disable keymaps

```lua
require("clife").setup({
  keymaps = {
    capture = "<leader>q",   -- override
    journal = false,          -- disable (you have your own <leader>j)
  },
})
```

## Design notes

- **Pickers via Telescope** (projects, notes) — fast, native nvim feel.
- **Interactive flows shell out** (review, inbox triage, week, new-project) — opens `cl <subcommand>` in a terminal split. Reuses the polished CLI experience instead of duplicating logic.
- **In-process for the small stuff** (capture, new-note) — direct file write, fast feedback, no shell roundtrip.
