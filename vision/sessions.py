"""Past Vision conversations, read from the providers' own session stores.

Claude Code keeps one JSONL per session under ~/.claude/projects/<cwd-encoded>/; Vision's turns are
tagged `entrypoint: "sdk-cli"` (interactive `claude` sessions say "cli"), which is how they are told
apart. Codex keeps rollout-*.jsonl files under ~/.codex/sessions/YYYY/MM/DD/; Vision's carry
`originator: "codex_exec"` (exec) or `"vision"` (app-server) and a developer message that starts with the persona. Grok keeps one
directory per session under ~/.grok/sessions/<cwd-encoded>/<id>/; Vision's are the ones whose
system_prompt.txt contains the persona.

Listing reads only the head of each file (title, cwd, marker); the file's mtime is "last active".
A Claude ↔ Codex ↔ Grok switch reads the whole file for user/assistant text. `claude --resume <id>`
finds a session from any working directory (verified with claude 2.1.272).
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from datetime import datetime
from dataclasses import dataclass

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
HEAD_BYTES = 256 * 1024  # enough to reach the first real prompt past the environment attachments
TITLE_WIDTH = 48
HISTORY_LIMIT = 60  # recent turns shown in history views; transfers use limit=0
CODEX_ORIGINATORS = ("codex_exec", "vision")  # `codex exec`, and codex_app.py's app-server clientInfo name
# Legacy summary prompt; skip it when reading older session logs.
HANDOFF_USER_PREFIX = "This conversation is being handed over to a different model"


@dataclass
class SessionInfo:
    id: str
    provider: str  # a providers.REGISTRY name
    title: str
    last_active: float  # epoch seconds
    cwd: str = ""

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def age(self, now: float | None = None) -> str:
        """'3 min ago', '2 h ago', 'yesterday', 'Sep 12'."""
        secs = max(0.0, (now or time.time()) - self.last_active)
        if secs < 90:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)} min ago"
        if secs < 86400:
            return f"{int(secs // 3600)} h ago"
        if secs < 172800:
            return "yesterday"
        if secs < 7 * 86400:
            return f"{int(secs // 86400)} days ago"
        return time.strftime("%b %d", time.localtime(self.last_active))


def _clean_title(text: str) -> str:
    """One line, capped; a model-handoff prompt is reduced to the user's actual message (and a project
    chat's turn to what the user typed)."""
    from vision.projects import checkin_note, unwrap_turn

    role, text = unwrap_turn(_strip_handoff(text))
    text = " ".join((checkin_note(text) if role == "checkin" else text).split())
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1] + "…"


def _head_lines(path: str):
    """The first HEAD_BYTES of a JSONL file as parsed objects (last partial line dropped)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(HEAD_BYTES)
    except OSError:
        return
    lines = chunk.split(b"\n")
    if len(chunk) == HEAD_BYTES:
        lines = lines[:-1]
    for line in lines:
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _worker_prompt(text: str) -> bool:
    """Private voice workers are never selectable as typed conversations."""
    try:
        data = json.loads(text)
        return isinstance(data, dict) and data.get("type") == "vision_task"
    except (ValueError, TypeError):
        return False


def _claude_session(path: str) -> SessionInfo | None:
    """Parse one Claude Code session file; None unless it is a Vision (sdk-cli) session."""
    sid = os.path.splitext(os.path.basename(path))[0]
    vision = False
    title = ""
    first_prompt = ""
    cwd = ""
    patience = 40  # records to keep scanning past the first prompt for the AI title, which comes later
    for rec in _head_lines(path):
        t = rec.get("type")
        if t == "ai-title" and rec.get("aiTitle"):
            title = rec["aiTitle"]
        elif t == "user":
            if rec.get("entrypoint") != "sdk-cli":
                return None
            vision = True
            cwd = cwd or rec.get("cwd", "")
            content = (rec.get("message") or {}).get("content")
            if not first_prompt and isinstance(content, str):
                if _worker_prompt(content):
                    return None
                first_prompt = content
        if vision and title:
            break
        if first_prompt:
            patience -= 1
            if patience <= 0:
                break
    if not vision:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return SessionInfo(sid, "claude", _clean_title(title or first_prompt or "(no prompt)"), mtime, cwd)


def _codex_session(path: str) -> SessionInfo | None:
    """Parse one Codex rollout; None unless it is a Vision (codex_exec/app-server + persona) thread."""
    sid = ""
    cwd = ""
    persona = False
    first_prompt = ""
    for rec in _head_lines(path):
        t = rec.get("type")
        p = rec.get("payload") or {}
        if t == "session_meta":
            if p.get("originator") not in CODEX_ORIGINATORS:
                return None
            sid, cwd = p.get("id") or "", p.get("cwd") or ""
        elif t == "response_item" and p.get("type") == "message":
            text = "".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
            if p.get("role") == "developer":
                if text.lstrip().startswith("You are Vision's silent task worker"):
                    return None
                persona = persona or text.lstrip().startswith("You are Vision")
            elif p.get("role") == "user" and not text.lstrip().startswith("<"):
                if _worker_prompt(text):
                    return None
                first_prompt = text
                break
    if not (sid and persona):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return SessionInfo(sid, "codex", _clean_title(first_prompt or "(no prompt)"), mtime, cwd)


def _newest_first(paths: list[str]) -> list[str]:
    def mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    return sorted(paths, key=mtime, reverse=True)


from vision.providers import REGISTRY as _REGISTRY
from vision.providers import get as _provider

PROVIDERS = tuple(_REGISTRY)  # the built-in ones; `_providers()` also has the config-defined servers
PROVIDER_TITLES = {p.name: p.label for p in _REGISTRY.values()}


def _providers() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def _title(provider: str) -> str:
    p = _provider(provider)
    return p.label if p else provider


# Where each provider keeps its conversations (the registry's session_paths hooks).
def claude_session_paths() -> list[str]:
    return glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))


def codex_session_paths() -> list[str]:
    from vision.codex import CODEX_SESSIONS

    return glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl"))


def grok_session_paths() -> list[str]:
    from vision.grok import GROK_SESSIONS

    return glob.glob(os.path.join(GROK_SESSIONS, "*", "*", "summary.json"))


def local_session_paths() -> list[str]:
    from vision.local import SESSIONS_DIR

    return glob.glob(os.path.join(str(SESSIONS_DIR), "*.json"))


def list_sessions(provider: str, limit: int = 15) -> list[SessionInfo]:
    """The most recently active Vision conversations for a provider, newest first."""
    p = _provider(provider)
    paths_of, parse = (p.hook("session_paths"), p.hook("parse_session")) if p else (None, None)
    if paths_of is None or parse is None:
        return []
    paths = paths_of()
    out: list[SessionInfo] = []
    seen: set[str] = set()
    for path in _newest_first(paths):
        info = parse(path)
        if info and info.provider != provider:
            continue  # the local-server providers share one folder; each file says whose it is
        if info and info.id not in seen:
            seen.add(info.id)
            out.append(info)
            if len(out) >= limit:
                break
    return out


def list_all_sessions(limit: int = 15) -> list[SessionInfo]:
    """Vision conversations for every provider, Claude, Codex, Grok then Local, newest first within each."""
    out: list[SessionInfo] = []
    for provider in _providers():
        out.extend(list_sessions(provider, limit=limit))
    return out


def find_session(provider: str, prefix: str, limit: int = 200) -> SessionInfo | None:
    """A session whose id starts with `prefix` (case-insensitive), or None."""
    prefix = prefix.strip().lower()
    if not prefix:
        return None
    for info in list_sessions(provider, limit=limit):
        if info.id.lower().startswith(prefix):
            return info
    return None


def find_any_session(prefix: str, prefer: str | None = None, limit: int = 200) -> SessionInfo | None:
    """Like find_session, but walks every provider. `prefer` is tried first (the active brain)."""
    if prefer:
        hit = find_session(prefer, prefix, limit=limit)
        if hit:
            return hit
    for provider in _providers():
        if provider == prefer:
            continue
        hit = find_session(provider, prefix, limit=limit)
        if hit:
            return hit
    return None


def session_key(info: SessionInfo) -> str:
    """Picker value: provider-prefixed so Claude/Codex/Grok/Local ids cannot collide."""
    return f"{info.provider}:{info.id}"


def session_from_key(key: str, listed: list[SessionInfo]) -> SessionInfo | None:
    """Resolve a picker value or a bare id prefix against `listed`."""
    key = (key or "").strip()
    if not key:
        return None
    provider, sep, sid = key.partition(":")
    if sep and provider in _providers() and sid:
        return next((s for s in listed if s.provider == provider and s.id == sid), None)
    return next((s for s in listed if s.id == key), None)


def session_rows(sessions: list[SessionInfo], now: float | None = None) -> list[tuple[str, str, str]]:
    """(value, label, description) rows for the picker. value is provider:id."""
    return [(session_key(s), s.title, f"{s.age(now)} · {s.short_id}") for s in sessions]


def session_tabs(now: float | None = None, limit: int = 15) -> list[tuple[str, list[tuple[str, str, str]], str]]:
    """Claude / Codex / Grok / Local tabs for `/session`. Empty providers keep the tab with a note."""
    tabs = []
    for provider in _providers():
        rows = session_rows(list_sessions(provider, limit=limit), now)
        tabs.append((_title(provider), rows, "" if rows else "no conversations"))
    return tabs


VISION_MARKER = "You are Vision, a personal AI assistant"


def _parse_iso(stamp: str) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _grok_session(summary_path: str) -> SessionInfo | None:
    """Parse one Grok session directory; None unless Vision started it (its own index, see
    vision.grok.remember_session_id; older sessions may carry the persona in system_prompt.txt)."""
    from vision.grok import known_session_ids

    folder = os.path.dirname(summary_path)
    try:
        with open(summary_path, encoding="utf-8") as f:
            data = json.loads(f.read())
    except (OSError, ValueError):
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    sid = info.get("id") or os.path.basename(folder)
    if sid not in known_session_ids():
        try:
            with open(os.path.join(folder, "system_prompt.txt"), encoding="utf-8", errors="replace") as f:
                if VISION_MARKER not in f.read(8192):
                    return None
        except OSError:
            return None
    cwd = info.get("cwd") or ""
    title = (data.get("generated_title") or data.get("session_summary") or "").strip()
    last = _parse_iso(data.get("last_active_at") or "") or _parse_iso(data.get("updated_at") or "")
    try:
        mtime = os.path.getmtime(summary_path)
    except OSError:
        return None
    return SessionInfo(sid, "grok", _clean_title(title or "(no prompt)"), last or mtime, cwd)


def _local_messages(path: str, *, include_context: bool = False) -> tuple[dict, list[dict]]:
    """A local-model session file: ({"model", "at"}, user/assistant text messages)."""
    with open(path, encoding="utf-8") as f:
        data = json.loads(f.read())
    out = []
    for m in data.get("messages", []):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):  # OpenAI-style parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            text = content.strip()
            if m["role"] == "user" and not include_context:
                earlier, text = _unfold_handoff(text)
                out.extend(earlier)
                text = text.strip()
            if text:
                out.append({"role": m["role"], "text": text})
    return data, out


def _local_session(path: str) -> SessionInfo | None:
    """Parse one local-server conversation saved by vision/local.py (always Vision's)."""
    try:
        data, messages = _local_messages(path)
        mtime = os.path.getmtime(path)
    except (OSError, ValueError):
        return None
    from vision.models import provider_for

    sid = os.path.splitext(os.path.basename(path))[0]
    first = next((m["text"] for m in messages if m["role"] == "user"), "")
    at = data.get("at")
    provider = provider_for(str(data.get("model") or "")) if data.get("model") else "local"  # "ollama/qwen3" → ollama
    return SessionInfo(sid, provider, _clean_title(first or "(no prompt)"), float(at) if isinstance(at, (int, float)) else mtime)


def local_history(session_id: str, limit: int = HISTORY_LIMIT, *, include_context: bool = False) -> list[dict]:
    """The last `limit` user/assistant messages of a local-model session."""
    from vision.local import SESSIONS_DIR

    try:
        _, messages = _local_messages(str(SESSIONS_DIR / f"{session_id}.json"), include_context=include_context)
    except (OSError, ValueError):
        return []
    return messages[-limit:]


# -- transcript reconstruction ----------------------------------------------
# Providers keep the real conversation in their own JSONL. Vision does not have a parallel log, so a
# Claude ↔ Codex ↔ Grok switch rebuilds user/assistant text from those files. Tool traces are dropped:
# the new model cannot resume the old session, and the replies already say what was done. The outgoing
# model is not asked, so a rate-limit there cannot wipe the chat.


_HANDOFF_START = "You are taking over an ongoing conversation"
_HANDOFF_MARKER = "The user's next message:\n\n"


def _strip_handoff(text: str) -> str:
    """If this user turn is Vision's injected handoff wrapper (or shell-mode note), keep only the user's real message."""
    from vision.brain import SHELL_NOTE_START

    if text.startswith((_HANDOFF_START, SHELL_NOTE_START)) and _HANDOFF_MARKER in text:
        # Earlier transfers can be nested inside the context; the final marker introduces this turn.
        return text.rsplit(_HANDOFF_MARKER, 1)[1]
    return text


def _transcript_turns(note: str) -> list[dict]:
    """format_transcript's "User: …" / "Vision: …" blocks back into turns; a first user block that is
    itself a handoff wrapper (a second switch) is unfolded too."""
    out: list[dict] = []
    if note.startswith("User: " + _HANDOFF_START) and _HANDOFF_MARKER in note:
        inner = note[len("User: "):]
        cut = inner.rindex(_HANDOFF_MARKER) + len(_HANDOFF_MARKER)
        rest = inner[cut:]
        end = rest.find("\n\nVision: ")
        message, note = (rest, "") if end < 0 else (rest[:end], rest[end + 2:])
        out, message = _unfold_handoff(inner[:cut] + message)
        out.append({"role": "user", "text": message.strip()})
    for block in re.split(r"\n\n(?=(?:User|Vision): )", note):
        role, _, text = block.partition(": ")
        if role in ("User", "Vision") and text.strip():
            out.append({"role": "user" if role == "User" else "assistant", "text": text.strip()})
    return out


def _unfold_handoff(text: str) -> tuple[list[dict], str]:
    """A handoff wrapper as (the earlier turns it carries, the user's real message); ([], text) if
    it is not one. History views show the carried turns so a switched chat keeps its past."""
    if not (text.startswith(_HANDOFF_START) and _HANDOFF_MARKER in text):
        return [], text
    head, message = text.rsplit(_HANDOFF_MARKER, 1)
    body = head.split("\n\n", 1)[1] if "\n\n" in head else ""  # past "What happened so far:"
    body = body.rsplit("\n\n---\n\n", 1)[0]
    return _transcript_turns(body), message


def _content_text(content) -> str:
    """Plain text of a message body: a string, or the text parts of a list (tool results skipped)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") == "tool_result":
            continue
        text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _append_turn(out: list[dict], role: str, text: str, *, include_context: bool = False) -> None:
    """Push a user/assistant turn, merging consecutive assistant fragments and skipping empties."""
    text = text.strip()
    if not text:
        return
    if role == "user":
        if not include_context:
            from vision.projects import unwrap_turn

            earlier, text = _unfold_handoff(text)
            for m in earlier:
                _append_turn(out, m["role"], m["text"])
            role, text = unwrap_turn(_strip_handoff(text.strip()))  # shell-mode note and a project chat's status block off, a thread report as a note
            text = text.strip()
        if not text or text.startswith("<") or text.startswith(HANDOFF_USER_PREFIX):
            return
    if role == "assistant" and out and out[-1]["role"] == "assistant":
        out[-1]["text"] += "\n\n" + text
        return
    out.append({"role": role, "text": text})


# Parsed Claude sessions, kept by file. The typed session is read on every voice turn (the handoff
# context) and three times when a chat opens on it (the journal's base, the arrow history, the shown
# turns): a 30 MB session is 100 ms of JSON each time. Claude Code only appends to the file, so the
# bytes already folded in stay as they were and only what the file gained is parsed; a file that
# shrank, or changed under the same size, is read again from the start.
_PARSED_KEEP = 6  # files kept parsed: the chat's own session, the voice's typed one, one being resumed…
_parsed: dict[tuple[str, bool], "_ParsedSession"] = {}
_parsed_lock = threading.Lock()


class _ParsedSession:
    def __init__(self) -> None:
        self.offset = 0  # bytes folded into `turns` so far (whole records only)
        self.size = 0
        self.mtime_ns = 0
        self.turns: list[dict] = []


def _fold_claude_lines(out: list[dict], lines, *, include_context: bool) -> None:
    """Fold the session records in `lines` (bytes, one JSON object each) into `out`."""
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("isMeta"):
            continue
        t = ev.get("type")
        content = (ev.get("message") or {}).get("content")
        if t == "user":
            _append_turn(out, "user", _content_text(content), include_context=include_context)
        elif t == "assistant":
            _append_turn(out, "assistant", _content_text(content))


def _claude_turns(path: str, include_context: bool) -> list[dict]:
    """Every user/assistant text turn of the session file: what was parsed before plus what the file
    gained since. Copies, so a caller may annotate them (agentlog.attach does)."""
    key = (path, include_context)
    with _parsed_lock:
        try:
            st = os.stat(path)
        except OSError:
            _parsed.pop(key, None)
            return []
        p = _parsed.get(key)
        if p is None or st.st_size < p.offset or (st.st_size == p.size and st.st_mtime_ns != p.mtime_ns):
            p = _ParsedSession()
        if st.st_size != p.size or st.st_mtime_ns != p.mtime_ns:
            try:
                with open(path, "rb") as f:
                    f.seek(p.offset)
                    data = f.read()
            except OSError:
                return [dict(m) for m in p.turns]
            lines = data.split(b"\n")
            tail = lines.pop()  # after the last newline: a record still being written, or a file without a final one
            folded = len(data) - len(tail)
            if tail.strip():
                try:
                    json.loads(tail)
                except ValueError:
                    pass  # torn: it is read again, whole, next time
                else:
                    lines.append(tail)
                    folded = len(data)
            _fold_claude_lines(p.turns, (ln for ln in lines if ln.strip()), include_context=include_context)
            p.offset += folded
            p.size, p.mtime_ns = st.st_size, st.st_mtime_ns
            _parsed.pop(key, None)
            _parsed[key] = p  # most recent last
            while len(_parsed) > _PARSED_KEEP:
                _parsed.pop(next(iter(_parsed)))
        return [dict(m) for m in p.turns]


def claude_history(session_id: str, limit: int = HISTORY_LIMIT, *, include_context: bool = False) -> list[dict]:
    """The last `limit` user/assistant text messages of a Claude Code session (all of them for 0)."""
    paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{session_id}.jsonl"))
    if not paths:
        return []
    try:
        path = max(paths, key=os.path.getmtime)
    except OSError:
        return []
    return _claude_turns(path, include_context)[-limit:]


def claude_session_cwd(session_id: str) -> str:
    """The folder a Claude Code session's first turn ran in, or "" when there is no such session."""
    if not session_id:
        return ""
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{session_id}.jsonl")):
        for rec in _head_lines(path):
            if rec.get("cwd"):
                return rec["cwd"]
    return ""


def _codex_rollout_path(session_id: str) -> str | None:
    from vision.codex import CODEX_SESSIONS

    paths = glob.glob(os.path.join(CODEX_SESSIONS, "**", f"rollout-*-{session_id}.jsonl"), recursive=True)
    return max(paths, key=os.path.getmtime) if paths else None


def codex_history(session_id: str, limit: int = HISTORY_LIMIT, *, include_context: bool = False) -> list[dict]:
    """The last `limit` user/assistant text messages of a Codex exec thread."""
    path = _codex_rollout_path(session_id)
    if not path:
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                p = ev.get("payload") or {}
                if ev.get("type") != "response_item" or p.get("type") != "message":
                    continue
                role = p.get("role")
                if role in ("user", "assistant"):
                    _append_turn(out, role, _content_text(p.get("content")), include_context=include_context)
    except OSError:
        return []
    return out[-limit:]


def _grok_plain(content) -> str:
    """Plain text of a Grok chat_history message (string or list of text blocks)."""
    if isinstance(content, str):
        text = content
    else:
        text = _content_text(content)
    start = text.find("<user_query>")
    if start != -1:
        start += len("<user_query>")
        end = text.find("</user_query>", start)
        if end != -1:
            return text[start:end]
    return text


def _grok_chat_path(session_id: str) -> str | None:
    from vision.grok import GROK_SESSIONS

    paths = glob.glob(os.path.join(GROK_SESSIONS, "*", session_id, "chat_history.jsonl"))
    return max(paths, key=os.path.getmtime) if paths else None


def grok_history(session_id: str, limit: int = HISTORY_LIMIT, *, include_context: bool = False) -> list[dict]:
    """The last `limit` user/assistant text messages of a Grok CLI session."""
    path = _grok_chat_path(session_id)
    if not path:
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("synthetic_reason"):
                    continue
                t = rec.get("type")
                if t not in ("user", "assistant"):
                    continue
                text = _grok_plain(rec.get("content")).strip()
                if t == "user" and text.startswith("<"):
                    continue
                _append_turn(out, t, text, include_context=include_context)
    except OSError:
        return []
    return out[-limit:]


def session_history(provider: str, session_id: str, limit: int = HISTORY_LIMIT, *, include_context: bool = False) -> list[dict]:
    """Saved turns; limit=0 reads all turns, include_context preserves earlier provider transfers.

    History views unfold the injected context into the earlier turns it carries. Transfers retain it raw, including nested transfers,
    so switching providers again does not discard the conversation from before the first switch.
    """
    if not session_id:
        return []
    p = _provider(provider) or _provider("claude")
    read = p.hook("history")
    history = read(session_id, limit, include_context=include_context) if read else []
    if include_context:
        return history
    from vision.agentlog import attach

    return attach(history, provider or "claude", session_id)  # the agent rows each reply ran


def format_transcript(messages: list[dict], max_chars: int | None = None) -> str:
    """All user/Vision turns as plain text, optionally capped to the most recent text."""
    blocks = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if text:
            role = "User" if m.get("role") == "user" else "Vision"
            blocks.append(f"{role}: {text}")
    if not blocks:
        return ""
    if max_chars is None:
        return "\n\n".join(blocks)
    kept: list[str] = []
    size = 0
    omitted = False
    for block in reversed(blocks):
        extra = len(block) + (2 if kept else 0)
        if size + extra > max_chars:
            if not kept:
                kept.append(block[: max(1, max_chars) - 1] + "…")
            omitted = True
            break
        kept.append(block)
        size += extra
    if omitted:
        kept.append("[earlier turns omitted]")
    kept.reverse()
    return "\n\n".join(kept)


def user_turns(provider: str, session_id: str, limit: int = HISTORY_LIMIT) -> list[str]:
    """This conversation's user messages, oldest first — what Up/Down in the input box recalls."""
    return [m["text"] for m in session_history(provider, session_id, limit) if m.get("role") == "user" and m.get("text")]


class LiveTitle:
    """The current conversation's title as it stands right now, for the terminal tab.

    `get(provider, session_id)` is cheap enough to call on every repaint: it looks at the session
    store at most every POLL seconds, and for Claude it reads only the bytes appended since last
    time. Claude names a session (an `ai-title` record) a little after the first reply and may
    rename it later, so the newest one wins; before that, the first prompt stands in, as in the
    session picker. Codex has no AI title, so the first prompt is it. Grok's summary.json gains a
    `generated_title` at some point and is small enough to reread whole."""

    POLL = 2.0

    def __init__(self) -> None:
        self._key: tuple[str, str] | None = None
        self._title = ""
        self._path: str | None = None
        self._offset = 0  # Claude: how far into the JSONL the last scan got
        self._ai_title = ""
        self._first_prompt = ""
        self._checked = 0.0

    def get(self, provider: str, session_id: str | None, now: float | None = None) -> str:
        if not session_id:
            self._key = None
            return ""
        now = time.time() if now is None else now
        key = (provider, session_id)
        if key != self._key:
            self.__init__()
            self._key = key
        elif now - self._checked < self.POLL:
            return self._title
        self._checked = now
        scanner = getattr(self, (_provider(provider) or _provider("claude")).title_scanner or "", None)
        try:
            if scanner is not None:
                scanner(session_id)
        except OSError:
            pass
        return self._title

    def _scan_claude(self, sid: str) -> None:
        if self._path is None:
            paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{sid}.jsonl"))
            if not paths:
                return  # the file appears with the first turn
            self._path = max(paths, key=os.path.getmtime)
        with open(self._path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        lines = chunk.split(b"\n")
        tail = lines.pop()  # a partial last line: leave it for next time
        self._offset += len(chunk) - len(tail)
        for line in lines:
            if b'"ai-title"' in line:
                rec = _loads(line)
                if rec.get("type") == "ai-title" and rec.get("aiTitle"):
                    self._ai_title = rec["aiTitle"]
            elif not self._first_prompt and b'"user"' in line:
                rec = _loads(line)
                content = (rec.get("message") or {}).get("content")
                if rec.get("type") == "user" and isinstance(content, str) and not _worker_prompt(content):
                    self._first_prompt = content
        text = self._ai_title or self._first_prompt
        self._title = _clean_title(text) if text else ""

    def _scan_codex(self, sid: str) -> None:
        if self._title:
            return  # the first prompt never changes
        path = _codex_rollout_path(sid)
        info = _codex_session(path) if path else None
        if info and info.title != "(no prompt)":
            self._title = info.title

    def _scan_grok(self, sid: str) -> None:
        from vision.grok import GROK_SESSIONS

        paths = glob.glob(os.path.join(GROK_SESSIONS, "*", sid, "summary.json"))
        info = _grok_session(max(paths, key=os.path.getmtime)) if paths else None
        if info and info.title != "(no prompt)":
            self._title = info.title


def _loads(line: bytes) -> dict:
    try:
        rec = json.loads(line)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


# -- search and export --------------------------------------------------------
# `vision sessions grep` reads every provider's transcripts; `/export` and `vision sessions export`
# write one as markdown. Both go through the history readers above, so what they see is what the
# history views show (tool traces dropped, provider transfers unfolded).

EXPORT_DIR = os.path.join("~", "Documents", "vision")
GREP_LINES = 3  # matching lines shown per conversation unless every one is asked for
GREP_WIDTH = 160  # a shown line is windowed around its first match past this many characters


@dataclass
class SessionMatch:
    info: SessionInfo
    lines: list[tuple[str, str]]  # (role, line) in conversation order: the best GREP_LINES, or all of them
    total: int  # how many lines matched in all


def transcript_path(info: SessionInfo) -> str | None:
    """The file a conversation's turns are read from, or None when it is gone."""
    try:
        if info.provider == "claude":
            paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{info.id}.jsonl"))
            return max(paths, key=os.path.getmtime) if paths else None
        if info.provider == "codex":
            return _codex_rollout_path(info.id)
        if info.provider == "grok":
            return _grok_chat_path(info.id)
        from vision.local import SESSIONS_DIR

        path = str(SESSIONS_DIR / f"{info.id}.json")
        return path if os.path.exists(path) else None
    except OSError:
        return None


def _plain_history(info: SessionInfo) -> list[dict]:
    """A conversation's user/assistant text, without the agent rows session_history attaches."""
    p = _provider(info.provider) or _provider("claude")
    read = p.hook("history") if p else None
    try:
        return read(info.id, 0, include_context=False) if read else []
    except (OSError, ValueError):
        return []


def _file_may_contain(path: str, needles: list[str]) -> bool:
    """A cheap look at the raw file before it is parsed: a word absent from the bytes is absent from
    the turns. Only plain ASCII words are checked (JSON may escape anything else)."""
    plain = [n for n in needles if n.isascii() and n.isalnum()]
    if not plain:
        return True
    try:
        with open(path, "rb") as f:
            data = f.read().lower()
    except OSError:
        return False
    return all(n.encode() in data for n in plain)


def grep_sessions(words: list[str], per_session: int = GREP_LINES, providers: tuple[str, ...] | None = None) -> list[SessionMatch]:
    """Conversations whose user or Vision text has every word (case-insensitive), newest first.

    Each comes with its matching lines: the `per_session` with the most words in them (0 = all), in
    conversation order."""
    needles = [w.lower() for w in words if w.strip()]
    if not needles:
        return []
    out: list[SessionMatch] = []
    for provider in providers or _providers():
        for info in list_sessions(provider, limit=10**6):
            path = transcript_path(info)
            if path is None or not _file_may_contain(path, needles):
                continue
            history = _plain_history(info)
            if not all(n in "\n".join(m.get("text") or "" for m in history).lower() for n in needles):
                continue
            lines = [(m.get("role") or "assistant", ln.strip())
                     for m in history for ln in (m.get("text") or "").splitlines()
                     if any(n in ln.lower() for n in needles)]
            if not lines:
                continue
            if per_session and len(lines) > per_session:
                ranked = sorted(range(len(lines)), key=lambda i: -sum(n in lines[i][1].lower() for n in needles))
                keep = sorted(ranked[:per_session])  # the best few, back in conversation order
                shown = [lines[i] for i in keep]
            else:
                shown = lines
            out.append(SessionMatch(info, shown, len(lines)))
    out.sort(key=lambda m: m.info.last_active, reverse=True)
    return out


def match_window(line: str, needles: list[str], width: int = GREP_WIDTH) -> str:
    """One shown line: a long one is windowed around its first match, ellipses marking the cuts."""
    if len(line) <= width:
        return line
    low = line.lower()
    first = min((low.find(n) for n in needles if n in low), default=0)
    start = max(0, min(first - width // 3, len(line) - width))
    piece = line[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(line) else "")


def export_markdown(title: str, provider: str, messages: list[dict], when: float | None = None, model: str = "") -> str:
    """The conversation as markdown: a title, a provider/model/date line, then a section per turn."""
    stamp = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title.strip() or 'Vision conversation'}", "", " · ".join(x for x in (_title(provider), model, stamp) if x), ""]
    for m in messages:
        text = (m.get("text") or "").strip()
        if text:
            lines += [f"## {'You' if m.get('role') == 'user' else 'Vision'}", "", text, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def export_path(title: str, when: float | None = None, directory: str = "") -> str:
    """Where an export goes by default: ~/Documents/vision/<date>-<title-slug>.md, never over an
    existing file (a -2, -3… suffix)."""
    day = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].rstrip("-") or "conversation"
    folder = os.path.expanduser(directory or EXPORT_DIR)
    path = os.path.join(folder, f"{day}-{stem}.md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{day}-{stem}-{n}.md")
        n += 1
    return path


def export_session(info: SessionInfo, out: str = "", model: str = "") -> str:
    """Write a conversation as markdown to `out` (a file, or a folder for the default name; empty =
    the default place) and return the path. ValueError when there is nothing to export."""
    messages = session_history(info.provider, info.id, limit=0)
    if not any((m.get("text") or "").strip() for m in messages):
        raise ValueError("nothing to export: no turns in this conversation yet")
    out = os.path.expanduser(out.strip()) if out else ""
    if not out or os.path.isdir(out) or out.endswith(os.sep):
        path = export_path(info.title, info.last_active, out)
    else:
        path = os.path.abspath(out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(export_markdown(info.title, info.provider, messages, info.last_active, model))
    return path
