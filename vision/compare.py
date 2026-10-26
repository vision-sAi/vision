"""/compare: one prompt to several exact brains at once, the answers side by side.

`/compare opus:high gpt-6-astra:medium grok-4.6 which of these is right?` names the brains as the
exact model (any name /model takes) with an optional `:effort`, then the prompt. Each brain is a fresh
driver of its own, read-only (plan-mode permissions, so two of them never edit the same files at the
same time), given the conversation so far the way a cross-provider handoff is, and thrown away after
its answer. The conversation itself does not see the turn: nothing is sent to the chat's own brain and
no session is resumed or remembered.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_COLUMN = 40  # narrower than this per brain and the answers stack instead of sitting side by side
PROGRESS_EVERY_S = 0.25  # how often streaming text alone refreshes the live line (status changes refresh at once)

# The brains run with plan-mode permissions (read-only), whose persona asks for a plan. A comparison
# wants the answer itself, so the prompt says so; reads and read-only commands are still fine.
PREFACE = ("This question is being put to several models at once and their answers compared side by side. "
           "Answer it directly and completely in this one reply (reading files and running read-only commands "
           "is fine); do not propose a plan, ask whether to proceed, or wait for approval.\n\n")


@dataclass(frozen=True)
class Pick:
    """One brain of a comparison: the exact model and the effort it runs at."""

    model: str
    effort: str

    def label(self) -> str:
        from vision.models import model_label

        return f"{model_label(self.model)} ({self.effort})" if self.effort else model_label(self.model)

    def spec(self) -> str:
        """The token that names it on the command line: `opus:high`."""
        return f"{self.model}:{self.effort}" if self.effort else self.model


def is_model(token: str) -> bool:
    """Whether a /compare token names a model: a catalogue alias, or `<server>/<model>` for a
    config-defined server (vision/providers.py), the same names /model accepts."""
    from vision.models import find, provider_for
    from vision.providers import get

    if find(token):
        return True
    head, sep, _ = token.partition("/")
    return bool(sep) and provider_for(token) == head and get(head) is not None


def parse(arg: str, current_effort: str) -> tuple[list[Pick], str, str]:
    """Split a /compare argument into (picks, prompt, error). The leading tokens that resolve as a
    model (with an optional `:effort`) are the brains; the first that does not starts the prompt.
    A pick without an effort gets the session's effort fitted to that model. Error is "" when fine."""
    from vision.models import coerce_effort, efforts_for, model_label, supports_effort

    picks: list[Pick] = []
    words = arg.split()
    i = 0
    while i < len(words):
        name, _, effort = words[i].partition(":")
        if not name or not is_model(name):
            break
        if effort:
            if not supports_effort(name, effort):
                levels = ", ".join(efforts_for(name)) or "no effort setting"
                return [], "", f"{model_label(name)} doesn't take effort {effort} ({levels})"
        else:
            effort, _ = coerce_effort(name, current_effort)
        pick = Pick(name, effort)
        if pick not in picks:
            picks.append(pick)
        i += 1
    prompt = " ".join(words[i:]).strip()
    if words and not picks:
        return [], "", f"{words[0]} isn't a model /model knows; /compare opus:high gpt-6-astra:medium <prompt>"
    if len(picks) == 1:
        return [], "", f"name at least two models to compare (only {picks[0].label()} given)"
    return picks, prompt, ""


def picks_from_labels(answer: str, current_effort: str) -> list[Pick]:
    """The picks ticked in the multi-select form: its answer is the model names joined by ', '."""
    names = [w.strip() for w in answer.split(",") if w.strip()]
    picks, _, _ = parse(" ".join(names), current_effort)
    return picks


def armed_label(picks: list[Pick]) -> str:
    return " · ".join(p.label() for p in picks)


@dataclass
class Answer:
    pick: Pick
    text: str = ""
    status: str = ""  # the brain's live status (`Bash`, `thinking`…), "" while text flows
    error: str = ""
    tokens: int = 0  # output tokens, where the driver counts them
    elapsed: float = 0.0
    done: bool = False
    started: float = field(default_factory=time.monotonic)

    def stats(self) -> str:
        parts = [f"{self.elapsed:.1f}s"]
        if self.tokens:
            parts.append(f"↓{_short(self.tokens)}")
        if self.error == "cancelled":
            parts.append("cancelled")
        elif self.error:
            parts.append("failed")
        return " · ".join(parts)


def _short(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Comparison:
    """Runs one prompt on every pick at once. `make_brain(pick)` builds a fresh driver for a pick
    (the chat supplies it with the right config and the handoff transcript; tests hand in fakes).
    `on_change` is called whenever the live line should be redrawn (see progress)."""

    def __init__(self, picks: list[Pick], prompt: str, make_brain: Callable[[Pick], object],
                 on_change: Callable[[], None] | None = None):
        self.picks = picks
        self.prompt = prompt
        self.make_brain = make_brain
        self.on_change = on_change or (lambda: None)
        self.answers = [Answer(p) for p in picks]
        self.cancelled = False
        self._brains: list = []
        self._lock = threading.Lock()
        self._last_change = 0.0

    def run(self) -> list[Answer]:
        """Ask every brain in its own thread and wait for them all. Blocking: run it off the UI thread."""
        threads = [threading.Thread(target=self._one, args=(a,), daemon=True, name=f"compare-{a.pick.model}") for a in self.answers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return self.answers

    def cancel(self) -> None:
        """Esc: stop every brain still at work; their partial answers stay."""
        self.cancelled = True
        with self._lock:
            brains = list(self._brains)
        for b in brains:
            try:
                b.cancel()
            except Exception:  # noqa: BLE001  (a brain that already finished has nothing to stop)
                pass

    def _one(self, a: Answer) -> None:
        def on_text(delta: str) -> None:
            a.text += delta
            a.status = ""
            self._changed()

        def on_status(tool: str) -> None:
            a.status = tool or ""
            self._changed(now=True)

        brain = None
        try:
            brain = self.make_brain(a.pick)
            with self._lock:
                self._brains.append(brain)
            if self.cancelled:
                raise RuntimeError("cancelled")
            turn = brain.ask(PREFACE + self.prompt, on_text=on_text, on_status=on_status)
            if getattr(turn, "text", None):
                a.text = turn.text  # the whole reply as the driver saw it (a delta may have been missed)
            if getattr(turn, "is_error", False):
                a.error = turn.error or "failed"
            usage = getattr(turn, "usage", None) or {}
            a.tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or getattr(brain, "output_tokens", 0) or 0)
        except Exception as e:  # noqa: BLE001  (one brain failing must not take the others with it)
            a.error = "cancelled" if self.cancelled else str(e)
        finally:
            if self.cancelled and not a.error:
                a.error = "cancelled"
            a.elapsed = time.monotonic() - a.started
            a.done = True
            if brain is not None:
                try:
                    getattr(brain, "close", brain.cancel)()  # nothing kept alive for a next turn: there is none
                except Exception:  # noqa: BLE001
                    pass
            self._changed(now=True)

    def _changed(self, now: bool = False) -> None:
        t = time.monotonic()
        if now or t - self._last_change >= PROGRESS_EVERY_S:
            self._last_change = t
            self.on_change()

    def progress(self) -> str:
        """The live line: `Opus 5 (high): ↓1.2k · GPT-6 Astra (medium): using Bash… · Grok 4.6 (high): done`."""
        from vision.reply import status_label

        parts = []
        for a in self.answers:
            if a.done:
                state = "failed" if a.error and a.error != "cancelled" else ("cancelled" if a.error else "done")
            elif a.status:
                state = status_label(a.status)
            elif a.text:
                state = f"↓{_short(a.tokens or max(1, len(a.text) // 4))}"
            else:
                state = "thinking…"
            parts.append(f"{a.pick.label()}: {state}")
        return " · ".join(parts)


# -- rendering -----------------------------------------------------------------------------------------
class CompareView:
    """The finished comparison in the transcript: a column per brain when the terminal is wide enough
    for them (MIN_COLUMN each), the answers stacked as panels otherwise. Decided at render time, so a
    resized terminal re-lays it out."""

    def __init__(self, answers: list[Answer], prompt: str, untouched: str = ""):
        self.answers = answers
        self.prompt = prompt
        self.untouched = untouched  # the chat's own brain, which did not see this turn

    def _cell(self, a: Answer):
        from rich.console import Group
        from rich.text import Text

        from vision.ui import ChatMarkdown

        parts = []
        if a.text.strip():
            parts.append(ChatMarkdown(a.text.strip()))
        if a.error and a.error != "cancelled":
            parts.append(Text(f"could not answer: {a.error}", style="red"))
        elif a.error:
            parts.append(Text("cancelled", style="yellow"))
        elif not a.text.strip():
            parts.append(Text("(no answer)", style="dim"))
        return Group(*parts)

    def __rich_console__(self, console, options):
        from rich.box import ROUNDED
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        width = options.max_width
        n = len(self.answers)
        if n and width // n >= MIN_COLUMN:
            table = Table(box=ROUNDED, expand=True, show_header=True, show_footer=True, padding=(0, 1), border_style="dim")
            for a in self.answers:
                table.add_column(Text(a.pick.label(), style="bold"), footer=Text(a.stats(), style="dim"), ratio=1, overflow="fold")
            table.add_row(*(self._cell(a) for a in self.answers))
            yield table
        else:
            for a in self.answers:
                yield Panel(self._cell(a), title=Text(a.pick.label(), style="bold"), subtitle=Text(a.stats(), style="dim"),
                            title_align="left", subtitle_align="right", box=ROUNDED, border_style="dim")
        tail = "compared outside the conversation"
        if self.untouched:
            tail += f" · nothing was sent to {self.untouched}"
        yield Text(tail, style="dim")


def as_markdown(answers: list[Answer], prompt: str) -> str:
    """One markdown note of the whole comparison, for a phone joined to the chat."""
    blocks = [f"**Compared:** {prompt}"]
    for a in answers:
        body = a.text.strip() or ("cancelled" if a.error == "cancelled" else f"could not answer: {a.error}" if a.error else "(no answer)")
        blocks.append(f"## {a.pick.label()}\n\n{body}\n\n_{a.stats()}_")
    return "\n\n".join(blocks)


def silence(brain) -> None:
    """A comparison brain must not become the `-c` target or leave a saved session behind: whatever
    the driver uses to remember its session is switched off on this instance alone."""
    for name in ("_remember_session", "_save"):
        if callable(getattr(brain, name, None)):
            setattr(brain, name, lambda: None)
