"""Assembling a provider's reply text without the narration models write before a tool call.

Codex and Grok tend to say "I'm checking the forecast." as a message of its own, run the tool, then
answer; the user then reads (or hears) a stage direction before the reply. `ReplyText` holds a short
first line back until the next event: a tool call drops it (the status line already says a tool is
running), anything else flushes it as ordinary text. After each tool the hold applies again, so
"Now let me look at X." between tools goes the same way. Text longer than a line, or spanning
lines, or running to a second sentence, is never held: it is an answer or a warning ("This wipes
the build directory. Running it now."), and streams straight through.
"""
from __future__ import annotations

import re
from typing import Callable

# What a brain reports through `on_status` besides a tool name. A brain only says what it can see
# in its stream; `status_label` turns it into the words on the live line. `thinking` is sent only
# while a real reasoning block is streaming, so "thinking…" is never a guess.
WORKING = ""           # the model's turn and nothing has arrived yet (request in flight)
THINKING = "thinking"  # a reasoning block is streaming
READING = "reading"    # every tool result is in; the model has not started its next block
WRITING = "writing"    # a message is being produced but arrives whole (Codex)
_LABELS = {WORKING: "working…", THINKING: "thinking…", READING: "reading results…", WRITING: "writing…"}


def status_label(status: str) -> str:
    """The live-line words for a brain status: `using Bash…`, `thinking…`, `reading results…`.
    A status that already ends in an ellipsis is a ready-made line (the supervisor's
    `Opus 5 is running Bash…`) and is shown as it is."""
    if status.endswith("…"):
        return status
    return _LABELS.get(status) or f"using {status}…"


def retry_label(attempt: int | None, max_retries: int | None, delay_ms: int | None, reason: str = "") -> str:
    """The live-line words while a brain waits out an API retry (`overloaded, retrying in 4 s (2/10)…`).
    Ends in an ellipsis so `status_label` shows it as it is."""
    bits = []
    if reason:
        bits.append(reason)
    when = f"retrying in {max(1, round(delay_ms / 1000))} s" if delay_ms else "retrying"
    if attempt and max_retries:
        when += f" ({attempt}/{max_retries})"
    bits.append(when)
    return ", ".join(bits) + "…"


def dedupe_status(on_status: Callable[[str], None] | None) -> Callable[[str], None] | None:
    """Wrap `on_status` so a repeat (Grok sends a `thought` line per chunk) does not redraw the live line."""
    if on_status is None:
        return None
    last: list[str | None] = [None]

    def call(status: str) -> None:
        if status != last[0]:
            last[0] = status
            on_status(status)
    return call

HOLD_CHARS = 120  # narration is one short sentence; anything longer streams as it arrives
_SECOND_SENTENCE = re.compile(r"[.!?]\s+\S")  # a sentence ended and another began


class ReplyText:
    def __init__(self, on_text: Callable[[str], None] | None = None, paragraphs: bool = False):
        """`paragraphs`: separate every emitted message with a blank line (Codex sends whole
        messages); otherwise only text that follows a tool call starts a new paragraph."""
        self.on_text = on_text
        self.paragraphs = paragraphs
        self.parts: list[str] = []
        self.dropped: list[str] = []  # the narration lines that were held and then dropped
        self._held = ""
        self._holding = True
        self._after_tool = False

    def add(self, chunk: str) -> None:
        """Text from the model, whole or streamed."""
        if not chunk:
            return
        if self._holding:
            if self.paragraphs and self._held:
                chunk = "\n\n" + chunk
            self._held += chunk
            held = self._held.strip()
            if len(held) > HOLD_CHARS or "\n" in held or _SECOND_SENTENCE.search(held):
                self._flush()
            return
        self._emit(chunk)

    def tool(self) -> None:
        """A tool call started: a held line was narration."""
        if self._holding and self._held.strip():
            self.dropped.append(self._held.strip())
        self._held = ""
        self._holding = True
        self._after_tool = True

    def finish(self) -> str:
        """The turn is over (or cancelled): whatever is held is real text."""
        self._flush()
        return "".join(self.parts)

    @property
    def last(self) -> str | None:
        """The last message emitted, without its paragraph separator (Codex task replies)."""
        return self.parts[-1].lstrip("\n") if self.parts else None

    def _flush(self) -> None:
        if self._held:
            self._emit(self._held)
            self._held = ""
        self._holding = False

    def _emit(self, chunk: str) -> None:
        if self.parts and (self._after_tool or self.paragraphs) and not self.parts[-1].endswith("\n"):
            chunk = "\n\n" + chunk
        self._after_tool = False
        self.parts.append(chunk)
        if self.on_text:
            self.on_text(chunk)
