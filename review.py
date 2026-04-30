import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from rich.rule import Rule

import projects
import notes

console = Console()

SECTIONS = [
    ("projects", lambda: projects.main(statuses=projects.REVIEW_STATUSES)),
    ("notes",    notes.main),
]


def main():
    console.print()
    console.print(Rule(
        "[bold steel_blue1]  Full Review[/bold steel_blue1]  [grey50]projects → notes[/grey50]",
        style="steel_blue1 dim"
    ))

    for name, fn in SECTIONS:
        fn()
        console.print(f"  [grey35]— {name} done. press any key to continue, q to quit —[/grey35]  ", end="")

        fd = sys.stdin.fileno()
        import tty, termios
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            key = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        console.print()
        if key == "q":
            break

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


if __name__ == "__main__":
    main()
