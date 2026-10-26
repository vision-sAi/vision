"""Codex brain: drives OpenAI's GPT models through the Codex CLI in headless mode.

Turns run through `codex app-server` (vision/codex_app.py: messages into a running turn, streamed
text, live usage and tool rows) unless `[codex].transport = "exec"`, which uses `codex exec --json`
as documented for scripting; both on your normal Codex (ChatGPT) login and subscription and Codex's
own thread store. The notes below are the exec path's.

Verified against codex-cli 0.154.0:
- JSONL events: thread.started{thread_id}, turn.started, item.started/item.completed{item},
  turn.completed{usage}, turn.failed{error}, top-level error{message} (transient retries).
- No text streaming: the reply arrives whole, per agent_message item (a turn may have several).
- Prompts are written to stdin and then closed; Codex waits for EOF before starting the turn.
- A non-UUID resume id silently starts a new thread, so the id in thread.started is checked.
- Subscription windows are not on stdout; they are in the thread's rollout file under ~/.codex/sessions.
"""
from __future__ import annotations

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text

from vision import usage as usage_ui
from vision.config import STATE_DIR, BrainConfig, weather_ready
from vision.persona import system_prompt
from vision.reply import READING, THINKING, WRITING, ReplyText, dedupe_status, retry_label

if TYPE_CHECKING:
    from vision.brain import Turn

LAST_SESSION_FILE = STATE_DIR / "last_session.codex"  # JSON {"id": thread_id, "model": slug, "at": ts}
USAGE_FILE = STATE_DIR / "usage.codex.json"
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
CODEX_SESSIONS = os.path.join(CODEX_HOME, "sessions")
APP_SERVER_TIMEOUT = 15.0  # seconds to wait for `codex app-server` to answer account/rateLimits/read


class CodexError(RuntimeError):
    pass


def find_codex() -> str:
    exe = shutil.which("codex")
    if exe:
        return exe
    for cand in (os.path.expanduser("~/.local/bin/codex"), "/usr/local/bin/codex", "/usr/bin/codex"):
        if os.path.exists(cand):
            return cand
    raise CodexError("Codex CLI ('codex') not found on PATH. Install it (npm i -g @openai/codex) and run `codex login` once.")


def toml_str(s: str) -> str:
    """A JSON string literal is a valid TOML basic string, which is what `-c key=value` wants."""
    return json.dumps(s)


def sandbox_for(cfg: BrainConfig) -> str:
    """Codex has no per-tool allow list; the sandbox stands in for it.

    Plan mode → read-only. Auto mode → `[codex].sandbox` when one is named there, else
    danger-full-access (the Claude side of auto mode has every tool too).
    """
    if getattr(cfg, "mode", "auto") == "plan":
        return "read-only"
    chosen = (getattr(cfg, "codex", None) and cfg.codex.sandbox) or "auto"
    return chosen if chosen in SANDBOXES else "danger-full-access"


def _is_uuid(s: str) -> bool:
    parts = s.split("-")
    return len(s) == 36 and len(parts) == 5 and all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


def _snake(obj):
    """The app-server speaks camelCase; the rollout file (and everything Vision stored) is snake_case."""
    if isinstance(obj, dict):
        return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): _snake(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_snake(v) for v in obj]
    return obj


def app_server_call(exe: str, method: str, params: dict, timeout: float = APP_SERVER_TIMEOUT) -> dict | None:
    """One JSON-RPC request to a throwaway `codex app-server` (stdio): the whole reply ({"result"} or
    {"error"}), or None when the CLI is missing or slow to answer."""
    try:
        proc = subprocess.Popen(
            [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=dict(os.environ),
            encoding="utf-8",
        )
    except OSError:
        return None
    assert proc.stdin and proc.stdout
    deadline = time.monotonic() + timeout

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    # Lines come off a thread, not select(): a readline() can pull two replies into the text stream's
    # buffer, after which select() sees an empty pipe and the second reply would be lost.
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        for text in iter(proc.stdout.readline, ""):
            lines.put(text)
        lines.put("")

    threading.Thread(target=pump, daemon=True, name="codex-app-server").start()

    def wait_for(msg_id: int) -> dict | None:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                return None
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                return msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "vision", "title": "Vision", "version": "0.1.0"},
                         "capabilities": {"experimentalApi": True}}})
        if not wait_for(1):
            return None
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        reply = wait_for(2)
    except (OSError, ValueError):
        return None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
    return reply


def fetch_rate_limits(exe: str, timeout: float = APP_SERVER_TIMEOUT) -> dict | None:
    """Live subscription windows from `codex app-server` (JSON-RPC over stdio), no model call.

    Returns the same shape the rollout's token_count event carries (`primary`/`secondary` with
    used_percent, window_minutes, resets_at; plan_type) so one renderer serves both. None when the
    CLI is missing, not logged in, or slow to answer.
    """
    reply = app_server_call(exe, "account/rateLimits/read", {}, timeout)
    if not reply or not isinstance(reply.get("result"), dict):
        return None
    limits = reply["result"].get("rateLimits")
    if not isinstance(limits, dict):
        return None
    rl = _snake(limits)
    # Banked resets sit beside rateLimits in the reply, not inside it.
    if isinstance(reply["result"].get("rateLimitResetCredits"), dict):
        rl["reset_credits"] = _snake(reply["result"]["rateLimitResetCredits"])
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict) and "window_duration_mins" in w:
            w["window_minutes"] = w.pop("window_duration_mins")
    return rl


def consume_reset_credit(exe: str, key: str, credit_id: str | None = None, timeout: float = APP_SERVER_TIMEOUT) -> str:
    """Spend one banked reset: `credit_id`'s, or the backend's pick without one. `key` names this attempt, so a retry with
    the same key cannot spend a second one. The outcome: reset, nothingToReset, noCredit or
    alreadyRedeemed. Raises CodexError when the app-server refuses or never answers."""
    params = {"idempotencyKey": key, **({"creditId": credit_id} if credit_id else {})}
    reply = app_server_call(exe, "account/rateLimitResetCredit/consume", params, timeout)
    if not reply:
        raise CodexError("Codex didn't answer; try again in a minute")
    if isinstance(reply.get("error"), dict):
        raise CodexError(reply["error"].get("message") or "Codex refused the reset")
    outcome = (reply.get("result") or {}).get("outcome")
    if not isinstance(outcome, str):
        raise CodexError("Codex sent back an answer Vision can't read")
    return outcome


class CodexBrain:
    """Same interface as the Claude brain so the CLI does not care which one is thinking."""

    provider = "codex"

    def __init__(self, cfg: BrainConfig, voice_mode: bool = False, session_id: str | None = None):
        self.cfg = cfg
        self._codex_path: str | None = None
        try:
            self._codex_path = find_codex()
        except Exception:  # noqa: BLE001  not installed (yet): see the `codex` property
            pass
        self.voice_mode = voice_mode
        self.task_mode = False
        self.session_id = session_id
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.workdir = os.path.abspath(os.path.expanduser(cfg.workdir)) if cfg.workdir else os.getcwd()
        self.last_usage: dict | None = None
        self.model: str | None = None  # slug used on the last turn
        self._model_seen_for: str | None = None
        self.handoff: str | None = None  # previous provider's transcript, sent with the next fresh thread's first message
        # (tokens the model read on its last response, its window): the `% ctx` figure / phone gauge.
        self.context: tuple[int, int] | None = None
        self._rpc = None  # the running turn's app-server (vision.codex_app.AppServerTurn), for steer and cancel
        self._cancelled_app = False

    # -- session helpers -------------------------------------------------
    # The CLI's path: found when the brain is made if it is there, else looked up again when a turn
    # needs it, so a machine without it still gets a chat screen (and the start-up note saying how to
    # set it up) and the first message says what is missing, instead of a crash.
    @property
    def codex(self) -> str:
        if self._codex_path is None:
            self._codex_path = find_codex()
        return self._codex_path

    @staticmethod
    def _read_last() -> dict:
        try:
            return json.loads(LAST_SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def last_session_id() -> str | None:
        return CodexBrain._read_last().get("id") or None

    @staticmethod
    def last_session_model() -> str | None:
        return CodexBrain._read_last().get("model") or None

    def _remember_session(self) -> None:
        if self.task_mode:
            return
        if self.session_id:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LAST_SESSION_FILE.write_text(json.dumps({"id": self.session_id, "model": self.cfg.model, "at": time.time()}), encoding="utf-8")

    def new_session(self) -> None:
        self.session_id = None
        self.last_usage = None
        self.handoff = None
        self.context = None

    def resume(self, session_id: str) -> None:
        """Continue an earlier thread from the next turn on (and make it the `-c` target)."""
        self.session_id = session_id
        self.last_usage = None
        self.handoff = None
        self.context = None
        self._remember_session()

    def resolved_model(self) -> str | None:
        if self.model and self._model_seen_for == self.cfg.model:
            return self.model
        return self.cfg.model or None

    # -- main entry point -------------------------------------------------
    def _persona(self, sandbox: str) -> str:
        if self.task_mode:
            from vision.delegation import worker_prompt

            return worker_prompt(self.cfg, self.workdir, self.provider, sandbox)
        from vision.folderprompt import for_brain

        return system_prompt(
            self.voice_mode, self.cfg.address_user_as, self.workdir, self.cfg.allowed_tools,
            provider="codex", sandbox=sandbox, denied_tools=self.cfg.denied_tools, mode=self.cfg.mode,
            weather=weather_ready(self.cfg), project=getattr(self, "project_prompt", ""),
            folder=for_brain(self.workdir, self.cfg), ask_tools=getattr(self.cfg, "ask_tools", None),
        )

    def _transport(self) -> str:
        return getattr(getattr(self.cfg, "codex", None), "transport", "app-server") or "app-server"

    def app_server_settings(self, sandbox: str) -> dict:
        """thread/start (and thread/resume) settings: what the exec path passes as flags and `-c`."""
        from vision.codex_app import _config_overrides

        config = {"service_tier": "priority" if self.cfg.fast else "default"}
        if self.cfg.effort:
            config["model_reasoning_effort"] = self.cfg.effort
        if sandbox == "workspace-write":
            from vision.memory import MEMORY_DIR  # Vision's memory file lives outside the workdir

            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            config["sandbox_workspace_write.writable_roots"] = [str(MEMORY_DIR)]
        config.update(_config_overrides(getattr(getattr(self.cfg, "codex", None), "extra_config", []) or []))
        settings = {"cwd": self.workdir, "approvalPolicy": "never", "sandbox": sandbox,
                    "developerInstructions": self._persona(sandbox), "config": config}
        if self.cfg.model:
            settings["model"] = self.cfg.model
        return settings

    def steer(self, text: str) -> bool:
        """Send a message into the running turn (app-server only; exec can't take one): False and
        the caller queues it when nothing is running or Codex says the turn can't be steered."""
        with self._lock:
            rpc = self._rpc
        return bool(rpc and rpc.steer(text))

    def _command(self) -> list[str]:
        sandbox = sandbox_for(self.cfg)
        persona = self._persona(sandbox)
        common = ["--json", "--skip-git-repo-check"]
        if self.cfg.model:
            common += ["-m", self.cfg.model]
        if self.cfg.effort:
            common += ["-c", f"model_reasoning_effort={toml_str(self.cfg.effort)}"]
        # Codex's /fast command selects the priority service tier. Set default explicitly when off
        # so Vision's session state wins over a user-wide Codex preference without rewriting it.
        common += ["-c", f"service_tier={toml_str('priority' if self.cfg.fast else 'default')}"]
        common += ["-c", f"developer_instructions={toml_str(persona)}"]
        if sandbox == "workspace-write":
            # Vision's memory file lives outside the workdir; let the brain save to it.
            from vision.memory import MEMORY_DIR

            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            common += ["-c", f"sandbox_workspace_write.writable_roots=[{toml_str(str(MEMORY_DIR))}]"]
        for extra in getattr(getattr(self.cfg, "codex", None), "extra_config", []) or []:
            common += ["-c", extra]
        if self.session_id:
            # resume has no -s/-C; the sandbox goes through config and --all ignores the cwd filter.
            return [self.codex, "exec", "resume", self.session_id, *common, "--all", "-c", f"sandbox_mode={toml_str(sandbox)}"]
        return [self.codex, "exec", *common, "-s", sandbox, "-C", self.workdir]

    def ask(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_question: Callable[[list[dict]], dict[str, str] | None] | None = None,  # Claude-only (AskUserQuestion); codex exec has no equivalent
        on_agent: Callable | None = None,  # sub-agent rows, read from Codex's collab items (vision.subagents)
        on_tool: Callable | None = None,  # live tool rows (app-server only; exec items arrive whole, after the fact)
    ) -> Turn:
        try:
            self.codex  # not installed: the turn says so instead of raising, the same on every front end
        except Exception as e:  # noqa: BLE001  (that provider's own "not installed" message)
            from vision.brain import Turn

            turn = Turn(session_id=self.session_id, model=self.cfg.model or None)
            turn.is_error, turn.error = True, str(e)
            return turn
        if self._transport() != "exec":
            from vision.codex_app import run_turn

            return run_turn(self, prompt, on_text=on_text, on_status=on_status, on_agent=on_agent, on_tool=on_tool)
        from vision.brain import Turn, brain_env, inject_handoff

        env = brain_env("codex", getattr(self, "chat_id", ""))
        turn = Turn(session_id=self.session_id, model=self.cfg.model or None)
        prompt = inject_handoff(self, prompt)
        requested = self.session_id
        reply = ReplyText(on_text, paragraphs=True)
        on_status = dedupe_status(on_status)
        last_error = ""
        turn_id: str | None = None
        from vision.subagents import AgentTracker, codex_item

        subs = AgentTracker(turn, on_agent, model=self.cfg.model or "", effort=self.cfg.effort or "")

        with self._lock:
            self._proc = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.workdir,
                env=env,
                text=True,
                bufsize=1,
                encoding="utf-8",
            )
        proc = self._proc
        diagnostics: list[str] = []
        completed = False
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(prompt)
            proc.stdin.close()  # Codex waits for EOF before it starts the turn.
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics.append(line)
                    continue
                t = ev.get("type")
                if t == "thread.started":
                    tid = ev.get("thread_id")
                    if requested and tid and tid != requested:
                        # Codex could not find the thread and quietly started a new one.
                        last_error = "previous conversation not found; started a new one"
                    turn.session_id = tid or turn.session_id
                elif t == "turn.started":
                    turn_id = ev.get("turn_id") or turn_id
                elif t in ("item.started", "item.updated", "item.completed") and codex_item(subs, ev.get("item", {}), t == "item.completed"):
                    # A sub-agent: its own row (on_agent); the live line says an agent is at work.
                    if t == "item.started":
                        reply.tool()
                        turn.tools_used.append("Agent")
                        if on_status:
                            on_status("Agent")
                    elif t == "item.completed" and on_status:
                        on_status(READING)
                elif t == "item.started":
                    item = ev.get("item", {})
                    kind = item.get("type", "")
                    if kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "collab_agent_tool_call", "sub_agent_activity"):
                        label = {"command_execution": "Bash", "file_change": "Edit", "web_search": "WebSearch"}.get(kind, kind)
                        turn.tools_used.append(label)
                        reply.tool()
                        if on_status:
                            on_status(label)
                    elif kind == "reasoning" and on_status:
                        on_status(THINKING)  # a real reasoning item: the one time "thinking…" is the truth
                    elif kind == "agent_message" and on_status:
                        on_status(WRITING)  # Codex sends the message whole when it is done
                elif t == "item.completed":
                    item = ev.get("item", {})
                    if item.get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list", "collab_agent_tool_call", "sub_agent_activity"):
                        if on_status:
                            on_status(READING)  # tool done, the model has its result
                    elif item.get("type") == "agent_message" and item.get("text"):
                        reply.add(item["text"])
                    elif item.get("type") == "error" and item.get("message"):
                        last_error = item["message"]
                elif t == "error":
                    # A top-level error mid-turn is Codex retrying a dropped stream; the turn goes on.
                    last_error = ev.get("message") or last_error
                    if on_status and ev.get("message"):
                        on_status(retry_label(None, None, None, ev["message"].strip().splitlines()[0][:40]))
                elif t == "turn.completed":
                    self._record_usage(ev.get("usage"), turn, fresh_thread=not requested)
                    self._note_context(turn.session_id, turn_id)
                    completed = True
                    break
                elif t in ("turn.failed", "thread.failed"):
                    turn.is_error = True
                    error = ev.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    turn.error = message or last_error or "codex turn failed"
            if completed:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            else:
                proc.wait()
        except KeyboardInterrupt:
            self.cancel()
            turn.is_error = True
            turn.error = "cancelled"
            turn.text = reply.finish()
            turn.usage = turn.usage or self._cancelled_usage(turn, turn_id, fresh_thread=not requested)
            return turn
        finally:
            with self._lock:
                self._proc = None
            subs.close("cancelled" if turn.error == "cancelled" else "cut off when the turn ended")

        turn.text = reply.finish()
        if self.task_mode and reply.last is not None:
            try:
                turn.data = json.loads(reply.last)  # the final data reply, after any private commentary
            except ValueError:
                pass  # the delegation boundary reports an unconfirmed result
        diagnostic_text = "\n".join(diagnostics)
        if not completed and proc.returncode not in (0, None):
            turn.is_error = True
            if proc.returncode < 0:
                turn.error = "cancelled"
            else:
                err_lines = [ln for ln in diagnostics if ln and "stdin" not in ln.lower()]
                turn.error = turn.error or last_error or (err_lines[-1] if err_lines else f"codex exited with code {proc.returncode}")
            if proc.returncode < 0:
                turn.usage = turn.usage or self._cancelled_usage(turn, turn_id, fresh_thread=not requested)
            if "no rollout found" in (turn.error + diagnostic_text).lower() or "thread/resume" in diagnostic_text.lower():
                self.session_id = None  # stale thread id: next turn starts clean
                if not self.task_mode:
                    try:
                        LAST_SESSION_FILE.unlink()
                    except FileNotFoundError:
                        pass
        else:
            if turn.session_id and _is_uuid(turn.session_id):
                self.session_id = turn.session_id
                self._remember_session()
            self.handoff = None  # delivered with this turn
            self.model = self.cfg.model or self.model
            self._model_seen_for = self.cfg.model
        return turn

    def _note_context(self, session_id: str | None, turn_id: str | None) -> None:
        """How full the thread is after this turn. The stream's turn.completed usage is the thread's
        running total, so the last response's own prompt size (and the model's window, which only
        the rollout knows) comes from the turn's last token_usage_record / token_count there."""
        if not session_id or not turn_id:
            return
        paths = glob.glob(os.path.join(CODEX_SESSIONS, "**", f"rollout-*-{session_id}.jsonl"), recursive=True)
        if not paths:
            return
        read = window = 0
        try:
            with open(max(paths, key=os.path.getmtime), encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = ev.get("payload") or {}
                    if ev.get("type") == "token_usage_record" and payload.get("turn_id") == turn_id:
                        usage = payload.get("usage") or {}
                        read = int(usage.get("input_tokens") or 0) or read  # includes the cached part
                    elif ev.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        window = int(info.get("model_context_window") or 0) or window
        except OSError:
            return
        if read:
            self.context = (read, window or 258_400)

    def _cancelled_usage(self, turn, turn_id: str | None, fresh_thread: bool = False) -> dict | None:
        """Recover Codex's latest partial counts when cancellation prevents turn.completed.

        Codex checkpoints usage in its rollout while a turn runs. The JSONL stream normally exposes
        the same information only in turn.completed, which is the event cancellation cuts off.
        """
        if not turn.session_id or not turn_id:
            return None
        paths = glob.glob(os.path.join(CODEX_SESSIONS, "**", f"rollout-*-{turn.session_id}.jsonl"), recursive=True)
        if not paths:
            return None
        found = None
        try:
            with open(max(paths, key=os.path.getmtime), encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") != "token_usage_record":
                        continue
                    payload = ev.get("payload", {})
                    if payload.get("turn_id") != turn_id:
                        continue
                    usage = payload.get("thread_token_usage")
                    if isinstance(usage, dict):
                        found = usage
        except OSError:
            return None
        if found:
            self._record_usage(found, turn, fresh_thread=fresh_thread)
            return turn.usage
        return None

    # -- usage --------------------------------------------------------------
    def _record_usage(self, usage: dict | None, turn, fresh_thread: bool = False) -> None:
        """Record Codex's cumulative thread snapshot and derive a last-turn delta when possible."""
        if not usage:
            return
        prev = self.last_usage or self.cached_usage() or {}
        same_thread = bool(turn.session_id and prev.get("thread_id") == turn.session_id)
        before = prev.get("thread") or {}
        if fresh_thread or same_thread:
            last = {
                k: max(0, v - (before.get(k, 0) if same_thread else 0))
                for k, v in usage.items()
                if isinstance(v, (int, float))
            }
        else:
            # On the first Vision turn of an already-existing Codex thread, the event is cumulative
            # and there is no trustworthy baseline from which to isolate this one turn.
            last = None
        self.last_usage = {
            "provider": "codex",
            "model": self.cfg.model,
            "thread_id": turn.session_id,
            "last": last,
            "thread": usage,
            "turns": int(prev.get("turns", 0)) + 1 if same_thread else 1,
            "at": time.time(),
        }
        turn.usage = last  # this turn only; the cumulative thread snapshot lives in last_usage
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

    def rate_limits(self) -> dict | None:
        """Codex's subscription windows: live from the app-server, else the current thread's rollout.

        Returns {"rate_limits": {...}, "source": "live" | "rollout", "at": ts} or None.
        """
        live = fetch_rate_limits(self.codex)
        if live:
            return {"rate_limits": live, "source": "live", "at": time.time()}
        return self._rollout_rate_limits()

    def _rollout_rate_limits(self) -> dict | None:
        """The last token_count event in this thread's rollout file (a snapshot from that reply)."""
        sid = self.session_id or self.last_session_id()
        if not sid:
            return None
        paths = glob.glob(os.path.join(CODEX_SESSIONS, "**", f"rollout-*-{sid}.jsonl"), recursive=True)
        if not paths:
            return None
        found, stamp = None, None
        try:
            with open(max(paths, key=os.path.getmtime), encoding="utf-8") as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = ev.get("payload", {})
                    if payload.get("type") == "token_count" and payload.get("rate_limits"):
                        found, stamp = payload, ev.get("timestamp")
        except OSError:
            return None
        if not found:
            return None
        at = None
        if isinstance(stamp, str):
            try:
                from datetime import datetime

                at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                at = None
        return {"rate_limits": found["rate_limits"], "source": "rollout", "at": at or (self.last_usage or {}).get("at")}

    def usage_report(self) -> str | None:
        return None  # Codex has no equivalent of Claude Code's /usage text; see usage_renderable().

    def ping_usage(self) -> dict | None:
        return self.last_usage

    def usage_renderable(self, full: bool = False):
        """A table for /usage in the same shape as Claude's: session and week windows, bars, resets."""
        limits = self.rate_limits()
        if not limits:
            return Text("No Codex usage yet: ask something first, or run `codex login`.", style="dim")
        rl = limits["rate_limits"]
        plan = (rl.get("plan_type") or "").capitalize() or None
        t = usage_ui.usage_table("Codex", plan)
        for key, default in (("primary", "Current session"), ("secondary", "Current week")):
            w = rl.get(key) or {}
            if not w:
                continue
            mins = w.get("window_minutes") or 0
            if not mins:
                label = default
            elif mins < 1440:
                label = "Current session"
            else:
                label = "Current week" if mins == 10080 else "Current window"
            usage_ui.add_window(t, label, w.get("used_percent"), w.get("resets_at"))
        usage_ui.add_banked(t, usage_ui.codex_banked(rl))
        parts = [t]
        if limits.get("source") != "live":
            parts.append(usage_ui.footer("from the last reply's rate-limit snapshot", limits.get("at")))
        if full:
            credits = rl.get("credits") or {}
            extras = []
            if credits.get("unlimited"):
                extras.append("credits: unlimited")
            elif credits.get("has_credits"):
                extras.append(f"credits: {credits.get('balance')}")
            if rl.get("rate_limit_reached_type"):
                extras.append(f"limit reached: {rl['rate_limit_reached_type']}")
            if extras:
                parts.append(Text(" · ".join(extras), style="dim"))
        return usage_ui.usage_group(*parts)

    def cancel(self) -> None:
        with self._lock:
            proc, rpc = self._proc, self._rpc
        if rpc is not None:
            self._cancelled_app = True
            rpc.interrupt()  # the turn ends as `interrupted`; the process is killed if it lingers
            return
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
