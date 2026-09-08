"""triage_tui.py — `cl triage --tui`. The TAG NAVIGATOR, M-4 in the Bridge deck.

Three columns: TAGS · NOTES · one note. The left column is the vocabulary
with the unplaced queue pinned on top; picking a tag loads its notes into
the middle; the right is the note you are on, with the tag box under it.

IT WAS A TRIAGE QUEUE UNTIL 2026-09-07
--------------------------------------
The complaint that produced this: "we have a great inbox to tag system, but
I lose everything when it leaves the inbox." That was exactly true, and the
cause was one line — `triage.queue` skipped any note that had tags. Tagging
a note was therefore the act of making it invisible to the only surface that
could see it. The fix was not a second tool but a parameter: the queue takes
a VIEW, `None` for unplaced and a tag otherwise, and the same three panes
answer both questions.

`g` returns to the unplaced queue, so triage is now one view in the
navigator rather than a separate mode you leave.

Every write goes through `cl stream set`, so the vocabulary guard applies
here exactly as it does at the prompt: a variant resolves to the spelling
already in use, a near-duplicate is refused with the candidates. Nothing
about tag identity is reimplemented in this file.

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

TAGS ARE OBJECTS, NOT A STRING
------------------------------
`i` focuses the CHIPS: h/l walk the note's tags, `d` removes the one under the
cursor, `a` picks a new one from the vocabulary. There is no text field.

Two earlier versions failed, and both failed the same way. The first was
add-only, so a tag could never be removed from here at all. The second held a
comma-separated list you edited as text — better, but his verdict was exact:
"they're still not necessarily easier than going into the frontmatter because
I don't have vim commands." That is a correct diagnosis of the wrong layer.
Motions would have made editing a SERIALIZATION bearable; a tag set has no
punctuation anyone should have to steer a cursor through.

`a` does not open a second box. It hands the TAGS column its other job — the
column is already on screen, already filters as you type, already shows how
many notes carry each tag, which IS the reuse affordance. A word matching
nothing is still offered, and `cl stream set` gets the last word: it resolves
`recipes` to the existing `recipe` and refuses a genuine near-duplicate with
candidates. One vocabulary, one guard, no second place to misspell a tag into
existence.

This also retired the `#vocab` strip, whose whole job was showing the
vocabulary while you typed. The column does that better and permanently, and
the four rows come back to the prose.

WHAT IT DELIBERATELY CANNOT DO
------------------------------
Edit a note's BODY in place. Enter on a row OPENS the file (`o`, in the writer); the
middle column is a list of doors, not a document. That is the fork where
Logseq went the other way — it made reference lists editable inline, which is
why it needs block UUIDs written into your markdown to know where an edit
belongs. Keeping the file as the unit of editing skips the whole problem.

Route and promote are gone with the 2026-09-07 flatten: nothing moves any
more, so placing a note IS tagging it, which is what this surface does. `c`
hands off to the Hermes pane next door for the notes that need a conversation
rather than a keystroke.

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
import time
import sys
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.css.query import NoMatches
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

/* RESPONSIVE — one breakpoint, because stacking the list removed the need
   for two. Wide and medium are now the SAME shape; the only question left is
   whether the tag column fits. Below 72 columns it does not (22 of them is a
   third of the screen), so it hides and `/` or `h` brings it back over the
   list. M-4 only gets about two thirds of the terminal, which is why an
   80-col ssh session has to be a first-class case rather than an afterthought. */
#tagcol  { width: 22; min-width: 16; border-right: solid #272320; }
#tagfilter { border: none; height: 1; background: #0a0a0a; color: #d8d4cf;
             padding: 0 1; }
#tagfilter:focus { background: #141210; color: #e8a34e; }
#taglist { height: 1fr; background: #000000; }
#taglist > ListItem { padding: 0 1; }
#taglist Static { text-wrap: nowrap; text-overflow: ellipsis; }
#taglist > ListItem.--highlight { background: #241809; }

#rightcol { width: 1fr; height: 1fr; layout: vertical; }
/* The cap lives on the LIST, not on this container, and is set in ROWS by
   _apply_layout rather than as a percentage. A `max-height: 45%` here
   resolved against a parent that is itself `height: auto` — circular, so
   Textual grew the list to its content and the container simply CLIPPED it:
   the cursor moved onto rows you could not see. A bound on the scrollable
   widget is what makes it scroll instead of overflow. */
#queuecol { width: 1fr; height: auto; border-bottom: solid #272320; }
/* auto on the LIST too, and no max-height on it — the cap belongs on the
   container. This is the rail's rule (#inboxcol InboxPane ListView), which
   is the one place in this system that already gets content-sizing right. */
#queue { height: auto; overflow-y: auto; background: #000000; }
#detailcol { width: 1fr; height: 1fr; padding: 0 1; }

/* Reading the whole view: the list has nothing to add while you read it, and
   the prose wants every row. */
#body.readall #queuecol { display: none; }
#body.readall #tagcol { display: none; }
#body.readall #tagchips { display: none; }

#body.narrow #tagcol { display: none; }
/* `/` has to bring the column back or the only door to the vocabulary is
   walled up. It returns over the list rather than as a modal: you choose a
   tag by watching the list under it change. */
#body.narrow.tags-open #tagcol { display: block; width: 1fr; height: 1fr;
                                 border-right: none;
                                 border-bottom: solid #272320; }
#body.narrow.tags-open #rightcol { height: 40%; }
.hdr { background: #141210; color: #e8a34e; text-style: bold; padding: 0 1; height: 1; }
#children { height: auto; padding: 0 1; }
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
#queue, #taglist, #preview {
    scrollbar-size-vertical: 1;
    scrollbar-background: #000000;
    scrollbar-color: #3a3833;
    scrollbar-background-hover: #000000;
    scrollbar-color-hover: #5f5a54;
    scrollbar-background-active: #000000;
    scrollbar-color-active: #e8a34e;
}
/* The chips sit directly under the meta line, above the prose — where the
   note's tags already read as part of its header. One row, no border at rest
   so it costs nothing on a note you are only reading. */
#tagchips { height: auto; min-height: 1; padding: 0 0 1 0; }
#tagchips:focus { background: #141210; }
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
    if not out:
        # NO STDOUT AT ALL — a crash, not a refusal. This used to fall through
        # to `data = {}`, which got an `ok` from the exit code and NO `error`,
        # so every traceback surfaced as the two least useful words available:
        # "failed: unknown". The message that would have named the cause was
        # sitting in stderr the whole time.
        err = (p.stderr or "").strip()
        if p.returncode != 0 or err:
            return {"ok": False,
                    "error": (err.splitlines() or ["no output"])[-1][:200]}
        return {"ok": True}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": (p.stderr or out or "no output")[:200]}
    if isinstance(data, dict) and "ok" not in data:
        data["ok"] = p.returncode == 0
    return data if isinstance(data, dict) else {"ok": True, "rows": data}


def _rollups(items) -> dict:
    """Prefixes that NOBODY has applied as a tag -> how many notes sit under
    them.

    `projects/budget`, `projects/hearth` and `projects/clife` are one subject
    written three ways, and the store already knows it: every view matches
    hierarchically, so `cl stream tag projects` returns all thirteen. What was
    missing was somewhere to STAND. The tag column is built from the
    vocabulary in USE, nobody has ever written a bare `projects`, so the one
    row that would answer "show me all of them" did not exist — filtering to
    `project` listed the seven children and offered no way to take their
    union.

    A prefix that IS also a tag gets a row here too, and keeps exactly one row
    in the column — `blog` is a real tag and a parent, and it is the count
    that was wrong rather than the row. The vocabulary counts notes carrying
    the literal word (10), while selecting it has always shown everything
    beneath it as well (13), so the number in the column was quietly
    predicting the wrong screen. These counts are what the view returns.

    Counted over NOTES rather than summed over children, because a note
    carrying both `projects/clife` and `projects/hearth` is one project note
    and adding child counts would report it twice.
    """
    tags = {stream.norm_tag(t) for it in items for t in it["tags"]
            if stream.norm_tag(t)}
    prefixes = set()
    for tag in tags:
        parts = tag.split("/")
        for i in range(1, len(parts)):          # every level: a/b/c -> a, a/b
            prefixes.add("/".join(parts[:i]))
    out = {}
    for pre in prefixes:
        out[pre] = sum(
            1 for it in items
            if any((n := stream.norm_tag(t)) == pre or n.startswith(pre + "/")
                   for t in it["tags"]))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


class TagChips(Static):
    """The note's tags as OBJECTS you move between, not a string you edit.

    The tag box this replaces was a comma-separated Input, and his verdict on
    it was exact: "they're still not necessarily easier than going into the
    frontmatter because I don't have vim commands." That is the right
    diagnosis of the wrong layer. Adding motions to the box would have made
    editing a SERIALIZATION tolerable; tags are a set, and the punctuation
    between them is not content anyone should have to steer a cursor through.

    So: j/k walk the tags (w/b and h/l too), `d` removes the one under the
    cursor, `a` opens the vocabulary picker — which is the TAGS column that is already on screen,
    already filtered as you type, already showing counts. One widget, two
    jobs, and no second place where a tag can be misspelled into existence.
    """
    can_focus = True
    suggested: list = []          # render_chips may run before the first show()

    BINDINGS = [
        # j/k FIRST, because in this app j/k has one meaning everywhere —
        # move within whatever has focus — and that consistency beats being
        # literal about the chips being laid out horizontally. w/b are also
        # bound and are not an analogy: a chip IS a word. h/l too, matching
        # the column movement one pane over. Three spellings of one verb, and
        # no wrong guess.
        # ⏎ takes the suggestion. Free here and nowhere else: on the queue
        # Enter DESCENDS, and a row of tags has nothing below it to descend
        # into, so the key was doing nothing on the one widget where the
        # suggestion is now on screen.
        Binding("enter", "take", "Take suggestion", show=False),
        Binding("j,w,l,right", "next", "Next tag", show=False),
        Binding("k,b,h,left",  "prev", "Prev tag", show=False),
        # I / A for first / last, his choice over 0 / $. In vim those insert
        # at the start and append at the end of a LINE, and a row of chips is
        # exactly a line you are inserting into — so the mnemonic transfers
        # even though the motion is a jump rather than an insert.
        Binding("I", "first", "First tag", show=False),
        Binding("A", "last",  "Last tag",  show=False),
        Binding("d,x",     "remove", "Remove",     show=False),
        Binding("a,i",     "add",    "Add",        show=False),
        Binding("escape",  "leave",  "Back",       show=False),
    ]

    class Remove(Message):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

    class Add(Message):
        pass

    class Take(Message):
        """⏎ — apply the suggestion shown beside the chips."""

    class Leave(Message):
        pass

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.tags: list = []
        self.cursor = 0

    def show(self, tags: list, suggested=()) -> None:
        """``suggested`` is what an agent proposed and nobody has accepted.

        It belongs HERE, next to the tags it would become, rather than only on
        the queue row you came from. Descending into a note to decide about it
        used to hide the very thing you were deciding about: the panel showed
        the preview and the note's own tags, the suggestion stayed one screen
        back, and `a` on the queue accepted something no longer in front of
        you.
        """
        self.tags = list(tags)
        have = {t.lower() for t in self.tags}
        # Only what is not already on the note. A suggestion half-accepted is
        # the common case — you took `shopping` and left `home` — and echoing
        # the part already applied would read as a duplicate chip.
        self.suggested = [t for t in (suggested or []) if t.lower() not in have]
        self.cursor = min(self.cursor, max(0, len(self.tags) - 1))
        self.render_chips()

    def render_chips(self) -> None:
        t = Text()
        if not self.tags:
            t.append("(no tags)", DIM)
        for i, tag in enumerate(self.tags):
            focused = self.has_focus and i == self.cursor
            t.append(" " + tag + " ",
                     f"black on {ACCENT}" if focused else f"{ACCENT}")
            t.append(" ")
        # Ghost chips: dim, `+` prefixed, never selectable. They are not tags
        # on this note, they are what would be, and making them look like the
        # real ones would be a lie the cursor could walk into.
        for tag in self.suggested:
            t.append(" +" + tag + " ", DIM)
            t.append(" ")
        hint = "  a add" + ("  ·  d remove  ·  j/k move  ·  esc back"
                            if len(self.tags) > 1
                            else ("  ·  d remove  ·  esc back" if self.tags
                                  else "  ·  esc back"))
        if self.suggested:
            hint = "  ⏎ take" + hint
        t.append(hint, FAINT)
        self.update(t)

    def on_focus(self) -> None:
        self.render_chips()

    def on_blur(self) -> None:
        self.render_chips()

    def action_prev(self) -> None:
        if self.tags:
            self.cursor = (self.cursor - 1) % len(self.tags)
            self.render_chips()

    def action_next(self) -> None:
        if self.tags:
            self.cursor = (self.cursor + 1) % len(self.tags)
            self.render_chips()

    def action_first(self) -> None:
        if self.tags:
            self.cursor = 0
            self.render_chips()

    def action_last(self) -> None:
        if self.tags:
            self.cursor = len(self.tags) - 1
            self.render_chips()

    def action_remove(self) -> None:
        if self.tags:
            self.post_message(self.Remove(self.tags[self.cursor]))

    def action_add(self) -> None:
        self.post_message(self.Add())

    def action_take(self) -> None:
        if self.suggested:
            self.post_message(self.Take())

    def action_leave(self) -> None:
        self.post_message(self.Leave())


class TriageApp(App):
    TITLE = "cl triage — tag navigator"
    CSS = CSS

    BINDINGS = [
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        # h/l move BETWEEN columns, j/k move within one — the same split every
        # other vim surface in this system uses, so the navigator needs no new
        # habit. `/` jumps to the tag filter; `g` is the way back to the
        # unplaced queue from wherever you have wandered.
        Binding("h", "col_left", "Tags", show=False),
        Binding("l", "col_right", "Notes", show=False),
        Binding("slash", "focus_filter", "Filter tags", show=False),
        Binding("g", "view_untagged", "Unplaced", show=False),
        # PRIORITY, or these never fire: Screen binds tab to focus_next and
        # screen bindings are matched before the app's. The rail shipped two
        # dead tab bindings for exactly this reason.
        Binding("tab", "cycle_child(1)", "Next child", show=False,
                priority=True),
        Binding("shift+tab", "cycle_child(-1)", "Prev child", show=False,
                priority=True),
        # `z` reads the whole view as ONE page. The drawer answers "which
        # notes are under this tag"; this answers "what do they SAY", which is
        # a different question and the one you have when reviewing rather than
        # filing. Stepping note-by-note makes you rebuild the thread in your
        # head at every step.
        Binding("z", "read_all", "Read the whole tag", show=False),
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
        # None = the unplaced queue (what this app has always shown); a string
        # = that tag's notes. One field, and every view is a query over it.
        self.view_tag = triage.UNTAGGED
        self.tag_names: list = []      # what the left column currently lists
        self._tag_filter = ""
        self._n_unplaced = 0
        self._tag_sig = None
        self._layout_mode = None
        self._preview_timer = None
        self._items: list = []
        self._items_at = 0.0
        # slug of the note we are picking a tag FOR, or None when the tag
        # column is being used to navigate. One flag, because the column does
        # both jobs and must not guess which.
        self._picking = None
        self._readall = False
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
            # TAGS on the left; the note list stacked OVER the reader on the
            # right. Not three even columns: the note list is the one thing
            # here whose height is data, and the data says it is short.
            # Median notes per tag is 1, p90 is 6, and exactly one tag of 240
            # would fill a 20-row column — so a full-height middle column was
            # empty ~85% of the time while the reader, which wants every row
            # and column it can get, had half the width.
            #
            # This is the shape the writer's stream drawer and the rail's
            # inbox already use: lists size to their content, the thing being
            # READ takes what is left.
            with Vertical(id="tagcol"):
                yield Label("TAGS", classes="hdr", id="taghdr")
                yield Input(placeholder="filter…", id="tagfilter")
                yield ListView(id="taglist")
            with Vertical(id="rightcol"):
                with Vertical(id="queuecol"):
                    yield Label("QUEUE", classes="hdr", id="queuehdr")
                    # The children of the tag being viewed, when it has any.
                    # Hidden entirely otherwise — a permanently empty strip
                    # above the queue would cost a row of notes on every view
                    # that is not a parent, which is most of them.
                    yield Static("", id="children")
                    yield ListView(id="queue")
                with Vertical(id="detailcol"):
                    yield Label("", classes="hdr", id="detailhdr")
                    yield Static("", id="title")
                    yield Static("", id="meta")
                    yield Static("", id="hermes")
                    yield TagChips(id="tagchips")
                    yield VerticalScroll(Static("", id="previewtext"), id="preview")
        yield Static("", id="msg")

    async def on_mount(self) -> None:
        self.register_theme(HEARTH)
        self.theme = "hearth"
        self._apply_layout(self.size.width, self.size.height)
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
    def _queue_items(self) -> list:
        """Build the note rows. Pure — no widget touched, so the list can be
        assembled before anything on screen is disturbed."""
        out = []
        for r in self.rows:
            age = f"{r['age_days']}d" if r["age_days"] is not None else "—"
            t = Text()
            t.append(f"{age:>5} ", FAINT)
            t.append(r["title"][:44] or r["slug"][:44])
            if r["suggested"]:
                t.append(f"  {' '.join(r['suggested'])}", ACCENT)
            elif r["note"]:
                t.append("  ?", ACCENT)
            out.append(ListItem(Static(t)))
        return out

    def _family(self) -> list:
        """[parent, *children] for whatever view you are on, or [].

        Keyed off the TOP segment, so standing on `blog/kids` offers the same
        family as standing on `blog` — you are in one subject either way, and
        having Tab mean different things depending on which rung you entered
        from is the kind of modal surprise this app keeps removing.
        """
        tag = self.view_tag
        if not isinstance(tag, str) or not tag:
            return []
        root = tag.split("/")[0]
        kids = sorted(k for k in self.vocab if k.startswith(root + "/"))
        return [root] + kids if kids else []

    def _paint_children(self) -> None:
        """The children strip: what is under this tag, and which one is open.

        This is the August idea in one line — "views based on tags that
        basically pull up a page with all the relevant blocks" — finished
        rather than approximated. The blocks were already there; what a parent
        view could not say was what it contained. The open one is highlighted
        because Tab moves the selection AND the view together: there is no
        separate act of opening, so the strip is the only thing that can show
        where you are.
        """
        strip = self.query_one("#children", Static)
        fam = self._family()
        if not fam:
            strip.display = False
            return
        strip.display = True
        t = Text()
        t.append("children  ", FAINT)
        for i, name in enumerate(fam):
            # The root is shown as `all`, because selecting it is not picking
            # a child — it is the rollup, and calling it `blog` beside
            # `blog/kids` would read as a sibling of its own children.
            label = "all" if i == 0 else name.split("/", 1)[1]
            n = len(triage.queue(self._items, tag=name))
            on = name == self.view_tag
            t.append(f" {label} ", "black on #e8a34e" if on else ACCENT)
            t.append(f"{n} ", FAINT)
            if i < len(fam) - 1:
                t.append(" · ", FAINT)
        t.append("   tab cycles", FAINT)
        strip.update(t)

    def _repaint_queue(self, keep=None) -> None:
        """Swap the note list in ONE frame.

        The old path did `await view.clear()` and then appended row by row,
        which yields to the compositor with the list empty — on screen that is
        a black flash through the middle column on every hover. Building the
        rows first and handing clear/extend to the same frame removes it. The
        original await was there because an index set before a deferred
        removal landed got wiped; extend() resolves that without the yield.
        """
        view = self.query_one("#queue", ListView)
        keep = view.index if keep is None else keep
        items = self._queue_items()
        view.clear()
        if items:
            view.extend(items)
            view.index = min(keep or 0, len(items) - 1)
        if self.view_tag is triage.UNTAGGED:
            filled = sum(1 for r in self.rows if r["suggested"] or r["note"])
            hdr = f"UNPLACED — {len(self.rows)} · {filled} suggested"
        else:
            hdr = f"#{self.view_tag} — {len(self.rows)}"
        self.query_one("#queuehdr", Label).update(hdr)
        self._paint_children()
        self._paint_status()
        self._paint_detail()

    async def reload(self) -> None:
        """Read from the modules, not the CLI: this is a read of live files on
        every keystroke-driven refresh, and paying a subprocess for it would
        make the list lag the write that caused it. Writes still go out through
        `cl` — read cheap, write guarded."""
        # ONE read of the store per refresh, shared four ways: the rows, the
        # vocabulary, the unplaced count, and the preview, which reuses it
        # rather than re-reading 238 files to answer a cursor move.
        self._items = stream.load(include_daily=True)
        self._items_at = time.monotonic()
        self.rows = triage.queue(self._items, tag=self.view_tag)
        self.vocab = stream.vocabulary(self._items)
        self.parents = _rollups(self._items)
        self._n_unplaced = sum(1 for i in self._items if not i["tags"])
        try:
            self._slots_seen = triage.SLOTS.stat().st_mtime
        except OSError:
            self._slots_seen = 0.0
        self._paint_tags()
        self._repaint_queue()

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

    # ── read the whole view ─────────────────────────────────────────────────
    MAX_READ = 120_000        # a very long tag should not stall the terminal

    def action_read_all(self) -> None:
        self._readall = not self._readall
        self.query_one("#body").set_class(self._readall, "readall")
        if self._readall:
            self._paint_read_all()
            self.set_focus(self.query_one("#preview", VerticalScroll))
        else:
            self._paint_detail()
            self.set_focus(self.query_one("#queue", ListView))
        self._paint_status()

    def _paint_read_all(self) -> None:
        """Every note in the view, concatenated, read-only.

        READ-ONLY, deliberately, and that is the whole design decision. Making
        the notes editable in place means writing edits back into regions
        whose boundaries shift the moment you add a line — the block-identity
        problem one level up, and the reason Logseq has to inject UUIDs into
        your markdown. Here `z` is a reading posture: to change something you
        leave it, which costs one keystroke and can never corrupt the note
        below the one you meant.
        """
        head = ("unplaced" if self.view_tag is triage.UNTAGGED
                else "#" + str(self.view_tag))
        self.query_one("#detailhdr", Label).update(
            f"READING {head} — {len(self.rows)} notes")
        self.query_one("#title", Static).update("")
        self.query_one("#meta", Static).update("")
        self.query_one("#hermes", Static).display = False
        out = Text()
        used = 0
        for n, r in enumerate(self.rows):
            if used > self.MAX_READ:
                out.append(f"\n… {len(self.rows) - n} more notes not shown "
                           f"(over {self.MAX_READ // 1000}k characters)\n", DIM)
                break
            try:
                _, body, _ = stream.fm.split(KB / r["relpath"])
            except Exception:                          # noqa: BLE001
                body = "(unreadable)"
            if n:
                out.append("\n───\n\n", FAINT)
            age = f"{r['age_days']}d" if r["age_days"] is not None else "—"
            out.append(f"{r['title'] or r['slug']}\n", f"bold {ACCENT}")
            tags = " ".join("#" + t for t in (r.get("tags") or []))
            out.append(f"{age} · {r['slug']}" + (f"   {tags}" if tags else "")
                       + "\n\n", FAINT)
            text = body.strip() or "(empty)"
            out.append(text + "\n")
            used += len(text)
        self.query_one("#previewtext", Static).update(out)
        self.query_one("#preview", VerticalScroll).scroll_home(animate=False)

    # ── responsive ──────────────────────────────────────────────────────────
    # ONE breakpoint. Stacking the list over the reader removed the need for
    # a middle mode: the only question left is whether the 22-column tag list
    # fits, and below 72 it is a third of the screen. Measured against what
    # the pane needs — prose under ~34 columns wraps into gibberish.
    TAGCOL_MIN = 72

    def _apply_layout(self, width: int, height: int | None = None) -> None:
        body = self.query_one("#body")
        mode = "" if width >= self.TAGCOL_MIN else "narrow"
        body.set_class(mode == "narrow", "narrow")
        # The drawer's cap, in rows. Content-sized below it, scrolling above:
        # three notes take three rows, forty scroll inside the cap instead of
        # pushing the reader off the bottom.
        h = height if height is not None else self.size.height
        if h:
            self.query_one("#queue", ListView).styles.max_height = \
                max(4, int(h * 0.45))
        if mode != getattr(self, "_layout_mode", None):
            self._layout_mode = mode
            self._paint_status()

    def on_resize(self, event) -> None:
        self._apply_layout(event.size.width, event.size.height)

    # ── the tag column ──────────────────────────────────────────────────────
    def _paint_tags(self) -> None:
        """The vocabulary, most-used first, with the unplaced queue pinned on
        top as a pseudo-tag.

        Pinned and not sorted in: "what has nobody decided about" is a
        different KIND of question from "what is this about", and it is the
        one with a deadline — an untagged note is what `cl inbox
        --prune-noise` feeds on. It should never sort down under `#log` just
        because there are more log notes than unplaced ones.
        """
        # Signature-diffed, like the rail's inbox rows. The vocabulary is 240
        # items and rebuilding that ListView cost ~170ms of the ~190ms every
        # refresh took — on a surface where refresh runs after every keystroke
        # that writes. The vocabulary changes only when a tag is applied, so
        # the common repaint is a no-op.
        # view_tag is deliberately NOT in this signature. It only decided
        # which row rendered bold — and the cursor bar already says which tag
        # you are on, so the bold was redundant with it. Including it meant
        # every hover rebuilt all 240 rows, which is most of what made the
        # preview flash.
        sig = (tuple(self.vocab.items()), tuple(getattr(self, "parents", {}).items()),
               self._tag_filter, self._n_unplaced)
        if sig == getattr(self, "_tag_sig", None):
            return
        self._tag_sig = sig
        lst = self.query_one("#taglist", ListView)
        keep = lst.index
        # Restoring the highlight below fires Highlighted, which the preview
        # listens to — so a repaint would re-select the tag the cursor happens
        # to be sitting on and undo the switch that caused the repaint. `g`
        # bounced straight back to the previous tag because of it. The preview
        # must answer to the USER moving, never to this method putting the
        # cursor back where it was.
        lst.clear()
        self.tag_names = [triage.UNTAGGED]
        t = Text()
        t.append("unplaced", ACCENT)
        t.append(f"  {self._n_unplaced}".rjust(10), FAINT)
        lst.append(ListItem(Static(t)))
        q = self._tag_filter.lower()
        # Parents are merged into the same most-used-first order rather than
        # grouped above their children. The column answers "what is this
        # about", and `projects` (13 notes) is a bigger answer than `blog`
        # (10) — sorting it away from its weight to sit beside its children
        # would make the list two lists.
        # parents LAST, so a tag that also has children takes the rolled-up
        # count rather than the flat one — one row, and its number is what
        # selecting it will show.
        merged = sorted({**self.vocab, **getattr(self, "parents", {})}.items(),
                        key=lambda kv: -kv[1])
        for name, count in merged:
            if q and q not in name.lower():
                continue
            self.tag_names.append(name)
            row = Text()
            # A trailing slash is the whole marker, and it is enough: it reads
            # as a path prefix, which is exactly what it is. It means "this
            # row includes what is under it" — true both for `projects/`,
            # which nobody has ever applied as a word, and for `blog/`, which
            # is a real tag that also has children.
            parent = name in getattr(self, "parents", {})
            label = (name + "/" if parent else name)[:15]
            row.append(label, ACCENT if parent else "#d8d4cf")
            # Measured off the LABEL, not the name: the parent's trailing
            # slash is a character too, and counting the name left every
            # rollup's number one column out of step with the rest.
            row.append(f"{count}".rjust(max(1, 18 - len(label))), FAINT)
            lst.append(ListItem(Static(row)))
        shown = len(self.tag_names) - 1
        self.query_one("#taghdr", Label).update(
            f"TAGS — {shown}" + (f" / {len(self.vocab)}" if q else ""))
        if self.tag_names:
            lst.index = min(keep or 0, len(self.tag_names) - 1)

    async def _add_picked(self, tag: str, allow_new: bool = False) -> None:
        """Add one tag to the note we are picking for, then hand focus back to
        its chips — you are almost always adding two, and returning to the
        list would make the second one a journey."""
        slug, self._picking = self._picking, None
        self.query_one("#tagfilter", Input).value = ""
        self._tag_filter = ""
        self._close_tags_if_narrow()
        # NO `or self._current()` FALLBACK. The note being tagged is named by
        # slug, and if it is not in the current rows the honest answer is that
        # we lost it — not "tag whatever the cursor happens to be on". The
        # fallback is how a pick that had drifted out of view ended up
        # addressing an unrelated note, which is a silent mis-tag and by far
        # the worst outcome available here.
        row = next((r for r in self.rows if r["slug"] == slug), None)
        if not row:
            self._paint_status()
            self.notify(f"lost track of {slug or 'the note'} — nothing tagged",
                        severity="warning")
            return
        tags = list(row.get("tags") or [])
        if stream.norm_tag(tag) in [stream.norm_tag(t) for t in tags]:
            self.notify(f"already tagged #{tag}")
        else:
            flags = ["--tag", tag] + (["--new"] if allow_new else [])
            res = self._write(row, *flags)
            if res.get("ok"):
                self._undo.append(("retag", row["slug"],
                                   [stream.norm_tag(t) for t in tags]))
                if not tags:
                    triage.clear(row["slug"])
                self.notify(f"+{tag}")
        await self.reload()
        r = self._current()
        chips = self.query_one("#tagchips", TagChips)
        chips.show((r or {}).get("tags") or [],
                   (r or {}).get("suggested") or [])
        self.set_focus(chips)

    async def _switch_view(self, tag) -> None:
        self._close_tags_if_narrow()
        self._load_view(tag)
        self.set_focus(self.query_one("#queue", ListView))

    def action_cycle_child(self, step: int = 1) -> None:
        """Tab — walk the family, and the walk IS the opening.

        No Enter. On a tag page the only question is which slice you are
        looking at, and a selection that needs confirming is a second keypress
        to answer a question you already answered by arriving. Same bargain
        the tag column already makes, where moving the cursor loads the tag.

        Loads DIRECTLY, and moves the tag cursor afterwards to keep the column
        in step. Going through the cursor alone looked tidier — one
        implementation of "show this tag" — but that path is `_preview_tag`,
        which is deliberately gated on the tag column having FOCUS so a
        repaint cannot bounce the view around. Tab is pressed with focus on
        the queue, so the gate held and the strip never moved: the left picker
        walked while the page it was supposed to be selecting stayed put.
        Setting the index afterwards still fires Highlighted, and that handler
        returning early is now exactly what we want from it.
        """
        if isinstance(self.focused, Input) or not self._family():
            # Tab in a text box is still Tab, and on a view with no children
            # there is no family to walk — leave Textual's meaning alone.
            (self.screen.focus_next if step > 0
             else self.screen.focus_previous)()
            return
        fam = self._family()
        cur = self.view_tag if self.view_tag in fam else fam[0]
        nxt = fam[(fam.index(cur) + step) % len(fam)]
        self._load_view(nxt)
        if nxt in self.tag_names:               # keep the column in step
            lst = self.query_one("#taglist", ListView)
            want = self.tag_names.index(nxt)
            if lst.index != want:
                lst.index = want

    async def action_view_untagged(self) -> None:
        """`g` — back to the unplaced queue from anywhere.

        The CURSOR comes back too. The unplaced row is pinned at the top of
        the tag column, and `g` used to change the view without moving the
        highlight — so the column went on pointing at `projects/hearth` while
        the queue beside it listed unplaced notes, and the one widget whose
        job is to say where you are was the one saying the wrong thing.

        Setting the index emits Highlighted, which loads the row under it —
        and here that IS the unplaced view, so the two paths agree instead of
        fighting. (They have fought before: a repaint that restored the
        highlight used to bounce `g` straight back to the previous tag.)
        """
        lst = self.query_one("#taglist", ListView)
        if lst.index != 0:
            lst.index = 0                       # the pinned `unplaced` row
        if self.view_tag is not triage.UNTAGGED:
            await self._switch_view(triage.UNTAGGED)
        else:
            self.set_focus(self.query_one("#queue", ListView))

    def action_col_left(self) -> None:
        """`h`. At `narrow` the tag column is not on screen, so this opens it
        rather than focusing something invisible — `h` and `/` converge on the
        same door when there is only one."""
        if self._layout_mode == "narrow":
            self.action_focus_filter()
            return
        self.set_focus(self.query_one("#taglist", ListView))

    def action_col_right(self) -> None:
        self.set_focus(self.query_one("#queue", ListView))

    def action_focus_filter(self) -> None:
        if self._layout_mode == "narrow":
            self.query_one("#body").add_class("tags-open")
        self.query_one("#tagfilter", Input).focus()

    def _close_tags_if_narrow(self) -> None:
        if self._layout_mode == "narrow":
            self.query_one("#body").remove_class("tags-open")

    def _paint_status(self) -> None:
        if self._picking:
            # The tag column means something different right now, and a column
            # that silently changes verb is how a navigation keystroke becomes
            # an edit.
            self.query_one("#status", Static).update(Text.assemble(
                ("PICK A TAG  ", f"bold {ACCENT}"),
                (f"for {self._picking}", ACCENT),
                ("   type to filter · ⏎ adds it · a new word is offered · "
                 "Esc cancels", FAINT)))
            return
        if self._readall:
            self.query_one("#status", Static).update(Text.assemble(
                ("READING  ", f"bold {ACCENT}"),
                ("unplaced" if self.view_tag is triage.UNTAGGED
                 else f"#{self.view_tag}", ACCENT),
                (f"  {len(self.rows)} notes as one page", DIM),
                ("   j/k scroll · z or esc back · read-only", FAINT)))
            return
        where = ("unplaced" if self.view_tag is triage.UNTAGGED
                 else f"#{self.view_tag}")
        mode = getattr(self, "_layout_mode", None)
        # The hint is the first thing to go when the width does. At `narrow`
        # the tag column is hidden entirely, so `/` is the only way to reach
        # it and is the one key that must still be advertised.
        hint = ("  j/k move · ⏎ deeper · esc back · z read all · / filter · g unplaced · ?"
                if not mode else "  j/k · ⏎ deeper · esc back · / tags · o writer · ?")
        self.query_one("#status", Static).update(Text.assemble(
            ("NAV ", f"bold {ACCENT}") if mode else ("NAVIGATOR  ", f"bold {ACCENT}"),
            (where, ACCENT),
            (f"  {len(self.vocab)} tags", DIM),
            (hint, FAINT)))
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
            self.query_one("#tagchips", TagChips).show([])
            empty = ("nothing unplaced — every note carries tags"
                     if self.view_tag is triage.UNTAGGED
                     else f"nothing tagged #{self.view_tag}")
            self.query_one("#previewtext", Static).update(Text(empty, style=DIM))
            return
        self.query_one("#detailhdr", Label).update(row["slug"])
        self.query_one("#title", Static).update(
            Text(row["title"] or "(no title)", no_wrap=False, overflow="fold"))
        # Not the relpath: at the deck's ~41 columns "2026-03-28 00:00 ·
        # writing/_stream/2026/03/…" truncates to the half that says nothing,
        # and the header above already carries the note's identity.
        age = f"{row['age_days']}d old" if row["age_days"] is not None else ""
        meta = Text(" · ".join(x for x in (row["created"] or "undated", age) if x),
                    style=FAINT)
        # The note's OTHER tags, in a tag view. Without them a note read under
        # #hearth looks like it belongs only to #hearth, and the one thing a
        # navigator has to show is that a note lives in several places at once
        # — that is the whole argument for tags over directories.
        self.query_one("#tagchips", TagChips).show(
            row.get("tags") or [], row.get("suggested") or [])
        others = [t for t in row.get("tags", []) if t != self.view_tag]
        if others:
            meta.append("   ")
            meta.append(" ".join("#" + t for t in others[:6]), ACCENT)
        self.query_one("#meta", Static).update(meta)

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


    def _focused_list(self) -> ListView:
        """j/k move within whichever column has focus. Hard-wiring them to the
        queue made the tag column navigable only by arrow keys, which in a vim
        surface reads as the column being decorative."""
        tags = self.query_one("#taglist", ListView)
        return tags if self.focused is tags else self.query_one("#queue", ListView)

    def action_down(self) -> None:
        if self._readall:
            self.query_one("#preview", VerticalScroll).scroll_down(animate=False)
            return
        self._focused_list().action_cursor_down()

    def action_up(self) -> None:
        if self._readall:
            self.query_one("#preview", VerticalScroll).scroll_up(animate=False)
            return
        self._focused_list().action_cursor_up()

    def on_descendant_focus(self, event) -> None:
        """A list that gains focus gets a cursor, if it has not got one.

        `ListView.index` is None until something moves it, and `clear()` is
        deferred in Textual — so a list could arrive focused and populated with
        no row current, and the first j/k did nothing but reveal a cursor that
        should already have been there. Coming from the tag picker into the
        queue was the visible case: the page opened with nothing selected.

        Never MOVES an existing cursor — that is law 4, and a repaint or a
        return from the editor has to land you back on the row you were on.
        """
        w = getattr(event, "widget", None)
        if isinstance(w, ListView) and w.index is None and len(w):
            w.index = 0

    def on_list_view_highlighted(self, event) -> None:
        # A Highlighted can land while the app is coming down — clearing a
        # ListView on quit emits one, and by the time it is delivered the
        # widgets it refers to may be gone. Nothing here is worth an exception
        # on the way out.
        if not self.is_running:
            return
        try:
            if getattr(event.list_view, "id", "") == "taglist":
                self._preview_tag(event.list_view.index)
                return
            self._paint_detail()
        except NoMatches:
            return

    # ── live preview ────────────────────────────────────────────────────────
    def _preview_tag(self, i) -> None:
        """Moving over a tag LOADS it. No Enter required.

        Enter was a step that bought nothing: you cannot tell whether a tag is
        the one you want without seeing what is under it, so the commit was
        always guesswork followed by a correction. Highlight IS the query.

        Debounced, because holding `j` down the vocabulary would otherwise
        fire one store read per row. 90ms is under the point where a pause
        reads as lag and above a key-repeat interval, so a scroll costs one
        query at the row you actually stop on. Enter still exists and now
        means "commit and move to the notes" — the focus change, not the load.
        """
        # Only when the tag list actually has focus. A repaint restores the
        # highlight, and ListView.clear() is deferred, so the resulting
        # Highlighted event lands AFTER any flag this method could set — `g`
        # bounced straight back to the tag the cursor was resting on. Focus is
        # the honest signal: if you are not in this column, you did not move
        # in it.
        lst = self.query_one("#taglist", ListView)
        if self.focused is not lst or i is None or i >= len(self.tag_names):
            return
        # PICKING IS NOT BROWSING. While a tag is being chosen FOR a note, the
        # column is a picker, and loading each tag you move over swaps the
        # queue out from under the pick — taking the note you were tagging off
        # screen and out of `self.rows`. That is the whole failure: filter to
        # `idea`, press Enter with four matches, land on the list, and the
        # view silently became a tag browser while the note stayed untagged.
        if self._picking:
            return
        tag = self.tag_names[i]
        if tag == self.view_tag:
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._preview_timer = self.set_timer(0.09, lambda: self._load_view(tag))

    def _load_view(self, tag) -> None:
        """Show a tag's notes WITHOUT taking focus and WITHOUT re-reading the
        store.

        A cursor move cannot have changed any file, so re-running
        stream.load() for one — 238 files — and then rebuilding the 240-row
        tag list was paying the full refresh price to answer a question the
        last read already contains. The store snapshot is reused while it is
        fresh; only the note list is rebuilt.

        Not a coroutine any more: there is nothing to await, and going through
        run_worker added a frame boundary of its own.
        """
        if tag == self.view_tag:
            return
        if not self._items or time.monotonic() - self._items_at > 2.0:
            self._items = stream.load(include_daily=True)
            self._items_at = time.monotonic()
        self.view_tag = tag
        self.rows = triage.queue(self._items, tag=tag)
        self._repaint_queue(keep=0)

    def on_list_view_selected(self, event) -> None:
        """Enter means "go one level deeper", and it never leaves the app.

        TAGS -> that tag's notes -> that note's tags. Three rungs, one key,
        and Esc climbs back up each of them — so the whole surface is j/k to
        move, Enter to descend, Esc to return, at every level. Opening the
        file is `o`, which is a different KIND of act: it suspends this app
        and hands you to the writer.

        Enter briefly opened the writer here. That made the last rung
        inconsistent with the first two and put the one irreversible-feeling
        action (leaving) on the most-pressed key."""
        event.stop()
        if getattr(event.list_view, "id", "") == "taglist":
            i = event.list_view.index
            if self._picking:
                if i is not None and 0 < i < len(self.tag_names):
                    self.run_worker(self._add_picked(self.tag_names[i]))
                return
            if i is not None and i < len(self.tag_names):
                # The view is already loaded by the highlight preview; Enter
                # is the focus change, and only re-queries if you got here
                # faster than the debounce.
                self.run_worker(self._switch_view(self.tag_names[i]))
            return
        self.action_focus_tags()

    def action_focus_tags(self) -> None:
        """`i` — edit this note's tags.

        Focus moves to the CHIPS, not to a text field. See TagChips: tags are
        a set, and editing a comma-separated serialization of one was the
        thing that kept sending him back to the frontmatter.
        """
        row = self._current()
        if not row:
            return
        chips = self.query_one("#tagchips", TagChips)
        chips.show(row.get("tags") or [], row.get("suggested") or [])
        chips.cursor = 0
        self.set_focus(chips)

    # ── chip verbs ──────────────────────────────────────────────────────────
    async def on_tag_chips_remove(self, event: TagChips.Remove) -> None:
        row = self._current()
        if not row:
            return
        await self._apply(row, [t for t in (row.get("tags") or [])
                                if t != event.tag], stay=True)

    def on_tag_chips_add(self, event: TagChips.Add) -> None:
        """`a` — pick a tag from the vocabulary that is already on screen.

        The TAGS column is the picker: filtered as you type, showing how many
        notes each tag already has, which is the whole reuse affordance. A tag
        you type that matches nothing is still offered — `cl stream set` gets
        the last word and refuses a near-duplicate with candidates.
        """
        row = self._current()
        if not row:
            return
        self._picking = row["slug"]
        self._paint_status()
        self.action_focus_filter()

    async def on_tag_chips_take(self, event: TagChips.Take) -> None:
        """⏎ on the chips — accept the suggestion now that it is visible there.
        Routed to the same action `a` uses on the queue, so there is one
        implementation of "take what was proposed"."""
        await self.action_accept()

    def on_tag_chips_leave(self, event: TagChips.Leave) -> None:
        self.set_focus(self.query_one("#queue", ListView))

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
        elif kind == "retag":
            # Restore the whole set. An undo that only untagged what was added
            # could not take back a REMOVAL, which is half of what the box now
            # does.
            slug, before = rest
            cur = next((i["tags"] for i in stream.load(include_daily=True)
                        if i["slug"] == slug), [])
            cur = [stream.norm_tag(t) for t in cur]
            add = [t for t in before if t not in cur]
            rm = [t for t in cur if t not in before]
            flags = []
            if add:
                flags += ["--tag", ",".join(add), "--new"]
            if rm:
                flags += ["--untag", ",".join(rm)]
            if not flags:
                self.notify("nothing to undo on that note")
                return
            res = _run("stream", "set", slug, *flags)
            msg = (f"restored {' '.join('#' + t for t in before) or '(no tags)'}"
                   if res.get("ok") else f"undo failed: {res.get('error')}")
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

    def _hermes_pane(self) -> str:
        """The Hermes pane's id, resolved by its `@app` option.

        NOT `{top-right}`. The deck's own rule is that panes are found by
        `@app`, never by position, and this file was the exception — which
        broke the moment M-4 started opening zoomed: `{top-right}` resolves to
        the ZOOMED pane, so a kickoff typed itself into the triage app instead
        of into Hermes. Position is not identity.
        """
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-s", "-F", "#{pane_id} #{@app}"],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:                              # noqa: BLE001
            return ""
        for line in out.splitlines():
            pid, _, app = line.partition(" ")
            if app.strip() == "hermes_triage":
                return pid
        return ""

    def _send_to_chat(self, text: str = "", reveal: bool = True) -> bool:
        """Type a line into the Hermes pane; optionally bring it on screen.

        `reveal=False` is the point of M-4 opening zoomed: the request goes
        out and the navigator keeps the full width. You do not need to watch
        the conversation, because the suggestions arrive in the queue on their
        own — `_poll_slots` notices the sidecar change within two seconds.
        Reading Hermes' reasoning is the only reason to look at the pane, so
        looking is now a choice rather than a permanent third of the screen.
        """
        if not os.environ.get("TMUX"):
            self.notify("only inside the Bridge deck — this talks to the "
                        "Hermes pane", severity="warning")
            return False
        pane = self._hermes_pane()
        if not pane:
            self.notify("no hermes_triage pane in this deck",
                        severity="warning")
            return False
        try:
            if text:
                # -l is literal: the text carries backticks and colons, and
                # without it tmux would read parts of the line as key names.
                subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text],
                               capture_output=True, timeout=5, check=True)
                subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                               capture_output=True, timeout=5, check=True)
            if reveal:
                # Unzoom explicitly — select-pane does NOT unzoom on its own,
                # so without this the pane is focused but still invisible.
                zoomed = subprocess.run(
                    ["tmux", "display", "-p", "#{window_zoomed_flag}"],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                if zoomed == "1":
                    subprocess.run(["tmux", "resize-pane", "-Z"],
                                   capture_output=True, timeout=5)
                subprocess.run(["tmux", "select-pane", "-t", pane],
                               capture_output=True, timeout=5, check=True)
            return True
        except Exception as exc:                       # noqa: BLE001
            self.notify(f"no pane to talk to: {exc}", severity="warning")
            return False

    def action_kickoff(self) -> None:
        """`C` — ask again, whatever the queue looks like. For a second pass
        over notes Hermes left empty the first time."""
        # `C` asks WITHOUT revealing the pane: the answer comes back into
        # this list by itself, so the full width is worth more than watching
        # it think.
        if self._send_to_chat(self.KICKOFF, reveal=False):
            self._asked = True
            self.notify("asked Hermes to work the queue — "
                        "suggestions land here on their own · c to watch")

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
        self.notify("j/k move · ⏎ descends (tag → its notes → its tags) · "
                    "esc climbs back · h/l jump columns · / filter tags · "
                    "g back to unplaced · "
                    "tags: j/k or w/b move · I/A first/last · d remove · a add · "
                    "on the note list: t todo · d trash (recoverable) · u undo · "
                    "c chat (first one starts a pass) · C re-ask · "
                    "z read the whole tag as one page (read-only) · "
                    "o open in the writer · r reload · q quit", timeout=14)

    # ── writing ─────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "tagfilter":
            typed = event.value.strip().lstrip("#")
            if self._picking and typed and typed not in self.vocab:
                # A tag the vocabulary has never seen. Offered rather than
                # refused here — `cl stream set` gets the last word and turns
                # a near-duplicate back with candidates, which is the one
                # place that judgement should live.
                await self._add_picked(typed, allow_new=True)
                return
            if self._picking:
                # Land on the first REAL tag, never on the `unplaced`
                # pseudo-row that heads the column — it is a view, not a tag,
                # and Enter on it did nothing at all, which reads as the key
                # being broken. With exactly one match, skip the list entirely:
                # you have already said which tag you mean by typing it.
                real = self.tag_names[1:]
                if len(real) == 1:
                    await self._add_picked(real[0])
                    return
                lst = self.query_one("#taglist", ListView)
                if real:
                    lst.index = 1
                self.set_focus(lst)
                return
            # Otherwise Enter jumps to the list it just narrowed, rather than
            # leaving you typing at a result you cannot reach.
            self.set_focus(self.query_one("#taglist", ListView))
            return
        row = self._current()
        if not row:
            return
        tags = [t.strip() for t in event.value.replace(" ", ",").split(",")
                if t.strip()]
        if not tags:
            self.set_focus(self.query_one("#queue", ListView))
            return
        await self._apply(row, tags)

    async def _apply(self, row: dict, tags: list, stay: bool = False) -> None:
        """`tags` is the note's COMPLETE new set, not an addition.

        Diffed against what it carries, so one Enter both adds and removes —
        `cl stream set` takes --tag and --untag in the same call, and doing it
        as one write means a note is never briefly half-retagged on disk.
        """
        before = [stream.norm_tag(t) for t in (row.get("tags") or [])]
        after, seen = [], set()
        for t in tags:                       # order-stable, de-duplicated
            n = stream.norm_tag(t)
            if n and n not in seen:
                seen.add(n)
                after.append(n)
        add = [t for t in after if t not in before]
        rm = [t for t in before if t not in after]
        if not add and not rm:
            self.notify("no change")
            self.set_focus(self.query_one("#queue", ListView))
            return
        flags = []
        if add:
            flags += ["--tag", ",".join(add)]
        if rm:
            flags += ["--untag", ",".join(rm)]
        res = self._write(row, *flags)
        if not res.get("ok"):
            return                     # text stays in the box, ready to fix
        # Undo restores the tag set that was there, which is the only thing
        # that can take back a removal as well as an addition.
        self._undo.append(("retag", row["slug"], before))
        if not before:
            # It was unplaced and now is not: its slot has no one left to
            # advise, and keeping it accumulates suggestions for notes nobody
            # will see again.
            triage.clear(row["slug"])
        bits = []
        if add:
            bits.append("+" + " +".join(add))
        if rm:
            bits.append("-" + " -".join(rm))
        self.notify(" ".join(bits))
        if stay:
            # A chip edit is a correction to the note you are READING, not a
            # decision that files it — advancing would yank the note out from
            # under you mid-edit.
            keep = self.query_one("#queue", ListView).index
            await self.reload()
            v = self.query_one("#queue", ListView)
            if self.rows:
                v.index = min(keep or 0, len(self.rows) - 1)
            r = self._current()
            chips = self.query_one("#tagchips", TagChips)
            chips.show((r or {}).get("tags") or [],
                       (r or {}).get("suggested") or [])
            self.set_focus(chips)
            return
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
        # `escape` only — `z` is already an App binding, and handling it here
        # too toggled read mode twice per press, which looked like the key
        # doing nothing at all.
        if self._readall and event.key == "escape":
            self.action_read_all()
            event.stop()
            return
        filt = self.query_one("#tagfilter", Input)
        if self.focused is filt:
            if event.key == "escape":
                if self._picking:
                    self._picking = None
                    self._close_tags_if_narrow()
                    self._paint_status()
                    # AFTER the refresh, not now: focusing the chips inside
                    # this handler puts them in focus while the same Escape is
                    # still being processed, and their own escape binding then
                    # fires and bounces you out to the queue.
                    self.call_after_refresh(
                        self.set_focus, self.query_one("#tagchips", TagChips))
                else:
                    self.set_focus(self.query_one("#taglist", ListView))
                event.stop()
            return
        if self.focused is self.query_one("#taglist", ListView) \
                and event.key == "escape":
            if self._picking:
                self._picking = None
                self._paint_status()
                self._close_tags_if_narrow()
                self.call_after_refresh(
                    self.set_focus, self.query_one("#tagchips", TagChips))
                event.stop()
                return
            self._close_tags_if_narrow()
            self.set_focus(self.query_one("#queue", ListView))
            event.stop()
            return
        # Esc on the NOTES climbs back to the tags — the last rung of the
        # ladder Enter descends. Without it the loop was one-way at the top:
        # Enter went tags -> notes -> chips, but Esc only came back as far as
        # the notes and then stopped, which reads as the key having died.
        if self.focused is self.query_one("#queue", ListView) \
                and event.key == "escape":
            self.action_col_left()
            event.stop()
            return
        # Single-letter bindings must not fire while typing in the filter;
        # Input eats printable keys itself, so only escape needs handling
        # above.

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tagfilter":
            self._tag_filter = event.value
            self._paint_tags()


def main() -> None:
    if not sys.stdin.isatty():
        print("cl triage --tui needs a terminal.", file=sys.stderr)
        raise SystemExit(1)
    os.environ.setdefault("COLORTERM", "truecolor")
    TriageApp().run()


if __name__ == "__main__":
    main()
