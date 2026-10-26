"""Pip Capsule: a graphite shell, a 9-wide inset face, and jade eyes in a 16×4 gutter.

Flat foreground/background colors work in ordinary terminals. Idle glances and blinks;
thinking and tool use animate within the face; listening follows microphone levels, and
warmup follows component completion. The body never moves or changes size, apart from sliding in
from the left once when the chat opens ([buddy] slide_in).
"""
from __future__ import annotations

import random
import math
import time

from prompt_toolkit.formatted_text import FormattedText

from vision.warmup import WarmupProgress, WarmupStatus

WIDTH = 16   # exactly the transcript label gutter
HEIGHT = 4   # four sprite rows; the state caption only appears in the narrow inline form
MIN_COLUMNS = 72  # narrower terminals get the one-line form on the status row instead

_FACE_SPAN = WIDTH - 1  # an inline caption fits the first fifteen cells; the sixteenth is the gap to the input frame
_SHELL, _LOWER, _FACE, _EYE = "#899a90", "#4c6054", "#202d26", "#c1ebd1"
FACE_BG = _FACE  # the inset well behind the eyes; the open card paints it the same way the gutter does
_BASE = ("   ▄▄▄▄▄▄▄▄▄    ", "  ▐         ▌   ", "  ▐         ▌   ", "   ▀▀▀▀▀▀▀▀▀    ")
FACE_X0, FACE_X1 = 3, 11        # the well between the rails (rails sit at 2 and 12)
_LEFT_EYE, _RIGHT_EYE, _MOUTH = 5, 9, 7
_RAILS = (FACE_X0 - 1, FACE_X1 + 1)
_CAPTION = {
    "idle": "", "thinking": "thinking", "chore": "", "listening": "listening",
    "speaking": "speaking", "error": "error", "asleep": "zz",
}
_ANIMATION_STEP_S = 0.48
_BLINK_PERIOD_S = 4.0
_BLINK_DURATION_S = 0.14
_GLANCE_PERIOD_S = 7.0
_GLANCES = ((2.2, 2.9, 1), (4.8, 5.4, -1))  # (from, to, eye offset) within each glance period
SLIDE_S = 0.45  # the entrance: from off the gutter's left edge into place, easing out


def _glance(seconds: float) -> int:
    """Idle only: the eyes drift one cell right, later one cell left, and settle again."""
    phase = seconds % _GLANCE_PERIOD_S
    for start, end, offset in _GLANCES:
        if start < phase < end:
            return offset
    return 0


def _blend(a: str, b: str, amount: float) -> str:
    """Match the mockup's sixteen flat-color steps (including JS half-up rounding)."""
    mix = math.floor(max(0, min(1, amount)) * 16 + 0.5) / 16
    return "#" + "".join(
        f"{math.floor(int(a[i:i + 2], 16) * (1 - mix) + int(b[i:i + 2], 16) * mix + 0.5):02x}"
        for i in (1, 3, 5)
    )


def _art(state: str, seconds: float, progress: WarmupStatus | None, level: float, blink: bool, look: int = 0):
    """The Capsule study, rendered as four rows of glyphs and colors."""
    rows = [list(row) for row in _BASE]
    colors = [["#4f6255" if state == "asleep" else _LOWER if y == 3 else _SHELL for _ in row] for y, row in enumerate(rows)]
    accent = "#d8a191" if state == "error" else "#738378" if state == "asleep" else _EYE

    def put(y, x, char, color=accent):
        rows[y][x], colors[y][x] = char, color

    left, mouth, right = "▰", "━", "▰"
    if state == "idle" and blink:
        left = right = "─"
    elif state == "speaking":
        mouth = "O" if int(seconds / _ANIMATION_STEP_S) % 2 else "o"
    elif state == "error":
        left, mouth, right = "╱", "⌁", "╲"
    elif state == "asleep":
        left, mouth, right = "─", "·", "─"
        put(0, 14, "Z" if int(seconds) % 2 else "z")
    if state != "idle":
        look = 0
    put(1, _LEFT_EYE + look, left)
    put(1, _RIGHT_EYE + look, right)
    put(2, _MOUTH, mouth)

    if state == "thinking":
        phase = seconds % 2.4 / .8
        for index, x in enumerate((_MOUTH - 2, _MOUTH, _MOUTH + 2)):
            distance = min(abs(phase - index), 3 - abs(phase - index))
            put(2, x, "·", _blend("#5b6558", "#dbcaa5", max(0, 1 - distance)))
    elif state == "chore":
        fraction = progress.fraction if progress is not None else 0
        fill = fraction * (FACE_X1 - FACE_X0 - 1)
        for x in range(FACE_X0 + 1, FACE_X1):
            distance = abs(x - _MOUTH)
            coverage = fill if distance == 0 else (fill - (2 * distance - 1)) / 2
            colors[3][x] = _blend(_LOWER, "#a9d8bd", coverage)
        for x in (_LEFT_EYE, _RIGHT_EYE):
            put(1, x, "▰", _blend("#59796a", _EYE, .55 + .45 * fraction))
    elif state == "listening":
        breath = .2 + .3 * (1 - math.cos(seconds / 3 * math.pi * 2)) / 2
        for x in _RAILS:
            colors[1][x] = _blend(_SHELL, _EYE, max(breath, level))
        if level > .05:
            for i, weight in enumerate((.35, .7, 1, .7, .35)):
                height = min(5, math.floor(level * weight * 5 + .5))
                put(2, _MOUTH - 2 + i, "▁▂▃▄▅▆"[height])
    elif state == "tool":
        put(2, _MOUTH, " ")
        put(2, _LEFT_EYE, "›", _blend(_FACE, _EYE, .65))
        cycle = seconds % 2.4
        position, opacity = 0.0, 0.0
        if cycle < .2:
            opacity = cycle / .2
        elif cycle < 1.5:
            phase = (cycle - .2) / 1.3
            position = (2 * phase * phase if phase < .5 else 1 - (-2 * phase + 2) ** 2 / 2) * 4
            opacity = 1
        elif cycle < 1.9:
            position, opacity = 4.0, 1 - (cycle - 1.5) / .4
        else:
            position = 4.0
        cell, between = min(4, int(position)), position % 1

        def ink(x, char, strength):
            if strength > .02:
                put(2, x, char, _blend(_FACE, "#d0e7d9" if char == "━" else "#6c927e", strength))

        if cell > 0:
            ink(_LEFT_EYE + 1 + cell, "─", opacity * .38 * (1 - between))
        ink(_LEFT_EYE + 2 + cell, "━", opacity * (.38 + .62 * (1 - between)))
        if cell < 4:
            ink(_LEFT_EYE + 3 + cell, "━", opacity * between)
    return tuple("".join(row) for row in rows), colors, accent


def idle_sprite() -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Still idle Pip: four glyph rows and matching hex colours, 16 cells with gutter padding."""
    rows, colors, _ = _art("idle", 0.0, None, 0.0, False)
    return rows, tuple(tuple(row) for row in colors)


class Buddy:
    """State holder + renderer. Thread-safe enough: fields are only ever assigned whole."""

    def __init__(self, name: str = "Pip", sleep_after_s: float = 600.0, slide_in: bool = False):
        self.name = name
        self.sleep_after_s = sleep_after_s
        self.slide_in = slide_in
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

    def _paint(self, busy: bool, now: float):
        st = self.resolve(busy, now)
        if st != self._render_state:
            self._render_state, self._render_since = st, now
        seconds = max(0, now - self._render_since)
        warmup = self._warmup
        progress = warmup.status if warmup is not None and st == "chore" else None
        level, sampled = self._level
        level *= max(0, 1 - max(0, now - sampled) / .25)
        blink = (now + self._blink_phase) % _BLINK_PERIOD_S < _BLINK_DURATION_S
        rows, colors, accent = _art(st, seconds, progress, level, blink, _glance(now + self._blink_phase))
        if st == "tool":
            caption = f"› {self.tool}"[:_FACE_SPAN]
        elif st == "chore":
            if progress is not None:
                caption = "ready 100%" if progress.percent == 100 else f"warmup {progress.percent}%"
            else:
                caption = self.tool[:_FACE_SPAN]
        else:
            caption = _CAPTION[st]
        eyes = rows[1][FACE_X0:FACE_X1 + 1].strip()  # a glance shifts them, so read what is lit
        lower = rows[2][_LEFT_EYE:_RIGHT_EYE + 1].strip()
        inline = f"{eyes[0]} {lower} {eyes[-1]}"
        if st == "asleep":
            inline += " " + rows[0][14]
        return st, rows, inline, caption, accent, colors

    def _frame(self, busy: bool, now: float):
        return self._paint(busy, now)[:5]

    def _hidden(self, now: float) -> int:
        """Columns of him still off the gutter's left edge: all of them on the first frame, none once
        he has slid in (and always none with slide_in off)."""
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
        """The 16x4 buddy gutter: the four sprite rows."""
        now = time.time()
        _, rows, _, _, _, colors = self._paint(busy, now)
        hidden = self._hidden(now)
        parts = []
        for y, row in enumerate(rows):
            if y:
                parts.append(("", "\n"))
            for x in range(hidden, len(row)):  # sliding in: his right side shows first, flush left
                style = colors[y][x]
                if 1 <= y <= 2 and FACE_X0 <= x <= FACE_X1:
                    style += f" bg:{_FACE}"
                parts.append((style, row[x]))
            if hidden:
                parts.append(("", " " * hidden))
        return FormattedText(parts)

    def render_inline(self, busy: bool) -> FormattedText:
        """The same eyes, expression, and real progress for narrow terminals."""
        _, _, inline, caption, style = self._frame(busy, time.time())
        parts = [(style, f"[{inline}]")]
        if caption:
            parts += [("", " "), (style, caption)]
        return FormattedText(parts)
