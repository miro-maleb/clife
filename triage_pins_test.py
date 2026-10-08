#!/usr/bin/env python3
"""Regression suite for tag PINNING in the triage navigator.

    ./venv/bin/python3 triage_pins_test.py

Builds its own store in a temp directory and drives the real app against it
through Textual's Pilot -- never the live kb, because half of these presses
would write to it. Four bugs came out of writing this, and each one has a
check named after it below:

  * pinning silently ANDed the tag under the cursor after the repaint moved
    it, so pinning `hearth` became `hearth + ai` with a plausible count
  * unpinning dumped you in `all` instead of the view you pinned FROM
  * `ALL` is the string `*`, so it walked into _family() and the children
    strip offered `*` as somewhere to navigate
  * `/` typed a literal slash once the filter already had focus, which is
    exactly where a pin leaves you

The invariant that matters most is `rows match query`: whatever the app is
showing must equal triage.queue() computed independently. Everything else is
a way of getting the app into a state where that might stop being true.
"""
import asyncio
import itertools
import os
import pathlib
import random
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VOCAB = ["hearth", "ai", "tech", "writing", "poem", "recipe", "home", "todo",
         "todo/sooner", "projects/clife", "projects/hearth", "blog",
         "blog/kids", "buddhism"]

FAILS = []


def check(cond, what, extra=""):
    if not cond:
        FAILS.append(f"{what}  {extra}")
    return cond


def build_store(root: pathlib.Path, n=60, seed=7):
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (root / "_state").mkdir(exist_ok=True)
    rng = random.Random(seed)
    for i in range(n):
        k = rng.choice([0, 1, 1, 2, 2, 3, 4])
        tags = sorted(rng.sample(VOCAB, k))
        (notes / f"n{i:02d}.md").write_text(
            f"---\ncreated: 2026-0{i % 9 + 1}-1{i % 9} 10:0{i % 6}\n"
            f"tags: [{', '.join(tags)}]\n---\n\nnote {i} body\n")


async def settle(pilot, t=0.05):
    await asyncio.sleep(t)
    await pilot.pause()


async def run():
    import triage
    import triage_tui
    from textual.widgets import ListView, Static

    PV = triage_tui.PINNED_VIEWS

    def truth(app):
        """What the view SHOULD hold, computed without asking the app."""
        return len(triage.queue(app._items, tag=app.view_tag, pins=app.pins))

    app = triage_tui.TriageApp(view=triage.ALL)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, 0.3)
        lst = app.query_one("#taglist", ListView)
        real = list(app.tag_names[PV:])
        print(f"fixture: {len(app._items)} notes, {len(real)} tags")

        async def reset(view=triage.ALL):
            app.pins = []
            app._pin_return = []
            app.view_tag = view
            await app.reload()
            await settle(pilot)

        # every tag pins, and what is shown is what was asked for
        for name in real:
            await reset()
            lst.index = app.tag_names.index(name)
            app.action_pin_tag()
            await settle(pilot)
            check(app.pins == [name], "pin sets pins", f"{name}: {app.pins}")
            check(len(app.rows) == truth(app), "rows match query",
                  f"{name}: {len(app.rows)} vs {truth(app)}")
            check(len(app.rows) > 0, "a live tag never pins to nothing", name)
        print(f"  [ok] single pin over {len(real)} tags")

        # a facet that leads nowhere is the failure mode of every faceted
        # filter that does not recount against the current view
        for name in real:
            app.pins = [name]
            app.view_tag = triage.ALL
            await app.reload()
            await settle(pilot)
            for f in app.tag_names[PV:]:
                check(triage.queue(app._items, tag=triage.ALL, pins=[name, f]),
                      "no dead facet", f"{name} + {f}")
        print("  [ok] facets never dead-end")

        # pin then unpin is identity, and lands where it started
        for name in real[:8]:
            await reset()
            base = len(app.rows)
            lst.index = app.tag_names.index(name)
            app.action_pin_tag()
            await settle(pilot)
            app.action_unpin()
            await settle(pilot)
            check(app.pins == [], "unpin empties", str(app.pins))
            check(len(app.rows) == base, "unpin restores the count",
                  f"{name}: {len(app.rows)} vs {base}")
        print("  [ok] pin -> unpin is identity")

        # unpinning returns to the VIEW the pin was made from, not to `all`
        await reset()
        lst.index = PV
        await settle(pilot, 0.25)
        from_view, from_rows = app.view_tag, len(app.rows)
        app.action_pin_tag()
        await settle(pilot)
        app.action_unpin()
        await settle(pilot)
        check(app.view_tag == from_view, "unpin returns to the view pinned from",
              f"{app.view_tag!r} vs {from_view!r}")
        check(len(app.rows) == from_rows, "and to its row count",
              f"{len(app.rows)} vs {from_rows}")
        print("  [ok] unpin returns to the view pinned from")

        # AND is commutative
        pairs = list(itertools.combinations(real, 2))
        random.Random(3).shuffle(pairs)
        for a, b in pairs[:30]:
            check(len(triage.queue(app._items, tag=triage.ALL, pins=[a, b]))
                  == len(triage.queue(app._items, tag=triage.ALL, pins=[b, a])),
                  "AND is commutative", f"{a},{b}")
        print("  [ok] order independence")

        # a parent pin subsumes its children, the way a parent VIEW does
        for parent in [p for p in real if p in app.parents]:
            app.pins = [parent]
            app.view_tag = triage.ALL
            await app.reload()
            await settle(pilot)
            got = {r["slug"] for r in app.rows}
            for kid in [k for k in real if k.startswith(parent + "/")]:
                check({r["slug"] for r in triage.queue(app._items, tag=kid)} <= got,
                      "parent pin contains child", f"{parent} !> {kid}")
        print("  [ok] parent pins subsume children")

        # `ALL` is a string sentinel; it must not reach the children strip
        app.pins = []
        app.view_tag = triage.ALL
        await app.reload()
        await settle(pilot)
        check(app._family() == [], "ALL has no family", str(app._family()))
        print("  [ok] the ALL sentinel stays out of the strip")

        # the view rows are not tags and must never pin
        for idx in (0, 1):
            await reset()
            lst.index = idx
            await settle(pilot, 0.2)
            app.action_pin_tag()
            await settle(pilot)
            check(app.pins == [], "a view row does not pin",
                  f"row {idx} ({app.tag_names[idx]!r}) -> {app.pins}")
        print("  [ok] `unplaced` and `all` refuse to be pinned")

        # a parent always matches its own children, so filtering by its name
        # can never yield exactly one -- the case that used to refuse
        for word in ("todo", "projects", "ai"):
            await reset()
            await pilot.press("slash")
            await settle(pilot, 0.2)
            for ch in word:
                await pilot.press(ch)
            await settle(pilot, 0.25)
            await pilot.press("+")
            await settle(pilot, 0.3)
            check(app.pins and app.pins[0] == word, "filter+pin picks the top match",
                  f"{word} -> {app.pins}")
        print("  [ok] filter + pin, parents included")

        # `/` again once the box already has focus (where a pin leaves you)
        await reset()
        await pilot.press("slash")
        await settle(pilot, 0.2)
        await pilot.press("slash")
        await settle(pilot, 0.2)
        check(app._tag_filter == "", "a second `/` does not become text",
              repr(app._tag_filter))
        print("  [ok] `/` inside the filter is not a character")

        # g clears the whole query
        app.pins = real[:2]
        await app.reload()
        await settle(pilot)
        await app.action_view_untagged()
        await settle(pilot, 0.35)
        check(app.pins == [], "g clears pins", str(app.pins))
        check(app.view_tag is triage.UNTAGGED, "g goes to unplaced", repr(app.view_tag))
        print("  [ok] g resets")

        # states only a changing store can produce
        app.pins = ["poem", "recipe", "buddhism", "tech"]
        await app.reload()
        await settle(pilot)
        check(len(app.rows) == truth(app), "empty intersection is consistent")
        check(app.tag_names[PV:] == [], "empty intersection offers no facets")
        app.pins = ["a-tag-that-does-not-exist"]
        await app.reload()
        await settle(pilot)
        check(len(app.rows) == 0, "a pin naming nothing yields nothing")
        for _ in range(12):
            app.action_unpin()
        check(app.pins == [] and app._pin_return == [], "over-unpinning is safe")
        print("  [ok] empty intersections, ghost pins, over-unpinning")

        # ── Tab: neighbours must be a CYCLE, not a one-way walk ──────────
        # Reported from use: on `writing`, Tab went to `buddhism`, and from
        # there shift+Tab went somewhere unrelated instead of back. Children
        # never had this bug because [blog, blog/kids, ...] is a stable list
        # keyed off the root; neighbours are a GRAPH, and recomputing them
        # from wherever you landed left Tab with no inverse.
        async def goto(tag):
            app._fam_anchor = []
            await reset()
            app._load_view(tag)
            await settle(pilot, 0.08)

        for start in [t for t in real if "/" not in t][:8]:
            await goto(start)
            if len(app._family()) < 2:
                continue
            app.action_cycle_child(1)
            await settle(pilot, 0.08)
            mid = app.view_tag
            app.action_cycle_child(-1)
            await settle(pilot, 0.08)
            check(app.view_tag == start, "tab then shift+tab returns",
                  f"{start} -> {mid} -> {app.view_tag}")

        # the list must not change under you mid-walk, and must wrap home
        anchor_tag = next((t for t in real if "/" not in t
                           and t not in app.parents), real[0])
        await goto(anchor_tag)
        fam0 = list(app._family())
        if len(fam0) > 1:
            seen = [app.view_tag]
            for _ in range(len(fam0) - 1):
                app.action_cycle_child(1)
                await settle(pilot, 0.06)
                seen.append(app.view_tag)
                check(app._family() == fam0, "family fixed while walking",
                      f"{app.view_tag}: {app._family()}")
            check(len(set(seen)) == len(fam0), "walk visits each once", str(seen))
            app.action_cycle_child(1)
            await settle(pilot, 0.06)
            check(app.view_tag == fam0[0], "full cycle wraps home",
                  f"{app.view_tag} vs {fam0[0]}")

        # landing on a tag that HAS children must not hijack the walk
        kidded = next((t for t in fam0 if "/" in t), None)
        if kidded:
            await goto(anchor_tag)
            app.action_cycle_child(1)
            await settle(pilot, 0.06)
            while app.view_tag != kidded and app.view_tag != fam0[0]:
                app.action_cycle_child(1)
                await settle(pilot, 0.06)
            if app.view_tag == kidded:
                check(app._family() == fam0, "a child tag does not hijack the walk",
                      f"on {kidded}: {app._family()}")

        # arriving any other way starts a new neighbourhood
        await goto(anchor_tag)
        app.action_cycle_child(1)
        await settle(pilot, 0.06)
        check(app._fam_anchor, "anchor is set while walking")
        await pilot.press("h")
        await settle(pilot, 0.2)
        lst.index = 2
        await settle(pilot, 0.3)
        check(app._fam_anchor == [], "the tag column clears the anchor",
              str(app._fam_anchor))
        print("  [ok] Tab cycles neighbours reversibly")

        # ── the walk keeps the anchor, as a pin ──────────────────────────
        # The strip is headed `also`, so its numbers have to mean "also, with
        # this". They used to be each tag's own size -- `hearth 56`, which the
        # tag column already said two inches to the left -- and Tab landed on
        # all 56, throwing away the tag you were reading.
        anchor2 = next((t for t in real if "/" not in t
                        and t not in app.parents), real[0])
        await goto(anchor2)
        fam2 = list(app._family())
        if len(fam2) > 1:
            for _ in range(len(fam2) - 1):
                app.action_cycle_child(1)
                await settle(pilot, 0.08)
                check(app.pins == [anchor2], "anchor stays pinned while walking",
                      f"{app.view_tag}: {app.pins}")
                check(len(app.rows) == len(triage.queue(
                          app._items, tag=app.view_tag, pins=[anchor2])),
                      "rows are the intersection",
                      f"{app.view_tag}: {len(app.rows)}")
                check(anchor2 in app._view_label(), "header names the anchor",
                      app._view_label())
            app.action_cycle_child(1)
            await settle(pilot, 0.08)
            check(app.view_tag == anchor2, "lap returns home", repr(app.view_tag))
            check(app.pins == [] and app._walk_pin is None,
                  "and takes the walk pin down", str(app.pins))

            # shift+tab unwinds the pin too
            await goto(anchor2)
            app.action_cycle_child(1)
            await settle(pilot, 0.08)
            app.action_cycle_child(-1)
            await settle(pilot, 0.08)
            check(app.view_tag == anchor2 and app.pins == [],
                  "shift+tab returns and unpins", f"{app.view_tag} {app.pins}")

            # leaving another way takes it down as well
            await goto(anchor2)
            app.action_cycle_child(1)
            await settle(pilot, 0.08)
            await pilot.press("h")
            await settle(pilot, 0.2)
            # A row that is not the one the walk just landed on. Hardcoding
            # `3` assumed the column was ordered by weight: once it groups by
            # family, row 3 can BE where the walk stopped, and assigning the
            # index it already holds emits no Highlighted at all -- so the
            # check passed or failed on the tag column's sort order rather
            # than on anything about walks.
            lst.index = next(k for k in range(PV, len(app.tag_names))
                             if k != lst.index)
            await settle(pilot, 0.35)
            check(app.pins == [], "leaving the walk drops its pin", str(app.pins))

            # but a pin YOU set is not the walk's to remove
            await goto(anchor2)
            app.pins = [fam2[1]]
            app._pin_return = [anchor2]
            app._fam_anchor = []
            app.view_tag = anchor2
            await app.reload()
            await settle(pilot, 0.08)
            app.action_cycle_child(1)
            await settle(pilot, 0.08)
            check(fam2[1] in app.pins, "a manual pin survives the walk", str(app.pins))
        print("  [ok] Tab drills into the intersection")

        # ── search is a TERM, not a mode ─────────────────────────────────
        await reset()
        hay = None
        for word in ("note", "body"):
            if triage.queue(app._items, tag=triage.ALL, search=word):
                hay = word
                break
        if hay:
            app.search = hay
            await app.reload()
            await settle(pilot)
            # against the app's OWN view, not a hardcoded one: a preview
            # timer from an earlier section can still be in flight, and the
            # invariant that matters is "shown == the query", not "we are on
            # all notes"
            check(len(app.rows) == len(triage.queue(
                      app._items, tag=app.view_tag, pins=app.pins, search=hay)),
                  "search rows match query", f"{hay}: {len(app.rows)}")
            check(f'"{hay}"' in app._view_label(), "label carries the search",
                  app._view_label())
            check(app._family() != [],
                  "search results still get facets", str(app._family()))
            # it composes with a pin rather than replacing it
            facet = next((f for f in app._family() if f != app.view_tag),
                         app._family()[0])
            app.pins = [facet]
            await app.reload()
            await settle(pilot)
            check(len(app.rows) == len(triage.queue(
                      app._items, tag=app.view_tag, pins=[facet], search=hay)),
                  "search composes with pins", f"{facet}+{hay}: {len(app.rows)}")
            app.view_tag = triage.ALL
            check("all" not in app._view_label(),
                  "the label drops a redundant `all`", app._view_label())
            # `-` peels the newest term, then the search, from anywhere
            app.action_unpin()
            await settle(pilot)
            check(app.pins == [] and app.search == hay, "`-` drops the pin first",
                  f"{app.pins} {app.search!r}")
            app.action_unpin()
            await settle(pilot)
            check(app.search == "", "`-` then drops the search", repr(app.search))
            app.action_unpin()          # nothing left: must not raise
            await settle(pilot)
            check(True, "`-` on an empty query is safe")
            # word order does not matter, and unknown words match nothing
            two = triage.queue(app._items, tag=triage.ALL, search=f"{hay} note")
            check(len(two) == len(triage.queue(app._items, tag=triage.ALL,
                                               search=f"note {hay}")),
                  "search terms are unordered")
            check(triage.queue(app._items, tag=triage.ALL,
                               search="zzzznotathing") == [],
                  "an unmatched word matches nothing")
            app.search = ""
            await app.reload()
            await settle(pilot)
            # F -- widen to the whole store, keeping what was typed
            app.search = hay
            app.pins = [facet]
            app.view_tag = real[0]
            await app.reload()
            await settle(pilot)
            narrow = len(app.rows)
            await app.action_search_all()
            await settle(pilot, 0.3)
            check(app.pins == [] and app.view_tag == triage.ALL,
                  "F drops the tag and the pins", f"{app.pins} {app.view_tag!r}")
            check(app.search == hay, "F keeps what was typed", repr(app.search))
            check(len(app.rows) == len(triage.queue(app._items, tag=triage.ALL,
                                                    search=hay)),
                  "F searches the whole store", str(len(app.rows)))
            check(len(app.rows) >= narrow, "F widens rather than narrows",
                  f"{narrow} -> {len(app.rows)}")
            app.search = ""
            app.pins = []
            await app.reload()
            await settle(pilot)
        print("  [ok] search composes with tags and pins")

        # ── every binding must be documented in `?` ──────────────────────
        # The help was a 500-char notify that missed tab, T, s, p and i, and
        # told you trash was `d` when it is `dd`. A help that is wrong about
        # the destructive key is worse than no help, so this asserts coverage
        # rather than trusting anyone to remember.
        app.action_help()
        await settle(pilot, 0.2)
        text = app.query_one("#previewtext", Static).render().plain
        NAME = {"slash": "/", "plus": "+", "minus": "-",
                "question_mark": "?", "shift+tab": "S-tab"}
        for b in triage_tui.TriageApp.BINDINGS:
            probe = NAME.get(b.key, b.key)
            if b.key == "d":
                probe = "dd"            # doubled on the queue, single on chips
            check(probe in text, "binding is documented in `?`",
                  f"{b.key} ({b.action})")
        check(app._help, "`?` opens the help page")
        app.action_help()
        await settle(pilot, 0.2)
        check(not app._help and not app._readall, "`?` closes it again",
              f"help={app._help} readall={app._readall}")
        print("  [ok] every binding appears in `?`")

        # ── the `only` slice: a parent without its children ──────────────
        # A parent view is hierarchical, which is right for reading and wrong
        # for SORTING -- #blog answers with blog/kids too, so the notes that
        # have arrived under blog and not been given a child are invisible.
        for parent, kids in [(p, [k for k in real if k.startswith(p + "/")])
                             for p in app.parents]:
            if not kids:
                continue
            await goto(parent)
            fam = app._family()
            bare = triage.bare(parent)
            has_bare = any(i["tags"] and parent in i["tags"] for i in app._items)
            check((bare in fam) == has_bare,
                  "`only` is offered exactly when it holds something",
                  f"{parent}: in_fam={bare in fam} has_bare={has_bare}")
            if not has_bare:
                continue
            rollup = len(triage.queue(app._items, tag=parent))
            only = len(triage.queue(app._items, tag=bare))
            kid_rows = {r["slug"] for k in kids
                        for r in triage.queue(app._items, tag=k)}
            only_rows = {r["slug"] for r in triage.queue(app._items, tag=bare)}
            check(only < rollup, "`only` is a strict subset of the rollup",
                  f"{parent}: {only} vs {rollup}")
            check(not (only_rows & kid_rows),
                  "`only` shares no note with any child",
                  f"{parent}: {only_rows & kid_rows}")
            # and Tab must still treat this as a CHILDREN family, not
            # neighbours -- it must not pin the parent as an anchor
            app.action_cycle_child(1)
            await settle(pilot, 0.1)
            check(app.pins == [], "walking a parent's slices pins nothing",
                  f"{parent}: {app.pins}")
            check(app.view_tag == bare, "first tab lands on `only`",
                  f"{app.view_tag!r} vs {bare!r}")
        await reset()
        print("  [ok] the `only` slice")

        # ── the cursor must be VISIBLE, not merely set ───────────────────
        # Reported as "whenever I go into nvim or delete a file, the cursor
        # disappears" -- and it was neither: those are just the two things
        # that always repaint. `index` keeps its NUMBER across a repaint while
        # the children are replaced, so re-assigning it is not a change,
        # watch_index never runs, and no row gets `-highlight`. The list knew
        # where the cursor was; nothing on screen did.
        #
        # So this asserts the CLASS, not the index. Checking `index is not
        # None` is exactly the test that passed all along while the bar was
        # invisible.
        def lit(w):
            i = w.index
            return i is not None and "-highlight" in w.children[i].classes

        queue = app.query_one("#queue", ListView)
        tags = app.query_one("#taglist", ListView)
        await reset()
        for k in (0, 3, 7):
            app._repaint_queue(keep=k)
            await settle(pilot, 0.2)
            if len(queue):
                check(lit(queue), "queue cursor is lit after a repaint",
                      f"keep={k} index={queue.index}")
        app._tag_sig = None
        app._paint_tags()
        await settle(pilot, 0.2)
        check(lit(tags), "tag cursor is lit after a repaint", f"index={tags.index}")
        await app.reload()
        await settle(pilot, 0.25)
        check(lit(queue), "queue cursor survives a reload", f"index={queue.index}")
        # and it stays lit while the focus is somewhere else, which is the
        # state you come back to from the editor
        app.set_focus(tags)
        await settle(pilot, 0.2)
        check(lit(queue), "queue cursor stays lit while unfocused")
        app.set_focus(queue)
        await settle(pilot, 0.15)
        print("  [ok] the cursor is visible, not just set")

        # and finally: keep pressing things
        rng = random.Random(11)
        await reset()
        for step in range(150):
            facets = app.tag_names[PV:]
            if app.pins and (rng.random() < 0.4 or not facets):
                app.action_unpin()
            elif facets:
                lst.index = rng.randrange(PV, len(app.tag_names))
                app.action_pin_tag()
            await settle(pilot, 0.01)
            check(len(app.rows) == truth(app), "walk: rows match query",
                  f"step {step} pins={app.pins}")
            check(len(app.pins) == len(set(app.pins)), "walk: no duplicates",
                  f"step {step} {app.pins}")
            check(len(app.pins) == len(app._pin_return), "walk: stacks in step",
                  f"step {step} {len(app.pins)}/{len(app._pin_return)}")
        print(f"  [ok] random walk, 150 moves")


def main():
    with tempfile.TemporaryDirectory(prefix="clife-pins-") as tmp:
        root = pathlib.Path(tmp)
        build_store(root)
        # both, because stream.py reads KB_DIR and paths.py reads CLIFE_KB
        os.environ["KB_DIR"] = str(root)
        os.environ["CLIFE_KB"] = str(root)
        asyncio.run(run())
    print()
    if FAILS:
        groups = {}
        for f in FAILS:
            groups.setdefault(f.split("  ")[0], []).append(f)
        print(f"FAILED — {len(FAILS)} checks in {len(groups)} groups")
        for name, rows in groups.items():
            print(f"  {name}  x{len(rows)}")
            for r in rows[:3]:
                print(f"      {r}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
