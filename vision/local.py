"""Local brain: a model on an OpenAI-compatible server (llama.cpp's llama-server, Ollama, LM Studio, vLLM),
spoken to directly over HTTP.

No CLI, nothing leaves the network and no subscription is spent. Two drivers share the transport:
LocalBrain is the typed brain (`/model <the server's model>`) and the voice conversation's task worker,
LocalConversation the voice conversation model (the chat's own model, when that is a local one), each with the same
interface as its Claude counterpart so the rest of Vision does not care. The brain has tools (Bash, Read,
Write, Edit) that Vision runs itself, see `vision.localtools`; the conversation model stays tool-free and
delegates, as the Claude one does.

Verified against llama.cpp's llama-server (b11064):
- `/v1/chat/completions` streams OpenAI-style SSE; reasoning arrives as `reasoning_content` deltas.
- Thinking is a per-request switch, `chat_template_kwargs: {"enable_thinking": bool}`, for models whose chat
  template has one; effort "high" turns it on, "off" (the default) leaves it off. Others ignore it.
  There is no per-request budget: `reasoning_budget` in the body is ignored, only the server flag counts.
- `response_format: {"type": "json_schema", ...}` compiles the schema to a grammar, so the voice
  packet and the worker result cannot come back malformed; thinking must be off for that.
- `tools` (OpenAI function calling) streams `tool_calls` deltas by index and ends with finish_reason
  `tool_calls`; a `tool` message per call carries the result back. Tools and a `response_format` grammar
  cannot share one request (400 "failed to parse grammar"), so a worker turn runs its tool loop first and
  asks for the result object in a final, grammar-constrained call.
- The server keeps its own prompt cache; resending the whole conversation each turn is cheap.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text

from vision import localtools
from vision import usage as usage_ui
from vision.config import STATE_DIR, BrainConfig, weather_ready
from vision.persona import system_prompt
from vision.reply import READING, THINKING, ReplyText, dedupe_status

if TYPE_CHECKING:
    from vision.brain import Turn

SESSIONS_DIR = STATE_DIR / "local-sessions"  # <id>.json: {"model", "at", "messages": [...]}
LAST_SESSION_FILE = STATE_DIR / "last_session.local"  # JSON {"id": session_id, "model": slug, "at": ts}
CHARS_PER_TOKEN = 4  # rough; used to keep the transcript inside the server's context
MAX_TOOL_ROUNDS = 8  # tool calls per turn before the model is made to answer with what it has


class LocalError(RuntimeError):
    pass


def _model_name(alias: str, provider: str = "local") -> str:
    """What the server is asked for: the model id without a config-defined provider's prefix."""
    from vision.models import endpoint_model, provider_default

    return endpoint_model(alias or provider_default(provider) or "local-model")


def _endpoint(provider: str, cfg):
    from vision.providers import Endpoint, endpoint_for

    return endpoint_for(provider, cfg) or Endpoint()


class _Stream:
    """One streaming chat completion. `close()` from another thread cancels it."""

    def __init__(self, base_url: str, body: dict, timeout: float, headers: dict | None = None):
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream", **(headers or {})},
        )
        try:
            self.resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise LocalError(f"llama-server returned {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LocalError(f"llama-server at {base_url} is unreachable ({getattr(e, 'reason', e)}). Is the server running?") from e

    def __iter__(self):
        for raw in self.resp:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                yield json.loads(line[6:])
            except ValueError:
                continue

    def close(self) -> None:
        try:
            self.resp.close()
        except Exception:
            pass


def stream_chat(base_url: str, body: dict, timeout: float, on_delta: Callable[[dict], None], holder: dict,
                headers: dict | None = None) -> dict:
    """Run one streamed completion; `holder["stream"]` is set so a cancel can close it. Returns
    {"text", "reasoning", "tool_calls", "usage", "timings", "finish"}; tool calls are assembled from
    their indexed deltas into OpenAI message form ({"id", "type", "function": {"name", "arguments"}})."""
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    out = {"text": "", "reasoning": "", "tool_calls": [], "usage": None, "timings": None, "finish": None}
    calls: dict[int, dict] = {}
    stream = _Stream(base_url, body, timeout, headers)
    holder["stream"] = stream
    try:
        for ev in stream:
            if ev.get("usage"):
                out["usage"] = ev["usage"]
            if ev.get("timings"):
                out["timings"] = ev["timings"]
            for choice in ev.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    out["reasoning"] += delta["reasoning_content"]
                if delta.get("content"):
                    out["text"] += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    call = calls.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        call["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        call["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        call["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    out["finish"] = choice["finish_reason"]
                on_delta(delta)
    finally:
        holder.pop("stream", None)
        stream.close()
    out["tool_calls"] = [calls[i] for i in sorted(calls)]
    for i, call in enumerate(out["tool_calls"]):
        call["id"] = call["id"] or f"call_{uuid.uuid4().hex[:12]}_{i}"
    return out


def _size(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") + (len(json.dumps(m["tool_calls"])) if m.get("tool_calls") else 0) for m in messages)


def _trim(messages: list[dict], budget_chars: int) -> list[dict]:
    """Drop the oldest exchanges (never the system prompt) until the transcript fits. An exchange runs
    from one user message to the next, so a turn's tool calls and results go with it."""
    system, rest = messages[:1], messages[1:]
    while _size(system + rest) > budget_chars:
        nxt = next((i for i, m in enumerate(rest) if i > 0 and m.get("role") == "user"), None)
        if nxt is None:
            break
        rest = rest[nxt:]
    return system + rest


def _compact(messages: list[dict]) -> list[dict]:
    """The turn's messages as they go into the transcript: tool results shrunk, so a long `cat` does not
    sit in the context for the rest of the session."""
    out = []
    for m in messages:
        if m.get("role") == "tool" and len(m.get("content") or "") > localtools.HISTORY_OUTPUT:
            m = {**m, "content": localtools.clip(m["content"], localtools.HISTORY_OUTPUT)}
        out.append(m)
    return out


def _result_json(text: str) -> dict | None:
    """A worker's result object if the text already is one (fences tolerated), else None."""
    from vision.delegation import RESULT_SCHEMA, _validate

    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return _validate(json.loads(body), RESULT_SCHEMA)
    except (ValueError, TypeError):
        return None


class LocalBrain:
    """Same interface as the Claude brain so the CLI does not care which one is thinking."""

    def __init__(self, cfg: BrainConfig, voice_mode: bool = False, session_id: str | None = None):
        import os

        from vision.models import provider_for

        self.cfg = cfg
        self.provider = provider_for(cfg.model) if cfg.model else "local"  # "local", or a [providers.<name>] server
        self._ep = _endpoint(self.provider, cfg)
        self.voice_mode = voice_mode
        self.task_mode = False
        self.allowed_once: list[str] = []  # calls the user approved once (toolrules.gate), a scout's approvals
        self.on_allowed_used = None  # told when one of them runs
        self.on_approval = None  # (tool, input, rule) → an approval id: a call that asks first files one instead of asking now
        self.session_id = session_id
        self.workdir = os.path.abspath(os.path.expanduser(cfg.workdir)) if cfg.workdir else os.getcwd()
        self.last_usage: dict | None = None
        self.model: str | None = None
        self._model_seen_for: str | None = None
        self.handoff: str | None = None
        self.output_tokens = 0
        self.context: tuple[int, int] | None = None
        self._messages: list[dict] = []  # the transcript after the system prompt; loaded on resume
        self._holder: dict = {}
        self._lock = threading.Lock()
        self._running = False  # a turn is on and can still take a steered message
        self._steers: list[str] = []  # messages sent into the running turn, waiting for its next step
        self.on_steer_taken = None  # told each one as the model takes it in (where the reply splits), as Brain's
        if session_id:
            self._messages = self._load(session_id)

    # -- session helpers -------------------------------------------------
    @staticmethod
    def _read_last() -> dict:
        try:
            return json.loads(LAST_SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def last_session_id() -> str | None:
        return LocalBrain._read_last().get("id") or None

    @staticmethod
    def last_session_model() -> str | None:
        return LocalBrain._read_last().get("model") or None

    @staticmethod
    def _load(session_id: str) -> list[dict]:
        try:
            data = json.loads((SESSIONS_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
            return [m for m in data.get("messages", []) if isinstance(m, dict)]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        if self.task_mode or not self.session_id:
            return
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (SESSIONS_DIR / f"{self.session_id}.json").write_text(
            json.dumps({"model": self.cfg.model, "at": time.time(), "messages": self._messages}, ensure_ascii=False), encoding="utf-8")
        LAST_SESSION_FILE.write_text(json.dumps({"id": self.session_id, "model": self.cfg.model, "at": time.time()}), encoding="utf-8")

    def new_session(self) -> None:
        self.session_id = None
        self._messages = []
        self.last_usage = None
        self.handoff = None
        self.context = None

    def resume(self, session_id: str) -> None:
        self.session_id = session_id
        self._messages = self._load(session_id)
        self.last_usage = None
        self.handoff = None
        self.context = None
        self._save()

    def resolved_model(self) -> str | None:
        return self.model or _model_name(self.cfg.model, self.provider)

    def context_window(self) -> int:
        return int(self._ep.context)

    def _thinking(self) -> bool:
        return self.cfg.effort == "high"

    def tools(self) -> list[str]:
        """The tool names offered this turn (none in task mode's final call, none in plan mode but Read)."""
        return localtools.available(self.cfg.allowed_tools, self.cfg.mode)

    def _system(self) -> str:
        if self.task_mode:
            from vision.delegation import worker_prompt

            return worker_prompt(self.cfg, self.workdir, self.provider)
        from vision.folderprompt import for_brain

        return system_prompt(self.voice_mode, self.cfg.address_user_as, self.workdir, self.tools(),
                             provider=self.provider, mode=self.cfg.mode, denied_tools=self.cfg.denied_tools,
                             weather=weather_ready(self.cfg), project=getattr(self, "project_prompt", ""),
                             folder=for_brain(self.workdir, self.cfg), ask_tools=getattr(self.cfg, "ask_tools", None))

    # -- main entry point -------------------------------------------------
    def ask(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_question: Callable[[list[dict]], dict[str, str] | None] | None = None,  # Claude-only
        on_agent: Callable | None = None,  # Claude-only
        on_tool: Callable | None = None,
    ) -> Turn:
        """One user turn: a request, then as many tool rounds as the model asks for (up to
        MAX_TOOL_ROUNDS), then the reply. Tool calls stream to `on_tool` as ToolCall rows like the
        Claude brain's; a worker turn ends with a grammar-constrained call for the result object.
        Messages steered in meanwhile join at the next step and are answered in the same turn."""
        from vision import clis

        clis.refresh_models_soon((self.provider,), max_age=clis.SERVER_MODELS_RECHECK)  # a model swapped on the server
        with self._lock:
            self._running, self._steers = True, []
        try:
            return self._turn(prompt, on_text, on_status, on_tool, on_question)
        finally:
            with self._lock:
                self._running = False

    def _declined(self, name: str, arguments, on_question, allowed: set[str]) -> str | None:
        """[brain].ask_tools for a brain whose tools Vision runs: a matching call waits on the user's
        Yes / No (the same form Claude's ask rules show) and the model sees an error when they decline.
        None = run it. A worker has nobody to ask, so its call is refused with a way back."""
        from vision.toolrules import YES_FOR_TURN, ask_question, declined_message, first_match, gate

        if not getattr(self.cfg, "ask_tools", None) or self.cfg.mode == "plan":  # plan mode has no writing tools to ask about
            return None
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except ValueError:
            return None  # localtools.run reports the bad arguments itself
        rule = first_match(getattr(self.cfg, "ask_tools", None), name, args, self.workdir)
        if rule is None or rule in allowed:
            return None
        settled = gate(self, name, args, rule)  # a scout's approvals (projects.py)
        if settled is not None:
            return None if settled[0] else "Error: " + settled[1]
        if self.task_mode:
            return (f"Error: {name} needs the user's approval ({rule}), which a worker cannot ask for. Return status "
                    "needs_input saying exactly what you would run and why.")
        if not on_question:
            return f"Error: {name} needs the user's approval ({rule}) and nobody is here to give it. Carry on without it."
        question = ask_question(name, args, rule)
        choice = (on_question([question]) or {}).get(question["question"], "")
        if choice.startswith("Yes"):
            if choice == YES_FOR_TURN:
                allowed.add(rule)
            return None
        return "Error: " + declined_message(name, args)

    def steer(self, text: str) -> bool:
        """A message typed mid-reply (Ctrl-X / Send now), as Claude takes one: it goes in after the
        tool calls in flight, or once the text streaming now ends, and the model answers it in this
        turn. False when no turn can take it (none running, a worker, or already over): it queues."""
        with self._lock:
            if not self._running or self.task_mode:
                return False
            self._steers.append(text)
        return True

    def _take_steers(self, last: bool = False) -> list[dict]:
        """The steered messages as user messages; `last`: the model has answered, so with none
        waiting the turn stops taking them (a later one queues instead of being lost)."""
        with self._lock:
            texts, self._steers = self._steers, []
            if last and not texts:
                self._running = False
        if self.on_steer_taken:
            for t in texts:
                self.on_steer_taken(t)
        return [{"role": "user", "content": t} for t in texts]

    def take_untaken_steers(self) -> list[str]:
        """Steered messages the turn ended without taking in (an error, Esc): the caller queues them."""
        with self._lock:
            left, self._steers = self._steers, []
        return left

    def _turn(self, prompt: str, on_text, on_status, on_tool, on_question=None) -> Turn:
        from vision.brain import ToolCall, Turn, inject_handoff, tool_detail, tool_input

        turn = Turn(session_id=self.session_id, model=_model_name(self.cfg.model))
        prompt = inject_handoff(self, prompt)
        reply = ReplyText(on_text)
        on_status = dedupe_status(on_status)
        allowed: set[str] = set()  # ask rules the user waved through for the rest of this turn
        self.output_tokens = 0
        thinking = self._thinking() and not self.task_mode  # a grammar and a think block do not mix
        messages = _trim([{"role": "system", "content": self._system()}, *self._messages,
                          {"role": "user", "content": prompt}],
                         int(self._ep.context * CHARS_PER_TOKEN * 0.8))
        specs = localtools.specs(self.tools())
        live: dict[int, ToolCall] = {}  # this request's calls by delta index, as rows in the reply
        offered = {"tools": False}  # whether the request in flight carries tool specs

        def tool_changed(call: ToolCall) -> None:
            if on_tool:
                on_tool(call)

        def on_delta(delta: dict) -> None:
            if delta.get("reasoning_content") and on_status:
                on_status(THINKING)
            if delta.get("content"):
                self.output_tokens += max(1, len(delta["content"]) // CHARS_PER_TOKEN)
                reply.add(delta["content"])
            for tc in (delta.get("tool_calls") or []) if offered["tools"] else []:
                name = (tc.get("function") or {}).get("name")
                idx = int(tc.get("index") or 0)
                if name and idx not in live:  # the first delta of a call carries its name
                    reply.tool()
                    turn.tools_used.append(name)
                    call = live[idx] = ToolCall(tc.get("id") or f"local-{len(turn.tools)}", name)
                    turn.tools.append(call)
                    tool_changed(call)
                    if on_status:
                        on_status(name)

        def fail(error: str) -> Turn:
            turn.text = reply.finish()
            turn.is_error = True
            turn.error = "cancelled" if self._holder.get("cancelled") else error
            for call in live.values():
                if not call.done:
                    call.done, call.is_error, call.output = True, True, turn.error
                    tool_changed(call)
            return turn

        started = time.monotonic()
        with self._lock:
            self._holder["cancelled"] = False
        rounds = 0
        usage_total: dict = {}
        while True:
            live.clear()
            body = {
                "model": _model_name(self.cfg.model),
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": thinking and not (self.task_mode and not specs)},
            }
            offered["tools"] = bool(specs and rounds < MAX_TOOL_ROUNDS)
            if offered["tools"]:
                body["tools"] = specs
                if self.task_mode and rounds == 0:
                    # A worker exists to look and act; left to itself the model writes a plausible result
                    # from imagination instead. Its first move must be a tool call.
                    body["tool_choice"] = "required"
            elif self.task_mode:
                from vision.delegation import RESULT_SCHEMA

                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": RESULT_SCHEMA}}
            try:
                result = stream_chat(self._ep.base_url, body, self._ep.timeout_s, on_delta, self._holder, self._ep.headers())
            except LocalError as e:
                return fail(str(e))
            except (OSError, ValueError) as e:  # the stream broke mid-reply, or cancel() closed it
                return fail(f"llama-server stream failed: {e}")
            if self._holder.get("cancelled"):
                return fail("cancelled")
            if result["usage"]:
                for k, v in result["usage"].items():
                    if isinstance(v, (int, float)):
                        usage_total[k] = usage_total.get(k, 0) + v
                    else:
                        usage_total.setdefault(k, v)
                usage_total["total_tokens"] = int(result["usage"].get("total_tokens") or 0)  # the context now, not a sum
                self.context = (usage_total["total_tokens"], self.context_window())
            calls = result["tool_calls"] if "tools" in body else []  # no tools offered: whatever came is the answer
            if not calls:
                if self.task_mode and "tools" in body:
                    # Phase two of a worker turn: the model stopped calling tools. Its text is the result
                    # if it already validates; otherwise one more call, tools off, grammar on.
                    data = _result_json(result["text"])
                    if data is not None:
                        turn.data = data
                        break
                    messages = messages + [{"role": "assistant", "content": result["text"]},
                                           {"role": "user", "content": "Return the result object for the task now."}]
                    specs = []
                    continue
                steers = [] if self.task_mode else self._take_steers(last=True)
                if steers:  # sent in while this reply streamed: it carries on to answer them
                    reply.finish()
                    reply.tool()  # its answer starts a new paragraph
                    messages = messages + [{"role": "assistant", "content": result["text"]}] + steers
                    continue
                break
            rounds += 1
            messages = messages + [{"role": "assistant", "content": result["text"] or None, "tool_calls": calls}]
            for i, tc in enumerate(calls):
                name, arguments = tc["function"]["name"], tc["function"]["arguments"]
                call = live.get(i)
                if call is None:  # a call whose first delta carried no name: give it a row now
                    call = live[i] = ToolCall(tc["id"], name)
                    turn.tools.append(call)
                    turn.tools_used.append(name)
                call.id = tc["id"]
                try:
                    call.detail = tool_detail(name, json.loads(arguments))
                    call.input = tool_input(name, json.loads(arguments))
                except ValueError:
                    call.detail = arguments[:60]
                tool_changed(call)
                if self._holder.get("cancelled"):
                    return fail("cancelled")
                if name not in self.tools():
                    output, is_error = f"Error: {name} is not available in this session.", True
                elif (declined := self._declined(name, arguments, on_question, allowed)) is not None:
                    output, is_error = declined, True
                else:
                    output, is_error = localtools.run(name, arguments, self.workdir, self.cfg.denied_tools, self._holder, self._lock,
                                                      brave_key=getattr(self.cfg.local, "brave_api_key", ""))
                if self._holder.get("cancelled"):
                    return fail("cancelled")
                call.output, call.is_error, call.done = output, is_error, True
                tool_changed(call)
                messages = messages + [{"role": "tool", "tool_call_id": tc["id"], "content": output}]
            messages = messages + self._take_steers()  # the next step reads them after the results
            if on_status:
                on_status(READING)

        turn.text = reply.finish()
        turn.duration_ms = int((time.monotonic() - started) * 1000)
        if usage_total:
            turn.usage = usage_total
            self.output_tokens = int(usage_total.get("completion_tokens") or self.output_tokens)
        if self.task_mode and turn.data is None:
            try:
                turn.data = json.loads(result["text"])
            except ValueError:
                turn.data = None
        if not self.session_id:
            self.session_id = uuid.uuid4().hex
        self._messages = _compact(messages[1:] + [{"role": "assistant", "content": result["text"]}])
        turn.session_id = self.session_id
        self._save()
        self.handoff = None
        self.model = _model_name(self.cfg.model, self.provider)
        self._model_seen_for = self.cfg.model
        self.last_usage = {"provider": self.provider, "model": self.model, "usage": turn.usage,
                           "timings": result["timings"], "at": time.time()}
        return turn

    # -- usage --------------------------------------------------------------
    @staticmethod
    def cached_usage() -> dict | None:
        return None

    def usage_report(self) -> str | None:
        return None

    def ping_usage(self) -> dict | None:
        return self.last_usage

    def usage_renderable(self, full: bool = False):
        """No pool to show: the model is the user's own. Report what the server is running and the last turn's speed."""
        from vision.models import provider_label

        base = self._ep.base_url
        try:  # llama-server says what it is running; another server just gets the model we ask for
            with urllib.request.urlopen(urllib.request.Request(base.rstrip("/").removesuffix("/v1") + "/props", headers=self._ep.headers()), timeout=3) as r:
                props = json.load(r)
        except Exception:
            props = {}
            if self.provider == "local":
                return Text(f"Local model unavailable: llama-server at {base} did not answer.", style="dim")
        model = str((props.get("model_alias") or props.get("model_path") or self.resolved_model() or "?")).rsplit("/", 1)[-1]
        ctx = props.get("default_generation_settings", {}).get("n_ctx") or self.context_window()
        t = usage_ui.usage_table(provider_label(self.provider), model)
        usage_ui.add_window(t, "Self-hosted", 0.0, "never: your own hardware, no limits")
        parts = [t, Text(f"{base} · context {ctx:,} tokens", style="dim")]
        timings = (self.last_usage or {}).get("timings") or {}
        if timings.get("predicted_per_second"):
            parts.append(Text(f"last turn: {timings['predicted_per_second']:.0f} tok/s, "
                              f"prompt read in {timings.get('prompt_ms', 0) / 1000:.1f}s", style="dim"))
        return usage_ui.usage_group(*parts)

    def cancel(self) -> None:
        with self._lock:
            self._holder["cancelled"] = True
            stream = self._holder.get("stream")
            proc = self._holder.get("proc")
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        if stream is not None:
            stream.close()


class LocalConversation:
    """The voice conversation model on the local server: same contract as ClaudeConversation.

    The packet protocol is unchanged (the first input carries everything, later ones are incremental)
    because the transcript is kept here and resent each turn; the server's prompt cache makes that
    cheap. Speech streams out of the JSON reply as it is written, through the same SpeechStream."""

    def __init__(self, cfg):
        from vision.models import provider_for

        self.cfg = cfg
        self.provider = provider_for(cfg.conversation.model) if cfg.conversation.model else "local"
        self._ep = _endpoint(self.provider, cfg)
        self._lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self.usage = None
        self.timing = None
        self._messages: list[dict] = []
        self._previous = None
        self._sent_chars = 0
        self._calls = 0
        self._holder: dict = {}

    def _system(self) -> str:
        from vision.conversation import conversation_prompt

        from vision.models import model_label

        model = self.cfg.conversation.model
        return (conversation_prompt(self.cfg.brain.address_user_as, web=False,  # no web tools on the local server
                                    front_end=getattr(self.cfg, "router", None) is not None and self.cfg.router.mode == "on")
                + f"\nYou are {model_label(model) or model}, an open model running on the user's own hardware; "
                "the models listed above are the workers you can delegate to, not what you are. If asked what "
                "model or brain is answering, say so; the worker's model only handles delegated tasks.")

    def warm_up(self) -> None:
        try:  # /models is every OpenAI-style server's cheapest answer (llama-server, Ollama, a hosted API)
            urllib.request.urlopen(urllib.request.Request(self._ep.base_url.rstrip("/") + "/models", headers=self._ep.headers()), timeout=3).read()
        except Exception as e:
            raise LocalError(f"the model server at {self._ep.base_url} is not answering ({e}).") from e

    def close(self) -> None:
        with self._lock:
            self._messages = []
            self._previous = None
            self._sent_chars = self._calls = 0
            stream = self._holder.get("stream")
        if stream is not None:
            stream.close()

    def cancel(self) -> None:
        self.close()

    def complete(self, packet: dict, cancel: threading.Event, on_speech=None) -> dict:
        from vision.brain import BrainError
        from vision import clis
        from vision.conversation import HISTORY_CHARS, RESPONSE_SCHEMA, ClaudeConversation, SpeechStream

        clis.refresh_models_soon((self.provider,), max_age=clis.SERVER_MODELS_RECHECK)  # a model swapped on the server
        with self._turn_lock:
            self.usage = None
            with self._lock:
                if cancel.is_set():
                    raise BrainError("cancelled")
                previous_history = (self._previous or {}).get("history", [])
                history = packet.get("history", [])
                pruned = bool(previous_history and (not history or history[0] != previous_history[0]))
                if pruned or self._sent_chars > HISTORY_CHARS + 24_000 or self._calls >= 20:
                    self.close()
                cold = not self._messages
                payload = json.dumps(ClaudeConversation._payload(self, packet), ensure_ascii=False)
                messages = [{"role": "system", "content": self._system()}, *self._messages,
                            {"role": "user", "content": payload}]
            if self.timing:
                self.timing.event("conversation_sent", cold=cold)
            parser = SpeechStream()
            spoken = {"any": False}

            def on_delta(delta: dict) -> None:
                if not delta.get("content") or on_speech is None or cancel.is_set():
                    return
                piece = parser.feed(delta["content"])
                if piece:
                    if self.timing and not spoken["any"]:
                        spoken["any"] = True
                        self.timing.event("speech_stream")
                    on_speech(piece)

            body = {
                "model": _model_name(self.cfg.conversation.model, self.provider),
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema", "json_schema": {"name": "response", "schema": RESPONSE_SCHEMA}},
            }
            try:
                result = stream_chat(self._ep.base_url, body, self.cfg.conversation.timeout_s, on_delta, self._holder, self._ep.headers())
            except LocalError as e:
                self.close()
                raise BrainError("cancelled" if cancel.is_set() else str(e)) from e
            except (OSError, ValueError) as e:
                self.close()
                raise BrainError("cancelled" if cancel.is_set() else f"llama-server stream failed: {e}") from e
            if cancel.is_set():
                self.close()
                raise BrainError("cancelled")
            self.usage = result["usage"]
            try:
                data = json.loads(result["text"])
            except ValueError as e:
                self.close()
                raise BrainError("The local model returned an invalid voice response.") from e
            with self._lock:
                self._messages = messages[1:] + [{"role": "assistant", "content": result["text"]}]
                self._previous = json.loads(json.dumps(packet))
                self._sent_chars += len(payload) + len(result["text"])
                self._calls += 1
            return data
