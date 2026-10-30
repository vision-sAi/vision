"""Crash recovery for Vision chats.

Each accepted event is appended and fsynced before it is shown or acted on. A
truncated final JSON line is ignored after a crash; earlier lines remain valid.
This protects Vision's received text, not bytes still in a provider or a tool.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from vision.config import DATA_DIR

JOURNAL_DIR = DATA_DIR / "chat-journals"
SCHEDULED_RESTORE_DAYS = 30
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


class ChatJournal:
    def __init__(self, chat_id: str, directory: Path | None = None):
        if not _SAFE_ID.fullmatch(chat_id):
            raise ValueError("invalid chat id")
        self.path = (directory or JOURNAL_DIR) / f"{chat_id}.jsonl"
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._tail_clean = False

    def claim(self) -> bool:
        """Hold this chat for as long as this process runs, so another window doesn't take a live
        chat for a crashed one. The kernel drops the lock when the process dies. False when another
        running process already holds it."""
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._owner is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._owner = fd
        return True

    def release(self) -> None:
        if self._owner is not None:
            os.close(self._owner)
            self._owner = None
            self._tail_clean = False

    def append(self, kind: str, **fields) -> None:
        record = json.dumps({"event": kind, "at": time.time(), **fields}, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            owned = self._owner is not None
            fd = self._owner if owned else os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                # A prior process may have stopped mid-write. Trim that torn tail
                # before the next event, so valid records remain independently parseable.
                end = os.lseek(fd, 0, os.SEEK_END) if not self._tail_clean else 0
                if end:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        pos = end
                        while pos:
                            pos -= 1
                            os.lseek(fd, pos, os.SEEK_SET)
                            if os.read(fd, 1) == b"\n":
                                pos += 1
                                break
                        os.ftruncate(fd, pos)
                data = record.encode("utf-8")
                while data:
                    written = os.write(fd, data)
                    if not written:
                        raise OSError("journal write made no progress")
                    data = data[written:]
                (getattr(os, "fdatasync", os.fsync))(fd)
                if owned:
                    self._tail_clean = True
            finally:
                if not owned:
                    os.close(fd)

    def read(self) -> list[dict]:
        try:
            with self.path.open(encoding="utf-8") as file:
                return [parsed for line in file if line.endswith("\n")
                        if (parsed := _parse_json(line)) is not None]
        except FileNotFoundError:
            return []


def compact_agent_frame(frame: dict, previous: dict | None) -> dict:
    """Journal only new agent steps; the live wire frame still has the full list."""
    saved = dict(frame)
    steps = saved.pop("steps", [])
    old = (previous or {}).get("steps") or []
    saved["steps_added"] = steps[len(old):] if steps[:len(old)] == old else steps
    return saved


def _parse_json(line: str) -> dict | None:
    try:
        parsed = json.loads(line)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def in_use(path: Path) -> bool:
    """True while a running Vision process holds this chat (ChatJournal.claim)."""
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def saved_chats(directory: Path | None = None) -> list[tuple[str, list[dict]]]:
    root = directory or JOURNAL_DIR
    if not root.exists():
        return []
    chats = []
    for path in root.glob("*.jsonl"):
        # Scheduled runs create a new chat every time. Keep their journals on disk,
        # but stop opening old completed runs at every server startup.
        try:
            old = time.time() - path.stat().st_mtime > SCHEDULED_RESTORE_DAYS * 86400
            records = ChatJournal(path.stem, root).read()
        except (OSError, ValueError):
            continue
        if (records and records[0].get("event") == "chat" and records[0].get("source", "server") == "server"
                and records[-1].get("event") != "close"):
            if old and any(r.get("event") == "scheduled" for r in records):
                state = recover(records)
                if not state["interrupted"] and not state["pending"]:
                    continue
            chats.append((path.stem, records))
    return chats


_BRAINS_CACHE: dict[Path, tuple[float, int, dict[str, tuple[str, str]]]] = {}


def session_brains(directory: Path | None = None) -> dict[str, tuple[str, str]]:
    """The model and effort each provider session last ran on, from every chat's journal: {session_id: (model,
    effort)}. A file is re-read only when it has changed since the last call."""
    root = directory or JOURNAL_DIR
    out: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return out
    for path in root.glob("*.jsonl"):
        try:
            st = path.stat()
            hit = _BRAINS_CACHE.get(path)
            if not hit or hit[:2] != (st.st_mtime, st.st_size):
                found: dict[str, tuple[str, str]] = {}
                model = effort = ""
                for r in ChatJournal(path.stem, root).read():
                    if r.get("event") in ("chat", "model"):
                        model, effort = r.get("model") or model, r.get("effort") or ""
                    sid = r.get("session_id")
                    if sid and model and r.get("event") in ("done", "model"):
                        found[sid] = (model, effort)
                hit = _BRAINS_CACHE[path] = (st.st_mtime, st.st_size, found)
        except (OSError, ValueError):
            continue
        out.update(hit[2])
    return out


def interrupted_journals(directory: Path | None = None) -> list[tuple[str, dict]]:
    """All chats with a turn that started but never reached a durable done event."""
    root = directory or JOURNAL_DIR
    if not root.exists():
        return []
    out = []
    for path in root.glob("*.jsonl"):
        if in_use(path):
            continue
        try:
            records = ChatJournal(path.stem, root).read()
        except (OSError, ValueError):
            continue
        if records and records[0].get("event") == "chat" and records[-1].get("event") != "close":
            state = recover(records)
            if state["interrupted"] or state["pending"]:
                out.append((path.stem, state))
    return sorted(out, key=lambda item: item[1]["updated"], reverse=True)


def latest_terminal_recovery(directory: Path | None = None) -> tuple[str, dict] | None:
    """Recover only when the latest terminal conversation stopped mid-turn."""
    root = directory or JOURNAL_DIR
    if not root.exists():
        return None
    paths = sorted(root.glob("terminal-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        if in_use(path):  # still open in another window, not interrupted
            continue
        try:
            records = ChatJournal(path.stem, root).read()
        except (OSError, ValueError):
            continue
        if not any(r.get("event") in ("queued", "start", "steer_attempt") for r in records):
            continue
        state = recover(records)
        return (path.stem, state) if state["interrupted"] or state["pending"] else None
    return None


def recover(records: list[dict], requeue: bool = False) -> dict:
    """Rebuild the visible transcript and state of one journalled chat. `requeue`: the last process
    restarted on purpose, so messages still queued are left in `pending` to run, not shown as lost."""
    from vision.agentlog import turn_entries

    meta = records[0]
    history: list[dict] = []
    pending: list[str] = []
    steer_attempts: list[str] = []
    active: dict | None = None
    has_base = False
    session_id = meta.get("session_id")
    model = meta.get("model")
    effort = meta.get("effort")
    mode = ""  # auto or plan, once the phone has set it ("" = the configured one)
    updated = meta.get("at") or time.time()
    for record in records[1:]:
        kind = record.get("event")
        updated = record.get("at") or updated
        if kind == "base":
            history = list(record.get("history") or [])
            has_base = True
        elif kind == "queued":
            pending.append(record.get("text") or "")
        elif kind == "unqueued":
            try:
                pending.remove(record.get("text") or "")
            except ValueError:
                pass
        elif kind == "steer_attempt":
            steer_attempts.append(record.get("text") or "")
        elif kind in ("steered", "steer_failed"):
            text = record.get("text") or ""
            if text in steer_attempts:
                steer_attempts.remove(text)
            if active and kind == "steered":
                active["steers"].append((len(active["partial"]), text, len(active["agents"])))
        elif kind == "start":
            if active:
                history.extend(_interrupted(active))
            text = record.get("text") or ""
            if text in pending:
                pending.remove(text)
            active = {"text": text, "partial": "", "agents": {}, "tools": {}, "steers": []}
        elif active and kind == "delta":
            active["partial"] += record.get("text") or ""
        elif active and kind in ("agent", "tool"):
            frame = record.get("frame") or {}
            if frame.get("id"):
                if kind == "agent" and "steps_added" in frame:
                    previous = active["agents"].get(frame["id"]) or {}
                    frame = {**frame, "steps": (previous.get("steps") or []) + (frame["steps_added"] or [])}
                    frame.pop("steps_added", None)
                active[kind + "s"][frame["id"]] = frame
        elif kind == "done":
            session_id = record.get("session_id") or session_id
            if active:
                history.extend(turn_entries(active["text"], record.get("text") or active["partial"],
                                            record.get("error") or "", active["partial"],
                                            list(active["agents"].values()), active["steers"],
                                            list(active["tools"].values())))
                active = None
        elif kind == "model":
            model = record.get("model") or model
            effort = record.get("effort") or effort
            session_id = record.get("session_id")
        elif kind == "mode":
            mode = record.get("mode") or mode
    recovery_start = len(history)
    if active:
        history.extend(_interrupted(active))
    pending.extend(text for text in steer_attempts if text and text not in pending)
    from vision.projects import report_note

    for text in [] if requeue else pending:
        if report_note(text) is not None:
            continue  # a project's threads reporting in, not something the user asked: nothing to send again
        history.extend(({"role": "user", "text": text},
                        {"role": "assistant", "text": "", "error": "Queued when Vision stopped. Send this again to run it."}))
    return {"history": history, "session_id": session_id, "model": model, "effort": effort, "mode": mode,
            "title": meta.get("title") or "", "workdir": meta.get("workdir") or "",  # "" in journals from before it was saved
            "created": meta.get("at") or updated,
            "updated": updated, "interrupted": bool(active), "pending": pending, "has_base": has_base,
            "recovery_rows": history[recovery_start:]}


def _interrupted(active: dict) -> list[dict]:
    from vision.agentlog import turn_entries

    agents = []
    for frame in active["agents"].values():
        row = dict(frame)
        if not row.get("done"):
            row.update(done=True, failed=True, cut_off=True,
                       status=row.get("status") or "Interrupted when Vision stopped")
        agents.append(row)
    tools = []
    for frame in active["tools"].values():
        row = dict(frame)
        if not row.get("done"):
            row.update(done=True, is_error=True, cut_off=True,
                       output=row.get("output") or "Interrupted while this tool was running")
        tools.append(row)
    return turn_entries(active["text"], active["partial"],
                        "Vision stopped during this reply. Check any tool changes before retrying.",
                        active["partial"], agents, active["steers"], tools)


def recovery_context(history: list[dict], activity: list[dict] | None = None) -> str:
    """Conversation text plus interrupted tool activity for a fresh model context."""
    from vision.sessions import format_transcript

    text = format_transcript(history)
    lines = []
    for row in history if activity is None else activity:
        for tool in row.get("tools") or []:
            status = "interrupted" if tool.get("cut_off") else "failed" if tool.get("is_error") else "completed"
            label = " ".join(part for part in (tool.get("name"), tool.get("detail")) if part)
            lines.append(f"- {label or 'Tool'}: {status}")
            if output := (tool.get("output") or "").strip():
                lines.append(f"  Output: {output[-4000:]}")
    if lines:
        text += "\n\nTool activity before Vision stopped:\n" + "\n".join(lines)
    return text
