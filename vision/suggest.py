"""The next message the user would likely send, guessed after a reply, as Claude Code's terminal does.

One small, quick call with the end of the conversation, whatever brain the chat is on: Claude's Haiku
when Claude Code is there, else a small Codex model, else Grok. A provider whose call fails (signed out,
model gone) is passed over for a while and the next one asked. The phone shows the guess as grey text in
an empty composer and the terminal in its empty box.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

TURNS = 6           # transcript rows given to the model
ROW_CHARS = 2000    # each cut to this (the end of a long reply is what the next message answers)
MAX_CHARS = 120     # a longer guess is not a message someone would type

# Who makes the guess, in this order, and on which of their models: the smallest quick one each has.
# Codex's is the newest Luna it lists (codex_model), this one when its list can't be read.
GUESSERS = ("claude", "codex", "grok")
MODELS = {"claude": "haiku", "codex": "gpt-5.6-luna", "grok": "grok-4.5"}
RETRY_S = 600       # a provider whose call failed is passed over for this long
_failed: dict[str, float] = {}

PROMPT = """You predict the next message a user will type to their assistant, Vision.

Read the conversation below and write the single message the user would most naturally send next:
a follow-up, a go-ahead to an offer the assistant made ("yes, do it"), the obvious next step.
Write it in the user's own voice and style, short (under 12 words), as they would type it.
If there is no clear next message, reply with nothing at all.
Reply with the message only: no quotes, no preamble, no explanation. Do not use any tools."""


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
    return text


def _claude(exe: str, convo: str, cwd: str, timeout: float, prompt: str = PROMPT, model: str | None = None) -> str | None:
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


def suggest(history: list[dict], providers=None, timeout: float = 60) -> str:
    """The guess for what follows `history` (rows of role and text), or "" for none or when no provider
    could make one. `providers`: the ones Vision has on ([providers] enabled); all of them when None."""
    convo = _rows(history)
    if not convo or not any(m.get("role") == "assistant" for m in history[-TURNS:]):
        return ""
    answer = one_shot(f"<conversation>\n{convo}\n</conversation>", providers, timeout)
    return clean(answer) if answer is not None else ""
