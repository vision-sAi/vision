"""Codex turns through `codex app-server` (JSON-RPC over stdio) rather than `codex exec`.

`codex exec` takes the prompt once and stops listening. The app-server keeps the turn open to its
client, which buys Vision four things:

- turn/steer: a message typed mid-reply goes into the running turn (Ctrl-X / Send now), as with Claude;
- item/agentMessage/delta: the reply streams in as it is written, not a whole message at a time;
- thread/tokenUsage/updated: usage and the context window arrive live, no rollout file to read;
- typed items: tool calls become live rows (on_tool) and sub-agents (collabAgentToolCall) their own.

Protocol used by the Codex CLI:
initialize → initialized → thread/start | thread/resume → turn/start; then notifications
item/started, item/completed, item/agentMessage/delta, item/reasoning/summaryTextDelta,
thread/tokenUsage/updated, error, turn/completed; turn/steer {threadId, input, expectedTurnId};
turn/interrupt {threadId, turnId}. `[codex].transport = "exec"` goes back to `codex exec`.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable
from typing import TYPE_CHECKING

from vision.checklist import checklist_of
from vision.reply import READING, THINKING, ReplyText, dedupe_status, retry_label

if TYPE_CHECKING:
    from vision.brain import unified_diff_hunks, Turn
    from vision.codex import CodexBrain

RESPONSE_TIMEOUT = 60.0  # seconds for the app-server to answer a setup request (initialize, thread/start…)
STEER_TIMEOUT = 5.0
INTERRUPT_GRACE = 3.0  # after turn/interrupt, how long before the process is killed instead
TOOL_LABELS = {"commandExecution": "Bash", "fileChange": "Edit", "webSearch": "WebSearch", "imageView": "Read", "mcpToolCall": "MCP"}


class AppServerError(RuntimeError):
    pass


def _config_overrides(entries: list[str]) -> dict:
    """`[codex].extra_config` holds `codex exec -c key=value` strings (TOML values); the app-server
    takes the same overrides as a map of dotted keys to JSON values."""
    out: dict = {}
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if not sep:
            continue
        try:
            out[key.strip()] = tomllib.loads(f"v = {value.strip()}")["v"]
        except tomllib.TOMLDecodeError:
            out[key.strip()] = value.strip()
    return out


def _snake(obj):
    from vision.codex import _snake as snake

    return snake(obj)


class AppServerTurn:
    """One Vision turn on a `codex app-server` of its own: spawned for the turn, gone after it.

    The ask thread pumps stdout (`pump`), handling notifications as they come and filing responses
    for whoever waits on them: itself during setup, another thread for a steer or an interrupt."""

    def __init__(self, exe: str, cwd: str, env: dict, stall_s: float = 180):
        self.proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=cwd, env=env, text=True, bufsize=1, encoding="utf-8")
        self.stderr: list[str] = []
        threading.Thread(target=lambda: self.stderr.append(self.proc.stderr.read() if self.proc.stderr else ""), daemon=True).start()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, list] = {}  # request id → [Event, response]
        self._waiting_lock = threading.Lock()
        self.pump_thread: threading.Thread | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.closed = False
        self.stall_s = stall_s
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        try:
            while line := self.proc.stdout.readline():
                self._lines.put(line)
        finally:
            self._lines.put(None)

    # -- writing
    def _send(self, obj: dict) -> bool:
        with self._write_lock:
            if self.closed:
                return False
            try:
                self.proc.stdin.write(json.dumps(obj) + "\n")
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def request(self, method: str, params: dict) -> tuple[int, list]:
        with self._waiting_lock:
            self._next_id += 1
            rid = self._next_id
            slot = self._waiting[rid] = [threading.Event(), None]
        if not self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            slot[1] = {"error": {"message": "codex app-server is gone"}}
            slot[0].set()
        return rid, slot

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result: dict | None = None, error: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result or {}})})

    # -- reading
    def pump(self, on_notification: Callable[[str, dict], None], until: Callable[[], bool]) -> bool:
        """Read messages until `until()` holds (True) or stdout ends (False)."""
        self.pump_thread = threading.current_thread()
        last_line = time.monotonic()
        while not until():
            try:
                line = self._lines.get(timeout=0.25)
            except queue.Empty:
                if self.stall_s > 0 and time.monotonic() - last_line > self.stall_s:
                    self.kill()
                    raise AppServerError("codex app-server stopped responding")
                continue
            if line is None:
                return False
            last_line = time.monotonic()
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                # A request from the server (an approval, a question): Vision runs Codex with approvals
                # off, so none should come; refuse rather than leave the turn waiting on an answer.
                self.respond(msg["id"], error={"code": -32601, "message": f"Vision does not handle {msg['method']}"})
            elif "method" in msg:
                on_notification(msg["method"], msg.get("params") or {})
            elif "id" in msg:
                with self._waiting_lock:
                    slot = self._waiting.pop(msg["id"], None)
                if slot is not None:
                    slot[1] = msg
                    slot[0].set()
        return True

    def call(self, method: str, params: dict, on_notification: Callable[[str, dict], None]) -> dict:
        """A setup request, answered while notifications keep flowing; the `result`, or AppServerError."""
        _, slot = self.request(method, params)
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        if not self.pump(on_notification, lambda: slot[0].is_set() or time.monotonic() > deadline) or not slot[0].is_set():
            raise AppServerError(self._last_words() or f"codex app-server did not answer {method}")
        reply = slot[1] or {}
        if isinstance(reply.get("error"), dict):
            raise AppServerError(reply["error"].get("message") or f"{method} failed")
        return reply.get("result") or {}

    def _last_words(self) -> str:
        text = "".join(self.stderr).strip()
        return text.splitlines()[-1] if text else ""

    # -- control from other threads
    def steer(self, text: str) -> bool:
        if not (self.thread_id and self.turn_id) or self.closed:
            return False
        _, slot = self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                              "input": [{"type": "text", "text": text, "text_elements": []}]})
        if threading.current_thread() is self.pump_thread:
            return True  # called from a callback of the turn itself: its answer arrives on this thread later
        if not slot[0].wait(STEER_TIMEOUT):
            return False
        return not isinstance((slot[1] or {}).get("error"), dict)  # no active turn / not steerable: queue it

    def interrupt(self) -> None:
        if self.thread_id and self.turn_id:
            self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

        def kill_later() -> None:
            time.sleep(INTERRUPT_GRACE)
            self.kill()

        threading.Thread(target=kill_later, daemon=True).start()

    def close(self) -> None:
        with self._write_lock:
            self.closed = True
            try:
                self.proc.stdin.close()
            except (OSError, ValueError, AttributeError):
                pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except OSError:
                    pass


def run_turn(brain: "CodexBrain", prompt: str, *, on_text=None, on_status=None, on_agent=None, on_tool=None) -> "Turn":
    """One Codex turn over the app-server, with the same results and side effects as the exec path."""
    from vision.brain import ToolCall, Turn, brain_env, inject_handoff, one_line, tool_input, unified_diff_hunks
    from vision.codex import LAST_SESSION_FILE, _is_uuid, sandbox_for
    from vision.subagents import AgentTracker, codex_item

    turn = Turn(session_id=brain.session_id, model=brain.cfg.model or None)
    prompt = inject_handoff(brain, prompt)
    requested = brain.session_id
    reply = ReplyText(on_text)
    on_status = dedupe_status(on_status)
    subs = AgentTracker(turn, on_agent, model=brain.cfg.model or "", effort=brain.cfg.effort or "")
    tools: dict[str, ToolCall] = {}
    streamed: set[str] = set()  # agent messages that arrived as deltas
    state = {"done": False, "status": "", "error": "", "last_error": "", "usage": None, "last_message": None}
    child_said: dict[str, str] = {}  # a sub-agent thread's latest message: its row's summary when it completes

    def tool_changed(call: ToolCall) -> None:
        if on_tool:
            on_tool(call)

    def item_started(item: dict) -> None:
        kind = item.get("type", "")
        if kind == "reasoning":
            if on_status:
                on_status(THINKING)
        elif kind in TOOL_LABELS:
            label = TOOL_LABELS[kind] if kind != "mcpToolCall" else (item.get("tool") or "MCP")
            turn.tools_used.append(label)
            reply.tool()
            if item.get("id"):
                detail = item.get("command") or item.get("query") or (item.get("path") or "") or " ".join(
                    c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict))
                call = tools[item["id"]] = ToolCall(item["id"], label, one_line(str(detail)))
                call.input = tool_input(label, {k: item[k] for k in ("command", "cwd", "query", "path", "server", "arguments")
                                                if item.get(k) not in (None, "")})
                turn.tools.append(call)
                tool_changed(call)
            if on_status:
                on_status(label)

    def item_completed(item: dict) -> None:
        kind = item.get("type", "")
        if kind == "agentMessage":
            text = item.get("text") or ""
            if item.get("id") not in streamed and text:
                reply.add(("\n\n" if reply.parts or reply._held else "") + text)
            state["last_message"] = text or state["last_message"]
        elif kind in TOOL_LABELS:
            call = tools.get(item.get("id") or "")
            if call is not None:
                call.output = item.get("aggregatedOutput") or ""
                call.is_error = item.get("status") == "failed" or (item.get("exitCode") not in (None, 0))
                if kind == "fileChange":  # each change may carry its unified diff
                    call.diff = unified_diff_hunks("\n".join(
                        c.get("diff") or "" for c in item.get("changes") or [] if isinstance(c, dict)))
                call.done = True
                tool_changed(call)
            if on_status:
                on_status(READING)
        elif kind == "error" and item.get("message"):
            state["last_error"] = item["message"]

    def child_event(thread: str, method: str, params: dict) -> None:
        """A sub-agent's own thread, streamed by the app-server. Its tool
        calls are steps on its row and its messages the row's summary; none of it is the reply."""
        item = params.get("item") or {}
        kind = item.get("type", "")
        if method == "item/started" and kind in TOOL_LABELS:
            label = TOOL_LABELS[kind] if kind != "mcpToolCall" else (item.get("tool") or "MCP")
            subs.step(thread, label, str(item.get("command") or item.get("query") or item.get("path") or ""))
        elif method == "item/completed" and kind == "agentMessage" and item.get("text"):
            child_said[thread] = item["text"]

    def notification(method: str, params: dict) -> None:
        thread = params.get("threadId") or ""
        if thread and rpc.thread_id and thread != rpc.thread_id:
            child_event(thread, method, params)
            return
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId") or "", params.get("delta") or ""
            if delta:
                if item_id not in streamed:
                    streamed.add(item_id)
                    if reply.parts or reply._held:
                        delta = "\n\n" + delta  # a new message of the same reply: its own paragraph
                reply.add(delta)
        elif method in ("item/started", "item/completed", "item/updated"):
            item = params.get("item") or {}
            norm = _normalized(item)
            if codex_item(subs, norm, method == "item/completed"):
                child = subs.get(norm.get("agent_thread_id"))
                if child is not None and child.done and not child.summary and child_said.get(norm["agent_thread_id"]):
                    child.summary = child_said[norm["agent_thread_id"]]
                    subs._changed(child)
                if method == "item/started" and on_status:
                    on_status("Agent")
                return
            if method == "item/started":
                item_started(item)
            elif method == "item/completed":
                item_completed(item)
        elif method == "turn/plan/updated":
            checklist_of(brain).codex_plan(params.get("plan"))
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            if on_status:
                on_status(THINKING)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            if isinstance(usage.get("total"), dict):
                state["usage"] = _snake(usage["total"])
            last = usage.get("last") or {}
            if last.get("inputTokens"):
                brain.context = (int(last["inputTokens"]), int(usage.get("modelContextWindow") or 0) or 258_400)
        elif method == "error":
            err = params.get("error") or {}
            message = (err.get("message") if isinstance(err, dict) else str(err)) or ""
            if params.get("willRetry"):
                if on_status and message:
                    on_status(retry_label(None, None, None, message.strip().splitlines()[0][:40]))
            elif message:
                state["last_error"] = message
        elif method == "turn/completed":
            done = params.get("turn") or {}
            if not rpc.turn_id or done.get("id") in (None, rpc.turn_id):
                state["done"] = True
                state["status"] = done.get("status") or "completed"
                err = done.get("error") or {}
                state["error"] = (err.get("message") if isinstance(err, dict) else str(err or "")) or ""

    sandbox = sandbox_for(brain.cfg)
    rpc = AppServerTurn(brain.codex, brain.workdir, brain_env("codex", getattr(brain, "chat_id", "")), brain.cfg.stall_s)
    with brain._lock:
        brain._rpc = rpc
    try:
        rpc.call("initialize", {"clientInfo": {"name": "vision", "title": "Vision", "version": "0.1.0"},
                                "capabilities": {"experimentalApi": True}}, notification)
        rpc.notify("initialized", {})
        settings = brain.app_server_settings(sandbox)
        thread = None
        if brain.session_id:
            try:
                thread = rpc.call("thread/resume", {"threadId": brain.session_id, **settings}, notification).get("thread")
            except AppServerError:
                thread = None  # unknown or unreadable thread: a fresh one, as exec does
        if not thread:
            thread = rpc.call("thread/start", settings, notification).get("thread") or {}
        rpc.thread_id = thread.get("id")
        turn.session_id = rpc.thread_id or turn.session_id
        started = rpc.call("turn/start", {"threadId": rpc.thread_id, "input": [{"type": "text", "text": prompt, "text_elements": []}]},
                           notification)
        rpc.turn_id = (started.get("turn") or {}).get("id") or rpc.turn_id
        rpc.pump(notification, lambda: state["done"])
    except AppServerError as e:
        state["error"] = state["error"] or str(e)
    except KeyboardInterrupt:
        brain._cancelled_app = True
    finally:
        rpc.close()
        with brain._lock:
            brain._rpc = None
        subs.close("cancelled" if brain._cancelled_app else "cut off when the turn ended")
        for call in tools.values():
            if not call.done:
                call.done = call.is_error = True
                tool_changed(call)

    turn.text = reply.finish()
    if brain.task_mode and state["last_message"]:
        try:
            turn.data = json.loads(state["last_message"])
        except ValueError:
            pass
    if state["usage"]:
        brain._record_usage(state["usage"], turn, fresh_thread=not requested)
    cancelled = brain._cancelled_app or state["status"] == "interrupted"
    brain._cancelled_app = False
    if cancelled:
        turn.is_error, turn.error = True, "cancelled"
    elif not state["done"] or state["status"] == "failed":
        turn.is_error = True
        turn.error = state["error"] or state["last_error"] or rpc._last_words() or "codex app-server ended the turn early"
        if requested and not turn.session_id:
            brain.session_id = None
            if not brain.task_mode:
                try:
                    LAST_SESSION_FILE.unlink()
                except FileNotFoundError:
                    pass
    if not turn.is_error or cancelled:
        if turn.session_id and _is_uuid(turn.session_id):
            brain.session_id = turn.session_id
            brain._remember_session()
    if not turn.is_error:
        brain.handoff = None  # delivered with this turn
        brain.model = brain.cfg.model or brain.model
        brain._model_seen_for = brain.cfg.model
    return turn


def _normalized(item: dict) -> dict:
    """An app-server item in the shape vision.subagents reads (the exec stream's snake_case)."""
    if item.get("type") == "subAgentActivity":
        return {"type": "sub_agent_activity", "id": item.get("id"), "kind": item.get("kind"),
                "agent_thread_id": item.get("agentThreadId"), "agent_path": item.get("agentPath")}
    if item.get("type") != "collabAgentToolCall":
        return item
    out = _snake(item)
    out["type"] = "collab_agent_tool_call"
    tool = item.get("tool") or ""
    out["tool"] = {"spawnAgent": "spawn_agent", "sendInput": "send_input", "closeAgent": "close_agent"}.get(tool, tool)
    states = item.get("agentsStates") or {}
    out["agents_states"] = {k: {"status": _state(v.get("status") if isinstance(v, dict) else v), "message": (v or {}).get("message") if isinstance(v, dict) else ""}
                            for k, v in states.items()}  # child ids are values, not keys to snake
    out["receiver_thread_ids"] = list(item.get("receiverThreadIds") or [])
    out["status"] = {"inProgress": "in_progress"}.get(item.get("status") or "", item.get("status") or "")
    return out


def _state(status) -> str:
    return {"pendingInit": "pending_init", "notFound": "not_found"}.get(status, status or "")
