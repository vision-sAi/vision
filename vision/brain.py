"""The brain: drives Claude through the Claude Code CLI in headless (print) mode.

This uses `claude -p` exactly as documented for scripting, so it runs on your normal
Claude Code login and subscription. No API keys, no token extraction.
Conversation continuity uses Claude Code's own session store via `--resume`.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from vision.config import DATA_DIR, STATE_DIR, WORKSPACE_DIR, BrainConfig, weather_ready
from vision import clis, models
from vision.checklist import TODO_TOOLS, checklist_of
from vision.persona import system_prompt
from vision.providers import REGISTRY as _REGISTRY
from vision.reply import READING, THINKING, dedupe_status, retry_label

_RETRY_REASONS = {529: "overloaded", 429: "rate limited", 500: "API error", 502: "API error", 503: "API error", 504: "API error"}


def _retry_reason(status: int | None, message: str) -> str:
    """A short reason for the retry line: a known HTTP status, else the error's first words."""
    if status in _RETRY_REASONS:
        return _RETRY_REASONS[status]
    if not message:
        return "connection lost" if status is None else f"API error {status}"
    return message.strip().splitlines()[0][:40]


LAST_SESSION_FILE = STATE_DIR / "last_session"
USAGE_FILE = STATE_DIR / "usage.json"
WINDOWS_FILE = STATE_DIR / "claude_windows.json"  # model id → context window Claude Code last reported


def _saved_windows() -> dict[str, int]:
    try:
        data = json.loads(WINDOWS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int) and v > 0} if isinstance(data, dict) else {}


def _save_window(model_id: str, window: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        WINDOWS_FILE.write_text(json.dumps({**_saved_windows(), model_id: window}), encoding="utf-8")
    except OSError:
        pass


# A held turn (see Brain.ask) closes claude's stdin this long after its last background task reported
# in if no new sub-turn has started, and this long after a reply that had a message steered into it.
NOTIFICATION_GRACE_S = 60.0
STEER_GRACE_S = 5.0
STEER_WAITS = 6  # grace periods a held turn waits for a sent but untaken steered message before it ends
# A background Bash (run_in_background) or a Monitor holds the turn like a background agent, until its
# task_notification says it is over. Claude Code caps a background command at 2 h; past this a missed
# notification stops holding the turn instead of keeping it open for ever.
SHELL_HOLD_MAX_S = 3 * 3600.0
SHELL_ENDED = {"completed", "failed", "killed", "stopped", "error", "cancelled", "canceled", "timeout", "expired"}


class BrainError(RuntimeError):
    pass


# -- plan mode ---------------------------------------------------------------
# Claude Code's plan mode: read-only tools until the model calls ExitPlanMode, which reaches us as a
# permission request carrying the plan. Approving it moves Claude Code itself to bypassPermissions for
# the rest of that turn (a setMode update, allowed by --allow-dangerously-skip-permissions at launch),
# and Vision switches to auto mode for the turns after it.
PLAN_QUESTION = {
    "question": "Carry out this plan?",
    "header": "Plan",
    "multiSelect": False,
    "options": [
        {"label": "Yes", "description": "switch to auto mode and start"},
        {"label": "No", "description": "stay in plan mode; type what should change instead"},
    ],
}


def local_handoff(brain) -> str | None:
    """Read the full saved conversation text. None if there is nothing to carry.

    Used when the new model cannot resume the old session (Claude ↔ Codex ↔ Grok). Does not call the
    outgoing model, so a rate-limit on that side cannot wipe the conversation.
    """
    sid = getattr(brain, "session_id", None)
    if not sid:
        return None
    from vision.sessions import format_transcript, session_history

    text = format_transcript(session_history(brain.provider, sid, limit=0, include_context=True))
    return text or None


def voice_catchup(brain, history: list[dict]) -> str | None:
    """The voice conversation's turns since the brain last saw it, as a transcript, or None.

    Spoken turns (and those typed during a call) are answered by the conversation model and kept in
    the voice conversation's own history; the brain's session on disk never sees them. This moves the
    brain's high-water mark to the end of that history and returns what lies past the old mark, so a
    typed turn can carry it in. The mark is the last history entry itself (the history drops old turns
    from the front, so an index would drift): an entry no longer there means everything is unseen.
    """
    from vision.sessions import format_transcript

    if not history:
        return None
    seen = getattr(brain, "_voice_seen", None)
    start = 0
    if seen is not None:
        for i in range(len(history) - 1, -1, -1):
            if history[i] is seen:
                start = i + 1
                break
    brain._voice_seen = history[-1]
    messages: list[dict] = []
    for entry in history[start:]:
        if entry.get("user"):
            messages.append({"role": "user", "text": entry["user"]})
        for event in entry.get("events", []):
            reply = event.get("assistant") if isinstance(event, dict) else None
            if isinstance(reply, dict) and (reply.get("speech") or "").strip():
                messages.append({"role": "vision", "text": reply["speech"]})
    return format_transcript(messages, max_chars=12_000) or None


SHELL_NOTE_START = "Since your last turn the user ran these commands themselves"


def shell_note(runs: list[tuple[str, str, int | None]], max_chars: int = 12_000) -> str:
    """Shell-mode commands (`!` in the terminal) as the brain is told them: each command, what it printed
    (the end of it, when long) and how it ended."""
    per = max(500, max_chars // max(1, len(runs)))
    out = []
    for command, output, code in runs:
        output = output.strip()
        if len(output) > per:
            output = "…" + output[-per:]
        ended = "" if code == 0 else f"\n<bash-exit>{'stopped' if code is None else code}</bash-exit>"
        out.append(f"<bash-input>{command}</bash-input>\n<bash-stdout>{output}</bash-stdout>{ended}")
    return "\n\n".join(out)


def inject_handoff(brain, prompt: str) -> str:
    """Prepend the previous provider's transcript to the first message of a fresh session, the voice
    turns the session has not seen (`catchup`, set by the typed turn's routing) to any message, and the
    commands the user ran in shell mode since the last turn (`shell`, a shell_note)."""
    shell = getattr(brain, "shell", None)
    if shell:
        brain.shell = None
        prompt = (f"{SHELL_NOTE_START} (Vision's `!` shell mode), in {getattr(brain, 'workdir', '') or 'their folder'}:\n\n{shell}\n\n---\n\n"
                  f"Take them into account; do not mention this note unless asked. The user's next message:\n\n{prompt}")
    note = getattr(brain, "handoff", None)
    catchup = getattr(brain, "catchup", None)
    if catchup:
        brain.catchup = None  # delivered with this prompt (a fresh session's handoff carries the transcript already)
    if note and not brain.session_id:
        return (
            "You are taking over an ongoing conversation from another model. What happened so far:\n\n"
            f"{note}\n\n---\n\nContinue naturally from here; do not mention the handoff unless asked. "
            f"The user's next message:\n\n{prompt}"
        )
    if not catchup:
        return prompt
    return (
        "Since your last turn the user talked with Vision by voice (those turns were answered by the "
        f"conversation model, not you). What was said:\n\n{catchup}\n\n---\n\nContinue naturally from "
        f"here; do not mention this note unless asked. The user's next message:\n\n{prompt}"
    )


_SHIM = """#!/bin/sh
# Installed by Vision. A shell command inside a Vision turn must not start another Claude Code, Codex
# or Grok run: that would spend a subscription without the user knowing. The user switches brains with /model.
echo "blocked by Vision: '$(basename "$0")' cannot be run from inside a Vision turn (it would spend a subscription behind the user's back). Ask the user to switch with /model instead." >&2
exit 1
"""


def _vision_shim() -> str:
    """`vision` for the model's shell: the Python running this turn, so a dev serve's turns run the dev
    checkout's `vision remind`/`schedule` rather than whichever install comes first on PATH."""
    return f"#!/bin/sh\n# Installed by Vision: runs the same Vision as the turn that called it.\nexec {shlex.quote(sys.executable)} -m vision \"$@\"\n"


def _shim_dir() -> str:
    """Directory holding fake `claude`, `codex` and `grok` commands and the turn's own `vision`;
    created/refreshed on demand ("" if it cannot be)."""
    d = DATA_DIR / "shims"
    try:
        d.mkdir(parents=True, exist_ok=True)
        shims = {p.cli: _SHIM for p in _REGISTRY.values() if p.cli}  # every provider CLI, blocked inside a turn
        shims["vision"] = _vision_shim()
        for name, text in shims.items():
            f = d / name
            if not f.exists() or f.read_text(encoding="utf-8") != text:
                f.write_text(text, encoding="utf-8")
            f.chmod(0o755)
    except OSError:
        return ""
    return str(d)


def _holds_shims(d: str) -> bool:
    """Whether a folder holds Vision's fake provider CLIs: this Vision's, or another's (a dev copy's)."""
    for name in [p.cli for p in _REGISTRY.values() if p.cli]:
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                if "# Installed by Vision." in f.read(512):
                    return True
        except OSError:
            continue
    return False


def without_shims(path: str) -> str:
    """PATH with Vision's shim folders taken out. A PATH saved or used outside a turn (the background
    service's, written by `vision service install` from a shell that may itself be inside a turn) must
    find the real `claude`, or every chat the service runs is blocked."""
    return os.pathsep.join(d for d in path.split(os.pathsep) if d and not _holds_shims(d))


def brain_env(provider: str, chat: str = "") -> dict[str, str]:
    """Environment for a brain subprocess (the model's shell inherits it).

    `chat` is the `vision serve` chat this brain answers (its `chat_id`, set by the chat): VISION_CHAT
    tells `vision remind` and `vision schedule`, run from the model's shell, where to come back to.

    Drops a parent Claude Code session's nesting guard, puts shims for `claude`, `codex` and `grok`
    first on PATH so a shell command cannot start another run, and points every *other* provider's
    CLI at an empty config dir so it has no credentials even if the shim is bypassed (a profile that
    reorders PATH, an absolute path). Cross-provider work only ever happens through /model."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    shims = _shim_dir()
    if shims:
        env["PATH"] = shims + os.pathsep + env.get("PATH", "")
    empty = DATA_DIR / "no-credentials"
    try:
        empty.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for other in _REGISTRY.values():
        if other.home_env and other.name != provider:
            env[other.home_env] = str(empty)
    own = _REGISTRY.get(provider)
    env.update(dict(own.turn_env) if own else {})  # e.g. Grok's memory off: Vision's MEMORY.md is the shared store
    env.pop("VISION_CHAT", None)
    if chat:
        env["VISION_CHAT"] = chat
    return env


def find_claude() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    for cand in (os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude", "/usr/bin/claude"):
        if os.path.exists(cand):
            return cand
    raise BrainError("Claude Code CLI ('claude') not found on PATH. Install it and run `claude` once to log in.")


def _sum_usage(per_message: dict[str, dict]) -> dict | None:
    """Claude Code's per-call usage dicts added up (the `result` event's total, when a turn never got
    one). None when nothing was counted, so the footer shows no numbers rather than zeros."""
    if not per_message:
        return None
    total: dict[str, int] = {}
    for usage in per_message.values():
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
    return total


@dataclass
class Turn:
    text: str = ""
    session_id: str | None = None
    tools_used: list[str] = field(default_factory=list)
    is_error: bool = False
    error: str = ""
    cost_usd: float | None = None
    duration_ms: int | None = None
    model: str | None = None  # full model id Claude Code reported for this turn
    usage: dict | None = None  # this turn's token counts in the provider's own keys; None when unknown
    plan_approved: bool = False  # plan mode: the user approved the plan; Vision is in auto mode from here on
    agents: list[AgentRun] = field(default_factory=list)  # subagents the model spawned this turn, in start order
    tools: list[ToolCall] = field(default_factory=list)  # the main conversation's tool calls this turn, in order
    data: dict | None = None  # validated by the delegation boundary; never sent to speech directly


# -- subagents ----------------------------------------------------------------
# The Agent tool runs a nested Claude with its own context; its work only ever came back to us as one
# tool result. In the stream it is visible though: `system/task_started` opens it, every message the
# child sends or receives carries `parent_tool_use_id` (its Agent call), and `system/task_notification`
# closes it with a summary. AgentRun is that view, updated live through Brain.ask's on_agent.
@dataclass
class AgentRun:
    id: str  # tool_use_id of the Agent call
    kind: str  # subagent type: Explore, Plan, general-purpose… (or "agent": the voice model's task worker)
    label: str  # the one-line description the model gave the task
    steps: list[tuple[str, str]] = field(default_factory=list)  # (tool name, one-line detail) as the child calls them
    done: bool = False
    failed: bool = False
    cut_off: bool = False  # never finished: the turn ended, was cancelled or stalled while it ran (failed is set too)
    tool_uses: int = 0
    duration_ms: int = 0
    summary: str = ""  # the child's final report (what the model gets as the tool result)
    # The row says which brain is spending: its model and effort. A Claude subagent runs at its
    # parent's effort (it has no knob of its own) and, unless the Agent call names a model, on the
    # parent's model. The voice worker also shows the tokens it has produced so far while it runs
    # (`tokens_fn`, polled by the UI) and its usage once done.
    model: str = ""
    effort: str = ""
    started: float = 0.0  # time.monotonic() when it started; 0 = unknown (no live timer)
    tokens_fn: Callable[[], int] | None = None
    usage: dict | None = None  # the provider's own token counts for the run, once known
    tokens: int = 0  # output tokens the provider reported for it (a workflow agent's `tokens`), when it has no usage
    # The supervisor's report once a launched agent stops (vision/supervisor.py): its state, the
    # result's summary, what changed, what was checked, what is open. Shown under the row.
    details: list[str] = field(default_factory=list)
    prompt: str = ""  # the task as given to the child, when the provider exposes it

    @property
    def started_at(self) -> float:
        """When it started as wall-clock time (epoch seconds), so another machine's live timer runs
        from the real start rather than from when it first heard of the row; 0 when unknown."""
        return time.time() - (time.monotonic() - self.started) if self.started else 0.0

    @property
    def status(self) -> str:
        """The done tail: time and tokens, `2.3s · ↑12,340 ↓1,204`; the red ✗ says it failed.
        A Claude subagent row, which has no usage of its own, shows its tool count first."""
        bits = []
        if not self.usage:
            n = self.tool_uses or len(self.steps)
            bits.append(f"{n} tool{'s' if n != 1 else ''}")
        bits.append(clock(self.duration_ms / 1000))
        if self.usage:
            bits.append(usage_summary(self.usage))
        elif self.tokens:
            bits.append(f"↓{self.tokens:,}")
        return " · ".join(bits)


def clock(seconds: float, live: bool = False) -> str:
    """An agent's time: `17.3s` (`17s` on a live timer), `6m 22s` past a minute, `1h 04m` past an hour."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s" if live else f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    return f"{m // 60}h {m % 60:02d}m"


def agent_frame(run: AgentRun) -> dict:
    """The wire form of a subagent row: what the server sends the phone, a terminal sends the server,
    and a chat's history keeps. `started` is wall-clock, so a device that joins late times it right."""
    frame = {"type": "agent", "id": run.id, "kind": run.kind, "label": run.label, "done": run.done,
             "tools": run.tool_uses or len(run.steps), "model": models.model_label(run.model) if run.model else "", "effort": run.effort,
             "started": round(run.started_at, 3)}
    frame.update({"steps": [{"tool": tool, "detail": detail} for tool, detail in run.steps],
                  "prompt": run.prompt, "summary": run.summary})
    tokens = run.tokens or (run.tokens_fn() if run.tokens_fn and not run.done else 0)
    if tokens:
        frame["tokens"] = tokens
    if run.done:
        frame.update({"status": run.status, "failed": run.failed, "cut_off": run.cut_off, "details": list(run.details),
                      "duration_ms": run.duration_ms})
    elif run.steps:
        frame["step"] = {"tool": run.steps[-1][0], "detail": run.steps[-1][1]}
    return frame


def run_from_frame(frame: dict, run: AgentRun | None = None) -> AgentRun:
    """An AgentRun rebuilt (or brought up to date) from an `agent_frame`, on the device that did not
    run it: its live timer runs from the frame's wall-clock start."""
    if run is None:
        run = AgentRun(frame.get("id") or "", frame.get("kind") or "agent", frame.get("label") or "")
    run.kind = frame.get("kind") or run.kind
    run.label = frame.get("label") or run.label
    run.model = frame.get("model") or run.model
    run.effort = frame.get("effort") or run.effort
    run.tool_uses = max(run.tool_uses, int(frame.get("tools") or 0))
    run.tokens = int(frame.get("tokens") or run.tokens)
    if frame.get("started"):
        run.started = time.monotonic() - max(0.0, time.time() - float(frame["started"]))
    run.prompt = frame.get("prompt", run.prompt)
    run.summary = frame.get("summary", run.summary)
    steps = frame.get("steps")
    step = frame.get("step")
    if isinstance(steps, list):
        run.steps = [(s.get("tool") or "", s.get("detail") or "") for s in steps if isinstance(s, dict)]
    elif isinstance(step, dict):
        pair = (step.get("tool") or "", step.get("detail") or "")
        if pair[0] and (not run.steps or run.steps[-1] != pair):
            run.steps.append(pair)
    if frame.get("done"):
        run.done = True
        run.failed = bool(frame.get("failed"))
        run.cut_off = bool(frame.get("cut_off"))
        run.duration_ms = int(frame.get("duration_ms") or run.duration_ms)
        run.details = list(frame.get("details") or run.details)
    return run


@dataclass
class ToolCall:
    """One tool call of the main conversation, shown live in the reply the way Claude Code shows its
    own: `⏺ Bash(git status)` and, once the result is back, its length under a `⎿` (a click on the
    row shows the whole result). Made when
    the call starts streaming (name only), given its detail when the full input arrives in the
    `assistant` message, and closed by the `user` message that carries the tool_result."""

    id: str  # tool_use_id
    name: str
    detail: str = ""  # tool_detail() of the input: the command, the path, the pattern…
    done: bool = False
    is_error: bool = False
    output: str = ""  # the text the model got back (the UI shows it on a click)
    diff: list = field(default_factory=list)  # an edit's hunks, see edit_diff; the UI draws them as Claude Code does
    input: list = field(default_factory=list)  # tool_input() of the input: [[key, value], …] for the phone's call page


TOOL_OUTPUT_CHARS = 4000  # the tail of a tool result a `tool` frame carries (shown on a click or tap)
INPUT_CHARS = 2000  # longest single input value a `tool` frame carries
INPUT_FIELDS = 8  # most input fields a `tool` frame carries
# What an edit's input says that its diff already shows, left off the call page.
EDIT_BODY = {"old_string", "new_string", "content", "edits", "new_source"}
DIFF_LINES = 400  # most diff lines a `tool` frame carries (a new file's first lines, a huge rewrite's start)
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")


def tool_frame(call: ToolCall) -> dict:
    """The wire form of a main-conversation tool call: what the server sends the phone and a
    terminal sends the server (the phone draws it as a row, as the terminal does)."""
    frame = {"type": "tool", "id": call.id, "name": call.name, "detail": call.detail, "done": call.done,
             "is_error": call.is_error, "output": (call.output or "")[-TOOL_OUTPUT_CHARS:]}
    if call.diff:
        frame["diff"] = call.diff
    if call.input:
        frame["input"] = call.input
    return frame


def tool_input(name: str, args) -> list[list[str]]:
    """A tool call's input as the phone's call page lists it: `[["description", "Check the tests"],
    ["command", "npm test"]]`, the description first, then the rest in the model's order. Each value
    whole (up to INPUT_CHARS), not one_line()'d; an edit's body is left to its diff."""
    if not isinstance(args, dict):
        return []
    keys = sorted(args, key=lambda k: k != "description")
    out = []
    for key in keys:
        value = args[key]
        if (name in EDIT_TOOLS and key in EDIT_BODY) or value is None or value == "":
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        whole = name == "ExitPlanMode" and key == "plan"  # the phone matches it to the reply to draw the plan card
        text = text if whole or len(text) <= INPUT_CHARS else text[: INPUT_CHARS - 1] + "…"
        out.append([str(key), text])
    return out[:INPUT_FIELDS]


def _cap_hunks(hunks: list[dict]) -> list[dict]:
    """The hunks cut to DIFF_LINES lines in all, dropping any left empty."""
    out, left = [], DIFF_LINES
    for h in hunks:
        if left <= 0:
            break
        lines = [str(ln) for ln in h.get("lines") or []][:left]
        if lines:
            out.append({"old": int(h.get("old") or 0), "new": int(h.get("new") or 0), "lines": lines})
            left -= len(lines)
    return out


def edit_diff(result) -> list[dict]:
    """An edit's hunks from Claude Code's structured tool result: `[{"old": 12, "new": 12, "lines":
    [" context", "-gone", "+added"]}]`, the line numbers where each hunk starts in the old and new
    file. A newly written file is one hunk of added lines. Empty when the result has neither."""
    if not isinstance(result, dict):
        return []
    patch = result.get("structuredPatch")
    if isinstance(patch, list) and patch:
        return _cap_hunks([{"old": h.get("oldStart"), "new": h.get("newStart"), "lines": h.get("lines")}
                           for h in patch if isinstance(h, dict)])
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return _cap_hunks([{"old": 0, "new": 1, "lines": ["+" + ln for ln in result["content"].splitlines()]}])
    return []


def line_diff(old: list[str], new: list[str]) -> list[str]:
    """`old` to `new` as diff rows (` context`, `-gone`, `+added`), matched line by line.

    difflib.ndiff did this before, and it also pairs changed lines up character by character, which is
    quadratic in the lines that changed: an Edit rewriting a few hundred lines held the brain's stream
    reader, and with it the whole reply, for seconds (15 s at 300 lines). Line-level opcodes give the
    same rows in microseconds, and the result's own patch replaces this preview once the edit lands."""
    import difflib

    rows: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "equal":
            rows.extend(" " + ln for ln in old[i1:i2])
        else:
            rows.extend("-" + ln for ln in old[i1:i2])
            rows.extend("+" + ln for ln in new[j1:j2])
    return rows


def input_diff(name: str, args) -> list[dict]:
    """The same from the call's own input, for when the result carries no patch: Edit's old and new
    strings, MultiEdit's edits, Write's content. Line numbers are unknown here (0: the UI shows none)."""
    if not isinstance(args, dict):
        return []
    if name == "Write" and isinstance(args.get("content"), str):
        return _cap_hunks([{"old": 0, "new": 0, "lines": ["+" + ln for ln in args["content"].splitlines()]}])
    edits = args.get("edits") if name == "MultiEdit" else [args] if name == "Edit" else []
    hunks = []
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        old, new = str(e.get("old_string") or "").splitlines(), str(e.get("new_string") or "").splitlines()
        lines = line_diff(old, new)
        if lines:
            hunks.append({"old": 0, "new": 0, "lines": lines})
    return _cap_hunks(hunks)


def unified_diff_hunks(text: str) -> list[dict]:
    """Hunks from a unified diff (`@@ -12,3 +12,4 @@` headers), as Codex reports a file change."""
    hunks: list[dict] = []
    for line in str(text or "").splitlines():
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            hunks.append({"old": int(m.group(1)), "new": int(m.group(2)), "lines": []})
        elif hunks and line[:1] in (" ", "-", "+") and not line.startswith(("--- ", "+++ ")):
            hunks[-1]["lines"].append(line)
    return _cap_hunks(hunks)


def context_figure(brain) -> dict | None:
    """How full the chat's context is, `{"tokens": read, "window": size}`, for the phone's gauge
    and its switch-model warning. None for brains that don't report it (Codex, Grok) or before
    the first turn."""
    ctx = getattr(brain, "context", None)
    if not ctx or not ctx[1]:
        return None
    return {"tokens": int(ctx[0]), "window": int(ctx[1])}


def usage_summary(usage: dict) -> str:
    """`↑12,340 ↓1,204`: what a run read (fresh plus cached input) and wrote."""
    return f"↑{context_read(usage):,} ↓{usage.get('output_tokens', 0):,}"


def context_read(usage: dict) -> int:
    """How much the model read on one call: its fresh input plus what came from the cache. After the
    last call of a turn this is the size of the conversation as the model sees it."""
    return usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)


def result_text(content) -> str:
    """The text of a tool_result's content: a plain string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


STEP_WIDTH = 96  # longest tool detail kept per step (the UI trims further to fit the terminal)


def tool_detail(name: str, args: dict) -> str:
    """One line saying what a tool call does, from its input: the command for Bash, the path for
    file tools, the pattern for searches, the task for a nested Agent. Empty if nothing fits."""
    if not isinstance(args, dict):
        return ""
    if name == "Bash":
        detail = args.get("description") or args.get("command") or ""
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        detail = args.get("file_path") or args.get("notebook_path") or ""
    elif name in ("Grep", "Glob"):
        detail = args.get("pattern") or ""
        if args.get("path"):
            detail += f"  in {args['path']}"
    elif name in ("Agent", "Task"):
        detail = args.get("description") or args.get("prompt") or ""
    elif name == "Workflow":
        # The script is the input's first string, and its first line is only `export const meta = {`.
        meta = re.search(r"""\bdescription\s*:\s*(['"`])(.+?)\1""", args.get("script") or "")
        detail = args.get("name") or (meta.group(2) if meta else "") or os.path.basename(args.get("scriptPath") or "")
    elif name in ("WebSearch", "WebFetch"):
        detail = args.get("query") or args.get("url") or ""
    else:
        detail = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    return one_line(detail)


def one_line(detail) -> str:
    """The first line of a detail, home as `~`, cut to STEP_WIDTH."""
    home = os.path.expanduser("~")
    detail = str(detail).strip().splitlines()[0] if str(detail).strip() else ""
    detail = detail.replace(home + "/", "~/").replace(home, "~")
    return detail if len(detail) <= STEP_WIDTH else detail[: STEP_WIDTH - 1] + "…"


def claude_default_model() -> str | None:
    """The model Claude Code itself would use when we pass no --model (env, then settings.json)."""
    env = os.environ.get("ANTHROPIC_MODEL")
    if env:
        return env
    for path in (
        os.path.expanduser("~/.claude/settings.local.json"),
        os.path.expanduser("~/.claude/settings.json"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                model = json.load(f).get("model")
        except (OSError, ValueError, AttributeError):
            continue
        if model:
            return str(model)
    return None


class _LiveClaude:
    """A `claude -p` process, kept from one turn to the next (see Brain.keep_alive_s). Its stdout is read
    on a thread into a queue, so a turn can end on a timer while the process lives on, and its stderr
    tail is kept for the error a dying process leaves behind."""

    def __init__(self, proc: subprocess.Popen, argv: list[str], cwd: str, session_id: str | None):
        self.proc = proc
        self.argv = argv  # the command line without --resume: a turn that needs another one starts a new process
        self.cwd = cwd
        self.session_id = session_id  # the session the process is on (what its events report)
        self.lines: queue.Queue = queue.Queue()
        self.stderr: deque[str] = deque(maxlen=200)
        self.cost = 0.0  # Claude Code's total_cost_usd counts up over the process; a turn's share is what it added
        self.ended = False  # stdout reached its end: the process is gone, or going
        self.input_closed = False
        self._errors = threading.Thread(target=self._read_errors, daemon=True)
        self._errors.start()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        self.lines.put(None)

    def _read_errors(self) -> None:
        # Read as it comes: left in the pipe until exit, a chatty claude fills the pipe buffer, blocks
        # on the write, and Vision blocks with it on stdout, a silent deadlock.
        try:
            for line in self.proc.stderr or ():
                self.stderr.append(line)
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return not self.ended and self.proc.poll() is None

    def drop_stale(self) -> None:
        """Lines a finished turn left unread (none, normally) are not the next turn's."""
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                return
            if line is None:
                self.ended = True
                self.lines.put(None)
                return

    def reap(self, timeout: float = 10.0) -> None:
        try:
            self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        self._errors.join(timeout=2)

    def close_input(self) -> None:
        """Close claude's input (once): with nothing more to read it finishes what it has and exits."""
        if self.input_closed:
            return
        self.input_closed = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass

    def end(self) -> None:
        """Let claude exit (its input closed), or make it."""
        if not self.ended:
            self.close_input()  # its output has not ended: it is still there to notice
        self.ended = True
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001  still there: make it go
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
                    except Exception:  # noqa: BLE001
                        pass


class Brain:
    provider = "claude"

    def __init__(self, cfg: BrainConfig, voice_mode: bool = False, session_id: str | None = None):
        self.cfg = cfg
        self._claude_path: str | None = None
        try:
            self._claude_path = find_claude()
        except BrainError:  # noqa: BLE001  not installed (yet): see the `claude` property
            pass
        self.voice_mode = voice_mode
        self.task_mode = False  # a separate worker session, never a typed conversation
        self.allowed_once: list[str] = []  # calls the user approved once (toolrules.gate), a scout's approvals
        self.on_allowed_used = None  # told when one of them runs
        self.on_approval = None  # (tool, input, rule) → an approval id: a call that asks first files one instead of asking now
        self.session_id = session_id
        self._proc: subprocess.Popen | None = None
        self._cancelled = False  # cancel() was called mid-turn (from another thread: Esc in the chat screen)
        self._lock = threading.Lock()
        # stdin is written from the reader thread (form answers) and the UI thread (steer): one line at
        # a time, and never after the turn closed it.
        self._stdin_lock = threading.Lock()
        self._stdin_open = False
        self._steered = [False]
        # Steered messages sent but not yet taken in, by the uuid they went with: Claude Code queues one
        # until its next step and echoes it back (isReplay) when it joins the conversation; that echo,
        # not the send, is when it shows in the transcript (on_steer_taken), as in Claude Code.
        self._pending_steers: dict[str, str] = {}
        self.on_steer_taken: Callable[[str], None] | None = None
        # The process kept from the last turn, if any (see keep_alive_s), and the timer that ends it idle.
        self._live: _LiveClaude | None = None
        self._idle_timer: threading.Timer | None = None
        self._turning = False  # ask() is on a turn, from its process pick to its last bookkeeping: warm() waits
        self._plan_approved = False  # this turn's plan was approved: allow the tools that follow
        self._turn_allowed: set[str] = set()  # ask rules the user allowed for the rest of the turn (_answer_control)
        # Told the new mode when approving a plan switches it mid-turn, so the badge and the phone change
        # then, not when the turn ends (the question's own summary goes out before the switch).
        self.on_mode_change: Callable[[str], None] | None = None
        # A held turn (see ask): called with the live line (`1 background job still running…`) when the
        # model has finished and only background work keeps the turn open, and with "" when it is at
        # work again. The terminal closes the reply meanwhile and lets messages straight in.
        self.on_background: Callable[[str], None] | None = None
        self.workdir = os.path.abspath(os.path.expanduser(cfg.workdir)) if cfg.workdir else os.getcwd()
        self.last_usage: dict | None = None
        # Live figures for the chat screen: output tokens received so far this turn (the `↓ 1.2k`
        # on the reply's live line) and how full the context is after the last model call, as
        # (tokens the model read, its window) for the `34% ctx` in the status row.
        self.output_tokens = 0
        self.context: tuple[int, int] | None = None
        self._window = 0  # context window Claude Code reported for the model, once seen
        self._window_for: str | None = None  # the cfg.model it was reported under
        # Actual model id Claude Code reported on the last turn, and the cfg.model it was observed under.
        self.model: str | None = None
        self._model_seen_for: str | None = None
        # Previous provider's transcript, sent with the first message of the next fresh session.
        self.handoff: str | None = None
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # The CLI's path: found when the brain is made if it is there, else looked up again when a turn
    # needs it, so a machine without it still gets a chat screen (and the start-up note saying how to
    # set it up) and the first message says what is missing, instead of a crash.
    @property
    def claude(self) -> str:
        if self._claude_path is None:
            self._claude_path = find_claude()
        return self._claude_path

    def context_window(self) -> int:
        """The model's context window in tokens: what Claude Code reported for the current model
        setting in a `result` event's modelUsage, else what it last reported for that model id
        (saved across runs), else 200k for Haiku and 1M for the rest (what Claude Code gives them)."""
        if self._window and self._window_for == self.cfg.model:
            return self._window
        model_id = (self.resolved_model() or "").split("[")[0]
        saved = _saved_windows().get(model_id)
        if saved:
            return saved
        return 200_000 if "haiku" in model_id or "haiku" in (self.cfg.model or "") else 1_000_000

    def _note_window(self, model_usage) -> None:
        """Take the window of the chat's own model from modelUsage, which also lists helper models
        Claude Code ran on the side (Haiku, 200k), in no promised order."""
        if not isinstance(model_usage, dict):
            return
        windows = {k.split("[")[0]: int(v["contextWindow"]) for k, v in model_usage.items()
                   if isinstance(v, dict) and v.get("contextWindow")}
        if not windows:
            return
        own = (self.model or "").split("[")[0] if self._model_seen_for == self.cfg.model else ""
        model_id, window = (own, windows[own]) if own in windows else max(windows.items(), key=lambda kv: kv[1])
        self._window, self._window_for = window, self.cfg.model
        if self.context:
            self.context = (self.context[0], window)
        if _saved_windows().get(model_id) != window:
            _save_window(model_id, window)

    def resolved_model(self) -> str | None:
        """Best knowledge of the model actually in use: what Claude Code reported for the current
        model setting, else the setting itself, else Claude Code's own default. None if unknown."""
        if self.model and self._model_seen_for == self.cfg.model:
            return self.model
        return self.cfg.model or claude_default_model()

    # -- session helpers -------------------------------------------------
    @staticmethod
    def last_session_id() -> str | None:
        try:
            sid = LAST_SESSION_FILE.read_text(encoding="utf-8").strip()
            return sid or None
        except FileNotFoundError:
            return None

    def _remember_session(self) -> None:
        if self.task_mode:
            return  # a voice worker must not replace the typed --continue target
        if self.session_id:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LAST_SESSION_FILE.write_text(self.session_id, encoding="utf-8")

    def new_session(self) -> None:
        self.close()  # the kept process is on the old session
        self.session_id = None
        self.handoff = None
        self.context = None  # the `% ctx` figure belongs to the session it was measured in

    def resume(self, session_id: str) -> None:
        """Continue an earlier conversation from the next turn on (and make it the `-c` target)."""
        self.close()
        self.session_id = session_id
        self.handoff = None
        self.context = None
        self._remember_session()

    # -- the process between turns ------------------------------------------
    # Every typed turn used to start a new `claude -p --resume`: Node's start-up and the transcript
    # load came to 1.1 s warm and 2.2 s cold before the request even left (Claude Code 2.1.289), and the
    # transcript part grows with the session. The process already speaks a multi-turn protocol (its
    # input stays open for steers and form answers), so it is kept for the next turn and only
    # restarted when the command line changes (model, effort, mode, directory, the persona's date or
    # memory), the session does (/new, /session) or it has sat idle for keep_alive_s. A task worker
    # is never kept: the supervisor holds every run's worker, and each run is one task.
    @property
    def keep_alive_s(self) -> float:
        if self.task_mode:
            return 0.0
        return max(0.0, float(getattr(self.cfg, "keep_alive_s", 0) or 0))

    def _start_idle_timer(self, live: _LiveClaude) -> None:  # under self._lock
        self._stop_idle_timer()
        t = threading.Timer(self.keep_alive_s, self._expire_live, args=(live,))
        t.daemon = True
        t.start()
        self._idle_timer = t

    def _stop_idle_timer(self) -> None:  # under self._lock
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _expire_live(self, live: _LiveClaude) -> None:
        """keep_alive_s idle: the process goes (it holds a few hundred MB); the next turn starts one."""
        with self._lock:
            if self._live is not live:
                return
            with self._stdin_lock:
                if self._stdin_open:
                    return  # a turn began as the timer fired: ask() re-arms it afterwards
            self._live = None
            self._proc = None
        live.end()

    def _spawn(self, argv: list[str], env: dict[str, str]) -> _LiveClaude:
        """Start claude for the current settings (`argv` is the command line without --resume, what a
        kept process is compared on; the real one resumes the session)."""
        return _LiveClaude(subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=env,
            text=True,
            bufsize=1,
            encoding="utf-8",
        ), argv, self.workdir, self.session_id)

    def _fits(self, live: _LiveClaude, argv: list[str]) -> bool:
        """The kept process is on the current command line, directory and session: a turn can go to it."""
        return live.argv == argv and live.cwd == self.workdir and live.session_id == self.session_id

    def warm(self) -> None:
        """Start the process for the current settings ahead of the next turn: when a chat opens, and after
        a change the next turn would otherwise restart it for (model, effort, mode, fast mode, directory,
        memory, /new, /session). Its start-up (1.1 s warm, 2.2 s cold, plus the transcript load on
        a resumed session) then runs while the message is being typed instead of after it. A kept process
        that still fits stays; one that no longer does is replaced, unless a turn is running on it (the
        end of that turn replaces it, see ask). Nothing happens when the process is not kept (keep_alive_s
        0, a task worker) or when the CLI is missing (the first turn says so); one that is never used goes
        after keep_alive_s, like any idle one."""
        if self.keep_alive_s <= 0:
            return
        try:
            self.claude
        except BrainError:
            return
        argv = self._command(resume=False)
        env = brain_env("claude", getattr(self, "chat_id", ""))
        with self._lock:
            if self._turning:
                return  # its process stays until the turn ends; ask() then starts the replacement
            old = self._live
            if old is not None and old.alive() and self._fits(old, argv):
                return
            try:
                live = self._spawn(argv, env)
            except OSError:
                return  # the first turn reports what is wrong
            self._live = live
            self._proc = live.proc
            self._start_idle_timer(live)
        if old is not None:
            old.end()  # stale, or dead: outside the lock, as ending waits for the exit

    def close(self) -> None:
        """End the kept process now (the chat is over, /new, a switch of provider): nothing else changes."""
        with self._lock:
            self._stop_idle_timer()
            live, self._live = self._live, None
            if live is not None and self._proc is live.proc:
                self._proc = None
        if live is not None:
            live.end()

    # -- main entry point -------------------------------------------------
    def _command(self, resume: bool = True) -> list[str]:
        """The command line of a turn; `resume=False` leaves the --resume out (what a kept process is compared on)."""
        from vision.delegation import RESULT_SCHEMA, worker_prompt
        from vision.folderprompt import for_brain

        persona = (worker_prompt(self.cfg, self.workdir, self.provider) if self.task_mode else
                   system_prompt(self.voice_mode, self.cfg.address_user_as, self.workdir, self.cfg.allowed_tools, mode=self.cfg.mode,
                                 weather=weather_ready(self.cfg), project=getattr(self, "project_prompt", ""),
                                 folder=for_brain(self.workdir, self.cfg), ask_tools=getattr(self.cfg, "ask_tools", None)))
        cmd = [
            self.claude,
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",  # keeps stdin open: AskUserQuestion answers go back on it
            "--permission-prompt-tool", "stdio",  # ... as control_response replies to control_request
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",  # a steered message echoes back when the model takes it (see steer)
            "--append-system-prompt",
            persona,
        ]
        if self.task_mode:
            cmd += ["--json-schema", json.dumps(RESULT_SCHEMA)]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]  # "ultracode" is accepted as-is: xhigh + workflow orchestration
        # A flag-scoped setting keeps Vision's /fast state local to this session instead of changing
        # Claude Code's user-wide preference.
        settings: dict = {"fastMode": self.cfg.fast}
        if getattr(self.cfg, "ask_tools", None):
            # The ask list goes in as Claude Code's own `ask` permission rules: rules are evaluated deny,
            # then ask, then allow, and "tools matched by an explicit ask rule" are among the actions no
            # mode auto-approves, bypassPermissions included (docs: permissions, permission-modes). So the
            # auto-mode flags below stay as they are and a matching call comes to us as a can_use_tool
            # control_request through --permission-prompt-tool stdio, where _answer_control asks the user.
            settings["permissions"] = {"ask": list(getattr(self.cfg, "ask_tools", None))}
        cmd += ["--settings", json.dumps(settings)]
        if self.cfg.mode == "plan":
            # Plan mode: Claude Code's own read-only mode (reads, searches and read-only shell commands
            # run freely; edits wait for the plan to be approved — see _answer_control). The allow flag
            # lets approval switch the running process to bypass, as auto mode starts it.
            cmd += ["--permission-mode", "plan", "--allow-dangerously-skip-permissions"]
        else:
            # Auto mode: the full tool set, nothing needs approval. Deny patterns below still apply.
            cmd += ["--dangerously-skip-permissions"]
        if self.cfg.denied_tools:
            cmd += ["--disallowedTools", ",".join(self.cfg.denied_tools)]
        if resume and self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def ask(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_question: Callable[[list[dict]], dict[str, str] | None] | None = None,
        on_agent: Callable[[AgentRun], None] | None = None,
        on_tool: Callable[[ToolCall], None] | None = None,
    ) -> Turn:
        """Send one user turn. Streams text deltas to on_text; returns the completed Turn.
        on_status gets a tool name when a tool starts, `reading` once every tool of that batch has
        finished, and `thinking` when a real reasoning block starts (see vision.reply: repeats are
        deduped, `status_label` gives the words).
        on_question gets AskUserQuestion's `questions` list and blocks until it returns
        {question: answer} (multi-select answers comma-joined) or None for "cancelled".
        on_tool gets the ToolCall of a main-conversation tool call each time it changes: when it
        starts, when its input is complete, and when its result is back (subagents' own calls go
        to on_agent instead).
        on_agent gets the AgentRun of a subagent each time it changes: when it starts, after every
        tool call it makes, and when it finishes (`done`). The same object is passed each time."""
        turn = Turn(session_id=self.session_id, model=self.model if self._model_seen_for == self.cfg.model else None)
        try:
            self.claude  # not installed: the turn says so instead of raising, the same on every front end
        except BrainError as e:
            turn.is_error, turn.error = True, str(e)
            return turn
        env = brain_env("claude", getattr(self, "chat_id", ""))
        keep = self.keep_alive_s > 0  # the process stays for the next turn (see keep_alive_s)
        finished = False  # the read loop ran to the turn's end (no exception out of a callback)
        prompt = inject_handoff(self, prompt)
        streamed = []
        final_text_from_message = []
        saw_delta = False
        self._plan_approved = False
        self._turn_allowed = set()  # ask rules (or tools) the user waved through for the rest of this turn
        self._cancelled = False
        self._pending_steers = {}
        on_status = dedupe_status(on_status)
        pending: set[str] = set()  # top-level tool calls still running (status goes to `reading` at zero)
        seen_usage: dict[str, dict] = {}  # token counts per model call so far, by message id (the tally a cancelled turn reports)
        self.output_tokens = 0
        got_result = False  # the `result` event arrived: the turn ran to its end
        agents: dict[str, AgentRun] = {}  # by the Agent call's tool_use_id
        agent_tasks: dict[str, AgentRun] = {}  # the same rows by their task_id: a resumed agent reports under SendMessage's id
        # An agent from an earlier turn woken by SendMessage: its task_started comes on that call's id
        # (a tool of ours) and its row is not in `agents`; its steps then arrive under the old Agent
        # call's id, and its task_notification under SendMessage's again. Kept by task id until a row
        # is made for it (agent_for) so the notification can close that row rather than none (once
        # seven such rows never closed and held the turn open).
        resumed: dict[str, dict] = {}  # task_id → its task_started event
        resumed_by_call: dict[str, str] = {}  # SendMessage tool_use_id → task_id
        tools: dict[str, ToolCall] = {}  # the main conversation's tool calls, by tool_use_id
        child_tools: set[str] = set()  # subagents' tool calls (their rows are AgentRun.steps), by tool_use_id
        agent_models: dict[str, str] = {}  # model override seen on an Agent call before its AgentRun exists
        agent_prompts: dict[str, str] = {}
        todo_args: dict[str, dict] = {}  # a TodoWrite/TaskCreate/TaskUpdate call's input until its result comes
        workflows: dict[str, list[AgentRun]] = {}  # a Workflow call's tool_use_id → the agents its script spawned
        open_workflows: set[str] = set()  # Workflow calls whose task_notification has not come yet
        ended_workflows: set[str] = set()
        # Claude Code can still have work of its own running when a `result` arrives: a Workflow
        # (ultracode) or a background Agent. Closing stdin then ends the process and kills that work
        # (workflows used to die that way), so the turn is held open instead: the rows stay
        # live, and when the work reports back Claude answers it in the same reply. `idle`: the last
        # sub-turn ended and none has started since. `steered`: a message went into the turn (steer).
        # A background Bash or a Monitor is the same story (a cleanup script once died that way):
        # `shells` holds them by tool_use_id, each with the timer that gives up on it.
        idle = [False]
        steered = [False]
        # Background work reported in while a sub-turn was still going: Claude Code queues the notice and
        # answers it in a sub-turn of its own right after this one's `result`, so that result isn't the
        # end. (A poll that finished within a second once ended the reply at its first answer, and
        # the rest of the work ran with nobody reading it.)
        owed = [False]
        # The model answered in the current sub-turn (a main-conversation message since its `init`). Taking
        # queued notices in after a reply, Claude Code (2.1.307) first emits an `init` and at once a blank
        # `result` (num_turns 0, no model call), and only then starts the sub-turn that answers them: that
        # blank is no answer, so it settles nothing owed and ends nothing. (Once, with three agents' reports,
        # the reply ended at the blank, and the answer to the second and third streamed with nobody reading.)
        spoke = [False]
        grace: list[threading.Timer | None] = [None]
        waits = [0]  # grace periods spent waiting for a queued steered message (see wait_then_close)
        shells: dict[str, threading.Timer] = {}

        def background_left() -> bool:
            return bool(open_workflows) or bool(shells) or any(not r.done and "#" not in r.id for r in agents.values())

        def background_status() -> str:
            n = sum(1 for r in agents.values() if not r.done)
            if n:
                return f"{n} agent{'s' if n != 1 else ''} still working…"
            if open_workflows:
                return "workflow still running…"
            n = len(shells)
            return f"{n} background job{'s' if n != 1 else ''} still running…"

        def shell_started(tid: str) -> None:
            if tid in shells:
                return
            timer = shells[tid] = threading.Timer(SHELL_HOLD_MAX_S, shell_ended, args=(tid,))
            timer.daemon = True
            timer.start()

        def shell_ended(tid: str | None, notified: bool = False) -> None:
            """`notified`: its task_notification came, which Claude answers (a failed start or the hold's
            time limit sends none)."""
            timer = shells.pop(tid or "", None)
            if timer is not None:
                timer.cancel()
                owed[0] = owed[0] or (notified and not idle[0])
                background_changed()

        done = threading.Event()  # the turn is over (end_turn): the read loop stops at it

        def end_turn() -> None:
            """The turn is over. A kept process keeps its input open for the next turn (this must never
            race a steer: one sent after it queues instead); otherwise closing stdin lets claude exit."""
            with self._stdin_lock:
                self._stdin_open = False
                if not keep:
                    live.close_input()
            done.set()

        def wait_then_close(seconds: float) -> None:
            """End the turn in `seconds` unless a new sub-turn starts (Claude answering a task
            notification or a steered message) or more background work turns up meanwhile."""
            if grace[0]:
                grace[0].cancel()

            def fire() -> None:
                if idle[0] and not background_left() and not steered[0]:
                    if self._pending_steers:
                        # A steered message still queued in claude: its sub-turn is due. Only quiet
                        # grace periods count; claude still streaming (a long story
                        # answered meanwhile, say) is never cut off, nor is its background work killed.
                        if time.monotonic() - activity[0] < seconds:
                            wait_then_close(STEER_GRACE_S)
                            return
                        if waits[0] < STEER_WAITS:
                            waits[0] += 1
                            wait_then_close(STEER_GRACE_S)
                            return
                    end_turn()

            grace[0] = threading.Timer(seconds, fire)
            grace[0].daemon = True
            grace[0].start()

        def woke() -> None:
            """A new sub-turn started while the turn was held: the model is at work again."""
            if idle[0]:
                idle[0] = paused[0] = False
                activity[0] = time.monotonic()
                if grace[0]:
                    grace[0].cancel()
                if self.on_background:
                    self.on_background("")

        def background_changed() -> None:
            if not idle[0]:
                return
            if background_left():
                if on_status:
                    on_status(background_status())
                if self.on_background:
                    self.on_background(background_status())
            else:
                # All in: Claude gets a task notification and answers it; give it a minute to start.
                paused[0] = False
                activity[0] = time.monotonic()
                wait_then_close(NOTIFICATION_GRACE_S)

        def agent_changed(run: AgentRun) -> None:
            if on_agent:
                on_agent(run)
            background_changed()  # while held: the live line counts what is still at work

        def agent_finished(run: AgentRun, ev: dict) -> None:
            """A subagent's task_notification: its row closes with what it reported."""
            usage = ev.get("usage") or {}
            run.tool_uses = usage.get("tool_uses") or run.tool_uses
            run.duration_ms = usage.get("duration_ms") or run.duration_ms
            run.summary = ev.get("summary") or run.summary
            run.failed = ev.get("status") not in (None, "completed")
            run.done = True
            # A background agent's report comes as a notice to answer; a foreground one's is its tool result.
            owed[0] = owed[0] or (not idle[0] and run.id not in pending)
            agent_changed(run)

        def tool_changed(call: ToolCall) -> None:
            if on_tool:
                on_tool(call)

        def new_run(pid: str, kind: str, label: str) -> AgentRun:
            """A subagent's row: its model override if the Agent call had one, else the model it
            inherits from us; the effort is always ours (a native subagent has no effort knob)."""
            run = agents[pid] = AgentRun(pid, kind, label, model=agent_models.get(pid) or self.cfg.model or "", effort=self.cfg.effort or "",
                                         started=time.monotonic(), prompt=agent_prompts.get(pid, ""))
            turn.agents.append(run)
            return run

        def resumed_run(pid: str, ev: dict) -> AgentRun | None:
            """The row for a woken agent's first event this turn: matched to its task_started by the
            SendMessage call it came on, else by its description (the old Agent call's id is all the
            event carries). None when nothing woken is waiting for a row."""
            tid = resumed_by_call.get(pid) or ev.get("task_id")
            if tid not in resumed:
                label = ev.get("task_description") or ""
                tid = next((t for t, started in resumed.items() if label and started.get("description") == label), None)
                if tid is None:
                    return None
            started = resumed.pop(tid)
            resumed_by_call.pop(started.get("tool_use_id"), None)
            run = new_run(pid, started.get("subagent_type") or ev.get("subagent_type") or "agent",
                          started.get("description") or ev.get("task_description") or "")
            agent_tasks[tid] = run
            return run

        def agent_for(ev: dict) -> AgentRun | None:
            """The subagent a message belongs to (None for the main conversation). A child seen before
            its task_started, e.g. after a resume, still gets a run so its steps are not lost."""
            pid = ev.get("parent_tool_use_id")
            if not pid:
                return None
            if pid in tools or pid in child_tools:
                # A Bash call that runs a while streams its progress under its own id: not a subagent.
                # Given a row, it never got a task_notification and held the turn open until cancelled.
                # (A SendMessage call that woke an agent is the exception: the agent's events may come under it.)
                if pid not in resumed_by_call:
                    return None
            run = agents.get(pid)
            if run is None:
                run = resumed_run(pid, ev) or new_run(pid, ev.get("subagent_type") or "agent", ev.get("task_description") or "")
                agent_changed(run)
            return run

        def workflow_progress(wid: str, entries: list) -> None:
            """A Workflow (ultracode) runs its agents in the background: none of their messages reach
            the stream. Claude Code reports them instead as `task_progress` events on the Workflow call
            whose `workflow_progress` lists every agent so far (index, label, phase, state, model, tool
            calls, latest tool, tokens; startedAt/durationMs in epoch ms, as a run's saved record shows).
            Each becomes an AgentRun, `<phase>(<label>)`, updated in place."""
            if wid not in ended_workflows:
                open_workflows.add(wid)
            runs = workflows.setdefault(wid, [])
            for e in entries:
                if not isinstance(e, dict) or e.get("type") != "workflow_agent" or e.get("index") is None:
                    continue
                key = f"{wid}#{e['index']}"
                run = agents.get(key)
                before = None if run is None else (run.kind, run.label, run.model, run.tool_uses, len(run.steps), run.done, run.failed, run.tokens)
                if run is None:
                    run = new_run(key, "", "")
                    runs.append(run)
                run.kind = e.get("phaseTitle") or run.kind or "workflow"
                run.label = e.get("label") or run.label
                run.model = e.get("model") or run.model
                run.tool_uses = e.get("toolCalls") or run.tool_uses
                run.tokens = e.get("tokens") or run.tokens
                begun = e.get("startedAt") or e.get("queuedAt")
                if begun and not run.done:
                    # The timer runs from the agent's own start, not from when its first report reached us.
                    run.started = time.monotonic() - max(0.0, time.time() - begun / 1000)
                step = (e.get("lastToolName") or "", one_line(e.get("lastToolSummary") or ""))
                if step[0] and (not run.steps or run.steps[-1] != step):
                    run.steps.append(step)
                if not run.done and e.get("state") in ("done", "error"):
                    run.done, run.failed = True, e.get("state") == "error"
                    run.summary = e.get("error") or e.get("resultPreview") or ""
                    last = e.get("lastProgressAt")
                    run.duration_ms = int(e.get("durationMs") or (last - begun if begun and last else (time.monotonic() - run.started) * 1000))
                if before != (run.kind, run.label, run.model, run.tool_uses, len(run.steps), run.done, run.failed, run.tokens):
                    agent_changed(run)

        def workflow_ended(wid: str, status: str | None) -> None:
            """The workflow is over: agents it never reported finished stop spinning."""
            open_workflows.discard(wid)
            ended_workflows.add(wid)
            for run in workflows.get(wid, []):
                if not run.done:
                    run.done, run.failed = True, status not in (None, "completed")
                    run.cut_off = run.failed
                    run.summary = run.summary or f"workflow {status or 'ended'}"
                    run.duration_ms = run.duration_ms or int((time.monotonic() - run.started) * 1000)
                    agent_changed(run)
            owed[0] = owed[0] or not idle[0]
            background_changed()

        def emit(text: str) -> None:
            """Text we add to the reply ourselves (the plan awaiting approval)."""
            nonlocal saw_delta
            saw_delta = True
            streamed.append(text)
            if on_text:
                on_text(text)

        argv = self._command(resume=False)
        with self._lock:
            self._stop_idle_timer()
            live = self._live
            if live is not None:
                reusable = keep and self._fits(live, argv)
                if reusable:
                    live.drop_stale()
                if not (reusable and live.alive()):
                    # The command line changed (model, effort, mode, persona, directory), the session
                    # did (/new, /session) or the process is gone: a new one, resuming the session.
                    live.end()
                    live = None
            if live is None:
                live = self._spawn(argv, env)
            self._live = live
            self._proc = proc = live.proc
            self._stdin_open = True
            self._turning = True
            self._steered = steered
        # A stall watchdog: no stdout line for cfg.stall_s kills the process, so a turn cannot wait
        # forever on a hung network call or a dead subagent. A long tool call is quiet too, hence
        # the generous default. Paused while an AskUserQuestion waits on the user.
        activity = [time.monotonic()]
        # VISION_STREAM_LOG=<file>: every raw line claude prints, timestamped, for debugging the protocol
        # (it carries the conversation, so it is opt-in and never on by default).
        dump = os.environ.get("VISION_STREAM_LOG")
        paused = [False]
        stalled = [False]
        stopped = threading.Event()

        def watchdog() -> None:
            limit = float(self.cfg.stall_s or 0)
            while limit > 0 and not stopped.wait(min(5.0, limit / 4)):
                if not paused[0] and time.monotonic() - activity[0] > limit and proc.poll() is None:
                    stalled[0] = True
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    return

        threading.Thread(target=watchdog, daemon=True).start()

        def close_leftovers() -> None:
            """Rows the stream never closed (an agent killed with the process when the turn ended,
            a tool call cut short by a cancel or a stall) are marked done here, so they stop spinning."""
            reason = "cancelled" if self._cancelled else f"stalled: no output for {float(self.cfg.stall_s):.0f}s" if stalled[0] else "cut off when the turn ended"
            for run in agents.values():
                if not run.done:
                    run.done = run.failed = run.cut_off = True
                    run.summary = run.summary or reason
                    if run.started and not run.duration_ms:
                        run.duration_ms = int((time.monotonic() - run.started) * 1000)
                    agent_changed(run)
            for call in tools.values():
                if not call.done:
                    call.done = call.is_error = True
                    call.output = call.output or f"({reason})"
                    tool_changed(call)
            pending.clear()

        try:
            assert proc.stdin
            self._write(proc, {"type": "user", "message": {"role": "user", "content": prompt}})
            while True:
                try:
                    line = live.lines.get(timeout=0.25)
                except queue.Empty:
                    if done.is_set():
                        break  # ended on a timer (a held turn's grace) with nothing more arriving
                    continue
                if line is None:
                    live.ended = True  # stdout closed: the process has gone (or, not kept, is done)
                    break
                activity[0] = time.monotonic()
                line = line.strip()
                if not line:
                    continue
                if dump:
                    try:
                        with open(dump, "a", encoding="utf-8") as f:
                            f.write(f"{time.time():.3f} {line}\n")
                    except OSError:
                        pass
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if not ev.get("parent_tool_use_id") and t in ("assistant", "stream_event"):
                    spoke[0] = True
                    if idle[0]:
                        woke()
                elif t == "system" and ev.get("subtype") == "init":
                    spoke[0] = False  # a sub-turn starts: nothing answered in it yet
                    if idle[0]:
                        woke()
                if t == "system" and ev.get("subtype") == "init":
                    turn.session_id = ev.get("session_id") or turn.session_id
                    turn.model = ev.get("model") or turn.model
                    if turn.model:
                        self.model, self._model_seen_for = turn.model, self.cfg.model
                        # A model Claude Code's list does not know yet (it moved an alias or added one): re-read it.
                        if turn.model.split("[")[0] not in {m.model_id for m in models.CLAUDE_MODELS}:
                            clis.refresh_models_soon(("claude",), max_age=clis.CLAUDE_MODELS_RECHECK)
                elif t == "system" and ev.get("subtype") == "api_retry":
                    # The CLI is waiting out an API failure (529, rate limit, dropped stream): nothing else
                    # streams meanwhile, so say so instead of leaving "working…" up.
                    if on_status:
                        err = ev.get("error") or {}
                        message = err.get("message") if isinstance(err, dict) else str(err or "")
                        on_status(retry_label(ev.get("attempt"), ev.get("max_retries"), ev.get("retry_delay_ms"),
                                              _retry_reason(ev.get("error_status"), message or "")))
                elif t == "system" and ev.get("subtype") == "task_progress" and ev.get("tool_use_id") and ev.get("workflow_progress"):
                    workflow_progress(ev["tool_use_id"], ev["workflow_progress"])
                elif t == "system" and ev.get("subtype") == "task_notification" and (ev.get("tool_use_id") in workflows or ev.get("tool_use_id") in open_workflows):
                    workflow_ended(ev["tool_use_id"], ev.get("status"))
                elif t == "system" and ev.get("subtype") == "task_started" and ev.get("tool_use_id") and (
                    ev.get("task_type") == "local_workflow" or getattr(tools.get(ev["tool_use_id"]), "name", "") == "Workflow"
                ):
                    # A workflow is under way before its first agent reports: the turn waits for it.
                    if ev["tool_use_id"] not in ended_workflows:
                        open_workflows.add(ev["tool_use_id"])
                elif t == "system" and ev.get("subtype") == "task_started" and ev.get("task_id") and (
                    ev.get("tool_use_id") not in agents and ev.get("task_id") not in agent_tasks
                    and getattr(tools.get(ev.get("tool_use_id")), "name", "") == "SendMessage"
                ):
                    # An agent of an earlier turn woken by a message: no row yet (see `resumed`).
                    resumed[ev["task_id"]] = ev
                    resumed_by_call[ev["tool_use_id"]] = ev["task_id"]
                elif t == "system" and ev.get("subtype") == "task_notification" and ev.get("task_id") in resumed:
                    # It finished before any event of its own reached us: a row for its report.
                    run = resumed_run(ev["tool_use_id"], ev)
                    agent_finished(run, ev)
                elif t == "system" and ev.get("subtype") in ("task_started", "task_notification") and (
                    ev.get("tool_use_id") not in agents and ev.get("task_id") in agent_tasks
                ):
                    # A subagent woken again by SendMessage: its events carry that call's id, a tool of ours,
                    # so matched on the task id instead; else its row never closed and held the turn open.
                    run = agent_tasks[ev["task_id"]]
                    if ev.get("subtype") == "task_started":
                        run.done = run.failed = False
                        agent_changed(run)
                    else:
                        agent_finished(run, ev)
                elif t == "system" and ev.get("subtype") in ("task_started", "task_notification") and (
                    ev.get("task_type") == "local_bash" or ev.get("tool_use_id") in tools or ev.get("tool_use_id") in child_tools
                ):
                    # A Bash command that runs a few seconds becomes a background task and uses the same
                    # task events as a subagent, but is not one: its row is the tool call already shown
                    # (a subagent's, its step). Skipped, so no `agent · <its description> · 0 tools · 0.0s`
                    # ghost. Claude Code tags these `task_type: local_bash`; the id sets cover versions
                    # that don't, inside a subagent too (its tool_use arrives before the task_started).
                    # One of ours sent to the background (or a Monitor) holds the turn until it ends.
                    tid = ev.get("tool_use_id")
                    if ev.get("subtype") == "task_started" and ev.get("is_backgrounded") and tid in tools:
                        shell_started(tid)
                    elif ev.get("subtype") == "task_notification" and str(ev.get("status") or "").lower() in SHELL_ENDED:
                        shell_ended(tid, notified=True)
                elif t == "system" and ev.get("subtype") == "task_started" and ev.get("tool_use_id"):
                    run = agents.get(ev["tool_use_id"])
                    if run is None:
                        run = new_run(ev["tool_use_id"], "", "")
                    run.kind = ev.get("subagent_type") or run.kind or "agent"
                    run.label = ev.get("description") or run.label
                    if ev.get("task_id"):
                        agent_tasks[ev["task_id"]] = run
                    agent_changed(run)
                elif t == "system" and ev.get("subtype") == "task_notification" and ev.get("tool_use_id") in agents:
                    agent_finished(agents[ev["tool_use_id"]], ev)
                elif ev.get("parent_tool_use_id"):
                    # Inside a subagent: its tool calls are the steps shown live; its text is its own
                    # business (the model gets it as the Agent result) and must not leak into the reply.
                    run = agent_for(ev)
                    if t == "assistant" and run is not None:
                        for block in ev.get("message", {}).get("content", []):
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                if block.get("id"):
                                    child_tools.add(block["id"])
                                run.steps.append((block.get("name", "tool"), tool_detail(block.get("name", ""), block.get("input") or {})))
                                agent_changed(run)
                elif t == "stream_event":
                    e = ev.get("event", {})
                    et = e.get("type")
                    if et == "content_block_delta":
                        d = e.get("delta", {})
                        if d.get("type") == "text_delta":
                            saw_delta = True
                            streamed.append(d["text"])
                            if on_text:
                                on_text(d["text"])
                    elif et == "content_block_start":
                        cb = e.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            name = cb.get("name", "tool")
                            turn.tools_used.append(name)
                            if cb.get("id"):
                                pending.add(cb["id"])
                                if name not in ("Agent", "Task"):  # a subagent has its own row (on_agent)
                                    call = tools[cb["id"]] = ToolCall(cb["id"], name)
                                    turn.tools.append(call)
                                    tool_changed(call)
                            if on_status:
                                on_status(name)
                        elif cb.get("type") in ("thinking", "redacted_thinking"):
                            if on_status:
                                on_status(THINKING)  # a real reasoning block: the one time "thinking…" is the truth
                        elif cb.get("type") == "text" and saw_delta and streamed and not streamed[-1].endswith("\n"):
                            # A new text block after a tool call: keep paragraphs separated.
                            streamed.append("\n\n")
                            if on_text:
                                on_text("\n\n")
                elif t == "user" and ev.get("isReplay"):
                    # A message of ours joining the conversation: a steered one now reaches the model.
                    self._steer_taken(ev)
                elif t == "user":
                    # Tool results come back as a user message. Once none of the batch is still running
                    # (parallel calls each get their own result), the model is thinking again.
                    results = [b for b in ev.get("message", {}).get("content", []) if isinstance(b, dict) and b.get("type") == "tool_result"]
                    pending.difference_update(b.get("tool_use_id") for b in results)
                    structured = ev.get("tool_use_result") or ev.get("toolUseResult")
                    for b in results:
                        call = tools.get(b.get("tool_use_id"))
                        if call is not None:
                            call.output = result_text(b.get("content"))
                            call.is_error = bool(b.get("is_error"))
                            if call.name in EDIT_TOOLS and len(results) == 1:
                                call.diff = [] if call.is_error else edit_diff(structured) or call.diff
                            call.done = True
                            if call.name in TODO_TOOLS and not call.is_error:
                                checklist_of(self).claude_call(call.name, todo_args.pop(call.id, {}), structured if len(results) == 1 else None, call.output)
                            tool_changed(call)
                            if call.is_error:
                                shell_ended(call.id)  # it never started: no notification will come
                    if on_status and results and not pending:
                        on_status(READING)
                elif t == "control_request":
                    paused[0] = True  # the user may take as long as they like to answer
                    try:
                        self._answer_control(proc, ev, on_question, emit, turn)
                    finally:
                        activity[0] = time.monotonic()
                        paused[0] = False
                elif t == "rate_limit_event":
                    self._record_usage(ev.get("rate_limit_info"))
                elif t == "assistant":
                    msg = ev.get("message", {})
                    if msg.get("usage"):
                        seen_usage[msg.get("id") or str(len(seen_usage))] = msg["usage"]  # one event per content block: keep the last per message
                        self.output_tokens = sum(u.get("output_tokens", 0) for u in seen_usage.values())
                        self.context = (context_read(msg["usage"]), self.context_window())
                    # Fallback source of text when partial messages are unavailable.
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            final_text_from_message.append(block["text"])
                        elif block.get("type") == "tool_use" and block.get("id") in tools:
                            call = tools[block["id"]]  # the input streamed as JSON deltas; here it is whole
                            call.detail = tool_detail(call.name, block.get("input") or {})
                            call.input = tool_input(call.name, block.get("input") or {})
                            if call.name in TODO_TOOLS:
                                todo_args[block["id"]] = block.get("input") or {}  # the checklist takes it in with the result
                            if call.name == "Monitor" or (call.name == "Bash" and (block.get("input") or {}).get("run_in_background")):
                                shell_started(block["id"])
                            if call.name in EDIT_TOOLS:
                                call.diff = input_diff(call.name, block.get("input"))  # the result's patch replaces it
                            tool_changed(call)
                        elif block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                            # An Agent/Task call's own input carries the model override, if any (no
                            # effort knob exists for a native subagent, unlike the voice worker's).
                            # Its AgentRun usually doesn't exist yet (task_started follows this
                            # message), so stash it for whichever creation site runs first.
                            inp = block.get("input") or {}
                            model = inp.get("model")
                            if block.get("id"):
                                if model:
                                    agent_models[block["id"]] = model
                                agent_prompts[block["id"]] = inp.get("prompt") or ""
                                run = agents.get(block["id"])
                                if run is not None:
                                    run.model = model or run.model
                                    run.prompt = agent_prompts[block["id"]]
                                    agent_changed(run)
                elif t == "result" and got_result and not ev.get("is_error") and (ev.get("num_turns") == 0 or not spoke[0]):
                    # A blank sub-turn (see `spoke`): the one that answers what was taken in is moments away.
                    # Whatever was owed or steered still is; the turn waits for it as for any notice, and a
                    # held turn's background work keeps it held as before.
                    idle[0] = True
                    if background_left():
                        paused[0] = True
                    else:
                        wait_then_close(NOTIFICATION_GRACE_S)
                elif t == "result":
                    got_result = True
                    self._note_window(ev.get("modelUsage"))
                    turn.session_id = ev.get("session_id") or turn.session_id
                    turn.is_error = bool(ev.get("is_error")) or ev.get("subtype", "").startswith("error")
                    # A held turn has a `result` per sub-turn (the reply, then the answer to each
                    # task notification or steered message): cost, time and tokens add up.
                    if ev.get("total_cost_usd") is not None:
                        # Counts up over the process (claude 2.1.289 checked): the turn's share is what it added.
                        total = float(ev["total_cost_usd"])
                        turn.cost_usd = (turn.cost_usd or 0) + max(0.0, total - live.cost)
                        live.cost = max(live.cost, total)
                    if ev.get("duration_ms") is not None:
                        turn.duration_ms = (turn.duration_ms or 0) + ev["duration_ms"]
                    turn.usage = _sum_usage({"a": turn.usage, "b": ev["usage"]}) if turn.usage and ev.get("usage") else ev.get("usage") or turn.usage
                    if self.task_mode:
                        turn.data = ev.get("structured_output")
                    if turn.is_error:
                        turn.error = ev.get("result") or ev.get("error") or ev.get("subtype", "error")
                    elif not saw_delta and ev.get("result"):
                        final_text_from_message = [ev["result"]]
                    was_steered, steered[0] = steered[0], False
                    was_owed, owed[0] = owed[0], False
                    idle[0] = True
                    if background_left() and not turn.is_error:
                        # Background work still running: keep claude (and it) alive, see `idle` above.
                        paused[0] = True  # a long agent is quiet on the stream; the user can still cancel
                        if on_status:
                            on_status(background_status())
                        if self.on_background:
                            self.on_background(background_status())
                    elif was_owed and not turn.is_error:
                        # Its notice is queued behind this result: wait for the sub-turn that answers it.
                        wait_then_close(NOTIFICATION_GRACE_S)
                    elif was_steered or self._pending_steers:
                        # A message sent into the turn may be queued behind this reply: let it start.
                        wait_then_close(STEER_GRACE_S)
                    else:
                        end_turn()  # the turn is over: the process stays for the next, or its input closes and it exits
                if keep and done.is_set():
                    break  # the process stays; the next turn reads on from here
            if not keep:
                proc.wait()
                live.reap(timeout=5)
            elif live.ended:
                live.reap()
            stderr = "".join(live.stderr)
            finished = True
        except KeyboardInterrupt:
            self.cancel()
            turn.is_error = True
            turn.error = "cancelled"
            turn.text = "".join(streamed)
            turn.usage = _sum_usage(seen_usage)
            return turn
        finally:
            stopped.set()
            idle[0] = False  # no timers from here
            if grace[0]:
                grace[0].cancel()
            for timer in shells.values():
                timer.cancel()
            with self._stdin_lock:
                self._stdin_open = False
            # A steered message claude still holds would run at the start of the next turn as well as
            # being queued again by the caller (take_untaken_steers): such a process is not kept.
            if keep and finished and not self._cancelled and not stalled[0] and live.alive() and not self._pending_steers:
                live.session_id = turn.session_id or live.session_id
                with self._lock:
                    if self._live is live:
                        self._start_idle_timer(live)  # kept for the next turn, until keep_alive_s idle
            else:
                # A callback (including the durable journal) can raise while Claude is still
                # waiting on stdin. Reap it before dropping the only reference.
                live.end()
                with self._lock:
                    if self._live is live:
                        self._live = None
                    self._proc = None
                self._turning = False
            close_leftovers()

        if saw_delta:
            turn.text = "".join(streamed)
        else:
            turn.text = "\n\n".join(final_text_from_message)
            if turn.text and on_text:
                on_text(turn.text)

        if (self._cancelled and not got_result) or stalled[0] or (proc.returncode or 0) < 0:
            # Cut short from another thread (or by a signal, or by the stall watchdog): no `result`
            # event, so the token counts are what the model calls so far reported. The session is
            # kept: what was said is in it.
            turn.is_error = True
            turn.error = f"the brain stalled: no output for {float(self.cfg.stall_s):.0f}s" if stalled[0] and not self._cancelled else "cancelled"
            turn.usage = turn.usage or _sum_usage(seen_usage)
            if turn.session_id:
                self.session_id = turn.session_id
                self._remember_session()
        elif proc.returncode not in (0, None) and not turn.text:
            turn.is_error = True
            turn.error = turn.error or (stderr.strip().splitlines() or ["claude exited with code %s" % proc.returncode])[-1]
            if "resume" in turn.error.lower() or "session" in turn.error.lower():
                # Stale session id: drop it so the next turn starts clean.
                self.session_id = None
        else:
            self.session_id = turn.session_id
            self.handoff = None  # delivered with this turn; the session carries it from now on
            self._remember_session()
        self._turning = False
        if keep and live.alive() and self._live is live and not self._fits(live, self._command(resume=False)):
            # A setting changed during the turn (/effort, /mode, /cd, a memory line, the date): the kept
            # process no longer fits, so its replacement boots now, while the next message is being typed.
            threading.Thread(target=self.warm, daemon=True).start()
        return turn

    # -- control channel ------------------------------------------------
    def _answer_control(self, proc: subprocess.Popen, ev: dict, on_question, emit, turn: Turn) -> None:
        """Reply to a `can_use_tool` request. In auto mode only AskUserQuestion reaches us (everything
        else is pre-approved or denied by the flags): show the form, hand the answers back as
        `updatedInput.answers`; a cancelled form denies the call so the model can carry on.
        In plan mode ExitPlanMode brings the finished plan: show it, ask, and once approved allow
        whatever tools the rest of the turn needs."""
        req = ev.get("request", {})
        rid = ev.get("request_id")
        tool = req.get("tool_name", "tool")
        tool_input = req.get("input", {})
        if tool == "AskUserQuestion":
            if self.task_mode:
                msg = {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": {
                    "behavior": "deny", "message": "The voice conversation asks the user. Return status needs_input with your question in the structured result instead."
                }}}
                self._write(proc, msg)
                return
            questions = tool_input.get("questions")
            answers = on_question(questions) if questions and on_question else None
            if answers:
                result = {"behavior": "allow", "updatedInput": {**tool_input, "answers": answers}}
            else:
                result = {"behavior": "deny", "message": "The user dismissed the question form without answering; continue without these answers or ask in plain text."}
        elif tool == "ExitPlanMode":
            plan = (tool_input.get("plan") or "").strip()
            if plan:
                emit(f"\n\n{plan}\n\n")  # blank line after: the text that follows approval is a new paragraph, not the last bullet
            # Worker prose is deliberately suppressed. Put its plan in the approval form so the
            # user can still review the actual changes before approving them.
            question = {**PLAN_QUESTION, "question": f"Carry out this plan?\n\n{plan}"} if self.task_mode and plan else PLAN_QUESTION
            choice = ((on_question([question]) if on_question else None) or {}).get(question["question"], "")
            if choice == "Yes":
                self._plan_approved = turn.plan_approved = True
                if getattr(self.cfg, "plan_approval", "session") != "turn":
                    self.cfg.mode = "auto"  # the turns after this one run without approval too
                    if self.on_mode_change:
                        self.on_mode_change("auto")
                # Without a setMode the CLI drops to a mode of its own and tells the model so; bypass
                # matches auto. _plan_approved still allows the rest of the turn if a CLI refuses it.
                result = {"behavior": "allow", "updatedInput": tool_input, "updatedPermissions": [
                    {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}]}
            elif choice and choice != "No":
                result = {"behavior": "deny", "message": f"The user did not approve the plan and said: {choice}\nStay in plan mode and revise the plan accordingly."}
            else:
                result = {"behavior": "deny", "message": "The user did not approve the plan. Stay in plan mode; ask what they would like changed, or wait."}
        elif self.cfg.mode == "plan" and not self._plan_approved:
            result = {"behavior": "deny", "message": f"Vision is in plan mode: {tool} has to wait until the plan is approved. Finish the plan and call ExitPlanMode."}
        else:
            # Auto mode (or the approved plan being carried out): an [brain].ask_tools rule caught the
            # call, or it is one of the actions Claude Code approves in no mode (a critical-path rm, say).
            # Either way the user decides; a worker has nobody to ask, so it is told to come back with it.
            result = self._ask_to_run(tool, tool_input, on_question)
        msg = {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": result}}
        self._write(proc, msg)

    def _ask_to_run(self, tool: str, tool_input: dict, on_question) -> dict:
        """The allow/deny answer for a call that needs the user's say-so: the Yes / No form, or what
        they already said for this rule this turn."""
        from vision.toolrules import YES_FOR_TURN, ask_question, declined_message, first_match, gate

        rule = first_match(getattr(self.cfg, "ask_tools", None), tool, tool_input, self.workdir)
        settled = gate(self, tool, tool_input, rule)  # a scout's approvals (projects.py)
        if settled is not None:
            ok, message = settled
            return {"behavior": "allow", "updatedInput": tool_input} if ok else {"behavior": "deny", "message": message}
        if self.task_mode:
            return {"behavior": "deny", "message": f"{tool} needs the user's approval, which a worker cannot ask for: "
                    "return status needs_input saying exactly what you would run and why, and the user decides."}
        key = rule or tool
        if key in self._turn_allowed or (rule is None and self._plan_approved):
            return {"behavior": "allow", "updatedInput": tool_input}  # an approved plan runs on, as before
        if not on_question:
            return {"behavior": "deny", "message": f"{tool} needs the user's approval and nobody is here to give it: carry on without it."}
        question = ask_question(tool, tool_input, rule)
        choice = (on_question([question]) or {}).get(question["question"], "")
        if choice.startswith("Yes"):
            if choice == YES_FOR_TURN:
                self._turn_allowed.add(key)
            return {"behavior": "allow", "updatedInput": tool_input}
        return {"behavior": "deny", "message": declined_message(tool, tool_input)}

    def _write(self, proc: subprocess.Popen, msg: dict) -> bool:
        """One JSON line to claude's stdin; False once the turn has closed it."""
        with self._stdin_lock:
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def steer(self, text: str) -> bool:
        """Send a message into the running turn, as Claude Code does with what is typed while it works:
        the model takes it at its next step (between tool calls), or at once if the turn is only
        waiting on background work. False when no turn is running or it is already closing; the
        caller then queues the message as the next turn. A task worker takes one too (the voice
        layer's task update): a result after it replaces the one before, so each covers the whole task."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        with self._stdin_lock:
            if not self._stdin_open:
                return False
            uid = str(uuid.uuid4())
            try:
                proc.stdin.write(json.dumps({"type": "user", "uuid": uid, "message": {"role": "user", "content": text}}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
            self._steered[0] = True
            self._pending_steers[uid] = text
        return True

    def _steer_taken(self, ev: dict) -> None:
        """A replayed user message: if it is a steered one still waiting, it has reached the model now.
        Matched by uuid, else by its text (the oldest waiting one), in case a version echoes another id."""
        with self._stdin_lock:
            text = None
            for uid in (ev.get("uuid"), ev.get("source_uuid")):  # mid-turn it carries ours as source_uuid
                if text is None and uid:
                    text = self._pending_steers.pop(uid, None)
            if text is None:
                body = (ev.get("message") or {}).get("content")
                if isinstance(body, list):
                    body = "".join(b.get("text", "") for b in body if isinstance(b, dict) and b.get("type") == "text")
                # Claude Code may wrap a queued message (a reminder around it) when it attaches it mid-turn.
                for k, v in self._pending_steers.items():
                    if isinstance(body, str) and v.strip() and v.strip() in body:
                        text = self._pending_steers.pop(k)
                        break
            if text is not None and not self._pending_steers:
                self._steered[0] = False  # taken: no sub-turn is owed for it after this reply
        if text is not None and self.on_steer_taken:
            self.on_steer_taken(text)

    def take_untaken_steers(self) -> list[str]:
        """Steered messages the turn ended without taking in (claude died, or Esc): the caller queues
        them as the next turn rather than lose them."""
        with self._stdin_lock:
            left, self._pending_steers = list(self._pending_steers.values()), {}
        return left

    # -- subscription usage ---------------------------------------------
    def _record_usage(self, info: dict | None) -> None:
        if not info:
            return
        import time

        self.last_usage = {"info": info, "at": time.time()}
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps(self.last_usage), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def cached_usage() -> dict | None:
        try:
            return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def usage_report(self) -> str | None:
        """Claude Code's own /usage text (session, week, per-model windows). No model call is made."""
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        cmd = [self.claude, "-p", "/usage", "--output-format", "json", "--no-session-persistence", "--tools", ""]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.workdir, env=env, timeout=60, encoding="utf-8")
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
            return None
        text = data.get("result") or ""
        if data.get("is_error") or "used" not in text:
            return None
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "usage.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass
        return text

    def ping_usage(self) -> dict | None:
        """Make the cheapest possible Claude call just to read the current rate-limit windows."""
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        cmd = [self.claude, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--model", "haiku", "--tools", "", "--max-turns", "1"]
        try:
            r = subprocess.run(cmd, input="Reply with OK.", capture_output=True, text=True, cwd=self.workdir, env=env, timeout=120, encoding="utf-8")
        except (subprocess.TimeoutExpired, OSError):
            return None
        for line in r.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "rate_limit_event":
                self._record_usage(ev.get("rate_limit_info"))
        return self.last_usage

    def cancel(self) -> None:
        with self._stdin_lock:
            running = self._stdin_open
        if not running:
            self.close()  # nothing to cancel: a process kept between turns just goes
            return
        with self._lock:
            proc = self._proc
            self._cancelled = proc is not None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()


def warm_soon(brain) -> threading.Thread | None:
    """Start `brain`'s process for its current settings on a thread of its own, when it has one to start
    (Brain.warm; the other providers' drivers have none): the chat just opened, or a setting the next
    turn would restart it for changed. The caller never waits on it; the thread is returned for tests."""
    warm = getattr(brain, "warm", None)
    if warm is None:
        return None
    t = threading.Thread(target=warm, daemon=True, name="brain-warm")
    t.start()
    return t


def create_brain(
    cfg: BrainConfig,
    voice_mode: bool = False,
    *,
    continue_session: bool = False,
    session_id: str | None = None,
):
    """Construct the driver for cfg.model, optionally resuming that provider's last thread."""
    from vision.models import coerce_effort, provider_for

    clis.refresh_models_soon()  # once per process (then every few hours): new models, labels, efforts
    cfg.effort, _ = coerce_effort(cfg.model, cfg.effort)

    from vision.providers import get as get_provider

    p = get_provider(provider_for(cfg.model))
    cls = p.brain_class() if p is not None else Brain
    sid = session_id
    if sid is None and continue_session:
        sid = cls.last_session_id()
    return cls(cfg, voice_mode=voice_mode, session_id=sid)
