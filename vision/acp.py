"""Any agent that speaks ACP (the Agent Client Protocol: JSON-RPC over stdio) as a Vision provider.

A `[providers.<name>]` table with `type = "acp"` and a `command` (`["gemini", "--experimental-acp"]`)
makes one. Vision starts the command for each turn, opens or resumes a session, sends the prompt and
shows what comes back the way it shows Grok's agent mode, on the same transport (vision.grok_acp.
AgentSession) with only the protocol's standard part:

initialize → session/new {cwd, mcpServers} | session/load {sessionId, …} (which replays the history
as session/update notifications before it answers) → session/prompt {sessionId, prompt: [{type:
text, text}]} → its result {stopReason}. Meanwhile session/update carries agent_message_chunk,
agent_thought_chunk, tool_call {toolCallId, title, kind, rawInput} and tool_call_update {status,
content, rawInput}; session/request_permission {toolCall, options[{optionId, kind}]} is answered
here (allow once, unless [brain].denied_tools rules the command out or plan mode meets a write);
session/cancel ends a turn. Nothing vendor-specific: no rules meta, so the persona goes in as the
first prompt of a fresh session; no interject, so a message sent mid-reply queues for the next turn.

Vision keeps its own transcript of each session (user and assistant text) in the local-sessions
folder, the shape vision.local saves, so /session lists the agent's conversations and a resume that
the agent refuses still carries the history.

Built against tests/fake_acp_agent.py (the standard slice above); not yet run against a real agent.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from vision.checklist import checklist_of
from vision.config import STATE_DIR, BrainConfig, weather_ready
from vision.grok_acp import CANCEL_GRACE, AgentError, AgentSession, _KIND_LABELS, _args, _output, permission, result_of
from vision.persona import system_prompt
from vision.reply import READING, THINKING, ReplyText, dedupe_status

if TYPE_CHECKING:
    from vision.brain import Turn

CONTEXT_GUESS = 200_000  # tokens; an ACP agent doesn't say its window


class AcpBrain:
    """Same interface as the Claude brain so the CLI does not care which one is thinking."""

    def __init__(self, cfg: BrainConfig, voice_mode: bool = False, session_id: str | None = None):
        from vision.models import provider_for
        from vision.providers import get

        self.cfg = cfg
        self.provider = provider_for(cfg.model) if cfg.model else ""
        self.agent = get(self.provider).acp if get(self.provider) and get(self.provider).acp else None
        self.voice_mode = voice_mode
        self.task_mode = False
        self.session_id = session_id
        self.workdir = os.path.abspath(os.path.expanduser(cfg.workdir)) if cfg.workdir else os.getcwd()
        self.last_usage: dict | None = None
        self.model: str | None = None
        self._model_seen_for: str | None = None
        self.handoff: str | None = None
        self.output_tokens = 0
        self.context: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._acp: AgentSession | None = None  # the agent process: running a turn, or kept for the next one
        self._acp_key: tuple | None = None  # what the kept process was started for (command, directory, session)
        self._idle_timer: threading.Timer | None = None
        self._turning = False
        self._killed = False
        self._messages: list[dict] = self._load(session_id) if session_id else []

    # -- sessions (Vision's own transcript; the agent keeps the real one) ------------------
    @staticmethod
    def last_session_id() -> str | None:
        return None  # an agent's last session is resumed from /session, not on start-up

    @staticmethod
    def _dir():
        from vision.local import SESSIONS_DIR

        return SESSIONS_DIR

    def _load(self, sid: str) -> list[dict]:
        try:
            data = json.loads((self._dir() / f"{sid}.json").read_text(encoding="utf-8"))
            return [m for m in data.get("messages", []) if isinstance(m, dict)]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        if self.task_mode or not self.session_id:
            return
        d = self._dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.session_id}.json").write_text(
            json.dumps({"model": self.cfg.model, "provider": self.provider, "at": time.time(), "messages": self._messages}, ensure_ascii=False),
            encoding="utf-8")

    def new_session(self) -> None:
        self.close()  # the kept process is on the old session
        self.session_id = None
        self._messages = []
        self.handoff = None
        self.context = None

    def resume(self, session_id: str) -> None:
        self.close()
        self.session_id = session_id
        self._messages = self._load(session_id)
        self.handoff = None
        self.context = None

    # -- the process between turns (as vision.brain.Brain keeps claude) ----------------------
    # Each turn started the agent again, replayed the whole conversation through session/load and
    # waited up to two seconds for it to exit: a wait that grew with the conversation. The process
    # is kept for the next turn, and restarted when its command, directory or session changes or
    # after keep_alive_s idle. The persona a fresh session got in its first prompt stays as it was,
    # as it does for a session the agent resumes.
    @property
    def keep_alive_s(self) -> float:
        if self.task_mode:
            return 0.0
        return max(0.0, float(getattr(self.cfg, "keep_alive_s", 0) or 0))

    def _start_idle_timer(self, rpc: AgentSession) -> None:  # under self._lock
        self._stop_idle_timer()
        t = threading.Timer(self.keep_alive_s, self._expire, args=(rpc,))
        t.daemon = True
        t.start()
        self._idle_timer = t

    def _stop_idle_timer(self) -> None:  # under self._lock
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _expire(self, rpc: AgentSession) -> None:
        with self._lock:
            if self._acp is not rpc or self._turning:
                return
            self._acp = None
        rpc.close()
        rpc.kill()

    def close(self) -> None:
        """End the kept agent process now; the next turn starts one."""
        with self._lock:
            self._stop_idle_timer()
            rpc, self._acp = self._acp, None
        if rpc is not None:
            rpc.close()
            rpc.kill()

    def resolved_model(self) -> str | None:
        return self.model or self._model_arg() or self.cfg.model

    def context_window(self) -> int:
        return CONTEXT_GUESS

    def _model_arg(self) -> str:
        """The model id the agent gets (after the provider prefix); "" for the agent's own default."""
        from vision.models import endpoint_model

        m = endpoint_model(self.cfg.model or "")
        return "" if m in ("", "default") else m

    def _persona(self) -> str:
        if self.task_mode:
            from vision.delegation import worker_prompt

            return worker_prompt(self.cfg, self.workdir, self.provider)
        from vision.folderprompt import for_brain

        return system_prompt(self.voice_mode, self.cfg.address_user_as, self.workdir, self.cfg.allowed_tools, provider=self.provider,
                             denied_tools=self.cfg.denied_tools, mode=self.cfg.mode, weather=weather_ready(self.cfg),
                             project=getattr(self, "project_prompt", ""), folder=for_brain(self.workdir, self.cfg),
                             ask_tools=getattr(self.cfg, "ask_tools", None))

    def _command(self) -> list[str]:
        from vision import clis

        cmd = [clis.find_cli(self.provider), *self.agent.command[1:]]  # the full path: a turn's PATH has a shim of that name first
        if self.agent.model_flag and self._model_arg():
            cmd += [self.agent.model_flag, self._model_arg()]
        return cmd

    # -- the turn ------------------------------------------------------------------------
    def ask(self, prompt: str, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None) -> "Turn":
        from vision.brain import ToolCall, Turn, brain_env, inject_handoff, one_line, tool_detail, tool_input

        if self.agent is None:
            raise AgentError(f"{self.provider or 'this provider'} is not an ACP agent")
        prompt = inject_handoff(self, prompt)
        turn = Turn(session_id=self.session_id, model=self.resolved_model())
        reply = ReplyText(on_text)
        on_status = dedupe_status(on_status)
        tools: dict[str, ToolCall] = {}
        state = {"prompting": False, "cancelled": False, "last_error": ""}
        refused: list[tuple[str, str]] = []
        self._killed = False

        def tool_changed(call: ToolCall) -> None:
            if on_tool:
                on_tool(call)

        def session_update(update: dict, meta: dict) -> None:
            kind = update.get("sessionUpdate")
            if isinstance(meta.get("totalTokens"), int) and meta["totalTokens"]:
                self.context = (meta["totalTokens"], self.context_window())
            if kind == "agent_message_chunk":
                text = (update.get("content") or {}).get("text") or ""
                if text:
                    reply.add(text)
            elif kind == "plan":
                checklist_of(self).acp_plan(update.get("entries"))
            elif kind == "agent_thought_chunk":
                if on_status:
                    on_status(THINKING)
            elif kind == "tool_call":
                tid = update.get("toolCallId") or update.get("title") or ""
                label = _KIND_LABELS.get(update.get("kind") or "") or one_line(update.get("title") or "tool")[:24]
                turn.tools_used.append(label)
                reply.tool()
                if tid not in tools:
                    call = tools[tid] = ToolCall(tid, label, one_line(tool_detail(label, _args(update.get("rawInput"))) or update.get("title") or ""))
                    call.input = tool_input(label, _args(update.get("rawInput")))
                    turn.tools.append(call)
                    tool_changed(call)
                if on_status:
                    on_status(label)
            elif kind == "tool_call_update":
                call = tools.get(update.get("toolCallId") or "")
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

        def notification(method: str, params: dict) -> None:
            if not state["prompting"]:
                return  # session/load replays the old conversation first: none of it belongs to this turn
            if method == "session/update":
                session_update(params.get("update") or {}, params.get("_meta") or {})

        def request(method: str, params: dict) -> dict | None:
            if method == "session/request_permission":
                return permission(self, params, refused)
            return None

        env = brain_env(self.provider, getattr(self, "chat_id", ""))
        env.update(dict(self.agent.env))
        command = self._command()
        keep = self.keep_alive_s > 0
        key = (tuple(command), self.workdir, self.session_id)
        with self._lock:
            self._stop_idle_timer()
            rpc = self._acp
            if rpc is not None and not (keep and self._acp_key == key and not rpc.closed and rpc.proc.poll() is None):
                rpc.close()  # another command, directory or session, or it has gone: not this turn's
                rpc.kill()
                rpc = None
            started = rpc is None
            if started:
                try:
                    rpc = AgentSession(command, self.workdir, env)
                except (OSError, FileNotFoundError) as e:
                    self._acp = None
                    turn.is_error, turn.error = True, f"{self.agent.command[0]} could not start: {e}"
                    return turn
            self._acp, self._acp_key, self._turning = rpc, key, True
        requested = self.session_id
        error = ""
        fresh = False
        kept = False
        try:
            if started:
                rpc.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                                        "clientInfo": {"name": "vision", "title": "Vision", "version": "0.1.0"}}, notification, request)
                setup = {"cwd": self.workdir, "mcpServers": []}
                if self.session_id:
                    try:
                        rpc.call("session/load", {"sessionId": self.session_id, **setup}, notification, request)
                        rpc.session_id = self.session_id
                    except AgentError:
                        rpc.session_id = None  # gone or unreadable: a fresh session, with the history handed over below
                if not rpc.session_id:
                    rpc.session_id = rpc.call("session/new", setup, notification, request).get("sessionId")
                    fresh = True
            if not rpc.session_id:
                raise AgentError("the agent gave no session id")
            turn.session_id = rpc.session_id
            text = prompt
            if fresh:
                # No rules channel in plain ACP: the persona (and, on a lost session, the earlier turns) lead the first prompt.
                earlier = "".join(f"\n{m['role'].capitalize()}: {m['content']}" for m in self._messages if m.get("role") in ("user", "assistant")) if requested else ""
                text = f"<instructions>\n{self._persona()}\n</instructions>\n" + (f"\n<earlier_conversation>{earlier}\n</earlier_conversation>\n" if earlier else "") + f"\n{prompt}"
            state["prompting"] = True
            _, slot = rpc.request("session/prompt", {"sessionId": rpc.session_id, "prompt": [{"type": "text", "text": text}]})
            if not rpc.pump(notification, request, lambda: slot[0].is_set()) and not slot[0].is_set():
                error = state["last_error"] or rpc.last_words() or f"{self.agent.command[0]} ended the turn early"
            else:
                if (result_of(slot[1] or {}, "session/prompt").get("stopReason") or "") == "cancelled":
                    state["cancelled"] = True
        except AgentError as e:
            error = str(e)
        except KeyboardInterrupt:
            state["cancelled"] = True
        finally:
            kept = keep and not error and not state["cancelled"] and not self._killed and not rpc.closed and rpc.proc.poll() is None
            with self._lock:
                self._turning = False
                if kept and self._acp is rpc:
                    self._acp_key = (tuple(command), self.workdir, rpc.session_id)  # the session it is on now
                    self._start_idle_timer(rpc)
                else:
                    kept = False
                    if self._acp is rpc:
                        self._acp = None
            if not kept:
                rpc.close()
                rpc.kill()
            for call in tools.values():
                if not call.done:
                    call.done = call.is_error = True
                    tool_changed(call)

        turn.text = reply.finish()
        cancelled = state["cancelled"] or self._killed
        if cancelled and refused and not self._killed:
            what, why = refused[-1]
            rule = f"it matches your {why} rule" if why.startswith("Bash(") else why
            note = f"I didn't run `{what}`: {rule}."
            lead = "\n\n" if turn.text.strip() else ""
            turn.text = f"{turn.text.rstrip()}{lead}{note}"
            if on_text:
                on_text(lead + note)
            cancelled = False
        if cancelled:
            turn.is_error, turn.error = True, "cancelled"
        elif error:
            turn.is_error, turn.error = True, error
        if rpc.session_id and (not turn.is_error or cancelled):
            if fresh and requested:
                self._messages = list(self._messages)  # the old turns stay in Vision's transcript
            self.session_id = rpc.session_id
            self._messages += [{"role": "user", "content": prompt}] + ([{"role": "assistant", "content": turn.text}] if turn.text else [])
            self._save()
        if not turn.is_error:
            self.handoff = None
            self.model = self.resolved_model()
            self._model_seen_for = self.cfg.model
        return turn

    def steer(self, text: str) -> bool:
        return False  # plain ACP has no way into a running prompt: the caller queues it for the next turn

    def cancel(self) -> None:
        with self._lock:
            rpc, turning = self._acp, self._turning
        if not turning:
            self.close()  # nothing to cancel: a process kept between turns just goes
            return
        self._killed = True
        if rpc is not None:
            rpc.interrupt()

    # -- usage: nothing to read from an agent ----------------------------------------------
    @staticmethod
    def cached_usage() -> dict | None:
        return None

    def usage_report(self) -> str | None:
        return None

    def ping_usage(self) -> dict | None:
        return None

    def usage_renderable(self, full: bool = False):
        from rich.text import Text

        return Text(f"{self.provider} is an ACP agent; its own CLI shows its usage.", style="dim")


def new_session_id() -> str:
    return str(uuid.uuid4())
