"""Panes, the apps' look, for the terminal: one palette shared by rich (the transcript, the cards, every
command's output) and prompt_toolkit (the composer, the pickers, the status row). Hexes rather than
`bright_cyan`/`ansicyan`, which the two libraries and the terminal's palette each render differently.
The values are the desktop's dark tokens (desktop/src/styles.css); terminals are taken to be dark."""
from __future__ import annotations

import os

from rich.theme import Theme

ACCENT = "#8b6cff"      # violet: where you act, the composer's edge, cursors, running spinners
ACCENT_HI = "#a58bff"   # the accent as text on the dark background (model name, picked rows)
TEXT = "#eeeaf8"
DIM = "#9a93b0"         # secondary text: tool summaries, details, hints that matter
FAINT = "#5f5a72"       # tertiary: keys, footers, chevrons
CARD = "#221f2c"        # the composer's fill and other raised cards
CODE = "#121018"        # code blocks
LINE = "#34304a"        # hairlines and quiet borders
OK = "#5be3a0"
WARN = "#ffc46b"
DANGER = "#ff7a8a"
CHIP_INK, CHIP_BG = "#d2c6ff", "#2c2742"  # inline `code`, commands named in prose
SELECTION = "#3b3070"  # a mouse selection over the transcript
CARD_EDGE = "#564696"   # the open card's and the wizards' quiet border, the accent half into the card colour
SHELL = "#fd5db1"      # shell mode (`!`): its prompt, border and mark stay pink, the cue that it's a shell
VISION_HUE = 248.0     # Vision's own buddy, as the apps draw it (ios Buddy.swift VisionFace)
VISION_TRAIT = "sprout"

RICH_THEME = Theme({
    "accent": ACCENT_HI,
    "accent.bold": f"bold {ACCENT_HI}",
    "ok": OK,
    "warn": WARN,
    "err": DANGER,
    "dim": DIM,  # rich's `dim` attribute fades whatever colour; Panes' dim is a colour of its own
    "faint": FAINT,
    "chip": f"{CHIP_INK} on {CHIP_BG}",
    "heading": f"bold {TEXT}",
    # rich's own defaults, which otherwise leak green, magenta and cyan into Vision's output
    "status.spinner": ACCENT,
    "progress.spinner": ACCENT,
    "prompt.choices": ACCENT_HI,
    "prompt.default": DIM,
    "prompt.invalid": DANGER,
    "rule.line": LINE,
    "table.header": f"bold {DIM}",
    "repr.number": f"{ACCENT_HI}",
    "repr.str": CHIP_INK,  # quoted text in an error line shouldn't read as success
    "repr.bool_true": OK,
    "repr.bool_false": DANGER,
    "repr.none": DIM,
    "repr.url": f"underline {ACCENT_HI}",
    "repr.path": DIM,
    "repr.filename": TEXT,
}, inherit=True)


def truecolor() -> bool:
    """Whether the terminal says it draws 24-bit colour; gradients fall back to a flat colour without it."""
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def mix(a: str, b: str, t: float) -> str:
    """The colour `t` of the way from hex `a` to hex `b`."""
    t = max(0.0, min(1.0, t))
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def hsl(h: float, s: float, l: float) -> str:
    """CSS hsl() (degrees, percent, percent) as hex, for buddies drawn from a hue like the apps'."""
    import colorsys

    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


# One way to say how something went, for every command and wizard (rich markup).
def ok(text: str) -> str:
    return f"  [ok]✓[/ok] {text}"


def warn(text: str) -> str:
    return f"  [warn]![/warn] {text}"


def err(text: str) -> str:
    return f"  [err]✗[/err] {text}"


def heading(title: str, step: str = "") -> str:
    """A section's title: `◆ Title`, the step in faint after it (`· step 2 of 4`)."""
    line = f"\n[accent]◆[/accent] [heading]{title}[/heading]"
    return line + (f" [faint]· {step}[/faint]" if step else "")
