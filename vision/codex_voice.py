"""The voice conversation model on Codex: same contract as ClaudeConversation.

One `codex app-server` stays up between turns (as Claude's CLI does) on one ephemeral thread, so nothing
lands in ~/.codex/sessions or the /session list. Codex's own agent prompt is replaced (baseInstructions)
and every tool is switched off in config, except Codex's web search when [conversation].web is on. Each
reply is a turn with `outputSchema`, so it comes back as the same JSON the other voices write, and its
speech streams out of item/agentMessage/delta through SpeechStream.

Codex has no switch for its file-editing tool (apply_patch; openai/codex#8161, closed "not planned"):
the read-only sandbox with approvals off refuses any edit it tries.

Field names used by the Codex app-server: ThreadStartParams
baseInstructions, developerInstructions, ephemeral, config; TurnStartParams outputSchema, effort.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import time

# Everything but the model: no shell, no exec, no sub-agents, no patch format, no images, no plugins.
VOICE_CONFIG = {
    "features.shell_tool": False,
    "features.unified_exec": False,
    "features.multi_agent": False,
    "features.apply_patch_freeform": False,
    "features.plugins": False,
    "features.image_generation": False,
    "features.code_mode": False,
    "tools.view_image": False,
    "history.persistence": "none",
}


def strict_schema(node):
    """RESPONSE_SCHEMA in the form OpenAI's strict structured outputs accept (Codex sends outputSchema
    strict): every property required, no minLength. The optional fields already allow null, and
    validate_response takes them present as null, so the replies mean the same."""
    if isinstance(node, list):
        return [strict_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: strict_schema(v) for k, v in node.items() if k != "minLength"}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


class CodexConversation:
    """The voice conversation model on a Codex model (`[conversation].model` or the chat's /model)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self._rpc = None
        self._key = None  # the settings the running thread was started with
        self._cwd = None
        self._previous = None
        self._sent_chars = 0
        self._calls = 0
        self.usage = None
        self.timing = None

    def _prompt(self) -> str:
        from vision.conversation import conversation_prompt
        from vision.models import model_label

        conv = self.cfg.conversation
        prompt = conversation_prompt(self.cfg.brain.address_user_as, conv.web,
                                     front_end=getattr(self.cfg, "router", None) is not None and self.cfg.router.mode == "on")
        if conv.web:  # Codex has one web tool, not Claude's WebSearch and WebFetch
            prompt = prompt.replace("Your only tools are WebSearch and WebFetch", "Your only tool is web search")
        return (prompt + f"\nYou are {model_label(conv.model) or conv.model}, running through OpenAI Codex; the models "
                "listed above are the workers you can delegate to. Reply with the JSON object only, never a preamble "
                "or a note about what you are about to do.")

    def _settings(self) -> dict:
        conv = self.cfg.conversation
        config = {**VOICE_CONFIG, "web_search": "live" if conv.web else "disabled"}
        if conv.effort:
            config["model_reasoning_effort"] = conv.effort
        return {"model": conv.model, "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True,
                "baseInstructions": self._prompt(), "config": config}

    def _start(self, settings: dict) -> None:
        from vision.brain import brain_env
        from vision.codex import find_codex
        from vision.codex_app import AppServerTurn

        self._cwd = tempfile.TemporaryDirectory(prefix="vision-conversation-")
        try:
            rpc = AppServerTurn(find_codex(), self._cwd.name, brain_env("codex"), stall_s=0)
            self._rpc = rpc
            rpc.call("initialize", {"clientInfo": {"name": "vision", "title": "Vision", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": True}}, lambda *_: None)
            rpc.notify("initialized", {})
            thread = rpc.call("thread/start", {**settings, "cwd": self._cwd.name}, lambda *_: None).get("thread") or {}
            rpc.thread_id = thread.get("id")
            if not rpc.thread_id:
                raise RuntimeError("codex app-server started no thread")
        except BaseException:
            self.close()
            raise
        self._key = json.dumps(settings, sort_keys=True)

    def close(self) -> None:
        with self._lock:
            rpc, cwd = self._rpc, self._cwd
            self._rpc = self._cwd = self._key = self._previous = None
            self._sent_chars = self._calls = 0
        if rpc is not None:
            rpc.kill()
            rpc.close()
        if cwd is not None:
            cwd.cleanup()

    def scratch_dirs(self) -> list[str]:
        """The folder the live app-server thread runs in, every way it may be spelled (see in_workspace)."""
        from vision.conversation import scratch_spellings

        cwd = self._cwd
        return scratch_spellings(cwd.name) if cwd is not None else []

    def cancel(self) -> None:
        self.close()

    def warm_up(self) -> None:
        """Start the app-server and its thread during audio warm-up, without a model request."""
        with self._lock:
            settings = self._settings()
            if self._rpc is not None and (self._key != json.dumps(settings, sort_keys=True) or self._rpc.proc.poll() is not None):
                self.close()
            if self._rpc is None:
                self._start(settings)

    def complete(self, packet: dict, cancel: threading.Event, on_speech=None) -> dict:
        from vision.brain import BrainError
        from vision.codex_app import AppServerError, _snake
        from vision.conversation import HISTORY_CHARS, RESPONSE_SCHEMA, ClaudeConversation, SpeechStream

        with self._turn_lock:
            self.usage = None
            started = time.monotonic()
            try:
                with self._lock:
                    if cancel.is_set():
                        raise BrainError("cancelled")
                    settings = self._settings()
                    previous_history = (self._previous or {}).get("history", [])
                    history = packet.get("history", [])
                    pruned = bool(previous_history and (not history or history[0] != previous_history[0]))
                    if self._rpc is not None and (self._key != json.dumps(settings, sort_keys=True) or self._rpc.proc.poll() is not None
                                                  or pruned or self._sent_chars > HISTORY_CHARS + 24_000 or self._calls >= 20):
                        self.close()
                    cold = self._rpc is None
                    if cold:
                        self._start(settings)
                    rpc = self._rpc
                    payload = json.dumps(ClaudeConversation._payload(self, packet), ensure_ascii=False)
                parsers: dict[str, SpeechStream] = {}  # one per message: a stray preamble never feeds the reply's parser
                state = {"done": False, "status": "", "error": "", "messages": [], "spoken": False}

                def notification(method: str, params: dict) -> None:
                    if method == "item/agentMessage/delta":
                        item_id, delta = params.get("itemId") or "", params.get("delta") or ""
                        if delta and on_speech is not None and not cancel.is_set():
                            piece = parsers.setdefault(item_id, SpeechStream()).feed(delta)
                            if piece:
                                if self.timing and not state["spoken"]:
                                    state["spoken"] = True
                                    self.timing.event("speech_stream")
                                on_speech(piece)
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage" and item.get("text"):
                            state["messages"].append(item["text"])
                    elif method == "thread/tokenUsage/updated":
                        last = (params.get("tokenUsage") or {}).get("last")
                        if isinstance(last, dict):
                            self.usage = _snake(last)
                    elif method == "error" and not params.get("willRetry"):
                        err = params.get("error") or {}
                        state["error"] = (err.get("message") if isinstance(err, dict) else str(err)) or state["error"]
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        if not rpc.turn_id or turn.get("id") in (None, rpc.turn_id):
                            state["done"] = True
                            state["status"] = turn.get("status") or "completed"
                            err = turn.get("error") or {}
                            state["error"] = (err.get("message") if isinstance(err, dict) else str(err or "")) or state["error"]

                turn_params = {"threadId": rpc.thread_id, "input": [{"type": "text", "text": payload, "text_elements": []}],
                               "outputSchema": strict_schema(RESPONSE_SCHEMA)}
                if self.cfg.conversation.effort:
                    turn_params["effort"] = self.cfg.conversation.effort
                if self.timing:
                    self.timing.event("conversation_sent", cold=cold)
                rpc.turn_id = (rpc.call("turn/start", turn_params, notification).get("turn") or {}).get("id")
                timeout = self.cfg.conversation.timeout_s

                def finished() -> bool:
                    return state["done"] or cancel.is_set() or self._rpc is not rpc or time.monotonic() - started > timeout

                ended = rpc.pump(notification, finished)
                if cancel.is_set() or self._rpc is not rpc:
                    raise BrainError("cancelled")
                if not state["done"]:
                    if ended:
                        raise BrainError("The voice conversation timed out.")
                    raise BrainError(rpc._last_words() or "Codex voice connection closed before its result.")
                if state["status"] != "completed":
                    raise BrainError(state["error"] or f"Codex voice turn {state['status']}.")
                data = None
                for text in reversed(state["messages"]):  # the reply is the last message that parses
                    try:
                        data = json.loads(text)
                        break
                    except ValueError:
                        continue
                if data is None:
                    raise BrainError("Codex returned an invalid voice response.")
                with self._lock:
                    if cancel.is_set() or self._rpc is not rpc:
                        raise BrainError("cancelled")
                    self._previous = copy.deepcopy(packet)
                    self._sent_chars += len(payload) + len(json.dumps(data))
                    self._calls += 1
                return data
            except AppServerError as e:
                self.close()
                raise BrainError("cancelled" if cancel.is_set() else str(e)) from e
            except BaseException:
                self.close()
                raise
