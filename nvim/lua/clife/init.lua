-- clife.nvim — neovim integration for CLIfe
--
-- Activate from your init.lua:
--   vim.opt.rtp:prepend("/home/miro/clife/nvim")
--   require("clife").setup()       -- defaults
--
-- Override keymaps:
--   require("clife").setup({ keymaps = { capture = "<leader>q" } })
--
-- Disable a keymap with `false`:
--   require("clife").setup({ keymaps = { journal = false } })

local M = {}

local KB = vim.fn.expand("~/kb")
local PROJECTS = KB .. "/projects"
local INBOX = KB .. "/inbox"
local TEMPLATES = KB .. "/templates"
local CL = vim.fn.expand("~/clife/cl")

local default_keymaps = {
  capture           = "<leader>cc",
  capture_selection = "<leader>cv", -- visual mode
  new_note          = "<leader>cn",
  new_project       = "<leader>cp",
  new_sub_project   = "<leader>cs",
  inbox             = "<leader>ci",
  projects          = "<leader>cP",
  notes             = "<leader>cN",
  week              = "<leader>cw",
  journal           = "<leader>cj",
  review            = "<leader>cr",
  template          = "<leader>t",
}

-- ------------------------------------------------------------------
-- Helpers
-- ------------------------------------------------------------------

-- Walk up from `path` until we find a directory containing project.md.
-- Returns the absolute project dir path or nil.
local function find_project_dir(path)
  if not path or path == "" then return nil end
  local dir = vim.fn.fnamemodify(path, ":p:h")
  while dir ~= "/" and dir ~= "" do
    if vim.fn.filereadable(dir .. "/project.md") == 1 then
      return dir
    end
    local parent = vim.fn.fnamemodify(dir, ":h")
    if parent == dir then break end
    dir = parent
  end
  return nil
end

local function current_project_dir()
  return find_project_dir(vim.api.nvim_buf_get_name(0))
end

local function timestamp()
  return os.date("%Y-%m-%d-%H%M%S")
end

local function write_inbox(text)
  vim.fn.mkdir(INBOX, "p")
  local path = INBOX .. "/" .. timestamp() .. ".md"
  vim.fn.writefile(vim.split(text, "\n", { plain = true }), path)
  return path
end

local function get_visual_selection()
  -- Yank visual selection to register z, restore prior contents after read.
  local prev = vim.fn.getreg("z")
  vim.cmd('normal! "zy')
  local sel = vim.fn.getreg("z")
  vim.fn.setreg("z", prev)
  return sel
end

local function term_run(cmd, opts)
  opts = opts or {}
  vim.cmd((opts.split or "split") .. " | resize 18")
  vim.cmd("terminal " .. cmd)
  vim.cmd("startinsert")
end

local function notify(msg, level)
  vim.notify("[clife] " .. msg, level or vim.log.levels.INFO)
end

local function slugify(name)
  local s = name:lower()
  s = s:gsub("[%s%+]+", "-")
  s = s:gsub("[^%w%-]", "")
  s = s:gsub("%-+", "-")
  s = s:gsub("^%-+", ""):gsub("%-+$", "")
  return s
end

-- ------------------------------------------------------------------
-- Capture
-- ------------------------------------------------------------------

function M.capture()
  vim.ui.input({ prompt = "capture: " }, function(input)
    if not input or input == "" then return end
    local path = write_inbox(input)
    notify("→ " .. vim.fn.fnamemodify(path, ":t"))
  end)
end

function M.capture_selection()
  local sel = get_visual_selection()
  if not sel or sel == "" then
    notify("nothing selected", vim.log.levels.WARN)
    return
  end
  local buf_path = vim.api.nvim_buf_get_name(0)
  local rel = buf_path
  if buf_path:find(KB, 1, true) == 1 then
    rel = buf_path:sub(#KB + 2)
  end
  local line = vim.fn.line(".")
  local body = string.format("[from %s:%d]\n\n%s", rel, line, sel)
  local path = write_inbox(body)
  notify("→ " .. vim.fn.fnamemodify(path, ":t"))
end

-- ------------------------------------------------------------------
-- Notes & projects (creation)
-- ------------------------------------------------------------------

function M.new_note()
  local project = current_project_dir()
  if not project then
    notify("not inside a project (no parent project.md found)", vim.log.levels.WARN)
    return
  end
  vim.ui.input({ prompt = "note name: " }, function(input)
    if not input or input == "" then return end
    local slug = slugify(input)
    if slug == "" then
      notify("invalid name", vim.log.levels.WARN)
      return
    end
    local notes_dir = project .. "/notes"
    vim.fn.mkdir(notes_dir, "p") -- bootstraps notes/ if missing
    local path = notes_dir .. "/" .. slug .. ".md"
    if vim.fn.filereadable(path) == 1 then
      notify("already exists: " .. path, vim.log.levels.WARN)
      vim.cmd("edit " .. vim.fn.fnameescape(path))
      return
    end
    local today = os.date("%Y-%m-%d")
    local title = input
    local lines = {
      "---",
      "type: note",
      "created: " .. today,
      "tags: []",
      "---",
      "",
      "# " .. title,
      "",
      "",
    }
    vim.fn.writefile(lines, path)
    vim.cmd("edit " .. vim.fn.fnameescape(path))
    -- Position cursor on the empty line below the title
    vim.api.nvim_win_set_cursor(0, { #lines, 0 })
  end)
end

function M.new_project()
  -- Shell-out to `cl new --project` for the full interactive flow
  term_run(CL .. " new --project")
end

function M.new_sub_project()
  -- Only meaningful when current buffer is a project.md
  local buf_path = vim.api.nvim_buf_get_name(0)
  if vim.fn.fnamemodify(buf_path, ":t") ~= "project.md" then
    -- Fall back to interactive picker
    term_run(CL .. " new --sub-project")
    return
  end
  local project_name = vim.fn.fnamemodify(vim.fn.fnamemodify(buf_path, ":h"), ":t")
  vim.ui.input({ prompt = "sub-project name: " }, function(input)
    if not input or input == "" then return end
    -- Run cl new --sub-project NAME --in PROJECT, non-interactive
    local cmd = string.format(
      "%s new --sub-project %q --in %q",
      CL, input, project_name
    )
    term_run(cmd)
  end)
end

-- ------------------------------------------------------------------
-- Shell-out flows (interactive)
-- ------------------------------------------------------------------

function M.inbox()    term_run(CL .. " inbox")    end
function M.review()   term_run(CL .. " review")   end
function M.week()     term_run(CL .. " week")     end

-- ------------------------------------------------------------------
-- Pickers (Telescope)
-- ------------------------------------------------------------------

local function telescope_or_warn()
  local ok, _ = pcall(require, "telescope")
  if not ok then
    notify("telescope not installed; falling back to find-files", vim.log.levels.WARN)
    return false
  end
  return true
end

function M.projects()
  if not telescope_or_warn() then
    vim.cmd("edit " .. PROJECTS)
    return
  end
  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")

  local results = vim.fn.systemlist("find " .. PROJECTS .. " -name project.md -type f -not -path '*/archive/*'")

  pickers.new({}, {
    prompt_title = "clife projects",
    finder = finders.new_table({
      results = results,
      entry_maker = function(entry)
        local rel = entry:gsub(PROJECTS .. "/", ""):gsub("/project.md", "")
        return { value = entry, display = rel, ordinal = rel }
      end,
    }),
    sorter = conf.generic_sorter({}),
    attach_mappings = function(prompt_bufnr, _)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local sel = action_state.get_selected_entry()
        if sel then vim.cmd("edit " .. vim.fn.fnameescape(sel.value)) end
      end)
      return true
    end,
  }):find()
end

function M.notes()
  if not telescope_or_warn() then
    term_run(CL .. " notes")
    return
  end
  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")

  -- find every notes/*.md plus top-level kb/notes/*.md
  local cmd = "(find " .. PROJECTS ..
              " -path '*/notes/*.md' -type f; " ..
              "find " .. KB .. "/notes -maxdepth 1 -name '*.md' -type f 2>/dev/null) " ..
              "| sort -u"
  local results = vim.fn.systemlist(cmd)

  pickers.new({}, {
    prompt_title = "clife notes",
    finder = finders.new_table({
      results = results,
      entry_maker = function(entry)
        local rel = entry:gsub(KB .. "/", "")
        return { value = entry, display = rel, ordinal = rel }
      end,
    }),
    sorter = conf.generic_sorter({}),
    attach_mappings = function(prompt_bufnr, _)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local sel = action_state.get_selected_entry()
        if sel then vim.cmd("edit " .. vim.fn.fnameescape(sel.value)) end
      end)
      return true
    end,
  }):find()
end

-- ------------------------------------------------------------------
-- Templates
-- ------------------------------------------------------------------

-- Substitute {{...}} placeholders. Existing convention from kb/templates/journal.md
-- uses {{day}} {{month}} {{date}} {{year}}; {{today}} (ISO) and {{title}} are new.
-- Unknown placeholders are left as-is so they're visible to the user.
local function render_template(content)
  local subs = {
    day   = os.date("%A"),        -- "Wednesday"
    month = os.date("%B"),        -- "May"
    date  = os.date("%-d"),       -- "6" (unpadded day of month)
    year  = os.date("%Y"),        -- "2026"
    today = os.date("%Y-%m-%d"),  -- "2026-05-06" (ISO)
    title = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(0), ":t:r"),
  }
  return (content:gsub("{{(%w+)}}", function(k)
    return subs[k] or "{{" .. k .. "}}"
  end))
end

function M.template_insert()
  if not telescope_or_warn() then return end
  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")

  local results = vim.fn.systemlist("find " .. TEMPLATES .. " -maxdepth 1 -name '*.md' -type f | sort")
  if #results == 0 then
    notify("no templates in " .. TEMPLATES, vim.log.levels.WARN)
    return
  end

  pickers.new({}, {
    prompt_title = "templates → insert at cursor",
    finder = finders.new_table({
      results = results,
      entry_maker = function(entry)
        local name = vim.fn.fnamemodify(entry, ":t:r")
        return { value = entry, display = name, ordinal = name }
      end,
    }),
    sorter = conf.generic_sorter({}),
    attach_mappings = function(prompt_bufnr, _)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local sel = action_state.get_selected_entry()
        if not sel then return end
        local raw = table.concat(vim.fn.readfile(sel.value), "\n")
        local lines = vim.split(render_template(raw), "\n", { plain = true })
        local row = vim.api.nvim_win_get_cursor(0)[1]
        vim.api.nvim_buf_set_lines(0, row - 1, row - 1, false, lines)
        notify("inserted: " .. vim.fn.fnamemodify(sel.value, ":t:r"))
      end)
      return true
    end,
  }):find()
end

-- ------------------------------------------------------------------
-- Quick file openers
-- ------------------------------------------------------------------

function M.journal()
  local path = KB .. "/journal/" .. os.date("%Y-%m-%d") .. ".md"
  vim.cmd("edit " .. vim.fn.fnameescape(path))
end

-- ------------------------------------------------------------------
-- Setup — registers user commands and keymaps
-- ------------------------------------------------------------------

local subcommands = {
  capture           = M.capture,
  ["capture-selection"] = M.capture_selection,
  ["new-note"]      = M.new_note,
  ["new-project"]   = M.new_project,
  ["new-sub-project"] = M.new_sub_project,
  inbox             = M.inbox,
  projects          = M.projects,
  notes             = M.notes,
  week              = M.week,
  journal           = M.journal,
  review            = M.review,
  template          = M.template_insert,
}

function M.setup(opts)
  opts = opts or {}
  local keymaps = vim.tbl_extend("force", default_keymaps, opts.keymaps or {})

  -- :cl auto-expands to :Cl. nvim user-commands must start uppercase, but
  -- typing :cl<space> in command mode triggers this and feels native.
  vim.cmd([[cnoreabbrev <expr> cl getcmdtype() == ':' && getcmdline() == 'cl' ? 'Cl' : 'cl']])

  -- :Cl <subcommand>
  vim.api.nvim_create_user_command("Cl", function(args)
    local sub = args.fargs[1]
    if not sub then
      notify("usage: :Cl <" .. table.concat(vim.tbl_keys(subcommands), "|") .. ">", vim.log.levels.WARN)
      return
    end
    local fn = subcommands[sub]
    if not fn then
      notify("unknown subcommand: " .. sub, vim.log.levels.ERROR)
      return
    end
    fn()
  end, {
    nargs = "+",
    complete = function(_, line)
      local words = vim.split(line, "%s+")
      if #words <= 2 then
        local out = {}
        local prefix = words[2] or ""
        for k, _ in pairs(subcommands) do
          if k:find("^" .. prefix) then table.insert(out, k) end
        end
        return out
      end
      return {}
    end,
    desc = "CLIfe: capture / new-note / new-sub-project / inbox / etc.",
  })

  -- Default keymaps (skip any explicitly disabled with false)
  local function map(mode, lhs, fn, desc)
    if not lhs then return end
    vim.keymap.set(mode, lhs, fn, { desc = desc, silent = true })
  end

  map("n", keymaps.capture,           M.capture,           "clife: capture")
  map("v", keymaps.capture_selection, M.capture_selection, "clife: capture selection")
  map("n", keymaps.new_note,          M.new_note,          "clife: new note in current project")
  map("n", keymaps.new_project,       M.new_project,       "clife: new project")
  map("n", keymaps.new_sub_project,   M.new_sub_project,   "clife: new sub-project")
  map("n", keymaps.inbox,             M.inbox,             "clife: inbox triage")
  map("n", keymaps.projects,          M.projects,          "clife: projects picker")
  map("n", keymaps.notes,             M.notes,             "clife: notes picker")
  map("n", keymaps.week,              M.week,              "clife: weekly plan")
  map("n", keymaps.journal,           M.journal,           "clife: today's journal")
  map("n", keymaps.review,            M.review,            "clife: full review")
  map("n", keymaps.template,          M.template_insert,   "clife: insert template at cursor")
end

return M
