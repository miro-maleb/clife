"""todo_cli.py — `cl todo`, the terminal face of ~/kb/todo.md.

All reading and writing is todo.py's; this only parses argv and prints.

  cl todo [list] [--json]        open items, Sooner then Later
  cl todo add [--sooner] TEXT    append to Later (or Sooner)
  cl todo done MATCH             delete the one open item matching every word
  cl todo sweep                  drop checked lines (phone/Obsidian check-offs)
"""
import argparse
import json
import sys

import todo


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cl todo", description="the todo list (~/kb/todo.md)")
    sub = ap.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="open items, Sooner then Later")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--sooner", action="store_true", help="only the Sooner band")
    a = sub.add_parser("add", help="add an item (Later unless --sooner)")
    a.add_argument("--sooner", action="store_true")
    a.add_argument("text", nargs="+")
    d = sub.add_parser("done", help="delete the one open item matching every word")
    d.add_argument("match", nargs="+")
    sub.add_parser("sweep", help="drop checked lines")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    cmd = args.cmd or "list"

    if cmd == "list":
        rows = todo.open_items("Sooner" if getattr(args, "sooner", False) else None)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=1))
            return
        for band in todo.BANDS:
            got = [r for r in rows if r["band"] == band]
            if got:
                print(f"  ── {band} ({len(got)})")
                print("\n".join(f"  - {r['text']}" for r in got))
        if not rows:
            print("  todo.md is empty")
    elif cmd == "add":
        try:
            r = todo.add(" ".join(args.text), sooner=args.sooner)
        except ValueError as e:
            sys.exit(f"cl todo: {e}")
        print(f"  + {r['band']}: {r['text']}")
    elif cmd == "done":
        r = todo.done(" ".join(args.match))
        if r["ok"]:
            print(f"  - done: {r['removed']}")
        elif not r["matches"]:
            sys.exit("cl todo: no open item matches that")
        else:
            sys.exit("cl todo: ambiguous — matches:\n  " + "\n  ".join(r["matches"]))
    elif cmd == "sweep":
        print(f"  swept {todo.sweep()} checked line(s)")


if __name__ == "__main__":
    main()
