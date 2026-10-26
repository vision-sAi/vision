"""Subagent rows for Codex and Grok, the way Vision shows Claude's (vision.brain.AgentRun, on_agent).

Neither CLI documents its sub-agent events on the headless stream, so the readers below take the
shapes their own logs and protocol output:

- Codex: `collab_tool_call` items (`tool` spawn_agent / send_input / wait / close_agent, the child
  thread ids in `receiver_thread_ids`, each child's state in `agents_states`), and, in its rollout
  vocabulary, `sub_agent_activity` items (`kind` started / interacted / completed, `agent_thread_id`,
  `agent_path`).
- Grok: a `spawn_subagent` tool call (its `rawInput.description` is the label; with `background` it
  completes at once and hands back a `subagent_id`), finished by a later
  `get_command_or_subagent_output` / `kill_command_or_subagent` call on that id.

Every sub-agent event is also appended raw to ~/.local/state/vision/subagent-events.jsonl (kept
short), so the first real run shows whether these guesses match.

Codex's app-server reports spawns as sub_agent_activity (started / completed) and streams each child
thread's own items under its threadId (vision.codex_app keeps those out of the reply); Grok's
background spawn answers with a Text
result naming the subagent_id, and get_command_or_subagent_output with {type: TaskOutput, Result}.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable

from vision.brain import AgentRun, Turn, one_line
from vision.config import STATE_DIR

TRACE_FILE = STATE_DIR / "subagent-events.jsonl"
TRACE_LINES = 400
DONE_STATES = {"completed": False, "complete": False, "done": False, "shutdown": False,
               "errored": True, "error": True, "failed": True, "interrupted": True, "not_found": True, "cancelled": True, "killed": True}


_trace_lock = threading.Lock()
_trace_since_trim = [0]  # appends since the file was last cut back


def trace(provider: str, event: dict) -> None:
    """Keep the raw event (about the last TRACE_LINES of them) for checking the readers against reality.
    Appended, then cut back to TRACE_LINES lines once every TRACE_LINES appends: reading and rewriting
    the whole file for every sub-agent event cost up to 20 ms a time on the stream reader."""
    line = json.dumps({"t": time.time(), "provider": provider, "event": event})[:20000]
    try:
        with _trace_lock:
            TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trace_since_trim[0] += 1
            if _trace_since_trim[0] >= TRACE_LINES:
                _trace_since_trim[0] = 0
                kept = TRACE_FILE.read_text(encoding="utf-8").splitlines()[-TRACE_LINES:]
                TRACE_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


class AgentTracker:
    """One turn's sub-agent rows, keyed by whatever id the CLI uses (several ids may name one row)."""

    def __init__(self, turn: Turn, on_agent: Callable | None, model: str = "", effort: str = ""):
        self.turn, self.on_agent = turn, on_agent
        self.model, self.effort = model, effort
        self.rows: dict[str, AgentRun] = {}

    def _changed(self, run: AgentRun) -> None:
        if self.on_agent:
            self.on_agent(run)

    def get(self, key: str | None) -> AgentRun | None:
        return self.rows.get(key) if key else None

    def start(self, key: str, label: str = "", model: str | None = None, kind: str = "agent") -> AgentRun:
        run = self.rows.get(key)
        if run is None:
            run = AgentRun(key, kind, one_line(label)[:120], model=model or self.model, effort=self.effort, started=time.monotonic())
            self.rows[key] = run
            self.turn.agents.append(run)
        else:
            run.label = run.label or one_line(label)[:120]
            run.model = model or run.model
        self._changed(run)
        return run

    def alias(self, key: str, other: str) -> None:
        """`other` names the same agent as `key` (a spawn call's id and the child's own id)."""
        if key in self.rows and other and other not in self.rows:
            self.rows[other] = self.rows[key]

    def step(self, key: str, tool: str, detail: str = "") -> None:
        run = self.rows.get(key)
        if run is not None and not run.done:
            run.steps.append((tool, one_line(detail)))
            run.tool_uses += 1
            self._changed(run)

    def finish(self, key: str, failed: bool = False, summary: str = "") -> None:
        run = self.rows.get(key)
        if run is None or run.done:
            return
        run.done, run.failed = True, failed
        run.summary = summary or run.summary
        run.duration_ms = int((time.monotonic() - run.started) * 1000)
        self._changed(run)

    def close(self, reason: str) -> None:
        """The turn is over: rows nothing reported finished stop spinning (cut off)."""
        for run in {id(r): r for r in self.rows.values()}.values():
            if not run.done:
                run.done = run.failed = run.cut_off = True
                run.summary = run.summary or reason
                run.duration_ms = int((time.monotonic() - run.started) * 1000)
                self._changed(run)


# -- Codex ---------------------------------------------------------------------
CODEX_AGENT_ITEMS = ("collab_tool_call", "collab_agent_tool_call", "sub_agent_activity", "SubAgentActivity")


def codex_item(tracker: AgentTracker, item: dict, finished: bool) -> bool:
    """Fold one Codex item into the rows; True when it was a sub-agent item."""
    kind = item.get("type", "")
    if kind not in CODEX_AGENT_ITEMS:
        return False
    trace("codex", {"finished": finished, "item": item})
    if kind in ("sub_agent_activity", "SubAgentActivity"):
        key = item.get("agent_thread_id") or item.get("id") or ""
        path = item.get("agent_path") or ""
        phase = item.get("kind") or ""
        if phase == "started" or tracker.get(key) is None:
            tracker.start(key, path.rsplit("/", 1)[-1] or path)
        if phase == "interacted":
            tracker.step(key, "message")
        elif phase in ("completed", "errored", "interrupted"):
            tracker.finish(key, failed=phase != "completed")
        return True
    tool = item.get("tool") or item.get("name") or ""
    states = item.get("agents_states") or {}
    children = list(item.get("receiver_thread_ids") or []) or list(states)
    if tool in ("spawn_agent", "spawn") or (not tool and not finished):
        label = item.get("task_name") or item.get("prompt") or ""
        keys = children or [item.get("id") or ""]
        for key in keys:
            tracker.start(key, label, model=item.get("model") or None)
            tracker.alias(key, item.get("id") or "")
    elif tool in ("send_input", "send_message", "followup_task"):
        for key in children:
            tracker.step(key, "message", item.get("prompt") or "")
    for key, state in states.items():
        status = state.get("status") if isinstance(state, dict) else state
        if tracker.get(key) is None:
            tracker.start(key, key)
        if isinstance(status, str) and status.lower() in DONE_STATES:
            message = state.get("message") if isinstance(state, dict) else ""
            tracker.finish(key, failed=DONE_STATES[status.lower()], summary=message or "")
    if tool in ("close_agent",) and finished:
        for key in children:
            tracker.finish(key)
    if finished and tool in ("spawn_agent", "spawn") and item.get("status") == "failed":
        for key in children or [item.get("id") or ""]:
            tracker.finish(key, failed=True)
    return True


# -- Grok ----------------------------------------------------------------------
GROK_SPAWN = "spawn_subagent"
GROK_FOLLOW = ("get_command_or_subagent_output", "kill_command_or_subagent")


def _raw(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _grok_text(ev: dict, out: dict) -> str:
    """A tool result's words: rawOutput {type: Text, text} or the ACP content blocks."""
    if isinstance(out.get("text"), str):
        return out["text"]
    return "".join(c.get("content", {}).get("text", "") for c in ev.get("content") or [] if isinstance(c, dict))


def _grok_summary(output: str) -> str:
    """A sub-agent's answer without the <subagent_meta>/<worktree_path>/<subagent_result> trailer."""
    return output.split("<subagent_meta>", 1)[0].strip()


def grok_tool_call(tracker: AgentTracker, ev: dict, calls: dict[str, str]) -> bool:
    """A `tool_call` event: a spawn opens a row; a follow-up call is remembered by its call id."""
    name = ev.get("toolName") or ev.get("title") or ""
    tid = ev.get("toolCallId") or ""
    if name == GROK_SPAWN:
        trace("grok", ev)
        raw = _raw(ev.get("rawInput"))
        tracker.start(tid, raw.get("description") or one_line(raw.get("prompt") or "")[:60], model=raw.get("model") or None)
        calls[tid] = name
        return True
    if name in GROK_FOLLOW:
        trace("grok", ev)
        calls[tid] = name
        raw = _raw(ev.get("rawInput"))
        for key in raw.get("task_ids") or ([raw["task_id"]] if raw.get("task_id") else []):
            tracker.step(key, "check" if name == GROK_FOLLOW[0] else "stop")
        return True
    return False


def grok_tool_update(tracker: AgentTracker, ev: dict, calls: dict[str, str]) -> None:
    """A `tool_call_update`: the spawn's result names the sub-agent; a follow-up's result may end it."""
    tid = ev.get("toolCallId") or ""
    name = calls.get(tid)
    if not name:
        return
    trace("grok", ev)
    status = (ev.get("status") or "").lower()
    out = _raw(ev.get("rawOutput"))
    if name == GROK_SPAWN:
        sub = out.get("subagent_id") or out.get("task_id") or out.get("id")
        if not sub:  # Some versions return the id in a text result: "Subagent started in background.\nsubagent_id: …"
            found = re.search(r"subagent_id:\s*(\S+)", _grok_text(ev, out))
            sub = found.group(1) if found else None
        if sub:
            tracker.alias(tid, str(sub))
        if status in ("failed", "error", "cancelled"):
            tracker.finish(tid, failed=True, summary=str(out.get("error") or ""))
        elif status == "completed" and not sub and out:
            tracker.finish(tid, summary=str(out.get("output") or out.get("result") or ""))  # it ran in the foreground
        return
    if status != "completed":
        return
    # Task output may wrap results in a capitalized `Result` field.
    results = out.get("results") or out.get("Result") or out
    results = results if isinstance(results, list) else [results]
    for r in results:
        if not isinstance(r, dict):
            continue
        key = str(r.get("task_id") or r.get("subagent_id") or r.get("id") or "")
        state = str(r.get("status") or r.get("state") or "").lower()
        if name == GROK_FOLLOW[1]:
            tracker.finish(key, failed=True, summary="stopped")
        elif state in DONE_STATES:
            output = str(r.get("output") or r.get("result") or "")
            run = tracker.get(key)
            calls = re.search(r"<subagent_meta>[^<]*tool_calls=(\d+)", output)
            if run is not None and calls:
                run.tool_uses = int(calls.group(1))
            tracker.finish(key, failed=DONE_STATES[state], summary=_grok_summary(output))
