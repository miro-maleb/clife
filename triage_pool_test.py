#!/usr/bin/env python3
"""Regression suite for POOL MEMBERSHIP keys in the triage navigator.

    ./venv/bin/python3 triage_pool_test.py

`s` (sooner / later) and `t` (on the plate / off it) write tags, and the two
tags they write are the one pair `cl stream set --tag` will not accept: a
store where nothing yet carries `todo/sooner` reads it as a near-duplicate of
`todo` and refuses the whole command. So `s` notified "'todo/sooner' looks
like: todo" and wrote nothing, forever, for exactly as long as the child had
no notes — which is the state every store starts in.

Like the pins suite this builds its own store in a temp directory and drives
the real app through Pilot, then reads the FRONTMATTER back from disk. The
notify() path returns normally on a refusal, so an assertion against the app's
own state would have passed while nothing was written.
"""
import asyncio
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILS = []


def check(cond, what, extra=""):
    if not cond:
        FAILS.append(f"{what}  {extra}")
    return cond


def build_store(root: pathlib.Path):
    """Deliberately NO note carries `todo/sooner` — that is the bug's state."""
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (root / "_state").mkdir(exist_ok=True)
    fixture = [("a", ["todo"]), ("b", ["todo", "hearth"]),
               ("c", ["hearth"]), ("d", []), ("e", ["todo"])]
    for i, (name, tags) in enumerate(fixture):
        (notes / f"{name}.md").write_text(
            f"---\ncreated: 2026-09-1{i} 10:0{i}\n"
            f"tags: [{', '.join(tags)}]\n---\n\nnote {name} body\n")


async def settle(pilot, t=0.05):
    await asyncio.sleep(t)
    await pilot.pause()


def on_disk(root: pathlib.Path, slug: str) -> list:
    """Tags as the FILE has them, read without clife's help."""
    import yaml
    text = (root / "notes" / f"{slug}.md").read_text()
    fm = text.split("---", 2)[1]
    return sorted(yaml.safe_load(fm).get("tags") or [])


async def run(root: pathlib.Path):
    import triage
    import triage_tui

    app = triage_tui.TriageApp(view=triage_tui.POOL_TAG)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, 0.3)

        async def cursor_to(slug):
            await app.reload()
            await settle(pilot)
            i = next((i for i, r in enumerate(app.rows) if r["slug"] == slug),
                     None)
            check(i is not None, "fixture note is in the pool", slug)
            app._cursor_to(i or 0)
            await settle(pilot)

        # `s` on a plain todo: the child replaces the parent, ON DISK.
        await cursor_to("a")
        await app.action_sooner()
        await settle(pilot)
        check(on_disk(root, "a") == ["todo/sooner"], "s writes todo/sooner",
              f"got {on_disk(root, 'a')}")

        # and back again, without dropping the note off the plate
        await cursor_to("a")
        await app.action_sooner()
        await settle(pilot)
        check(on_disk(root, "a") == ["todo"], "s again returns it to later",
              f"got {on_disk(root, 'a')}")

        # a note with other tags keeps them through both directions
        await cursor_to("b")
        await app.action_sooner()
        await settle(pilot)
        check(on_disk(root, "b") == ["hearth", "todo/sooner"],
              "s leaves unrelated tags alone", f"got {on_disk(root, 'b')}")
        await cursor_to("b")
        await app.action_sooner()
        await settle(pilot)
        check(on_disk(root, "b") == ["hearth", "todo"],
              "and back", f"got {on_disk(root, 'b')}")

        # `t` takes an urgent note off the plate entirely — child included
        await cursor_to("e")
        await app.action_sooner()
        await settle(pilot)
        check(on_disk(root, "e") == ["todo/sooner"], "e is urgent",
              f"got {on_disk(root, 'e')}")
        await cursor_to("e")
        await app.action_todo()
        await settle(pilot)
        check(on_disk(root, "e") == [], "t takes the child off too",
              f"got {on_disk(root, 'e')}")

        # `t` puts an untagged note on the plate
        app.view_tag = triage.UNTAGGED
        await cursor_to("d")
        await app.action_todo()
        await settle(pilot)
        check(on_disk(root, "d") == ["todo"], "t puts it on the plate",
              f"got {on_disk(root, 'd')}")

        # the guard still guards: an ordinary near-duplicate is refused
        app.view_tag = triage.ALL
        await cursor_to("c")
        res = app._write(app._current(), "--tag", "hearthh")
        check(not res.get("ok") and res.get("blocked"),
              "near-duplicate guard still refuses", str(res)[:120])
        check(on_disk(root, "c") == ["hearth"], "and wrote nothing",
              f"got {on_disk(root, 'c')}")
    print("  [ok] pool keys write the tags they name")


def main():
    with tempfile.TemporaryDirectory(prefix="clife-pool-") as tmp:
        root = pathlib.Path(tmp)
        build_store(root)
        os.environ["KB_DIR"] = str(root)
        os.environ["CLIFE_KB"] = str(root)
        asyncio.run(run(root))
    print()
    if FAILS:
        print(f"FAILED — {len(FAILS)} checks")
        for f in FAILS:
            print(f"  {f}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
