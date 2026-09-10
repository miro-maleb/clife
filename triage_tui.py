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

import collections
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
from textual.widgets.input import Selection

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
#searchbox { display: none; height: 3; }
#queue > ListItem { padding: 0 1; }
/* One row, one line. At the deck's ~27-column queue a wrapped title runs to a
   second unindented line and the list stops being scannable at a glance. */
#queue Static { text-wrap: nowrap; text-overflow: ellipsis; }
#queue > ListItem.--highlight { background: #241809; }
/* NB: the `.--highlight` rules elsewhere in this sheet are DEAD — Textual
   spells the class `-highlight`, one dash, so those four have never applied
   and the highlight you see is the theme's. Left alone rather than fixed
   here: making them live would change four colours at once, which is a
   separate decision from this one.

   The cursor of the column you are DRIVING is brighter than the one you are
   not. Two lists and a chip row share j/k, Enter and `d`, and which of them
   answers depended on a focus state with almost no representation on screen —
   that is what made `d` (remove a tag) one rung from `d` (trash the note)
   feel like a coin toss. Same amber, further up. */
#queue:focus > ListItem.-highlight   { background: #5c3f1c; }
#taglist:focus > ListItem.-highlight { background: #5c3f1c; }
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
        # `f` and not `/`, which already opens the TAG filter and has for as
        # long as this app has existed. Two boxes, two keys; the one you press
        # says which you meant, rather than the one you happen to be standing
        # in deciding for you.
        Binding("f", "search", "Search notes", show=False),
        Binding("F", "search_all", "Search the whole store", show=False),
        Binding("g", "view_untagged", "Unplaced", show=False),
        # `T` — the pool, from anywhere. `t` puts one note in it; `T` goes
        # there. The same pairing `g` has with the unplaced queue, and the
        # reason the pool does not need a deck layout of its own.
        Binding("T", "view_pool", "The pool", show=False),
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
        # `+` pins, `-` unpins. NOT ctrl+enter, which was the first spelling
        # and had to go: Textual asks the terminal for the KITTY keyboard
        # protocol, while tmux's `extended-keys` implements modifyOtherKeys
        # (the xterm scheme) -- different requests, so tmux never answers and
        # ctrl+enter arrives as a bare \r. That is worse than a dead key,
        # because plain Enter on the tag column DESCENDS into the tag: the
        # alias did not fail quietly, it did something else. One key that
        # always works beats two where the second lies.
        Binding("plus", "pin_tag", "Pin this tag", show=False,
                priority=True),
        Binding("minus", "unpin", "Unpin", show=False, priority=True),
        Binding("z", "read_all", "Read the whole tag", show=False),
        Binding("i", "focus_tags", "Tag", show=False),
        Binding("a", "accept", "Accept suggestion", show=False),
        Binding("t", "todo", "Mark todo", show=False),
        # `s` — sooner on, sooner off. ONE key for both directions because it
        # is one bit: later is the absence of the tag, so there is no second
        # state to name and nothing that can contradict it.
        Binding("s", "sooner", "Sooner / later", show=False),
        # `p` — this outlived being a task. Strips the todo tags and opens the
        # picker seeded to `projects/`, rather than inventing a bare `project`
        # tag beside the namespace that already exists. You still name it,
        # because only you know which project it is.
        Binding("p", "promote", "Make a project", show=False),
        Binding("d", "trash", "Trash", show=False),
        Binding("u", "undo", "Undo last", show=False),
        Binding("c", "chat", "Jump to chat", show=False),
        Binding("C", "kickoff", "Ask Hermes again", show=False),
        Binding("o", "open", "Open in writer", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Keys", show=False),
    ]

    def __init__(self, view=None) -> None:
        super().__init__()
        # The tag to open on, or None for the unplaced queue. `--view todo` is
        # the pool; nothing else about this app changes.
        self._open_on = view
        self.rows: list[dict] = []
        self.vocab: dict = {}
        # None = the unplaced queue (what this app has always shown); a string
        # = that tag's notes. One field, and every view is a query over it.
        self.view_tag = triage.UNTAGGED
        # Tags ANDed with the view. One tag answers "what is this about";
        # two answer questions neither can alone -- practice AND money, todos
        # that are also house work. Empty is the ordinary single-tag view, so
        # nothing about the app changes until you pin something.
        self.pins: list = []
        # The view each pin replaced, so unpinning returns where you were
        # rather than dumping you in `all`. Parallel to `pins`, pushed and
        # popped with it.
        self._pin_return: list = []
        # The neighbour list Tab is currently walking, anchored to the tag it
        # was entered from. Children are a stable list keyed off the root, so
        # Tab and shift+Tab agree; neighbours are a GRAPH, and recomputing
        # them from wherever you just landed made Tab a one-way walk with no
        # inverse -- writing -> buddhism, then shift+Tab went somewhere else
        # entirely because buddhism's neighbours are a different list.
        self._fam_anchor: list = []
        # The anchor tag Tab pinned on your behalf while walking a
        # neighbourhood, or None. Tracked rather than inferred so that
        # returning home cannot remove a pin YOU put there.
        self._walk_pin = None
        # The tag at position 0 of an anchored walk, when there is one. A walk
        # started from #writing has writing as home; a walk started from a
        # query that is already several pins deep has no home, only facets.
        self._fam_home = None
        # Free-text narrowing, ANDed with everything else. A TERM, not a mode:
        # the page is already "a view is a query", and search is one more
        # predicate beside the tag and the pins. Making it a mode would forbid
        # `#writing` + "dumpling", and forbid the thing that makes search worth
        # having here -- searching first, then reading off the facet strip
        # which tags the matches carry, and pinning one. Search is also the
        # reason not to tag everything: what is findable by reading does not
        # need a word spent on it.
        self.search = ""
        self.tag_names: list = []      # what the left column currently lists
        self._tag_filter = ""
        self._n_unplaced = 0
        self._n_all = 0
        self._tag_sig = None
        self._layout_mode = None
        self._preview_timer = None
        self._items: list = []
        self._items_at = 0.0
        # slug of the note we are picking a tag FOR, or None when the tag
        # column is being used to navigate. One flag, because the column does
        # both jobs and must not guess which.
        self._picking = None
        # `dd` — the slug the first `d` armed, and the timer that forgets it.
        self._armed_trash = None
        self._trash_timer = None
        self._readall = False
        self._help = False
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
                    yield Input(placeholder="search notes…", id="searchbox")
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
        if self._open_on:
            self._load_view(self._open_on)
        self.set_focus(self.query_one("#queue", ListView))
        self.call_after_refresh(self._paint_hdr)   # now there is a width
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
        pool = self.view_tag == POOL_TAG
        for r in self.rows:
            age = f"{r['age_days']}d" if r["age_days"] is not None else "—"
            t = Text()
            if pool:
                # SOONER IS A MARK, NOT A SECTION. A band header would have to
                # be a row in the ListView, and a row that is not a note is the
                # pseudo-row problem this app has already been bitten by — `a`,
                # `dd` and Enter would all need to know it is not a note. A
                # column of marks sorts the same and cannot be selected.
                soon = SOONER in [stream.norm_tag(x) for x in (r.get("tags") or [])]
                t.append("▲ " if soon else "  ", ACCENT if soon else FAINT)
            t.append(f"{age:>5} ", FAINT)
            t.append(r["title"][:44] or r["slug"][:44])
            if r["suggested"]:
                t.append(f"  {' '.join(r['suggested'])}", ACCENT)
            elif r["note"]:
                t.append("  ?", ACCENT)
            out.append(ListItem(Static(t)))
        return out

    def _cursor_to(self, idx: int) -> None:
        """Put the queue cursor on a row, and mean it.

        Re-asserted after the next refresh because `ListView.clear()` is
        deferred in Textual: the repaint that rebuilt this list can land AFTER
        the index is set and reset it to None. That is why the cursor vanished
        after `s` — the toggle worked, the row moved, and the cursor was gone
        until you pressed j.
        """
        def place() -> None:
            v = self.query_one("#queue", ListView)
            if not len(v):
                return
            v.index = max(0, min(idx, len(v) - 1))
        place()
        self.call_after_refresh(place)

    def _queue_mode(self) -> bool:
        """Is this a queue you are WORKING, or a subject you are BROWSING?

        The distinction the pool taught us, and it was never about the pool.
        A queue is a view whose membership you are actively changing — the
        unplaced backlog and the todo pool — and its verbs exist to take
        things OUT of it. So the list is the thing you care about: it stays
        put when an item leaves, and your hands stay on it.

        A tag view is a subject you are reading. There the NOTE is the thing
        you care about, so the view follows it when it moves and focus lands
        on its chips, where you are probably adding a second word.

        Getting this backwards is what made `t` in the pool kick you out of
        the pool, and it did the same in triage: tagging an unplaced note
        yanked you into that tag's view mid-pass.
        """
        return self.view_tag is triage.UNTAGGED or self.view_tag == POOL_TAG

    def _is_soon(self, row) -> bool:
        return SOONER in [stream.norm_tag(x) for x in (row.get("tags") or [])]

    def _order_rows(self) -> None:
        """In the pool, `#sooner` floats to the top. Everywhere else the
        engine's order stands — oldest-first when unplaced (the backlog is the
        problem), newest-first in a tag view."""
        if self.view_tag == POOL_TAG:
            self.rows.sort(key=lambda r: not self._is_soon(r))

    def _cooccurring(self, limit: int = 12) -> list:
        """Tags that appear ON the notes currently in view, most-used first.

        Computed from `self.rows`, never from the whole store: the question is
        "what else is on THESE notes", and answering it from the vocabulary
        would offer tags whose intersection with the view is empty -- a menu
        of dead ends, which is the failure mode of every faceted filter that
        does not recount.
        """
        seen = collections.Counter()
        for r in self.rows:
            for t in r["tags"]:
                seen[t] += 1
        drop = set(self.pins) | ({self.view_tag} if isinstance(self.view_tag, str) else set())
        # A pin's own children are not a facet of it: standing on `todo` with
        # `todo/sooner` offered reads as a sibling when it is a subset.
        out = [t for t in seen
               if t not in drop and not any(t.startswith(str(p) + "/") for p in drop if p)]
        out.sort(key=lambda t: (-seen[t], t))
        return out[:limit]

    def _family(self) -> list:
        """[parent, *children] for whatever view you are on, or [].

        Keyed off the TOP segment, so standing on `blog/kids` offers the same
        family as standing on `blog` — you are in one subject either way, and
        having Tab mean different things depending on which rung you entered
        from is the kind of modal surprise this app keeps removing.
        """
        tag = self.view_tag
        real = isinstance(tag, str) and tag and tag != triage.ALL
        # The anchor wins over children. Walking `writing`'s neighbours and
        # landing on `projects/clife`, the children rule would hand back
        # `projects`' family and the walk would fall into it and never come
        # out -- which is the one-way trip that started this. While a
        # neighbourhood is being walked, it IS the family.
        if self._fam_anchor and (tag in self._fam_anchor or not real):
            return list(self._fam_anchor)
        if real:
            root = tag.split("/")[0]
            kids = sorted(k for k in self.vocab if k.startswith(root + "/"))
            if kids:
                return [root] + kids
        near = self._cooccurring()
        if not near:
            return []
        # From a bare tag view, that tag is HOME: position 0, and a full lap
        # comes back to it. From a query that is already pinned some levels
        # deep there is no home to come back to -- the panel is purely the
        # facets of what you have built, and `-` is the way back out.
        if real:
            return [tag] + near
        # A search with no tag is exactly when the facets matter most: 34
        # notes match "zen", and the strip says which tags they carry so you
        # can pin one. Gating this on `pins` alone left search results with no
        # strip -- and with no strip, Tab falls through to focus_next and the
        # keys you press afterwards land somewhere else entirely.
        return near if (self.pins or self.search) else []

    # What each view can DO, longest form first. The caller takes the first
    # that fits, the way the rail's inbox header does — a hint clipped
    # mid-word spends width on nothing and loses the count off the end too.
    #
    # Order inside a line is the order you reach for them. `dd delete` is last
    # everywhere because it is the one you want to have read before pressing,
    # and `a accept` is first on the unplaced queue because on a good day it
    # is the only key you press.
    VERBS = {
        "pool": ["s sooner · p project · t out · o open · dd delete",
                 "s sooner · p project · t out · dd delete",
                 "s sooner · t out · dd",
                 "s · t · dd"],
        "unplaced": ["a accept · i tag · t todo · p project · o open · dd delete",
                     "a accept · i tag · t todo · p project · dd delete",
                     "a accept · i tag · t todo · dd",
                     "a · i · t · dd"],
        "tag": ["i tag · t todo · o open · dd delete",
                "i tag · t todo · dd delete",
                "i tag · dd"],
    }

    def _view_label(self) -> str:
        """What to call the current view in a header.

        Two of the three views are not tags, so `"#" + view_tag` is wrong for
        both -- it used to print a bare `#None` before the unplaced branch was
        special-cased everywhere, and `all` would have printed `#*`. One
        function so the next pseudo-view only has to be added once."""
        base = ("unplaced" if self.view_tag is triage.UNTAGGED
                else "all" if self.view_tag == triage.ALL
                else "#" + str(self.view_tag))
        if self.pins:
            # `all` is what is left when the pins ARE the query, and printing
            # it adds a word that narrows nothing: `#buddhism + all + "zen"`
            # says less than `#buddhism + "zen"`.
            terms = ["#" + p for p in self.pins]
            if self.view_tag != triage.ALL:
                terms.append(base)
            base = " + ".join(terms)
        if self.search:
            base += f' + "{self.search}"'
        return base

    def _paint_hdr(self) -> None:
        """The queue header: what you are looking at, then what you can do to
        it. Its own method because the verbs depend on the COLUMN's width, and
        the first paint happens before there is a layout to measure — so this
        has to be callable again once there is one, and on every resize."""
        if self.view_tag == POOL_TAG:
            n_soon = sum(1 for r in self.rows if self._is_soon(r))
            hdr = f"POOL — {len(self.rows)} · {n_soon} sooner"
        elif self.view_tag is triage.UNTAGGED:
            filled = sum(1 for r in self.rows if r["suggested"] or r["note"])
            hdr = f"UNPLACED — {len(self.rows)} · {filled} suggested"
        elif self.view_tag == triage.ALL:
            hdr = f"ALL — {len(self.rows)} · {self._n_unplaced} unplaced"
        else:
            hdr = f"#{self.view_tag} — {len(self.rows)}"
        if self.pins or self.search:
            hdr = f"{self._view_label().upper()} — {len(self.rows)}"
        try:
            self.query_one("#queuehdr", Label).update(self._with_verbs(hdr))
        except Exception:  # noqa: BLE001 — not composed yet
            pass

    def _with_verbs(self, hdr: str) -> str:
        """Append what this view can do, trimmed to what the column has room
        for. The pool advertised its keys and triage did not, which is the
        whole reason `t` on an unplaced note was a surprise rather than a
        tool — the key worked, nobody had said so."""
        which = ("pool" if self.view_tag == POOL_TAG
                 else "unplaced" if self.view_tag is triage.UNTAGGED else "tag")
        # `all` is a browse view: it holds unplaced notes too, but `a accept`
        # there would be offered on notes that have nothing to accept.
        # The COLUMN's width, not the label's. `.hdr` sizes to its content, so
        # measuring the label asks "does this text fit inside itself" and the
        # answer is always no room to spare.
        try:
            room = self.query_one("#queuecol").size.width - 2   # padding
        except Exception:  # noqa: BLE001 — before the first layout
            room = 0
        if room <= 0:
            return hdr
        for hint in self.VERBS[which]:
            if room >= len(hdr) + len(hint) + 5:
                return f"{hdr}  ·  {hint}"
        return hdr

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
        kids = len(fam) > 1 and str(fam[1]).startswith(str(fam[0]) + "/")
        if kids:
            t.append("children  ", FAINT)
        else:
            # With pins up the strip IS the query builder, so it says what has
            # been built. `also` alone would leave the pins visible only in
            # the header, one line away from the thing they are narrowing.
            for p in self.pins:
                t.append(f"{p} ", ACCENT)
                t.append("+ ", FAINT)
            t.append("also  ", FAINT)
        for i, name in enumerate(fam):
            # The root is shown as `all`, because selecting it is not picking
            # a child — it is the rollup, and calling it `blog` beside
            # `blog/kids` would read as a sibling of its own children.
            if kids:
                label = "all" if i == 0 else name.split("/", 1)[1]
            else:
                label = name          # neighbours are whole tags, not suffixes
            # Every number is what you would SEE if you went there: the
            # pins already in the query, plus the tag you are standing on,
            # plus this one. So `hearth 7` in #writing's strip means seven
            # notes are both -- not that hearth has 56 of its own, which is
            # what the tag column already says two inches to the left. A strip
            # headed "also" whose numbers ignore the "also" tells you nothing.
            home = None if kids else (
                self._fam_home or (fam[0] if fam[0] == self.view_tag else None))
            extra = [home] if (home and home != name
                               and home not in self.pins) else []
            n = len(triage.queue(self._items, tag=name,
                                 pins=list(self.pins) + extra,
                                 search=self.search))
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
        self._paint_hdr()
        self._paint_children()
        self._paint_status()
        self._paint_detail()

    async def reload(self) -> None:
        """Read from the modules, not the CLI: this is a read of live files on
        every keystroke-driven refresh, and paying a subprocess for it would
        make the list lag the write that caused it. Writes still go out through
        `cl` — read cheap, write guarded."""
        # A reload ends any walk in progress. The anchor exists to hold a
        # neighbourhood STILL while Tab steps through it, and the store just
        # changed underneath -- so the frozen list is now the one thing on
        # screen that cannot show what you just did. Adding `letter` to a note
        # in #writing left it missing from #writing's own strip, because the
        # strip was still answering from the list captured before the write.
        self._fam_anchor = []
        self._fam_home = None
        # ONE read of the store per refresh, shared four ways: the rows, the
        # vocabulary, the unplaced count, and the preview, which reuses it
        # rather than re-reading 238 files to answer a cursor move.
        self._items = stream.load(include_daily=True)
        self._items_at = time.monotonic()
        self.rows = triage.queue(self._items, tag=self.view_tag, pins=self.pins,
                                 search=self.search)
        self._order_rows()
        self.vocab = stream.vocabulary(self._items)
        self.parents = _rollups(self._items)
        self._n_unplaced = sum(1 for i in self._items if not i["tags"])
        self._n_all = len(self._items)
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
        if self._help:                      # `z` out of help closes help
            self.action_help()
            return
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
        head = self._view_label()
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
        # The verb hints are measured against the column, so a resize can make
        # room for a longer one or take the room away. On a TIMER rather than
        # call_after_refresh: the new geometry is not in place by the next
        # refresh, so measuring then reads the old width and the hint stays
        # too long for the column it is now in.
        self.set_timer(0.15, self._paint_hdr)

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
               self._tag_filter, self._n_unplaced, self._n_all, tuple(self.pins))
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
        # Both pinned rows are VIEWS rather than tags, and PINNED_VIEWS is
        # the one place that says how many of them there are -- the `_picking`
        # path below has to skip exactly this many rows to land on a real tag,
        # and it used to say `[1:]` with the count written into it.
        self.tag_names = [triage.UNTAGGED, triage.ALL]
        for label, count in (("unplaced", self._n_unplaced),
                             ("all", self._n_all)):
            t = Text()
            t.append(label, ACCENT)
            t.append(f"  {count}".rjust(10 + len("unplaced") - len(label)), FAINT)
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
        # With pins up, the column stops being the vocabulary and becomes the
        # FACETS of what is pinned -- only tags that still leave notes, with
        # the count of the intersection rather than of the whole store. A
        # column that kept offering `recipe` while `recipe + hearth` is empty
        # is a menu of dead ends.
        if self.pins:
            live = collections.Counter()
            for r in self.rows:
                for t in r["tags"]:
                    live[t] += 1
            merged = [(k, v) for k, v in sorted(live.items(), key=lambda kv: (-kv[1], kv[0]))
                      if k not in self.pins]
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
        shown = len(self.tag_names) - PINNED_VIEWS
        self.query_one("#taghdr", Label).update(
            f"TAGS — {shown}" + (f" / {len(self.vocab)}" if q else ""))
        if self.tag_names:
            lst.index = min(keep or 0, len(self.tag_names) - 1)
            # While a filter is up, park the cursor on the first REAL tag
            # rather than on a pinned view row. Ctrl+Enter pins what the
            # cursor is on, and the only reason it worked from the filter box
            # at all was a shortcut for "exactly one match" -- which a PARENT
            # can never hit, because it always matches its own children too.
            # So `+` after typing `todo` reported nothing to pin. Moving the
            # cursor also makes the target visible instead of implied.
            # Safe to set: _preview_tag is gated on the tag column having
            # focus, and while you are typing the focus is the Input.
            if self._tag_filter and len(self.tag_names) > PINNED_VIEWS \
                    and (lst.index or 0) < PINNED_VIEWS:
                lst.index = PINNED_VIEWS

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
        # Look in the VIEW first, then the whole store. The slug is exact, so
        # widening the search is not the guess the `_current()` fallback was —
        # it addresses the note that was named and no other. It has to widen:
        # `p` strips the todo tags before opening the picker, which takes the
        # note out of the pool, and the pool deliberately does not follow it.
        # Searching only the current rows meant promote could never complete.
        row = (next((r for r in self.rows if r["slug"] == slug), None)
               or next((r for r in self._items if r["slug"] == slug), None))
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
        # Landing on the chips is right when you are TAGGING — you are usually
        # adding a second word, and going back to the list would make it a
        # journey. It is wrong in the pool, where the only reason you opened
        # the picker was `p`, and `p` means "this is not a task any more". You
        # come back to the LIST, on the next item, ready to keep working: the
        # chips of a note you just filed elsewhere are the one thing you are
        # certainly done with. Returning there read as being locked into the
        # next row, because j/k moved a tag cursor instead of the queue.
        self.set_focus(self.query_one("#queue", ListView)
                       if self._queue_mode() else chips)

    async def _switch_view(self, tag) -> None:
        self._close_tags_if_narrow()
        self._load_view(tag)
        self.set_focus(self.query_one("#queue", ListView))

    def action_pin_tag(self) -> None:
        """`+` — AND this tag onto the view and keep filtering.

        The tag column narrows to what still intersects, so the second tag is
        chosen from a list that cannot produce an empty result. Repeatable:
        each pin is another AND, and the header carries the chain so a view
        three deep still says what it is.

        Pins the tag UNDER THE CURSOR, or the filter's single match when you
        have typed one -- the same bargain Enter makes in the picker, for the
        same reason: having narrowed to one, you have already said which.
        """
        if self._picking:               # the column is a picker right now
            return
        # WALKING THE STRIP: `+` commits the facet you are looking at, and the
        # panel re-offers the facets of the narrower query. That is the whole
        # point of the strip -- build the query along the bottom, never
        # reaching for the column on the left. The anchor that Tab was holding
        # for you stops being provisional and becomes a pin like any other.
        if self._fam_anchor and isinstance(self.view_tag, str) \
                and self.view_tag != triage.ALL \
                and self.view_tag in self._fam_anchor \
                and self.view_tag != self._fam_home:
            self._walk_pin = None        # the anchor is yours now, not the walk's
            # Focus STAYS on the notes. Sending it to the filter box is right
            # when you pinned from the column, and wrong here -- the strip
            # exists so you never have to go left, and the next Tab would have
            # been eaten by the Input.
            self._commit_pin(self.view_tag, to_filter=False)
            return
        lst = self.query_one("#taglist", ListView)
        i = lst.index
        real = self.tag_names[PINNED_VIEWS:]
        if i is not None and PINNED_VIEWS <= i < len(self.tag_names):
            tag = self.tag_names[i]
        elif self._tag_filter and real:
            # Typing narrowed the list but the cursor never left a view row.
            # The top match is the one on screen under the box, and it is
            # sorted by weight, so a parent outranks its children -- `todo`
            # pins `todo`, not `todo/sooner`.
            #
            # Gated on there being a FILTER. Without that gate, `+` with the
            # cursor resting on `unplaced` or `all` would pin whatever happens
            # to top the vocabulary -- a tag you never named, from a row that
            # is not a tag at all.
            tag = real[0]
        else:
            self.notify("nothing to pin — put the cursor on a tag")
            return
        if tag in self.pins:
            return
        self._commit_pin(tag)

    def _commit_pin(self, tag: str, to_filter: bool = True) -> None:
        """Add one tag to the query and re-offer everything from there."""
        lst = self.query_one("#taglist", ListView)
        self.pins.append(tag)
        self._pin_return.append(self.view_tag)
        # A committed pin ends the walk it came from: the facets of the new,
        # narrower query are a different list, and holding the old one would
        # keep offering neighbours that no longer intersect.
        self._fam_anchor = []
        self._fam_home = None
        self.query_one("#tagfilter", Input).value = ""
        self._tag_filter = ""
        # The view drops to `all` so the pin is the whole query. Staying on
        # the old tag would silently AND it too, and the header would be the
        # only place that said so.
        self.view_tag = triage.ALL
        self.rows = triage.queue(self._items, tag=self.view_tag, pins=self.pins,
                                 search=self.search)
        self._order_rows()
        self._tag_sig = None            # facets changed; force the repaint
        self._paint_tags()
        # Land the cursor on the `all` row, which is what the view now IS.
        # Leaving it where it was put it on the first FACET after the repaint,
        # and the Highlighted handler loaded that -- so pinning `hearth`
        # silently became `hearth + ai`. Parking it on a row that agrees with
        # view_tag makes the handler a no-op instead of a surprise, and it is
        # also where you want to be: type to filter the facets and pin again.
        lst.index = 1
        self._repaint_queue(keep=0)
        # Straight back into the filter, because pinning is never the last
        # thing you do -- the point of a pin is to narrow and look again. It
        # also keeps you out of the tag list, where the letters you would type
        # to filter are app bindings instead: `r` reloads, `p` makes a project.
        if to_filter:
            self.set_focus(self.query_one("#tagfilter", Input))
        self.notify(self._view_label() + f" — {len(self.rows)}")

    def action_unpin(self) -> None:
        """`-` — drop the last term of the query, from anywhere on the page.

        Nothing needs to be highlighted and no particular pane needs focus:
        it removes the newest term, which is the only one you can be
        unambiguously asking about. It is a priority binding so the filter and
        search boxes do not eat it while you are typing in them.

        It used to return in silence when there was nothing to drop, which
        reads exactly like a broken key.
        """
        if self.search and not self.pins:
            self._clear_search()
            self.rows = triage.queue(self._items, tag=self.view_tag,
                                     pins=self.pins, search=self.search)
            self._order_rows()
            self._paint_tags()
            self._repaint_queue(keep=0)
            self.notify("search cleared")
            return
        if not self.pins:
            self.notify("nothing pinned — tab onto a tag in the strip, "
                        "or + on one in the column")
            return
        gone = self.pins.pop()
        if gone == self._walk_pin:
            self._walk_pin = None
        # Back to the view this pin was made from. Without it, pinning from
        # `#projects` (16 notes) and unpinning left you in `all` (60) -- the
        # count jumped, nothing said why, and the tag you had been reading was
        # gone. A pin is a step, so undoing it is a step back.
        if self._pin_return:
            self.view_tag = self._pin_return.pop()
        self.rows = triage.queue(self._items, tag=self.view_tag, pins=self.pins,
                                 search=self.search)
        self._order_rows()
        self._tag_sig = None
        self._paint_tags()
        self._repaint_queue(keep=0)
        self.notify(f"unpinned #{gone}" + (f" — {self._view_label()}" if self.pins else ""))

    def _drop_walk_pin(self) -> None:
        """Take down the pin Tab raised, if it is still ours to take down."""
        if self._walk_pin is None:
            return
        if self._walk_pin in self.pins:
            self.pins.remove(self._walk_pin)
            if self._pin_return:
                self._pin_return.pop()
        self._walk_pin = None

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
        # Standing on a tag with no children, Tab is about to walk its
        # neighbours -- pin that list now so the walk stays inside it.
        neigh = len(fam) > 1 and not str(fam[1]).startswith(str(fam[0]) + "/")
        neigh = neigh or (len(fam) == 1 and fam[0] != self.view_tag)
        if neigh and not self._fam_anchor:
            self._fam_anchor = list(fam)
            # Home only exists if we started FROM a tag. A walk begun from a
            # query that is already pinned deep has nowhere to return to.
            self._fam_home = fam[0] if fam[0] == self.view_tag else None
        if self.view_tag in fam:
            nxt = fam[(fam.index(self.view_tag) + step) % len(fam)]
        else:
            # Arriving from `all` (a pinned query): step onto the list rather
            # than treating position 0 as somewhere we have already been.
            nxt = fam[0] if step > 0 else fam[-1]
        if neigh:
            # Walking neighbours KEEPS the anchor, as a pin. Tab off #writing
            # onto #hearth used to mean "show me all 56 hearth notes", which
            # threw away the tag you were reading and made the walk a series
            # of unrelated destinations. It now means "the ones that are also
            # writing" -- which is what a strip headed `also` says, and what
            # its counts now show. The header carries `#writing + #hearth`, so
            # nothing about it is implicit.
            anchor = self._fam_home
            if anchor and nxt == anchor:
                self._drop_walk_pin()
            elif anchor and self._walk_pin is None and anchor not in self.pins:
                self.pins.append(anchor)
                self._pin_return.append(anchor)
                self._walk_pin = anchor
        self._load_view(nxt, keep_anchor=True)
        if nxt in self.tag_names:               # keep the column in step
            lst = self.query_one("#taglist", ListView)
            want = self.tag_names.index(nxt)
            if lst.index != want:
                lst.index = want

    def _disarm_trash(self) -> None:
        """Forget a half-typed `dd`. Called by the timer, by any other key,
        and by the second `d` itself just before the write."""
        self._armed_trash = None
        if self._trash_timer is not None:
            self._trash_timer.stop()
            self._trash_timer = None
        self._paint_status()

    async def action_view_pool(self) -> None:
        """`T` — the todo pool, from wherever you are."""
        if self.view_tag != POOL_TAG:
            await self._switch_view(POOL_TAG)
        else:
            self.set_focus(self.query_one("#queue", ListView))

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
        # Pins go too. `g` is "start over", and a g that left a two-tag
        # intersection standing would show the unplaced queue with most of it
        # invisible and nothing on screen saying why.
        had_pins = bool(self.pins)
        self.pins = []
        self._pin_return = []
        self._fam_anchor = []
        self._fam_home = None
        self._walk_pin = None
        self._clear_search()
        lst = self.query_one("#taglist", ListView)
        if lst.index != 0:
            lst.index = 0                       # the pinned `unplaced` row
        if had_pins and self.view_tag is triage.UNTAGGED:
            self._tag_sig = None
            await self.reload()
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
                (self._view_label(), ACCENT),
                (f"  {len(self.rows)} notes as one page", DIM),
                ("   j/k scroll · z or esc back · read-only", FAINT)))
            return
        where = self._view_label()
        mode = getattr(self, "_layout_mode", None)
        # The hint is the first thing to go when the width does. At `narrow`
        # the tag column is hidden entirely, so `/` is the only way to reach
        # it and is the one key that must still be advertised.
        # `f search` sits next to `/ filter` because the pair is the thing worth
        # learning: `/` narrows the TAG COLUMN, `f` narrows the NOTES. Two
        # boxes, and which one you meant is the key you pressed. `F` widens to
        # the whole store keeping what you typed, and is left out here -- the
        # bar is for the keys you reach for cold, and F is the one you find
        # once `f` has come up empty. `?` carries it.
        # `f search` takes `z read all`'s slot rather than being added to it:
        # the wide hint applies from 72 columns and was already 96 wide, so
        # anything appended is clipped off the right -- taking `?` with it,
        # which is the one key that must survive. `z` is a reading posture you
        # keep once you have it; `f` is the one being introduced.
        hint = ("  j/k move · ⏎ deeper · esc back · f search · / filter · "
                "g unplaced · ?"
                if not mode else "  j/k · ⏎ deeper · esc back · f search · / tags · ?")
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

    def _load_view(self, tag, keep_anchor: bool = False) -> None:
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
        if not keep_anchor:
            # Arriving any other way -- the tag column, `g`, a pin -- starts a
            # new neighbourhood, centred on where you actually are, and the
            # pin Tab was holding for you comes down with it.
            self._fam_anchor = []
            self._fam_home = None
            self._drop_walk_pin()
        if not self._items or time.monotonic() - self._items_at > 2.0:
            self._items = stream.load(include_daily=True)
            self._items_at = time.monotonic()
        self.view_tag = tag
        self.rows = triage.queue(self._items, tag=tag, pins=self.pins,
                                 search=self.search)
        self._order_rows()
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

    def action_search(self) -> None:
        """`f` — narrow the notes by text, as one more term in the query.

        The box lives above the queue and appears only when it has something
        to say, the same bargain the children strip makes: a permanently empty
        row here costs a note on every view, and most views are short.
        """
        box = self.query_one("#searchbox", Input)
        box.display = True
        self.set_focus(box)
        box.selection = Selection.cursor(len(box.value))

    async def action_search_all(self) -> None:
        """`F` — the same box, over everything.

        `f` searches WHERE YOU ARE, which is right most of the time and wrong
        at exactly the moment you care: you looked in #writing, it was not
        there, and the next thought is "is it anywhere". So this drops the
        tag and the pins and searches the whole store.

        It KEEPS what you have already typed, so f -> F widens the search you
        are in rather than making you retype it. That is the whole gesture:
        look here, then look everywhere.
        """
        # Kill any pending highlight preview. Moving the tag cursor schedules a
        # debounced _load_view, and a timer left in flight lands ~90ms later
        # and quietly puts the view back on whatever row the cursor had been
        # resting on -- so `F` appeared to work and then undid itself.
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        self.pins = []
        self._pin_return = []
        self._fam_anchor = []
        self._fam_home = None
        self._walk_pin = None
        # Cursor onto the `all` row FIRST. reload() repaints the tag column
        # and restores the cursor where it was, which fires Highlighted, which
        # loads that tag -- so setting view_tag before the reload had it
        # quietly overwritten by whatever row the cursor was resting on.
        # Parking it on `all` makes that handler agree with us instead.
        lst = self.query_one("#taglist", ListView)
        if lst.index != 1:
            lst.index = 1
        self.view_tag = triage.ALL
        self._tag_sig = None
        await self.reload()
        self.view_tag = triage.ALL              # re-assert, cheaply
        self.rows = triage.queue(self._items, tag=triage.ALL, pins=[],
                                 search=self.search)
        self._order_rows()
        self._repaint_queue(keep=0)
        self.action_search()

    def _clear_search(self) -> None:
        box = self.query_one("#searchbox", Input)
        box.value = ""
        box.display = False
        if self.search:
            self.search = ""
            self._tag_sig = None

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
        """`t` — in or out of the pool.

        Was `--todo`, which wrote `status: todo` — a field the pool does not
        read and which disagreed with the tag by design. This toggles the
        membership itself, so a note filed here by mistake leaves without
        being deleted. Takes the child with it: leaving the pool means
        leaving, not demoting to later.
        """
        row = self._current()
        if not row:
            return
        now = [stream.norm_tag(t) for t in (row.get("tags") or [])]
        if POOL_TAG in now or SOONER in now:
            after = [t for t in now if t not in (POOL_TAG, SOONER)]
        else:
            after = now + [POOL_TAG]
        await self._apply(row, after, stay=True)

    async def action_sooner(self) -> None:
        """Move one item between the two bins.

        The bins are `#sooner` and its absence. Nothing is `#later`, so there
        is no pair to disagree with each other and no lint to write — the
        thing that made `todo`-the-tag and `status: todo` drift apart cannot
        happen to a single bit.
        """
        row = self._current()
        if not row:
            return
        now = [stream.norm_tag(t) for t in (row.get("tags") or [])]
        if SOONER in now:                       # sooner -> later
            after = [t for t in now if t != SOONER]
            if POOL_TAG not in after:
                after.append(POOL_TAG)
        else:                                   # later -> sooner
            after = [t for t in now if t != POOL_TAG] + [SOONER]
        await self._apply(row, after, stay=True)

    async def action_promote(self) -> None:
        """`p` — a finished todo that earned keeping becomes a project.

        Two steps, deliberately: the todo tags come off straight away, and
        then the ordinary tag picker opens with `projects/` already typed. It
        does not guess a name — a project is a thing you are choosing to keep,
        and auto-coining `projects/<title-slug>` would grow one tag per todo,
        which is the sprawl this vocabulary was just cut back from.
        """
        row = self._current()
        if not row:
            return
        now = [stream.norm_tag(t) for t in (row.get("tags") or [])]
        if POOL_TAG in now or SOONER in now:
            await self._apply(row, [t for t in now
                                    if t not in (POOL_TAG, SOONER)], stay=True)
            row = next((r for r in self.rows if r["slug"] == row["slug"]), row)
        self._picking = row["slug"]
        self._paint_status()
        self.action_focus_filter()
        box = self.query_one("#tagfilter", Input)
        box.value = PROJECTS
        self._tag_filter = PROJECTS
        self._paint_tags()
        # COLLAPSE THE SELECTION, after the refresh. Focusing an Input selects
        # its contents, so the seeded `projects/` arrived highlighted and the
        # first character you typed replaced it — turning a head start into a
        # trap. `cursor_position` alone did not survive the focus that follows
        # it; an explicit empty selection at the end does.
        def to_end() -> None:
            box.selection = Selection.cursor(len(box.value))
        to_end()
        self.call_after_refresh(to_end)

    async def action_trash(self) -> None:
        """`dd` — this was never a note.

        DOUBLED, and only here. `d` on the chip row removes one tag, `d` on
        the queue used to remove the whole NOTE, and the two rungs are one
        Enter apart with almost nothing on screen saying which one you are
        on — so the cheapest slip in the app was also the most destructive.
        Vim already draws that line: `x` takes a character, `dd` takes the
        line. This is the line.

        Still no yes/no prompt. It goes to ~/kb/.trash, `u` puts it straight
        back, and a confirmation on each of sixty would make the pass slower
        than not doing it. The second `d` IS the confirmation, and it costs a
        keystroke rather than a dialogue.

        The armed state is VISIBLE and expires. An invisible mode that eats
        your next keystroke is how a guard becomes its own hazard.
        """
        row = self._current()
        if not row:
            return
        if not self._armed_trash:
            self._armed_trash = row["slug"]
            self.notify("d again to trash this note  ·  any other key cancels")
            if self._trash_timer is not None:
                self._trash_timer.stop()
            self._trash_timer = self.set_timer(1.5, self._disarm_trash)
            return
        if self._armed_trash != row["slug"]:
            # The cursor moved between the two presses. Re-arm on the new note
            # rather than trashing something the first `d` never referred to.
            self._armed_trash = row["slug"]
            return
        self._disarm_trash()
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

        It asks when there is anything LEFT to suggest, and once per session.
        The old test was "nothing has been suggested yet", which reads well
        and was wrong in practice: slots accumulate, so the table is almost
        never empty, and `c` quietly stopped starting arcs while ten notes sat
        there unsuggested. Measured 2026-09-08 — 26 unplaced, 16 already
        filled, and `c` sent nothing.

        `self._asked` is what stops it re-asking: the second `c` of a session
        is walk-over-and-talk, so pressing it to go READ the answer cannot
        bury that answer under a fresh request. `C` re-asks deliberately.

        The jump itself is the deck's own `select-pane -t {top-right}`, called
        rather than reimplemented, so `c` and M-l land in the same place. No
        -L bridge: inside a pane tmux reads $TMUX and finds its own server.
        """
        unfilled = [r for r in self.rows if not (r["suggested"] or r["note"])]
        fresh = not self._asked and bool(unfilled)
        if not self._send_to_chat(self.KICKOFF if fresh else ""):
            return
        if fresh:
            self._asked = True
            self.notify(f"asked Hermes to work the {len(unfilled)} unsuggested "
                        "— M-h back, r to reload")

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

    HELP = [
        ("MOVE", [
            ("j / k",        "up and down"),
            ("h / l",        "jump between the tag column and the notes"),
            ("enter",        "descend — a tag, its notes, that note's tags"),
            ("esc",          "climb back, and unwind the query one term at a time"),
            ("tab / S-tab",  "walk the strip: a tag's children, or what its notes"),
            ("",             "are ALSO about — anchored, so S-tab really goes back"),
        ]),
        ("VIEWS", [
            ("unplaced",     "notes nobody has placed — pinned top of the column"),
            ("all",          "every note in the store, placed or not"),
            ("T",            "the todo pool"),
            ("g",            "back to unplaced, and drop the whole query"),
        ]),
        ("NARROW IT", [
            ("f",            "search the notes you are looking at"),
            ("F",            "search the whole store, keeping what you typed"),
            ("/",            "filter the TAG COLUMN — not the notes"),
            ("+",            "pin the tag: AND it onto the view. repeatable"),
            ("-",            "drop the newest term, from anywhere on the page"),
        ]),
        ("ON A NOTE", [
            ("i",            "edit this note's tags"),
            ("a",            "accept the suggested tags"),
            ("t",            "toggle todo"),
            ("s",            "toggle todo/sooner"),
            ("p",            "make it a project"),
            ("dd",           "trash it — doubled, and `u` undoes"),
            ("o",            "open it in the writer"),
            ("z",            "read the whole view as one page"),
        ]),
        ("EDITING TAGS", [
            ("j/k or w/b",   "move along the chips · I / A first and last"),
            ("a",            "add one — type to filter, enter picks"),
            ("d",            "remove the one under the cursor"),
            ("enter",        "take the greyed suggestion"),
        ]),
        ("HERMES", [
            ("c",            "jump to the chat — the first one asks for suggestions"),
            ("C",            "ask again, deliberately"),
        ]),
        ("", [
            ("r",            "reload · q quit · ? closes this"),
        ]),
    ]

    def action_help(self) -> None:
        """`?` — the keys, as a PAGE rather than a toast.

        This was a 500-character notify on a 14-second timer: everything the
        app could do, in one wrapped paragraph, gone before you finished it.
        It was also missing tab, T, s, p and i, and it advertised `d trash`
        when trash is `dd`. A help that is wrong about the destructive key is
        worse than none.

        It borrows `z`'s posture — full width, read-only, esc to leave — so
        there is one way to be reading something here instead of two.
        """
        self._help = not self._help
        self._readall = self._help
        self.query_one("#body").set_class(self._help, "readall")
        if self._help:
            t = Text()
            for section, rows in self.HELP:
                t.append("\n")
                if section:
                    t.append(f"  {section}\n", f"bold {ACCENT}")
                for key, what in rows:
                    t.append(f"    {key:<14}", ACCENT if key else FAINT)
                    t.append(f"{what}\n", "#d8d4cf" if key else FAINT)
            self.query_one("#detailhdr", Label).update("KEYS")
            self.query_one("#title", Static).update("")
            self.query_one("#meta", Static).update("")
            self.query_one("#hermes", Static).display = False
            self.query_one("#previewtext", Static).update(t)
            self.set_focus(self.query_one("#preview", VerticalScroll))
        else:
            self._paint_detail()
            self.set_focus(self.query_one("#queue", ListView))
        self._paint_status()

    # ── writing ─────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "searchbox":
            # Enter KEEPS the search and hands you the results -- the box has
            # already filtered as you typed, so submitting is only "I am done
            # typing, let me read". Esc is the one that drops it.
            self.set_focus(self.query_one("#queue", ListView))
            return
        if event.input.id == "tagfilter":
            typed = event.value.strip().lstrip("#")
            # ONE VISIBLE MATCH WINS over minting a new word. Typing `hear`
            # while the column shows nothing but `hearth` and pressing Enter
            # means `hearth` -- it used to mean "create the tag `hear`",
            # because the new-word branch below ran first and only asked
            # whether the exact string was in the vocabulary. That is the
            # keystroke-level version of the erosion the cull just undid.
            # (`cl stream set` now also refuses prefixes, so this is the
            # second of two guards, not the only one.)
            if self._picking and typed:
                visible = self.tag_names[PINNED_VIEWS:]
                if len(visible) == 1 and visible[0] != typed:
                    await self._add_picked(visible[0])
                    self.notify(f"#{visible[0]}")
                    return
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
                real = self.tag_names[PINNED_VIEWS:]
                if len(real) == 1:
                    await self._add_picked(real[0])
                    return
                lst = self.query_one("#taglist", ListView)
                if real:
                    lst.index = PINNED_VIEWS
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
            # FOLLOW THE NOTE when the edit removed it from this view. Taking
            # `work-app` off the only note tagged `work-app` empties the view
            # you are standing in, and staying put means the note you were
            # working on vanishes along with the tag — the surface reports
            # "nothing tagged #work-app" and the panel goes blank, which reads
            # as having lost the note rather than having retagged it.
            #
            # So go where the note went: its first remaining tag, or the
            # unplaced queue when the last one is gone. Removing a tag is a
            # statement about the note, never a decision to stop looking at
            # it.
            # THE POOL DOES NOT FOLLOW. It is a working pass, not a browser:
            # `t` and `p` exist precisely to take a note OUT of the pool, and
            # a view that chased it would end every one of those keystrokes by
            # dropping you somewhere else with the list you were working gone
            # from under you. Staying put, with the cursor landing on whatever
            # moved up, is what "act on it and move on" means. Elsewhere the
            # note is the thing you were reading and following it is right —
            # see below.
            in_pool = self._queue_mode()
            if not in_pool and not any(r["slug"] == row["slug"] for r in self.rows):
                self._load_view(after[0] if after else triage.UNTAGGED)
                dest = after[0] if after else triage.UNTAGGED
                if dest in self.tag_names:      # keep the column in step
                    lst = self.query_one("#taglist", ListView)
                    want = self.tag_names.index(dest)
                    if lst.index != want:
                        lst.index = want
            v = self.query_one("#queue", ListView)
            here = next((i for i, r in enumerate(self.rows)
                         if r["slug"] == row["slug"]), None)
            want = here if here is not None else min(keep or 0,
                                                     max(0, len(self.rows) - 1))
            self._cursor_to(want)
            r = self._current()
            chips = self.query_one("#tagchips", TagChips)
            chips.show((r or {}).get("tags") or [],
                       (r or {}).get("suggested") or [])
            # The chips are where a CHIP edit should leave you. In the pool
            # every verb is pressed while walking the list, so the list is
            # where the hands stay.
            self.set_focus(v if in_pool else chips)
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
        # A half-typed `dd` is forgotten by anything that is not the second
        # `d`. The timer would get there anyway, and moving the cursor re-arms
        # rather than firing — this is the third belt, for keys that reach the
        # app at all.
        if self._armed_trash and event.key != "d":
            self._disarm_trash()
        # `escape` only — `z` is already an App binding, and handling it here
        # too toggled read mode twice per press, which looked like the key
        # doing nothing at all.
        if self._help and event.key == "escape":
            self.action_help()
            event.stop()
            return
        if self._readall and event.key == "escape":
            self.action_read_all()
            event.stop()
            return
        if self.focused is self.query_one("#searchbox", Input) \
                and event.key == "escape":
            self._clear_search()
            self.rows = triage.queue(self._items, tag=self.view_tag,
                                     pins=self.pins, search=self.search)
            self._order_rows()
            self._paint_tags()
            self._repaint_queue(keep=0)
            self.set_focus(self.query_one("#queue", ListView))
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
            # Esc unwinds the query one term at a time before it leaves it,
            # newest first: the search, then the pins, then the column.
            if self.search:
                self._clear_search()
                self.rows = triage.queue(self._items, tag=self.view_tag,
                                         pins=self.pins, search=self.search)
                self._order_rows()
                self._paint_tags()
                self._repaint_queue(keep=0)
            elif self.pins:
                self.action_unpin()
            else:
                self.action_col_left()
            event.stop()
            return
        # Single-letter bindings must not fire while typing in the filter;
        # Input eats printable keys itself, so only escape needs handling
        # above.

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tagfilter":
            # `/` is the key that OPENS this box, and after a pin the box
            # already has focus -- so the habit of pressing it again typed a
            # slash and killed the filter. Swallow a leading one; no tag
            # starts with a slash, so nothing legitimate is lost.
            if event.value.startswith("/"):
                event.input.value = event.value.lstrip("/")
                return
            self._tag_filter = event.value
            self._paint_tags()
        elif event.input.id == "searchbox":
            self.search = event.value.strip()
            self.rows = triage.queue(self._items, tag=self.view_tag,
                                     pins=self.pins, search=self.search)
            self._order_rows()
            # The facets recount against the MATCHES, so a search tells you
            # what its results are about -- which is the whole argument for
            # having search on the tag page rather than beside it.
            self._tag_sig = None
            self._paint_tags()
            self._repaint_queue(keep=0)


# How many rows at the top of the tag column are VIEWS rather than tags
# (`unplaced`, `all`). Anything walking the column to find a real tag skips
# this many; it was written as a literal `1` in one place and adding `all`
# would have made Enter-to-pick offer `all` as a tag to apply.
PINNED_VIEWS = 2

POOL_TAG = "todo"
# A CHILD, not a second tag. Flat `#todo` + `#sooner` is two independent bits
# and therefore four states, one of which — `#sooner` with no `#todo` — means
# nothing and is invisible to the pool forever. As a child there are two
# states and the meaningless one cannot be spelled. It is also one write at
# capture time, and the leaf resolution in stream.classify_tag turns a bare
# `#sooner` into this, so the short form lands in the namespace instead of
# orphaning itself.
SOONER = "todo/sooner"
PROJECTS = "projects/"


def main() -> None:
    if not sys.stdin.isatty():
        print("cl triage --tui needs a terminal.", file=sys.stderr)
        raise SystemExit(1)
    os.environ.setdefault("COLORTERM", "truecolor")
    # `--view TAG` opens on a tag instead of the unplaced queue. That is the
    # whole of the pool: it is not a second app, it is this one through a
    # different door. M-5 in the deck runs `--view todo`.
    view = None
    argv = sys.argv
    if "--view" in argv:
        i = argv.index("--view")
        if i + 1 < len(argv):
            view = argv[i + 1]
            # `all` is spelled `*` internally, but nobody should have to type a
            # glob at a shell that would expand it first.
            if view == "all":
                view = triage.ALL
    TriageApp(view=view).run()


if __name__ == "__main__":
    main()
