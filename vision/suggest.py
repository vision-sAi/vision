"""The next message the user would likely send, guessed after a reply, as Claude Code's terminal does.

One small, quick call with the end of the conversation, whatever brain the chat is on: Claude's Haiku
when Claude Code is there, else a small Codex model, else Grok. A provider whose call fails (signed out,
model gone) is passed over for a while and the next one asked. The phone shows the guess as grey text in
an empty composer and the terminal in its empty box.

Most of a Claude guess's wait is Claude Code starting, not Haiku answering, so one `claude` is kept
started ahead (a spare, waiting on stdin): a guess takes it and starts the next. Each process still
answers one guess and exits, so no guess sees another's conversation. `warm()` starts one early (when a
reply begins), so even the first guess finds it ready.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import tempfile
import threading
import time

TURNS = 6           # transcript rows given to the model
ROW_CHARS = 2000    # each cut to this (the end of a long reply is what the next message answers)
MAX_CHARS = 120     # a longer guess is not a message someone would type

# Who makes the guess, in this order, and on which of their models: the smallest quick one each has.
# Codex's is the newest Luna it lists (codex_model), this one when its list can't be read.
GUESSERS = ("claude", "codex", "grok")
MODELS = {"claude": "haiku", "codex": "gpt-5.6-luna", "grok": "grok-4.5"}
RETRY_S = 600       # a provider whose call failed is passed over for this long
SPARE_MAX_AGE = 20 * 60  # a spare older than this is let go (sign-ins and Claude Code itself move on, and an idle
                         # Vision shouldn't keep a Claude Code running for nothing)
_failed: dict[str, float] = {}

PROMPT = """You predict the next message a user will type to their assistant, Vision.

You write as the User, the person talking to Vision, never as the Assistant. The User is the one at the
phone or computer: they see what was built or installed, and they tell the Assistant how it went.
Read the conversation below and write the single message the user would most naturally send next:
a follow-up, a go-ahead to an offer the assistant made ("yes, do it"), the obvious next step.
Write it in the user's own voice and style, short (under 12 words), as they would type it.
When the assistant has just handed the user something to try (built, installed, deployed, ready to
test), the user tries it and reports back. Assume it worked and guess what they'd say then, as a
statement: "works, commit it", "looks good, ship it". Never ask whether it worked or what happened.
Examples of good guesses after "It's installed on your phone": "works, commit it", "nice, push it".
Bad guesses there: "Did it work?", "Is the bug gone?"
Never ask the assistant about something its last reply already answered or checked.
When the assistant has asked the user what they want, need or are working on, only the user knows
the answer: reply with exactly the word NONE. Never guess an empty opener such as "can you help me
with something?", "I have a question" or "hey": a guess must carry what the user would actually say.
If there is no clear next message, reply with exactly the word NONE.
Reply with the message only, or NONE: no quotes, no preamble, never an explanation of your choice.
Do not use any tools."""


def _rows(history: list[dict]) -> str:
    out = []
    for m in history[-TURNS:]:
        text = str(m.get("text") or "").strip()
        if not text or m.get("role") not in ("user", "assistant"):
            continue
        if len(text) > ROW_CHARS:
            text = ("…" + text[-ROW_CHARS:]) if m["role"] == "assistant" else text[:ROW_CHARS] + "…"
        out.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {text}")
    return "\n\n".join(out)


def clean(text: str) -> str:
    """The model's answer as a message, or "" when it is not one."""
    text = text.strip().strip('"“”').strip()
    if not text or "\n" in text or len(text) > MAX_CHARS:
        return ""
    if text.lower().rstrip(".") in ("nothing", "none", "n/a", "(nothing)", "no message"):
        return ""
    if not any(c.isalnum() for c in text.replace("<br>", "")):  # a model's "nothing": <br>, …, ---
        return ""
    return text


def _claude_command(exe: str, prompt: str, model: str | None) -> list[str]:
    return [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", model or MODELS["claude"],
            "--system-prompt", prompt, "--tools", "", "--max-turns", "1",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
            "--no-session-persistence"]


class _Claude:
    """One `claude -p` started now, waiting on stdin for the one message it will answer."""

    def __init__(self, exe: str, prompt: str, model: str | None):
        from vision.brain import brain_env

        self.key = (exe, prompt, model)
        self.at = time.monotonic()
        self.timer: threading.Timer | None = None
        self.dir = tempfile.TemporaryDirectory(prefix="vision-suggest-")
        try:
            self.proc = subprocess.Popen(_claude_command(exe, prompt, model), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                         encoding="utf-8", cwd=self.dir.name, env=brain_env("claude"))
        except BaseException:
            self.dir.cleanup()
            raise

    def ready_for(self, key: tuple) -> bool:
        return key == self.key and self.proc.poll() is None and time.monotonic() - self.at < SPARE_MAX_AGE

    def ask(self, text: str, timeout: float) -> str | None:
        """Its answer to `text`, or None when it failed (signed out, model gone, timed out)."""
        message = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        try:
            out, _ = self.proc.communicate(message + "\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            self.drop()
            return None
        finally:
            self.dir.cleanup()
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                return None if ev.get("is_error") else str(ev.get("result") or "")
        return None

    def drop(self) -> None:
        if self.timer:
            self.timer.cancel()
        try:
            self.proc.kill()
            self.proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001  already gone
            pass
        self.dir.cleanup()


_spare: _Claude | None = None
_spare_lock = threading.Lock()


def _take_spare(key: tuple) -> _Claude | None:
    global _spare
    with _spare_lock:
        spare, _spare = _spare, None
    if spare and spare.ready_for(key):
        if spare.timer:
            spare.timer.cancel()
        return spare
    if spare:
        spare.drop()
    return None


def _start_spare(exe: str, prompt: str = PROMPT, model: str | None = None) -> None:
    """Have a `claude` ready for the next guess, unless one already is."""
    global _spare
    key = (exe, prompt, model)
    with _spare_lock:
        if _spare and _spare.ready_for(key):
            return
        old, _spare = _spare, None
    if old:
        old.drop()
    try:
        fresh = _Claude(exe, prompt, model)
    except Exception:  # noqa: BLE001  it can't start: the next guess starts its own, as before
        return
    with _spare_lock:
        if _spare is None:
            _spare, fresh = fresh, None
            # Unused by SPARE_MAX_AGE (no turn since), it goes: nothing waits around while Vision is idle.
            _spare.timer = threading.Timer(SPARE_MAX_AGE, _expire, (_spare,))
            _spare.timer.daemon = True
            _spare.timer.start()
    if fresh:  # another thread got there first
        fresh.drop()


def _expire(spare: _Claude) -> None:
    global _spare
    with _spare_lock:
        if _spare is not spare:  # taken or replaced meanwhile
            return
        _spare = None
    spare.drop()


@atexit.register
def _drop_spare() -> None:
    global _spare
    with _spare_lock:
        spare, _spare = _spare, None
    if spare:
        spare.drop()


def _claude_once(exe: str, convo: str, cwd: str, timeout: float, prompt: str, model: str | None) -> str | None:
    """One `claude -p` started for this call: for the rare one-off calls (memory import and review)."""
    from vision.brain import brain_env

    cmd = [exe, "-p", "--output-format", "json", "--model", model or MODELS["claude"],
           "--system-prompt", prompt, "--tools", "", "--max-turns", "1",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
           "--no-session-persistence"]
    r = subprocess.run(cmd, input=convo, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                       env=brain_env("claude"), encoding="utf-8")
    out = json.loads(r.stdout)
    if not isinstance(out, dict) or out.get("is_error"):
        return None
    return str(out.get("result") or "")


def _claude(exe: str, convo: str, cwd: str, timeout: float, prompt: str = PROMPT, model: str | None = None) -> str | None:
    if prompt != PROMPT:  # no spare kept for the other one-off calls: they are rare
        return _claude_once(exe, convo, cwd, timeout, prompt, model)
    key = (exe, prompt, model)
    claude = _take_spare(key) or _Claude(*key)
    answer = claude.ask(convo, timeout)
    if answer is not None:
        _start_spare(*key)  # the next guess finds it started
    return answer


def warm(providers=None) -> None:
    """A reply has begun: start the `claude` its guess will use, so the guess doesn't wait on Claude Code
    starting. Nothing when Claude isn't the guesser (off in Vision, not installed, or recently failing)."""
    from vision.clis import find_cli

    if providers is not None and "claude" not in providers:
        return
    if time.monotonic() - _failed.get("claude", -RETRY_S) < RETRY_S:
        return
    try:
        exe = find_cli("claude")
    except Exception:  # noqa: BLE001  not installed
        return
    _start_spare(exe)


def codex_model() -> str:
    """Codex's fast, cheap model: the first Luna in its own list (newest first), else MODELS["codex"]."""
    from vision.models import _codex_models_from_cache

    return next((m.alias for m in _codex_models_from_cache() or [] if "luna" in m.alias), MODELS["codex"])


def _codex(exe: str, convo: str, cwd: str, timeout: float, prompt: str = PROMPT, model: str | None = None) -> str | None:
    from vision.brain import brain_env
    from vision.codex import toml_str

    last = os.path.join(cwd, "last.txt")
    cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "-m", model or codex_model(),
           "-c", f"model_reasoning_effort={toml_str('low')}", "-c", f"developer_instructions={toml_str(prompt)}",
           "-s", "read-only", "-C", cwd, "-o", last, "-"]
    r = subprocess.run(cmd, input=convo, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                       env=brain_env("codex"), encoding="utf-8")
    if r.returncode != 0:
        return None
    try:
        with open(last, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _grok(exe: str, convo: str, cwd: str, timeout: float, prompt: str = PROMPT, model: str | None = None) -> str | None:
    from vision.brain import brain_env
    from vision.grok import _HEADLESS_DISALLOWED

    path = os.path.join(cwd, "prompt.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(convo)
    cmd = [exe, "--prompt-file", path, "--verbatim", "--output-format", "streaming-json", "--cwd", cwd,
           "--no-auto-update", "--no-plan", "--always-approve", "--sandbox", "read-only", "--rules", prompt,
           "--disallowed-tools", _HEADLESS_DISALLOWED, "--model", model or MODELS["grok"], "--effort", "low"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=brain_env("grok"),
                       encoding="utf-8", stdin=subprocess.DEVNULL)
    text, ended = [], False
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "text" and ev.get("data"):
            text.append(ev["data"])
        elif ev.get("type") == "error":
            return None
        elif ev.get("type") == "end":
            ended = True
    return "".join(text) if ended else None


_ASK = {"claude": _claude, "codex": _codex, "grok": _grok}


def one_shot(text: str, providers=None, timeout: float = 60, prompt: str = PROMPT, models: dict | None = None,
             prefer: str | None = None) -> str | None:
    """One tool-free call with `prompt` as the system prompt and `text` as the message, on the first
    provider that answers: the quick models above, or `models` (provider → model) where given. `prefer`
    puts one provider first (a chat's own, so the call lands on the subscription already in use). None
    when no provider could answer. A failed provider is passed over for RETRY_S, for every caller."""
    from vision.clis import find_cli

    order = [p for p in GUESSERS if p == prefer] + [p for p in GUESSERS if p != prefer]
    for name in order:
        if providers is not None and name not in providers:
            continue
        if time.monotonic() - _failed.get(name, -RETRY_S) < RETRY_S:
            continue
        try:
            exe = find_cli(name)
        except Exception:  # noqa: BLE001  not installed
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="vision-suggest-") as cwd:
                answer = _ASK[name](exe, text, cwd, timeout, prompt, (models or {}).get(name))
        except Exception:  # noqa: BLE001  (timed out, unreadable output: as good as failed)
            answer = None
        if answer is None:
            _failed[name] = time.monotonic()
            continue
        return answer
    return None


def ask(convo: str) -> str:
    """The message the guesser answers: the conversation, then whose turn it is."""
    return f"<conversation>\n{convo}\n</conversation>\n\nThe User's next message to the Assistant:"


def suggest(history: list[dict], providers=None, timeout: float = 60) -> str:
    """The guess for what follows `history` (rows of role and text), or "" for none or when no provider
    could make one. `providers`: the ones Vision has on ([providers] enabled); all of them when None."""
    convo = _rows(history)
    if not convo or not any(m.get("role") == "assistant" for m in history[-TURNS:]):
        return ""
    answer = one_shot(ask(convo), providers, timeout)
    return clean(answer) if answer is not None else ""
