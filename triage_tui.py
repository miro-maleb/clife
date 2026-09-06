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
`cl inbox` and the writer's <leader>sp.

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

    async def on_mount(self) -> None:
        self.register_theme(HEARTH)
        self.theme = "hearth"
        await self.reload()
        self.set_focus(self.query_one("#queue", ListView))

    # ── data ────────────────────────────────────────────────────────────────
    async def reload(self) -> None:
        """Read from the modules, not the CLI: this is a read of live files on
        every keystroke-driven refresh, and paying a subprocess for it would
        make the list lag the write that caused it. Writes still go out through
        `cl` — read cheap, write guarded."""
        self.rows = triage.queue()
        self.vocab = stream.vocabulary(stream.load(include_daily=True))
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
            ("  i tag · a accept · d trash · u undo · o open · ? keys", FAINT)))

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
        """The vocabulary, filtered to the fragment being typed.

        This is the affordance half of the reuse rule — the guard in
        `cl stream set` is the floor, and a floor you only meet as a refusal
        teaches nothing. Seeing `groceries 1` while you type `grocer` is what
        stops `grocery` being coined in the first place."""
        frag = (typed.split(",")[-1] if typed else "").strip().lstrip("#").lower()
        items = [(t, n) for t, n in self.vocab.items() if frag in t] if frag \
            else list(self.vocab.items())
        t = Text()
        t.append("vocab  ", FAINT)
        if not items:
            t.append(f"no tag contains '{frag}' — it would be NEW", RUST)
        # Budgeted by width, not a fixed count: in the deck this column is ~41
        # columns, where fourteen tags is five wrapped lines eating the note
        # preview. Three lines' worth, then a count of the rest.
        # Two guards, because either alone has failed: a width budget (this
        # panel is ~39 columns in the deck) AND a hard count. `size.width` is 0
        # at first paint, before layout — trusting it alone printed twenty tags
        # into a three-line box on the very first frame.
        w = self.query_one("#vocab", Static).size.width or 39
        budget = w * 2
        limit = 10 if frag else 8
        used, shown = 7, 0
        for tag, n in items[:limit]:
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

    async def action_open(self) -> None:
        row = self._current()
        if not row:
            return
        editor = shutil.which("nvim-write") or "nvim"
        with self.suspend():
            subprocess.call([editor, str(KB / row["relpath"])])
        await self.reload()

    async def action_reload(self) -> None:
        await self.reload()
        self.notify("reloaded")

    def action_help(self) -> None:
        self.notify("j/k move · i tag · a accept suggestion · t todo · "
                    "d trash (recoverable) · u undo · o open in writer · "
                    "r reload · q quit", timeout=9)

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
