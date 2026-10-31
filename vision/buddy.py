"""Vision's buddy: the apps' squishy creature (desktop/src/buddy.ts, ios Buddy.swift) in half-block pixels.

A 16x8-pixel grid drawn as 16x4 cells, each cell's top pixel the glyph's colour and its bottom pixel the
background, so it works in any terminal with 24-bit colour. Colours come from a hue with the apps'
formulas, so a scout looks the same here as on the phone; Vision's own is theme.VISION_HUE with a sprout.

`face()` and `mini()` draw any buddy still (the open card, `vision project list`). `Buddy` is the one in
the chat's bottom-left gutter: the chat's state picks a mood as the iPhone's VisionFace does, and small
touches animate inside the body: blinks and glances while idle, dots while thinking, a dot running along
the mouth while a tool runs, a mouth that moves with the voice, bars that follow the microphone, and the
bottom row filling as /talk's warmup finishes loading. The body never moves or changes size, apart from
sliding in from the left once when the chat opens ([buddy] slide_in).
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from prompt_toolkit.formatted_text import FormattedText
from rich.text import Text

from vision.theme import ACCENT_HI, DANGER, DIM, OK, VISION_HUE, VISION_TRAIT, WARN, hsl, mix
from vision.warmup import WarmupProgress, WarmupStatus

WIDTH = 16   # exactly the transcript label gutter
HEIGHT = 4   # four rows of half blocks; the state caption only appears in the narrow inline form
MIN_COLUMNS = 72  # narrower terminals get the one-line form on the status row instead
SLIDE_S = 0.45  # the entrance: from off the gutter's left edge into place, easing out

FACE_MOODS = ("curious", "blink", "focused", "listening", "proud", "found_it", "needs_you", "still_waiting", "worried", "asleep", "out_cold")
_DIM_MOODS = ("asleep", "out_cold")
_LEAF, _LEAF_LOW, _STEM = "#7ee394", "#3fae5a", "#3a8d4e"
_X0, _X1 = 3, 11          # the body's columns, centred on 7; rows 2-6, feet on row 7
_LEFT, _RIGHT = 5, 9      # the eyes' columns
_PROP_X = 13              # a mood's mark (! ✦ z …) sits here, by its head; 14 and 15 stay clear of the input frame
_PROPS = {"needs_you": ("!", WARN), "found_it": ("✦", OK), "worried": ("✗", DANGER), "asleep": ("z", DIM),
          "out_cold": ("z", DIM), "still_waiting": ("…", DIM), "focused": ("…", ACCENT_HI)}

Pixels = list[list[str | None]]


# ---------------------------------------------------------------- drawing
def palette(hue: float, mood: str = "curious") -> dict:
    """The body's colours for a hue, as buddy.ts `buddySvg` works them out (dimmed while asleep)."""
    h = hue
    if mood in _DIM_MOODS:
        return {"top": hsl(h, 24, 52), "bottom": hsl(h, 26, 40), "low": hsl(h, 24, 36), "glow": hsl(h, 30, 70), "ink": hsl(h, 35, 12)}
    return {"top": hsl(h, 58, 70), "bottom": hsl(h, 54, 55), "low": hsl(h, 48, 44), "glow": hsl(h, 70, 88), "ink": hsl(h, 35, 12)}


def _shade(c: dict, y: int) -> str:
    """The body's colour on pixel row `y`: light on top, darker towards the feet."""
    return c["top"] if y == 2 else mix(c["top"], c["bottom"], (y - 2) / 3) if y < 6 else c["low"]


def face_pixels(mood: str = "curious", hue: float = VISION_HUE, trait: str = VISION_TRAIT, level: float = 0.0,
                look: int = 0) -> Pixels:
    """The buddy as 16x8 pixels (None = see-through): a 9x5 rounded body centred on column 7, a trait on
    top, two feet, eyes whose shape is the mood. `level` (0-1) opens the mouth while listening; `look`
    (-1, 0, 1) moves the eyes a column left or right, a glance."""
    c = palette(hue, mood)
    g: Pixels = [[None] * WIDTH for _ in range(8)]
    for y in range(2, 7):
        for x in range(_X0, _X1 + 1):
            if y in (2, 6) and x in (_X0, _X1):
                continue  # the rounded corners
            g[y][x] = _shade(c, y)
    g[3][_X0 + 1] = mix(c["top"], "#ffffff", .35)  # the light along its top, left
    for x in (4, 10):
        g[7][x] = mix(c["low"], "#000000", .3)
    sleepy = mood in _DIM_MOODS
    droop = sleepy or mood in ("still_waiting", "worried")
    if trait == "sprout":
        leaf = mix(_LEAF, "#2a2833", .45) if sleepy else _LEAF
        g[1][7] = _STEM
        if droop:
            g[1][6] = g[1][8] = leaf
        else:
            g[0][6] = g[0][8] = leaf
            g[1][6] = _LEAF_LOW
    elif trait == "ears":
        g[2][_X0] = g[2][_X1] = c["top"]
        if not droop:
            g[1][_X0] = g[1][_X1] = c["top"]
    elif trait == "curl":
        g[1][7] = c["top"]
        g[0][7] = g[0][8] = c["top"]
    else:  # antenna, off to the right as the apps draw it
        bulb = WARN if mood == "needs_you" else OK if mood in ("proud", "found_it") else DANGER if mood == "worried" else c["low"] if droop else c["glow"]
        g[1][9] = c["low"]
        g[0][9] = bulb
    ink = c["ink"]
    left, right = _LEFT + look, _RIGHT + look

    def eye(x: int, y: int) -> None:
        g[y][x] = ink

    if mood in ("curious", "needs_you", "listening", "still_waiting"):
        for y in (3, 4):
            eye(left, y)
            eye(right, y)
        if mood == "still_waiting":  # heavy lids
            g[3][left] = g[3][right] = mix(c["top"], c["bottom"], .4)
        if mood == "listening":  # the mouth opens with the voice it hears: bars one or two pixels deep
            for x, weight in ((6, .6), (7, 1.0), (8, .6)):
                depth = min(2, math.floor(level * weight * 2 + .5))
                for y in range(5, 5 + depth):
                    eye(x, y)
    elif mood in ("blink", "asleep"):
        for x in (left - 1, left, right, right + 1):
            eye(x, 4)
    elif mood == "focused":  # eyes narrowed, looking down at the work
        for x in (left, left + 1, right, right + 1):
            eye(x, 4)
    elif mood == "proud":  # ^ ^ and a smile
        eye(left, 3)
        eye(right, 3)
        for x in (6, 7, 8):
            eye(x, 5)
    elif mood == "found_it":
        for y in (3, 4):
            eye(left, y)
            eye(right, y)
        eye(7, 5)
    elif mood == "worried":
        eye(left, 4)
        eye(right, 4)
        g[5][7] = ink
    elif mood == "out_cold":  # > <
        eye(left - 1, 3), eye(left, 4), eye(left - 1, 5)
        eye(right + 1, 3), eye(right, 4), eye(right + 1, 5)
    return g


def _cells(g: Pixels) -> list[list[tuple[str, str | None, str | None]]]:
    """Pixels to (glyph, foreground, background) cells, two pixel rows per text row."""
    rows = []
    for r in range(0, len(g), 2):
        row = []
        for top, bottom in zip(g[r], g[r + 1]):
            row.append(("▀", top, bottom) if top else ("▄", bottom, None) if bottom else (" ", None, None))
        rows.append(row)
    return rows


def half_blocks(g: Pixels) -> list[Text]:
    """Pixels to rows of half blocks for rich: two pixel rows per text row."""
    rows = []
    for row in _cells(g):
        t = Text()
        for glyph, fg, bg in row:
            t.append(glyph, f"{fg} on {bg}" if bg else fg or "")
        rows.append(t)
    return rows


def face(mood: str = "curious", hue: float = VISION_HUE, trait: str = VISION_TRAIT, props: bool = True, level: float = 0.0) -> list[Text]:
    """The buddy as four rich Text rows of 16 cells; `props` puts the mood's mark (! ✦ z …) by its head."""
    rows = half_blocks(face_pixels(mood, hue, trait, level))
    mark = _PROPS.get(mood) if props else None
    if mark:
        rows[0] = rows[0][:_PROP_X] + Text(mark[0], f"bold {mark[1]}") + Text("  ")
    return rows


def mini(hue: float = VISION_HUE, trait: str = VISION_TRAIT, mood: str = "curious") -> list[Text]:
    """A 6x2-cell face for lists and status lines: body, eyes, the trait as a pixel or two."""
    c = palette(hue, mood)
    g: Pixels = [[None] * 6 for _ in range(4)]
    for x in range(1, 5):
        g[1][x] = c["top"]
        g[3][x] = c["low"]
    for x in range(6):
        g[2][x] = c["bottom"]
    if mood in _DIM_MOODS:
        g[3][1] = g[3][4] = c["ink"]
    else:
        g[2][1] = g[2][4] = c["ink"]
    if trait == "sprout":
        g[0][2] = g[0][3] = _LEAF
    elif trait == "ears":
        g[0][0] = g[0][5] = g[1][0] = g[1][5] = c["top"]
    elif trait == "curl":
        g[0][3] = c["top"]
    else:
        g[0][4] = WARN if mood == "needs_you" else c["glow"]
    return half_blocks(g)


# ---------------------------------------------------------------- the chat's buddy
_MOODS = {"idle": "curious", "thinking": "focused", "tool": "focused", "chore": "focused", "speaking": "proud",
          "listening": "listening", "error": "worried", "asleep": "asleep"}
_CAPTION = {
    "idle": "", "thinking": "thinking", "chore": "", "listening": "listening",
    "speaking": "speaking", "error": "error", "asleep": "zz",
}
# The narrow form's face: (left eye, mouth, right eye) in ink on the body's colour.
_INLINE = {"curious": ("•", " ", "•"), "blink": ("─", " ", "─"), "focused": ("━", " ", "━"), "proud": ("^", "‿", "^"),
           "listening": ("•", " ", "•"), "worried": ("•", "~", "•"), "asleep": ("─", " ", "─")}
_FACE_SPAN = WIDTH - 1  # an inline caption fits fifteen cells, like the gutter without its gap
_BLINK_PERIOD_S = 4.0
_BLINK_DURATION_S = 0.14
_GLANCE_PERIOD_S = 7.0
_GLANCES = ((2.2, 2.9, 1), (4.8, 5.4, -1))  # (from, to, eye offset) within each glance period
_DOTS = ("․", "‥", "…")  # thinking: the dots by its head count up
_DOTS_STEP_S = 0.4
_TALK_STEP_S = 0.24     # speaking: the mouth opens and closes at about a syllable's pace
_SCAN_STEP_S, _SCAN_REST_S = 0.22, 0.5  # a tool: a dot runs along the mouth, then a pause
_BREATH_S = 3.0         # listening while quiet: the body's sides brighten and fade


def _glance(seconds: float) -> int:
    """Idle only: the eyes drift one column right, later one column left, and settle again."""
    phase = seconds % _GLANCE_PERIOD_S
    for start, end, offset in _GLANCES:
        if start < phase < end:
            return offset
    return 0


@dataclass(slots=True)
class Frame:
    """One drawn moment: the state, its 16x8 pixels, the mark by its head, and the narrow form."""

    state: str
    mood: str
    pixels: Pixels
    prop: tuple[str, str] | None   # (glyph, colour) by its head
    caption: str                   # the narrow form's words after the face
    inline: tuple[str, str]        # the narrow form's face: (text, prompt_toolkit style)

    def cells(self) -> list[list[tuple[str, str]]]:
        """The 16x4 gutter as (prompt_toolkit style, glyph) cells."""
        rows = [[(f"fg:{fg} bg:{bg}" if bg else f"fg:{fg}" if fg else "", glyph) for glyph, fg, bg in row]
                for row in _cells(self.pixels)]
        if self.prop:
            rows[0][_PROP_X] = (f"fg:{self.prop[1]} bold", self.prop[0])
        return rows

    def text(self) -> list[str]:
        """The glyphs alone, a row each (for tests and checks)."""
        return ["".join(glyph for _, glyph in row) for row in self.cells()]


class Buddy:
    """State holder + renderer. Thread-safe enough: fields are only ever assigned whole."""

    def __init__(self, name: str = "Vision", sleep_after_s: float = 600.0, slide_in: bool = False,
                 hue: float = VISION_HUE, trait: str = VISION_TRAIT):
        self.name = name
        self.sleep_after_s = sleep_after_s
        self.slide_in = slide_in
        self.hue, self.trait = hue, trait
        self._shown_at: float | None = None  # first drawn: the slide runs from there, not from start-up
        self.state = "idle"     # explicit state: idle | tool | chore | listening | error
        self.tool = ""          # the tool's name, or the chore's caption
        self.last_activity = time.time()
        self.speaking_fn = lambda: False   # the caller wires this to the audio pipeline
        self._blink_phase = random.random() * _BLINK_PERIOD_S
        self._warmup: WarmupProgress | None = None
        self._render_state = None
        self._render_since = 0.0
        self._level = (0.0, 0.0)

    # -- state changes (called from any thread)
    def touch(self) -> None:
        """Any user activity: wakes it up and clears a stale error face."""
        self.last_activity = time.time()
        if self.state in ("error", "asleep"):
            self.state = "idle"

    def using(self, tool: str) -> None:
        self._warmup = None
        self.state, self.tool = "tool", tool

    def working(self) -> None:
        """Back to plain thinking (text is streaming again after a tool call)."""
        if self.state == "tool":
            self.state = "idle"

    def chore(self, what: str) -> None:
        """Housekeeping that is not the brain thinking (warming up the ears, transcribing)."""
        self._warmup = None
        self.state, self.tool = "chore", what

    def warming(self, progress: WarmupProgress) -> None:
        """Attach this load attempt; later completions cannot resurrect a detached one."""
        self._warmup = progress
        self.state, self.tool = "chore", "warming up"

    def listening(self, on: bool = True) -> None:
        self._warmup = None
        if not on or self.state != "listening":
            self._level = (0.0, 0.0)
        self.state = "listening" if on else "idle"

    def hear(self, level: float) -> None:
        """A normalized, speech-gated microphone level. No simulated waveform in the CLI."""
        if self.state == "listening":
            self._level = (max(0.0, min(1.0, level)), time.time())

    def fail(self) -> None:
        self._warmup = None
        self.state = "error"

    def rest(self) -> None:
        """Turn finished: drop tool/chore/listening, keep an error face until the next activity."""
        self.last_activity = time.time()
        self._warmup = None
        if self.state in ("tool", "chore", "listening"):
            self.state = "idle"

    # -- rendering
    def resolve(self, busy: bool, now: float | None = None) -> str:
        if now is None:
            now = time.time()
        if self.state == "error":
            return "error"
        if self.speaking_fn():
            return "speaking"
        if self.state == "listening":
            return "listening"
        if self.state == "chore":
            return "chore"
        if busy and self.state == "tool":
            return "tool"
        if busy:
            return "thinking"
        if now - self.last_activity > self.sleep_after_s:
            return "asleep"
        return "idle"

    def _paint(self, busy: bool, now: float) -> Frame:
        st = self.resolve(busy, now)
        if st != self._render_state:
            self._render_state, self._render_since = st, now
        seconds = max(0.0, now - self._render_since)
        warmup = self._warmup
        progress: WarmupStatus | None = warmup.status if warmup is not None and st == "chore" else None
        level, sampled = self._level
        level *= max(0.0, 1 - max(0.0, now - sampled) / .25)  # a level fades out a quarter second after the last one
        mood = _MOODS[st]
        look = 0
        if st == "idle":
            if (now + self._blink_phase) % _BLINK_PERIOD_S < _BLINK_DURATION_S:
                mood = "blink"
            else:
                look = _glance(now + self._blink_phase)
        g = face_pixels(mood, self.hue, self.trait, level if st == "listening" else 0.0, look)
        c = palette(self.hue, mood)
        prop = None
        mouth = " "
        if st == "thinking":
            prop = (_DOTS[int(seconds / _DOTS_STEP_S) % len(_DOTS)], ACCENT_HI)
        elif st == "tool":
            # One dot runs along the mouth, left to right, then rests a moment before the next pass.
            span = _RIGHT - _LEFT + 1
            step = seconds % (span * _SCAN_STEP_S + _SCAN_REST_S) / _SCAN_STEP_S
            if step < span:
                g[5][_LEFT + int(step)] = mix(_shade(c, 5), c["ink"], .6)
        elif st == "speaking":
            if int(seconds / _TALK_STEP_S) % 2:  # open: the smile drops into a round mouth
                g[5][6] = g[5][8] = _shade(c, 5)
                g[6][7] = c["ink"]
                mouth = "o"
        elif st == "listening":
            if level > .05:
                mouth = " ▁▂▃▄"[min(4, math.floor(level * 4 + .5))]
            # The sides breathe while it is quiet, and brighten with a voice.
            breath = .1 + .3 * (1 - math.cos(seconds / _BREATH_S * math.pi * 2)) / 2
            for y in (3, 4, 5):
                for x in (_X0, _X1):
                    g[y][x] = mix(_shade(c, y), c["glow"], max(breath, .7 * level))
        elif st == "chore" and progress is not None:
            # The bottom row fills left to right with the share of the voice that has finished loading.
            fill = progress.fraction * (_X1 - _X0 - 1)
            for i, x in enumerate(range(_X0 + 1, _X1)):
                g[6][x] = mix(c["low"], c["glow"], fill - i)
        elif st == "error":
            prop = _PROPS["worried"]
        elif st == "asleep":
            prop = ("Z" if int(seconds) % 2 else "z", DIM)

        if st == "tool":
            caption = f"› {self.tool}"[:_FACE_SPAN]
        elif st == "chore":
            if progress is not None:
                caption = "ready 100%" if progress.percent == 100 else f"warmup {progress.percent}%"
            else:
                caption = self.tool[:_FACE_SPAN]
        else:
            caption = _CAPTION[st]
        left, middle, right = _INLINE[mood]
        inline = (f" {left}{mouth if mouth != ' ' else middle}{right} ", f"fg:{c['ink']} bg:{c['top']}")
        return Frame(st, mood, g, prop, caption, inline)

    def _hidden(self, now: float) -> int:
        """Columns of it still off the gutter's left edge: all of them on the first frame, none once
        it has slid in (and always none with slide_in off)."""
        if not self.slide_in:
            return 0
        if self._shown_at is None:
            self._shown_at = now
        t = min(1.0, max(0.0, now - self._shown_at) / SLIDE_S)
        return round(WIDTH * (1 - t) ** 3)

    def slid_in(self) -> bool:
        """The entrance is over (or there is none): the screen can go back to its usual refresh."""
        return not self.slide_in or (self._shown_at is not None and time.time() - self._shown_at >= SLIDE_S)

    def render(self, busy: bool) -> FormattedText:
        """The 16x4 buddy gutter."""
        now = time.time()
        rows = self._paint(busy, now).cells()
        hidden = self._hidden(now)
        parts = []
        for y, row in enumerate(rows):
            if y:
                parts.append(("", "\n"))
            parts.extend(row[hidden:])  # sliding in: its right side shows first, flush left
            if hidden:
                parts.append(("", " " * hidden))
        return FormattedText(parts)

    def render_inline(self, busy: bool) -> FormattedText:
        """A tiny face on the body's colour and the caption (`› Bash`, `warmup 33%`), for narrow terminals."""
        frame = self._paint(busy, time.time())
        parts = [(frame.inline[1], frame.inline[0])]
        if frame.caption:
            colour = DANGER if frame.state == "error" else DIM if frame.state == "asleep" else ACCENT_HI
            parts += [("", " "), (f"fg:{colour}", frame.caption)]
        return FormattedText(parts)
