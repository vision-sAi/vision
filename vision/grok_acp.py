"""Grok turns through `grok agent stdio` (ACP: JSON-RPC over stdio) rather than headless `grok -p`.

Headless takes the prompt once and prints. Agent mode keeps the session open to its client, which
buys Vision:

- `_x.ai/interject`: a message typed mid-reply joins the running turn (Ctrl-X / Send now), as with
  Claude and Codex;
- live tool rows (on_tool), from the session's tool_call / tool_call_update updates;
- the permission prompts: the session runs with yoloMode off and Vision answers each request itself,
  approving everything [brain].denied_tools does not rule out (headless had --always-approve plus
  --deny for the same result; agent mode takes neither flag).

Protocol checked against the agent's setup, session/load replay, interject, queue and prompt_complete
events, and against the updates.jsonl Grok saves per session
(message, thought and tool shapes): initialize → session/new {cwd, mcpServers, _meta: {rules,
yoloMode}} | session/load (which replays the history as session/update before it answers) →
session/prompt; notifications session/update (agent_message_chunk, agent_thought_chunk, tool_call,
tool_call_update; `_meta.totalTokens` is the context), _x.ai/session/update and
_x.ai/session_notification (turn_completed with usage, retry_state), _x.ai/queue/changed; requests
session/request_permission. `_x.ai/interject {sessionId, text}` joins the running turn; one that
reaches an idle session becomes a turn of Grok's own ("interject-fallback-…"), which this turn waits
out. Sandbox via GROK_SANDBOX (agent mode has no --sandbox); effort via --reasoning-effort.
`[grok].transport = "headless"` goes back to `grok -p`; worker (task mode) turns always use it for
its --json-schema.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from vision.checklist import checklist_of
from vision.reply import READING, THINKING, ReplyText, dedupe_status, retry_label

if TYPE_CHECKING:
    from vision.brain import Turn
    from vision.grok import GrokBrain

RESPONSE_TIMEOUT = 60.0  # seconds for the agent to answer a setup request (initialize, session/new…)
STEER_TIMEOUT = 5.0
STEER_GRACE = 1.5  # after the reply ends, how long a just-sent interject gets to start Grok's turn for it
CANCEL_GRACE = 3.0  # after session/cancel, how long before the process is killed instead
_KIND_LABELS = {"read": "Read", "edit": "Edit", "delete": "Edit", "move": "Edit", "search": "Grep", "execute": "Bash", "fetch": "WebFetch"}
_WRITES = {"edit", "delete", "move"}


class AgentError(RuntimeError):
    pass


class AgentSession:
    """A `grok agent stdio` (or any ACP agent) of Vision's own: started for a turn and, since
    keep_alive_s, kept for the turns after it until its command, directory or session changes.

    A reader thread queues stdout lines and the ask thread handles them (`pump`), so it can also wait
    on a clock (the grace after a late interject). Requests from Grok go to `on_request`; responses
    are filed for whoever waits on them: the ask thread during setup, another thread for a steer."""

    def __init__(self, cmd: list[str], cwd: str, env: dict):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=cwd, env=env, text=True, bufsize=1, encoding="utf-8")
        self.stderr: list[str] = []
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, list] = {}  # request id → [Event, response]
        self._waiting_lock = threading.Lock()
        self.pump_thread: threading.Thread | None = None
        self.session_id: str | None = None
        self.closed = False
        self.lock = threading.Lock()  # `taking` and `last_steer`, read by the turn's end check
        self.taking = False  # a prompt is running and a steered message can still join it
        self.last_steer = 0.0

    def _read_stderr(self) -> None:
        try:
            self.stderr.append(self.proc.stderr.read())
            self.proc.stderr.close()
        except (OSError, ValueError):
            pass

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
            self.proc.stdout.close()
        except (OSError, ValueError):
            pass
        self.lines.put(None)

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
            slot[1] = {"error": {"message": "grok agent is gone"}}
            slot[0].set()
        return rid, slot

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result: dict | None = None, error: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result or {}})})

    # -- reading
    def pump(self, on_notification: Callable[[str, dict], None], on_request: Callable[[str, dict], dict | None],
             until: Callable[[], bool], timeout: float | None = None) -> bool:
        """Handle messages until `until()` holds (True), or stdout ends or `timeout` passes (False).
        `until` is checked after every message and at least five times a second."""
        self.pump_thread = threading.current_thread()
        deadline = time.monotonic() + timeout if timeout else None
        while not until():
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                if deadline and time.monotonic() > deadline:
                    return False
                continue
            if line is None:
                self.lines.put(None)  # stays ended for a later pump
                return False
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                result = on_request(msg["method"], msg.get("params") or {})
                if result is None:
                    self.respond(msg["id"], error={"code": -32601, "message": f"Vision does not handle {msg['method']}"})
                else:
                    self.respond(msg["id"], result)
            elif "method" in msg:
                on_notification(msg["method"], msg.get("params") or {})
            elif "id" in msg:
                with self._waiting_lock:
                    slot = self._waiting.pop(msg["id"], None)
                if slot is not None:
                    slot[1] = msg
                    slot[0].set()
        return True

    def call(self, method: str, params: dict, on_notification, on_request) -> dict:
        """A setup request, answered while notifications keep flowing; the `result`, or AgentError."""
        _, slot = self.request(method, params)
        self.pump(on_notification, on_request, lambda: slot[0].is_set(), RESPONSE_TIMEOUT)
        if not slot[0].is_set():
            raise AgentError(self.last_words() or f"grok agent did not answer {method}")
        return result_of(slot[1] or {}, method)

    def last_words(self) -> str:
        text = "".join(self.stderr).strip()
        return text.splitlines()[-1] if text else ""

    # -- control from other threads
    def steer(self, text: str) -> bool:
        with self.lock:
            if not self.taking or self.closed or not self.session_id:
                return False
            self.last_steer = time.monotonic()
        _, slot = self.request("_x.ai/interject", {"sessionId": self.session_id, "text": text})
        if threading.current_thread() is self.pump_thread:
            return True  # called from a callback of the turn itself: its answer arrives on this thread later
        if not slot[0].wait(STEER_TIMEOUT):
            return False
        return not isinstance((slot[1] or {}).get("error"), dict)

    def interrupt(self) -> None:
        if self.session_id:
            self.notify("session/cancel", {"sessionId": self.session_id})

        def kill_later() -> None:
            time.sleep(CANCEL_GRACE)
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
            self.proc.wait(timeout=2)
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


def _expire_kept(brain: "GrokBrain", rpc: AgentSession) -> None:
    """keep_alive_s idle: the kept agent goes; the next turn starts one."""
    with brain._lock:
        if brain._acp is not rpc or brain._turning:
            return
        brain._acp = None
    rpc.close()
    rpc.kill()


def result_of(reply: dict, method: str) -> dict:
    err = reply.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        raise AgentError(str(data) if isinstance(data, str) and data else err.get("message") or f"{method} failed")
    return reply.get("result") or {}


def _tool_name(update: dict) -> str:
    """Grok's own tool id (read_file, run_terminal_command…): in `_meta`, else the first title."""
    meta = (update.get("_meta") or {}).get("x.ai/tool") or {}
    return meta.get("name") or update.get("title") or ""


def _args(raw) -> dict:
    """A Grok tool input with the keys vision.brain.tool_detail reads."""
    args = dict(raw) if isinstance(raw, dict) else {}
    path = args.get("target_file") or args.get("file_path") or args.get("path")
    if path:
        args["file_path"] = path
    return args


def _output(update: dict) -> str:
    return "\n".join(c["content"].get("text", "") for c in update.get("content") or []
                     if isinstance(c, dict) and isinstance(c.get("content"), dict) and c["content"].get("type") == "text")


def _usage(u: dict) -> dict:
    """turn_completed's camelCase usage in the headless stream's names (what _record_usage keeps)."""
    return {"input_tokens": int(u.get("inputTokens") or 0), "output_tokens": int(u.get("outputTokens") or 0),
            "cache_read_input_tokens": int(u.get("cachedReadTokens") or 0),
            "cache_creation_input_tokens": int(u.get("cacheCreationTokens") or 0),
            "reasoning_tokens": int(u.get("reasoningTokens") or 0)}


def permission(brain: "GrokBrain", params: dict, refused: list | None = None) -> dict:
    """Answer a session/request_permission: allow once, unless a deny rule matches the command or
    plan mode meets a write. Never an "always" option: that would outlive Vision's rules.
    A refusal is appended to `refused` as (what, why): Grok ends the turn on one, so the caller says why."""
    from vision.localtools import denied

    call = params.get("toolCall") or {}
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    kind = ((call.get("_meta") or {}).get("x.ai/tool") or {}).get("kind") or call.get("kind") or ""
    refuse = None
    if raw.get("command"):
        refuse = denied(str(raw["command"]), brain.cfg.denied_tools)
    if not refuse and brain.cfg.mode == "plan" and (kind in _WRITES or _tool_name(call) in ("search_replace", "write")):
        refuse = "plan mode is read-only"
    if refuse and refused is not None:
        refused.append((str(raw.get("command") or call.get("title") or _tool_name(call) or "that"), refuse))
    options = params.get("options") or []
    want = ("reject_once",) if refuse else ("allow_once",)
    pick = next((o for o in options if o.get("kind") in want), None)
    if pick is None:
        if refuse:
            return {"outcome": {"outcome": "cancelled"}}
        pick = next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
    if pick is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": pick.get("optionId")}}


def run_turn(brain: "GrokBrain", prompt: str, *, on_text=None, on_status=None, on_agent=None, on_tool=None) -> "Turn":
    """One Grok turn over agent mode, with the same results and side effects as the headless path."""
    from vision.brain import ToolCall, Turn, brain_env, inject_handoff, one_line, tool_detail, tool_input
    from vision.grok import LAST_SESSION_FILE, TOOL_LABELS, _cli_model, sandbox_for
    from vision.subagents import AgentTracker, grok_tool_call, grok_tool_update

    model = _cli_model(brain.cfg.model) or None
    turn = Turn(session_id=brain.session_id, model=model)
    prompt = inject_handoff(brain, prompt)
    reply = ReplyText(on_text)
    on_status = dedupe_status(on_status)
    subs = AgentTracker(turn, on_agent, model=model or "", effort=brain.cfg.effort or "")
    sub_calls: dict[str, str] = {}
    tools: dict[str, ToolCall] = {}
    usage: dict = {}
    state = {"prompting": False, "busy": False, "last_error": "", "cancelled": False}
    refused: list[tuple[str, str]] = []  # Vision's own refusals this turn (Grok cancels the turn after one)

    def tool_changed(call: ToolCall) -> None:
        if on_tool:
            on_tool(call)

    def session_update(update: dict, meta: dict) -> None:
        kind = update.get("sessionUpdate")
        if isinstance(meta.get("totalTokens"), int) and meta["totalTokens"]:
            brain.context = (meta["totalTokens"], brain.context_window())
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text") or ""
            if text:
                reply.add(text)
        elif kind == "plan":
            checklist_of(brain).acp_plan(update.get("entries"))
        elif kind == "agent_thought_chunk":
            if on_status:
                on_status(THINKING)
        elif kind == "tool_call":
            name = _tool_name(update)
            tid = update.get("toolCallId") or name
            if grok_tool_call(subs, {**update, "toolName": name}, sub_calls):
                turn.tools_used.append("Agent")  # a sub-agent has its own row (on_agent), as with Claude
                reply.tool()
                if on_status:
                    on_status("Agent")
                return
            label = TOOL_LABELS.get(name) or _KIND_LABELS.get(update.get("kind") or "") or name or "tool"
            turn.tools_used.append(label)
            reply.tool()
            if tid not in tools:
                call = tools[tid] = ToolCall(tid, label, one_line(tool_detail(label, _args(update.get("rawInput")))))
                call.input = tool_input(label, _args(update.get("rawInput")))
                turn.tools.append(call)
                tool_changed(call)
            if on_status:
                on_status(label)
        elif kind == "tool_call_update":
            tid = update.get("toolCallId") or ""
            grok_tool_update(subs, update, sub_calls)
            call = tools.get(tid)
            status = (update.get("status") or "").lower()
            if call is not None and not call.done:
                if not call.detail and update.get("rawInput"):
                    call.detail = one_line(tool_detail(call.name, _args(update.get("rawInput"))))
                    call.input = call.input or tool_input(call.name, _args(update.get("rawInput")))
                    tool_changed(call)
                if status in ("completed", "failed", "cancelled", "error"):
                    call.output, call.is_error, call.done = _output(update), status != "completed", True
                    tool_changed(call)
            if on_status and status and not any(not c.done for c in tools.values()):
                on_status(READING)

    def grok_update(update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "turn_completed":
            for k, v in _usage(update.get("usage") or {}).items():
                usage[k] = usage.get(k, 0) + v
            if (update.get("stop_reason") or "") == "cancelled":
                state["cancelled"] = True
        elif kind == "retry_state":
            reason = str(update.get("reason") or "").strip().splitlines()[0][:40] if update.get("reason") else ""
            if update.get("type") == "retrying":
                if on_status:
                    on_status(retry_label(update.get("attempt"), update.get("max_retries"), None, reason))
            elif update.get("reason"):
                state["last_error"] = str(update["reason"])

    def notification(method: str, params: dict) -> None:
        if not state["prompting"]:
            return  # session/load replays the old conversation first: none of it belongs to this turn
        if method == "session/update":
            session_update(params.get("update") or {}, params.get("_meta") or {})
        elif method in ("_x.ai/session/update", "_x.ai/session_notification"):
            grok_update(params.get("update") or {})
        elif method == "_x.ai/queue/changed":
            state["busy"] = bool(params.get("runningPromptId") or params.get("entries"))

    def request(method: str, params: dict) -> dict | None:
        if method == "session/request_permission":
            return permission(brain, params, refused)
        return None  # ask_user_question, plan-mode tools: headless turned them off; refused here

    sandbox = sandbox_for(brain.cfg)
    env = brain_env("grok", getattr(brain, "chat_id", ""))
    env["GROK_SANDBOX"] = sandbox
    cmd = [brain.grok, "agent", "--no-leader"]
    if model:
        cmd += ["--model", model]
    if brain.cfg.effort:
        cmd += ["--reasoning-effort", brain.cfg.effort]
    cmd.append("stdio")
    # The agent is kept from one turn to the next (brain.keep_alive_s): each turn used to start it
    # again, replay the whole conversation through session/load and wait for it to exit. Another
    # command, directory, sandbox or session, or a process that has gone, means a new one. The rules
    # (the persona) it was given at its start stay, as they do for a session Grok resumes.
    keep = brain.keep_alive_s > 0
    key = (tuple(cmd), brain.workdir, sandbox, brain.session_id)
    with brain._lock:
        if brain._idle_timer is not None:
            brain._idle_timer.cancel()
            brain._idle_timer = None
        rpc = brain._acp
        if rpc is not None and not (keep and brain._acp_key == key and not rpc.closed and rpc.proc.poll() is None):
            rpc.close()
            rpc.kill()
            rpc = None
        started = rpc is None
        if started:
            rpc = AgentSession(cmd, brain.workdir, env)
        brain._acp, brain._acp_key, brain._turning = rpc, key, True
    requested = brain.session_id
    error = ""
    kept = False
    try:
        if started:
            rpc.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                                    "clientInfo": {"name": "vision", "title": "Vision", "version": "0.1.0"}}, notification, request)
            setup = {"cwd": brain.workdir, "mcpServers": [], "_meta": {"rules": brain._persona(sandbox), "yoloMode": False}}
            if brain.session_id:
                try:
                    rpc.call("session/load", {"sessionId": brain.session_id, **setup}, notification, request)
                    rpc.session_id = brain.session_id
                except AgentError:
                    rpc.session_id = None  # gone or unreadable: a fresh session, as a failed --resume did
            if not rpc.session_id:
                rpc.session_id = rpc.call("session/new", setup, notification, request).get("sessionId")
        turn.session_id = rpc.session_id or turn.session_id
        state["prompting"] = True
        with rpc.lock:
            rpc.taking = True
        _, slot = rpc.request("session/prompt", {"sessionId": rpc.session_id, "prompt": [{"type": "text", "text": prompt}]})

        def finished() -> bool:
            """The prompt has answered, Grok runs nothing (a late interject's turn included), and no
            interject is still on its way: from here a steered message queues in Vision instead."""
            with rpc.lock:
                if slot[0].is_set() and not state["busy"] and time.monotonic() - rpc.last_steer > STEER_GRACE:
                    rpc.taking = False
                    return True
            return False

        if not rpc.pump(notification, request, finished) and not slot[0].is_set():
            error = state["last_error"] or rpc.last_words() or "grok agent ended the turn early"
        elif slot[0].is_set():
            result = result_of(slot[1] or {}, "session/prompt")
            if (result.get("stopReason") or "") == "cancelled":
                state["cancelled"] = True
    except AgentError as e:
        error = str(e)
    except KeyboardInterrupt:
        state["cancelled"] = True
    finally:
        with rpc.lock:
            rpc.taking = False
        kept = keep and not error and not state["cancelled"] and not brain._killed and not rpc.closed and rpc.proc.poll() is None
        with brain._lock:
            brain._turning = False
            if kept and brain._acp is rpc:
                brain._acp_key = (tuple(cmd), brain.workdir, sandbox, rpc.session_id)  # the session it is on now
                t = threading.Timer(brain.keep_alive_s, _expire_kept, args=(brain, rpc))
                t.daemon = True
                t.start()
                brain._idle_timer = t
            else:
                kept = False
                if brain._acp is rpc:
                    brain._acp = None
        if not kept:
            rpc.close()
        cancelled = state["cancelled"] or brain._killed
        subs.close("cancelled" if cancelled else "cut off when the turn ended")
        for call in tools.values():
            if not call.done:
                call.done = call.is_error = True
                tool_changed(call)

    turn.text = reply.finish()
    if cancelled and refused and not brain._killed:
        # Grok stops the turn as "cancelled" when a permission is rejected; nobody cancelled it, so say what was refused.
        what, why = refused[-1]
        rule = f"it matches your {why} rule" if why.startswith("Bash(") else why
        note = f"I didn't run `{what}`: {rule}."
        lead = "\n\n" if turn.text.strip() else ""
        turn.text = f"{turn.text.rstrip()}{lead}{note}"
        if on_text:
            on_text(lead + note)
        cancelled = False
    if usage:
        turn.usage = usage
    if cancelled:
        turn.is_error, turn.error = True, "cancelled"
    elif error:
        turn.is_error, turn.error = True, error
        if requested and not rpc.session_id:
            brain.session_id = None
            try:
                LAST_SESSION_FILE.unlink()
            except FileNotFoundError:
                pass
    if rpc.session_id and (not turn.is_error or cancelled):
        brain.session_id = rpc.session_id
        brain._remember_session()
    if not turn.is_error:
        brain.handoff = None
        brain.model = model or brain.model
        brain._model_seen_for = brain.cfg.model
        if turn.usage:
            brain._record_usage(turn.usage, turn)
    return turn
