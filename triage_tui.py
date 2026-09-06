"""triage_tui.py — `cl triage --tui`. The triage surface, M-4 in the Bridge deck.

The queue on the left, one note open on the right, a tag box under it. Every
write goes through `cl stream set`, so the vocabulary guard applies here exactly
as it does at the prompt: a variant resolves to the spelling already in use, a
near-duplicate is refused with the candidates. Nothing about tag identity is
reimplemented in this file.

WHY IT LOOKS LIKE THE RAIL AND NOT LIKE `cl notes --tui`
-------------------------------------------------------
Hearth's palette (#e8a34e amber on true black), not tui_common's (#e8a87c on
#cdd9e5). This pane's home is the Bridge deck, beside the rail and across from
`hermes chat --tui`, and a triage pane in a different amber than the rail six
inches to its right reads as a different application. The clife TUI zoo is
archive; the deck is where this lives.

THE VOCABULARY IS ALWAYS ON SCREEN
----------------------------------
Not decoration. The whole argument for tags-as-routing is that placement is a
WORD, and the failure mode is a vocabulary that grows a near-duplicate for every
idea until no tag can answer "what is this about". `cl stream set` enforces that
in the writer; this shows the list while you type, filtered to what you have
typed so far, so the reuse is visible before the refusal has to happen. Same
rule, two places: the guard is the floor, this is the affordance.

WHAT IT DELIBERATELY CANNOT DO
------------------------------
Route and promote. Those file a note somewhere specific, and belong to
`cl inbox` and the writer's <leader>sp. `c` hands off to the Hermes pane next
door for the notes that need a conversation rather than a keystroke.

`d` (trash) IS here, having been argued out of the first cut on the grounds
that a surface which can destroy things is one you use carefully and therefore
less. That was wrong in the face of the actual backlog: most of a real queue is
test captures and specs whose work is done, notes with no answer to "what is
this about" because they are not about anything. Tag-only, they can only be
cleared by coining a junk tag — which poisons the vocabulary with precisely the
near-duplicates the guard exists to prevent. So the queue gets a verb meaning
"this was never a note", and it is recoverable three ways: the undo stack here,
`cl triage restore`, and the kb's git history behind both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import Input, Label, ListItem, ListView, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stream            # noqa: E402
import triage            # noqa: E402

CL = str(Path(__file__).resolve().parent / "cl")
KB = stream.KB

HEARTH = Theme(
    name="hearth",
    primary="#e8a34e", secondary="#b8b3ad", accent="#e8a34e",
    warning="#e8a34e", error="#d87a7a", success="#7dc47d",
    foreground="#d8d4cf", background="#000000", surface="#0a0a0a",
    panel="#141210", dark=True,
)

ACCENT, DIM, FAINT = "#e8a34e", "#8a8580", "#5f5a54"
RUST, MOSS = "#d87a7a", "#7dc47d"

CSS = """
Screen { background: #000000; }
#body { height: 1fr; }
#queuecol { width: 40%; min-width: 28; border-right: solid #272320; }
#detailcol { width: 1fr; padding: 0 1; }
.hdr { background: #141210; color: #e8a34e; text-style: bold; padding: 0 1; height: 1; }
#queue { height: 1fr; background: #000000; }
#queue > ListItem { padding: 0 1; }
/* One row, one line. At the deck's ~27-column queue a wrapped title runs to a
   second unindented line and the list stops being scannable at a glance. */
#queue Static { text-wrap: nowrap; text-overflow: ellipsis; }
#queue > ListItem.--highlight { background: #241809; }
#title { color: #d8d4cf; text-style: bold; padding: 1 0 0 0; height: auto; }
#meta { color: #5f5a54; height: 1; }
#hermes { height: auto; padding: 0 1; margin: 1 0; background: #221809; }
#hermes.flag { background: #241213; }
#preview { height: 1fr; color: #8a8580; }
/* Thin and quiet. Textual's default vertical scrollbar is 2 cells of solid
   accent, which in a 27-column queue is 7% of the width shouting for attention
   it does not deserve — the bar is a position readout, not a control anyone
   here reaches for. One cell, near-invisible until the pointer is on it. */
#queue, #preview, #vocab {
    scrollbar-size-vertical: 1;
    scrollbar-background: #000000;
    scrollbar-color: #3a3833;
    scrollbar-background-hover: #000000;
    scrollbar-color-hover: #5f5a54;
    scrollbar-background-active: #000000;
    scrollbar-color-active: #e8a34e;
}
#tagbox { border: round #272320; background: #0a0a0a; color: #d8d4cf; }
#tagbox:focus { border: round #e8a34e; }
#vocab { height: auto; max-height: 3; color: #8a8580; padding: 0 1; }
#status { height: 1; background: #141210; color: #8a8580; padding: 0 1; }
/* The message line. One row at the bottom, right-aligned, dim — and it can
   never overlap anything, which is the whole complaint about the toasts it
   replaces: they landed on the tag box and the note you were reading, to
   report something you had just done on purpose. */
#msg { height: 1; background: #000000; color: #5f5a54; padding: 0 1;
       text-align: right; text-wrap: nowrap; text-overflow: ellipsis; }
"""


def _run(*args) -> dict:
    """`cl ...` with --json, as a dict. The CLI is the API — the guard, the
    ambiguity refusal and the frontmatter write all live behind it, and calling
    stream.apply_set directly from here would be the second implementation this
    whole design exists to avoid."""
    try:
        p = subprocess.run([CL, *args, "--json"], capture_output=True,
                           text=True, timeout=30)
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    out = (p.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": (p.stderr or out or "no output")[:200]}
    if isinstance(data, dict) and "ok" not in data:
        data["ok"] = p.returncode == 0
    return data if isinstance(data, dict) else {"ok": True, "rows": data}


class TriageApp(App):
    TITLE = "cl triage"
    CSS = CSS

    BINDINGS = [
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("i", "focus_tags", "Tag", show=False),
        Binding("a", "accept", "Accept suggestion", show=False),
        Binding("t", "todo", "Mark todo", show=False),
        Binding("d", "trash", "Trash", show=False),
        Binding("u", "undo", "Undo last", show=False),
        Binding("c", "chat", "Jump to chat", show=False),
        Binding("C", "kickoff", "Ask Hermes again", show=False),
        Binding("o", "open", "Open in writer", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Keys", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self.vocab: dict = {}
        # A stack, not one slot. A purge pass is dozens of `d` in a row, and
        # single-level undo means "three back" is unreachable exactly when it
        # is most likely to be needed.
        self._undo: list[tuple] = []
        self._asked = False
        self._msg_timer = None
        self._slots_seen = 0.0

    # ── layout ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="queuecol"):
                yield Label("QUEUE", classes="hdr", id="queuehdr")
                yield ListView(id="queue")
            with Vertical(id="detailcol"):
                yield Label("", classes="hdr", id="detailhdr")
                yield Static("", id="title")
                yield Static("", id="meta")
                yield Static("", id="hermes")
                yield VerticalScroll(Static("", id="previewtext"), id="preview")
                yield Input(placeholder="tags…  (Enter applies · Esc back)",
                            id="tagbox")
                yield Static("", id="vocab")
        yield Static("", id="msg")

    async def on_mount(self) -> None:
        self.register_theme(HEARTH)
        self.theme = "hearth"
        await self.reload()
        self.set_focus(self.query_one("#queue", ListView))
        # Hermes fills slots from the pane next door and has no way to tell
        # this app it did. One stat every two seconds is cheaper than making
        # him press `r` to find out — and a suggestion he never sees is the
        # same as one that was never written.
        self.set_interval(2.0, self._poll_slots)

    # ── messages ────────────────────────────────────────────────────────────
    def notify(self, message, *, title: str = "",                 # type: ignore[override]
               severity: str = "information", timeout: float | None = None,
               markup: bool = True, **kw) -> None:
        """No toasts.

        Textual's notification is a card floating over the layout, and in a
        71-column pane it lands squarely on the tag box and the note body —
        covering the thing you are working on in order to announce something
        you just did deliberately. Nearly everything here is a confirmation of
        a keystroke, which is the weakest possible claim on the middle of the
        screen.

        So it goes to one dim right-aligned row at the bottom that cannot
        overlap anything. Severity picks the colour and nothing else.

        A confirmation clears after 2.5s — long enough to catch out of the
        corner of an eye, short enough that a fast pass never has the last
        note's line still sitting there. An error does NOT time out: a refusal
        is the one message worth still being there when you look up.
        """
        # `markup` and **kw keep this a drop-in for App.notify: Textual 8
        # passes markup=, and an override narrower than the method it replaces
        # fails at the one call site nobody tested.
        colour = {"error": RUST, "warning": ACCENT}.get(severity, DIM)
        try:
            self.query_one("#msg", Static).update(Text(str(message), style=colour))
        except Exception:  # noqa: BLE001 — before compose, or after unmount
            return
        if self._msg_timer is not None:
            self._msg_timer.stop()
            self._msg_timer = None
        if severity != "error":
            self._msg_timer = self.set_timer(timeout or 2.5, self._clear_msg)

    def _clear_msg(self) -> None:
        self._msg_timer = None
        try:
            self.query_one("#msg", Static).update("")
        except Exception:  # noqa: BLE001
            pass

    # ── data ────────────────────────────────────────────────────────────────
    async def reload(self) -> None:
        """Read from the modules, not the CLI: this is a read of live files on
        every keystroke-driven refresh, and paying a subprocess for it would
        make the list lag the write that caused it. Writes still go out through
        `cl` — read cheap, write guarded."""
        self.rows = triage.queue()
        self.vocab = stream.vocabulary(stream.load(include_daily=True))
        # Stamp the baseline HERE, where the sidecar is actually read. Taking
        # it on the first poll instead made that poll always look like a
        # change, which swallowed the notification for any suggestion that
        # landed in the first two seconds.
        try:
            self._slots_seen = triage.SLOTS.stat().st_mtime
        except OSError:
            self._slots_seen = 0.0
        view = self.query_one("#queue", ListView)
        keep = view.index
        # AWAITED. `clear()` hands back an AwaitRemove and takes the rows out
        # on a later frame; without the await, the index set at the bottom of
        # this method was applied first and then wiped to None when the removal
        # finally landed. On screen that read as the highlight vanishing after
        # every `d` until an arrow key put it back.
        await view.clear()
        for r in self.rows:
            age = f"{r['age_days']}d" if r["age_days"] is not None else "—"
            t = Text()
            t.append(f"{age:>5} ", FAINT)
            t.append(r["title"][:44] or r["slug"][:44])
            if r["suggested"]:
                t.append(f"  {' '.join(r['suggested'])}", ACCENT)
            elif r["note"]:
                t.append("  ?", ACCENT)
            view.append(ListItem(Static(t)))
        filled = sum(1 for r in self.rows if r["suggested"] or r["note"])
        self.query_one("#queuehdr", Label).update(
            f"QUEUE — {len(self.rows)} unplaced · {filled} suggested")
        if self.rows:
            view.index = min(keep or 0, len(self.rows) - 1)
        self._paint_status()
        self._paint_detail()

    async def _poll_slots(self) -> None:
        """Re-read when the sidecar changes underneath us.

        mtime only, not the file contents: this fires every two seconds for as
        long as the pane is open, and a full reload re-reads every note in the
        stream. The stat is the cheap question; the reload is the expensive
        answer, and it only runs when the answer changed.

        Never while the tag box has focus — a repaint rewrites that box from
        the row's suggestions, so an auto-reload mid-word would eat what he was
        typing. The poll comes back around in two seconds.
        """
        if isinstance(self.focused, Input):
            return
        try:
            m = triage.SLOTS.stat().st_mtime
        except OSError:
            return                       # no sidecar yet — nothing to notice
        if m == self._slots_seen:
            return
        before = sum(1 for r in self.rows if r["suggested"] or r["note"])
        await self.reload()             # re-stamps _slots_seen
        after = sum(1 for r in self.rows if r["suggested"] or r["note"])
        if after > before:
            self.notify(f"{after - before} new suggestion(s) from the chat")

    def _current(self) -> dict | None:
        i = self.query_one("#queue", ListView).index
        if i is None or not self.rows or i >= len(self.rows):
            return None
        return self.rows[i]

    def _paint_status(self) -> None:
        n = len(self.vocab)
        self.query_one("#status", Static).update(Text.assemble(
            ("TRIAGE  ", f"bold {ACCENT}"),
            (f"{n} tags", DIM),
            ("  i tag · a accept · d trash · u undo · c chat · ?", FAINT)))
        # `r` is deliberately absent from the hint: suggestions arrive on their
        # own now, and advertising a key for something that happens anyway
        # spends width teaching a habit nobody needs.

    def _paint_detail(self) -> None:
        row = self._current()
        hermes = self.query_one("#hermes", Static)
        if row is None:
            self.query_one("#detailhdr", Label).update("")
            self.query_one("#title", Static).update("")
            self.query_one("#meta", Static).update("")
            hermes.update("")
            hermes.display = False
            self.query_one("#previewtext", Static).update(
                Text("nothing unplaced — every note in the stream carries tags",
                     style=DIM))
            return
        self.query_one("#detailhdr", Label).update(row["slug"])
        self.query_one("#title", Static).update(
            Text(row["title"] or "(no title)", no_wrap=False, overflow="fold"))
        # Not the relpath: at the deck's ~41 columns "2026-03-28 00:00 ·
        # writing/_stream/2026/03/…" truncates to the half that says nothing,
        # and the header above already carries the note's identity.
        age = f"{row['age_days']}d old" if row["age_days"] is not None else ""
        self.query_one("#meta", Static).update(
            Text(" · ".join(x for x in (row["created"] or "undated", age) if x),
                 style=FAINT))

        if row["note"]:
            hermes.display = True
            hermes.set_class(row["flag"] in ("dup", "junk"), "flag")
            who = row["by"] or "hermes"
            hermes.update(Text.assemble(
                (f"{who}  ", f"bold {RUST if row['flag'] in ('dup','junk') else ACCENT}"),
                (row["note"], "#d8d4cf")))
        else:
            hermes.display = False

        body = ""
        p = KB / row["relpath"]
        try:
            raw = p.read_text(errors="replace")
            _, body, _ = stream.fm.split(p)
        except Exception:                          # noqa: BLE001
            body = "(unreadable)"
        self.query_one("#previewtext", Static).update(
            Text(body.strip()[:4000] or "(empty)", no_wrap=False, overflow="fold"))

        box = self.query_one("#tagbox", Input)
        box.value = ", ".join(row["suggested"])
        self._paint_vocab(box.value)

    def _paint_vocab(self, typed: str) -> None:
        """Two jobs on one panel, and they are not the same job.

        The tag you are still TYPING gets the vocabulary filtered to it — the
        affordance half of the reuse rule, so seeing `groceries·7` while typing
        `grocer` stops `grocery` being coined at all.

        Every tag you have already FINISHED (anything before the last comma)
        gets a real verdict from `stream.classify_tag` — the same call
        `cl stream set` makes. This used to look only at the fragment in
        progress, so `dharma, hermes` said nothing at all: `hermes` matched the
        vocabulary, and `dharma` — the one actually being coined — was never
        examined. A warning that goes quiet as soon as you type a second tag is
        worse than none, because the silence reads as approval.

        Classifier, not substring matching. The old test was `frag in tag`,
        which is a different rule from the one that decides the write, and two
        rules that can disagree about whether a tag is new is exactly the drift
        this design spends its effort avoiding.
        """
        parts = typed.split(",")
        frag = parts[-1].strip().lstrip("#").lower()
        done = [x.strip() for x in parts[:-1] if x.strip()]

        t = Text()
        lines = 0

        verdicts = []
        for raw in done:
            c = stream.classify_tag(raw, self.vocab)
            v = c["verdict"]
            if v == "variant" and c["tag"] != stream.norm_tag(raw):
                verdicts.append((f"{c['input']} → {c['tag']}", ACCENT))
            elif v == "near":
                verdicts.append(
                    (f"{c['input']} ≈ {c['candidates'][0]} — will refuse", RUST))
            elif v == "new":
                verdicts.append((f"{c['tag']} is NEW", ACCENT))
            # `exact` says nothing: a tag that is already in use is the normal
            # case, and narrating it would bury the one line that matters.
        if verdicts:
            for n, (txt, colour) in enumerate(verdicts):
                if n:
                    t.append("  ", FAINT)
                t.append(txt, colour)
            t.append("\n")
            lines = 1

        items = ([(x, n) for x, n in self.vocab.items() if frag in x] if frag
                 else list(self.vocab.items()))
        t.append("vocab  ", FAINT)
        if frag and not items:
            t.append(f"'{frag}' would be NEW", ACCENT)
        # Two guards, because either alone has failed: a width budget (this
        # panel is ~39 columns in the deck) AND a hard count. `size.width` is 0
        # at first paint, before layout — trusting it alone printed twenty tags
        # into a three-line box on the very first frame. The budget halves when
        # a verdict line is already using one of the three rows.
        w = self.query_one("#vocab", Static).size.width or 39
        budget = w * (2 - lines)
        used, shown = 7, 0
        for tag, n in items[:10 if frag else 8]:
            cost = len(tag) + len(str(n)) + 3
            if used + cost > budget:
                break
            t.append(tag, ACCENT if frag else DIM)
            t.append(f"·{n}  ", FAINT)
            used += cost
            shown += 1
        if shown < len(items):
            t.append(f"+{len(items) - shown}", FAINT)
        self.query_one("#vocab", Static).update(t)

    # ── actions ─────────────────────────────────────────────────────────────
    def action_down(self) -> None:
        self.query_one("#queue", ListView).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#queue", ListView).action_cursor_up()

    def on_list_view_highlighted(self, _e) -> None:
        self._paint_detail()

    def on_list_view_selected(self, event) -> None:
        """Enter on the queue opens the tag box. The list has exactly one verb
        worth having on the most obvious key, and it is the one this whole
        surface exists for."""
        event.stop()
        self.action_focus_tags()

    def action_focus_tags(self) -> None:
        if self._current():
            self.query_one("#tagbox", Input).focus()

    async def action_accept(self) -> None:
        row = self._current()
        if not row:
            return
        if not row["suggested"]:
            self.notify("no suggestion on this note", severity="warning")
            return
        await self._apply(row, row["suggested"])

    async def action_todo(self) -> None:
        row = self._current()
        if row:
            self._write(row, "--todo")
            await self.reload()

    async def action_trash(self) -> None:
        """`d` — this was never a note. No confirmation prompt: it goes to
        ~/kb/.trash, `u` puts it straight back, and a yes/no on each of sixty
        would make the pass slower than not doing it."""
        row = self._current()
        if not row:
            return
        res = _run("triage", "trash", row["slug"])
        if not res.get("ok"):
            self.notify(f"trash failed: {res.get('error')}", severity="error")
            return
        self._undo.append(("trash", res["trashed"], row["slug"]))
        self.notify(f"trashed {row['slug']}  ·  u to undo")
        await self._advance()

    async def action_undo(self) -> None:
        """Take back the last write, whatever it was.

        The guard refuses near-duplicates but cannot know a correctly-spelled
        tag was the wrong idea, and nothing can know a trashed note was wanted
        — so the surface that makes those changes has to be the one that
        reverses them."""
        if not self._undo:
            self.notify("nothing to undo", severity="warning")
            return
        kind, *rest = self._undo.pop()
        if kind == "trash":
            name, slug = rest
            res = _run("triage", "restore", name)
            msg = f"restored {slug}" if res.get("ok") else \
                  f"restore failed: {res.get('error')}"
        else:
            slug, tags = rest
            res = _run("stream", "set", slug, "--untag", ",".join(tags))
            msg = f"untagged {' '.join(tags)}" if res.get("ok") else \
                  f"undo failed: {res.get('error')}"
        self.notify(msg, severity="information" if res.get("ok") else "error")
        await self.reload()

    # Short on purpose: the `triage` skill in the chat pane carries the whole
    # procedure (which commands, reuse before coining, when to ask instead of
    # tag). Restating it here would be a second copy to drift — this only has
    # to say GO.
    KICKOFF = ("Work the triage queue: read `cl triage --json`, then fill slots "
               "with `cl triage suggest`. Reuse tags from `cl stream tags` "
               "before coining new ones. Leave a --note instead of guessing.")

    def _send_to_chat(self, text: str = "") -> bool:
        """Focus the Hermes pane, optionally typing a line into it first."""
        if not os.environ.get("TMUX"):
            self.notify("only inside the Bridge deck — this talks to the pane "
                        "beside it", severity="warning")
            return False
        try:
            if text:
                # -l is literal: the text carries backticks and colons, and
                # without it tmux would read parts of the line as key names.
                subprocess.run(["tmux", "send-keys", "-t", "{top-right}",
                                "-l", text], capture_output=True,
                               timeout=5, check=True)
                subprocess.run(["tmux", "send-keys", "-t", "{top-right}",
                                "Enter"], capture_output=True,
                               timeout=5, check=True)
            subprocess.run(["tmux", "select-pane", "-t", "{top-right}"],
                           capture_output=True, timeout=5, check=True)
            return True
        except Exception as exc:                   # noqa: BLE001
            self.notify(f"no pane to talk to: {exc}", severity="warning")
            return False

    def action_kickoff(self) -> None:
        """`C` — ask again, whatever the queue looks like. For a second pass
        over notes Hermes left empty the first time."""
        if self._send_to_chat(self.KICKOFF):
            self._asked = True
            self.notify("asked Hermes to work the queue — M-h back, r to reload")

    def action_chat(self) -> None:
        """`c` — hand the keyboard to the Hermes pane beside this one.

        The first `c` of a pass also STARTS one: the skill teaches Hermes how
        to work this queue but nothing was telling it when, so every session
        began by retyping the same request. One key does the obvious thing.

        It asks only when nothing has been suggested yet, which is the honest
        reading of "start a pass" — with slots already filled, `c` is just
        walk-over-and-talk, and re-sending the request would bury the answer
        you went there to read under a fresh one. State decides, not a counter:
        empty queue-of-suggestions means no pass has happened. `C` re-asks
        deliberately.

        The jump itself is the deck's own `select-pane -t {top-right}`, called
        rather than reimplemented, so `c` and M-l land in the same place. No
        -L bridge: inside a pane tmux reads $TMUX and finds its own server.
        """
        fresh = not self._asked and not any(
            r["suggested"] or r["note"] for r in self.rows)
        if not self._send_to_chat(self.KICKOFF if fresh else ""):
            return
        if fresh:
            self._asked = True
            self.notify("asked Hermes to work the queue — M-h back, r to reload")

    async def action_open(self) -> None:
        row = self._current()
        if not row:
            return
        editor = shutil.which("nvim-write") or "nvim"
        with self.suspend():
            subprocess.call([editor, str(KB / row["relpath"])])
        await self.reload()

    async def action_reload(self) -> None:
        """`r`. Kept even though the sidecar is polled: notes also change on
        disk from the writer, `kb-inbox`, the ingest timer and the other
        machine, and none of those touch the file being watched."""
        await self.reload()
        self.notify("reloaded")

    def action_help(self) -> None:
        self.notify("j/k move · i tag · a accept suggestion · t todo · "
                    "d trash (recoverable) · u undo · "
                    "c chat (first one starts a pass) · C re-ask · "
                    "o open in writer · "
                    "r reload · q quit", timeout=10)

    # ── writing ─────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        row = self._current()
        if not row:
            return
        tags = [t.strip() for t in event.value.replace(" ", ",").split(",")
                if t.strip()]
        if not tags:
            self.set_focus(self.query_one("#queue", ListView))
            return
        await self._apply(row, tags)

    async def _apply(self, row: dict, tags: list) -> None:
        res = self._write(row, "--tag", ",".join(tags))
        if not res.get("ok"):
            return                     # text stays in the box, ready to fix
        # Everything in this queue is untagged by definition, so whatever the
        # writer reports as the note's tags is exactly what this apply added —
        # which is what undo has to take back off again.
        self._undo.append(("tag", row["slug"],
                           (res.get("applied") or {}).get("tags") or tags))
        # The note has left the queue: it is tagged, which is the whole
        # definition of placed. Clearing its slot keeps the sidecar from
        # accumulating suggestions for notes nobody will see again.
        triage.clear(row["slug"])
        await self._advance()

    async def _advance(self) -> None:
        """The note just left the queue; land on whatever slid up into its
        place, focus on the list. Triage is decide-next-decide-next, and
        leaving focus in the tag box meant `u`, `d` and `j` all typed
        themselves into it instead of moving on."""
        i = self.query_one("#queue", ListView).index or 0
        await self.reload()
        view = self.query_one("#queue", ListView)
        if self.rows:
            view.index = min(i, len(self.rows) - 1)
        self.set_focus(view)

    def _write(self, row: dict, *flags) -> dict:
        res = _run("stream", "set", row["slug"], *flags)
        if not res.get("ok"):
            blocked = res.get("blocked") or []
            if blocked:
                # The refusal IS the feature. Say which existing tag it looks
                # like, and leave the text in the box so it can be corrected
                # rather than retyped.
                for b in blocked:
                    self.notify(f"'{b['input']}' looks like: "
                                f"{', '.join(b['candidates'])} — use that, "
                                f"or `cl stream set … --new`",
                                severity="warning", timeout=9)
            else:
                self.notify(f"failed: {res.get('error', 'unknown')}",
                            severity="error")
            return res
        notes = res.get("notes") or []
        msg = ", ".join(f"{k}: {v}" for k, v in (res.get("applied") or {}).items())
        self.notify(f"{row['slug']} → {msg}" + (f"  ({'; '.join(notes)})"
                                                if notes else ""))
        return res

    # ── keys ────────────────────────────────────────────────────────────────
    def on_key(self, event: events.Key) -> None:
        box = self.query_one("#tagbox", Input)
        if self.focused is box:
            if event.key == "escape":
                self.set_focus(self.query_one("#queue", ListView))
                event.stop()
            return
        # Single-letter bindings must not fire while typing a tag; Input eats
        # printable keys itself, so only escape needs handling above.

    def on_input_changed(self, event: Input.Changed) -> None:
        self._paint_vocab(event.value)


def main() -> None:
    if not sys.stdin.isatty():
        print("cl triage --tui needs a terminal.", file=sys.stderr)
        raise SystemExit(1)
    os.environ.setdefault("COLORTERM", "truecolor")
    TriageApp().run()


if __name__ == "__main__":
    main()
