"""The Surface palette, for the terminal.

ONE definition of the colours, so a TUI and the web lens cannot drift apart.
The values are copied from the `:root` block of
`~/hearth/surface/static/style.css` — true black, neutral greys, one warm
accent. Nothing here is a guess: if Surface's `:root` changes, change these and
every box that imports them follows.

The old clife palette was `steel_blue1` + `dark_sea_green4` + `grey50`, i.e. the
cool blue Surface deliberately moved away from. Nine modules still say
steel_blue1; they are not wrong, just old — convert one when you next touch it
rather than in a sweep.

Written as rich style strings so call sites stay readable:

    console.print(f"[{DIM}]…[/{DIM}]")
    console.print(CHIP("CAPTURE"), …)
"""

BG      = "#000000"   # --bg      true black
CARD    = "#101010"   # --card    raised surface
LINE    = "#262626"   # --line    neutral border — subtle on purpose
INK     = "#ededed"   # --ink     near-white
DIM     = "#a1a1a1"   # --dim     secondary
MUTE    = "#6f6f6f"   # --mute    tertiary
ACCENT  = "#d98b3a"   # --accent  the one warm colour
ON_ACCENT = "#000000" # --on-accent
DONE    = "#3ba55d"   # --done    green


def CHIP(label):
    """The `.cap-chip` pill: accent fill, black bold text, one space of padding.

    A terminal has no border-radius, so the pill is just the fill — which is
    what actually reads as "Surface" at a glance."""
    return f"[bold {ON_ACCENT} on {ACCENT}] {label} [/]"


RULE = f"{LINE}"          # a divider, drawn in the border colour
PROMPT = ACCENT           # Surface sets caret-color:var(--accent)
