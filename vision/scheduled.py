"""Scheduled tasks: a prompt `vision serve` runs on its own at a set time, each run in a fresh chat.

Tasks live in ~/.local/share/vision/scheduled.json and only fire while `vision serve` is running;
one that falls due while it is down runs late if it is less than CATCH_UP old, otherwise it moves on
to its next time (a one-off is switched off). Times are local wall-clock times.

A task: {id, title, prompt, repeat: once|daily|weekdays|weekly, time: "HH:MM", date: "YYYY-MM-DD"
(once), weekday: 0-6 (weekly, Monday = 0), enabled, created, last_run, next_run, last_chat,
kind: task|reminder|checkin, chat, project, every_minutes, anchor}.

`kind` "reminder" runs no model: the prompt is the words handed back to the user (a push to the
phone, a note in the chat). `chat` is the chat a task was set from (`vision remind`, `vision
schedule` inside a turn): it fires there while that chat is still open, in a fresh one otherwise.

`kind` "checkin" is a scout's check-in: `project` says whose, the server writes the
turn itself (Hub.checkin), so the prompt may be empty. Each project has at most one (`for_project`).
A check-in missed while the computer slept or Vision was down runs once when it's back, however late,
rather than being skipped (or piling up): `due` hands it back with `missed`, when it was due.

`repeat` "every" runs every `every_minutes`, counted from `anchor` (when the interval was set).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

STORE = Path.home() / ".local" / "share" / "vision" / "scheduled.json"
REPEATS = ("once", "daily", "weekdays", "weekly", "every")
KINDS = ("task", "reminder", "checkin")
EVERY_MIN, EVERY_MAX = 15, 7 * 24 * 60  # minutes between runs of an "every" task
CATCH_UP = 30 * 60  # a run missed by less than this still happens when the server comes back
_DELAY = re.compile(r"(\d+)\s*(d|h|m|s)[a-z]*")
_CLOCK = re.compile(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?")


def parse_clock(text: str) -> str:
    """"18:00", "6pm", "6.30 pm" → "HH:MM". Raises ValueError."""
    m = _CLOCK.fullmatch(str(text).strip().lower())
    if not m:
        raise ValueError(f"not a time: {text!r} (use 18:00 or 6pm)")
    hh, mm, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hh <= 12:
            raise ValueError(f"not a time: {text!r}")
        hh = hh % 12 + (12 if half == "pm" else 0)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"not a time: {text!r}")
    return f"{hh:02d}:{mm:02d}"


def when(delay: str = "", clock: str = "", day: str = "", now: float | None = None) -> tuple[str, str]:
    """(date, "HH:MM") for a one-off: `delay` from now ("20m", "1h30m", "2 days"), or `clock` on `day`
    (YYYY-MM-DD, "tomorrow"; none = the next time the clock reads that). Raises ValueError."""
    start = dt.datetime.fromtimestamp(time.time() if now is None else now)
    if delay:
        parts = _DELAY.findall(delay.lower())
        if not parts or _DELAY.sub("", delay.lower()).strip(" ,and"):
            raise ValueError(f"not a delay: {delay!r} (use 20m, 1h30m, 2d)")
        gap = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in parts)
        at = start + dt.timedelta(seconds=gap)
        if at.second or at.microsecond:  # tasks run on whole minutes: never early
            at = at.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        return at.date().isoformat(), at.strftime("%H:%M")
    hhmm = parse_clock(clock)
    hh, mm = _clock(hhmm)
    if day.strip().lower() == "tomorrow":
        date = start.date() + dt.timedelta(days=1)
    elif day.strip().lower() in ("", "today"):
        date = start.date()
        if not day and dt.datetime.combine(date, dt.time(hh, mm)) <= start:
            date += dt.timedelta(days=1)
    else:
        date = dt.date.fromisoformat(day.strip())
    return date.isoformat(), hhmm


def _clock(text: str) -> tuple[int, int]:
    hh, mm = (int(x) for x in str(text).split(":")[:2])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError("time must be HH:MM")
    return hh, mm


def next_run(task: dict, after: float) -> float | None:
    """The first time strictly after `after` this task is due, or None (a one-off already past)."""
    repeat = task.get("repeat") or "once"
    if repeat == "every":
        step = int(task.get("every_minutes") or 60) * 60
        anchor = float(task.get("anchor") or after)
        return anchor + (int((after - anchor) // step) + 1) * step if after >= anchor else anchor
    hh, mm = _clock(task.get("time") or "09:00")
    now = dt.datetime.fromtimestamp(after)
    if repeat == "once":
        day = dt.date.fromisoformat(task.get("date") or now.date().isoformat())
        at = dt.datetime.combine(day, dt.time(hh, mm)).timestamp()
        return at if at > after else None
    day = now.date()
    for _ in range(9):
        at = dt.datetime.combine(day, dt.time(hh, mm))
        ok = (repeat == "daily" or (repeat == "weekdays" and at.weekday() < 5)
              or (repeat == "weekly" and at.weekday() == int(task.get("weekday", 0))))
        if ok and at.timestamp() > after:
            return at.timestamp()
        day += dt.timedelta(days=1)
    return None


def clean(body: dict, old: dict | None = None) -> dict:
    """A task from the phone's form, checked; `old` supplies what the edit leaves out. Raises ValueError."""
    task = dict(old or {})
    interval_was = (task.get("repeat"), task.get("every_minutes"))
    for key in ("title", "prompt", "repeat", "time", "date", "weekday", "enabled", "kind", "chat", "project", "every_minutes", "workdir"):
        if key in body:
            task[key] = body[key]
    task["kind"] = task.get("kind") or "task"
    if task["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    task["chat"] = str(task.get("chat") or "")
    task["workdir"] = str(task.get("workdir") or "").strip()  # the folder a run works in; "" is the server's own
    task["prompt"] = str(task.get("prompt") or "").strip()
    if task["kind"] == "checkin":
        task["project"] = str(task.get("project") or "")
        if not task["project"]:
            raise ValueError("a status report needs its project")
    elif not task["prompt"]:
        raise ValueError("a scheduled task needs a prompt")
    task["title"] = (str(task.get("title") or "").strip()[:80] or task["prompt"].splitlines()[0][:60]
                     if task["prompt"] else str(task.get("title") or "").strip()[:80] or "Status report")
    if task.get("repeat") not in REPEATS:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
    if task["repeat"] == "every":
        try:
            task["every_minutes"] = int(task.get("every_minutes"))
        except (TypeError, ValueError):
            raise ValueError("every_minutes must be a number") from None
        if not EVERY_MIN <= task["every_minutes"] <= EVERY_MAX:
            raise ValueError(f"every_minutes must be between {EVERY_MIN} and {EVERY_MAX}")
        if (task["repeat"], task["every_minutes"]) != interval_was or not task.get("anchor"):
            task["anchor"] = time.time()  # counted from when it was set
    else:
        task.pop("every_minutes", None)
        task.pop("anchor", None)
        _clock(task.get("time") or "")
    if task["repeat"] == "once":
        dt.date.fromisoformat(str(task.get("date") or ""))
    if task["repeat"] == "weekly":
        task["weekday"] = int(task.get("weekday", 0)) % 7
    task["enabled"] = bool(task.get("enabled", True))
    task.setdefault("id", secrets.token_hex(4))
    task.setdefault("created", time.time())
    task.setdefault("last_run", 0.0)
    task.setdefault("last_chat", "")
    task["next_run"] = next_run(task, time.time()) if task["enabled"] else None
    if task["next_run"] is None:
        task["enabled"] = False
    return task


class Schedule:
    """The task list on disk. Every method is safe to call from any thread."""

    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.Lock()
        self._tasks: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [t for t in data if isinstance(t, dict) and t.get("id")] if isinstance(data, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tasks, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(t) for t in self._tasks), key=lambda t: (not t.get("enabled"), t.get("next_run") or 0))

    def upsert(self, body: dict) -> dict:
        with self._lock:
            tid = body.get("id")
            idx = next((i for i, t in enumerate(self._tasks) if t["id"] == tid), None) if tid else None
            task = clean(body, self._tasks[idx] if idx is not None else None)
            if idx is None:
                self._tasks.append(task)
            else:
                self._tasks[idx] = task
            self._save()
            return dict(task)

    def delete(self, tid: str) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t["id"] != tid]
            if len(self._tasks) != before:
                self._save()
            return len(self._tasks) != before

    def get(self, tid: str) -> dict | None:
        with self._lock:
            return next((dict(t) for t in self._tasks if t["id"] == tid), None)

    def due(self, now: float | None = None) -> list[dict]:
        """Tasks to run now. Each is moved on to its next time (and saved) before it is handed back, so a
        crash mid-run never fires it twice; one missed by more than CATCH_UP is skipped."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            changed = False
            for t in self._tasks:
                at = t.get("next_run")
                if not t.get("enabled") or not at or at > now:
                    continue
                t["next_run"] = next_run(t, now)
                if t["next_run"] is None:
                    t["enabled"] = False
                changed = True
                if now - at <= CATCH_UP or t.get("kind") == "checkin":
                    t["last_run"] = now
                    out.append({**t, **({"missed": at} if now - at > CATCH_UP else {})})
            if changed:
                self._save()
        return out

    def for_project(self, pid: str) -> dict | None:
        """The project's check-in, if it has one."""
        with self._lock:
            return next((dict(t) for t in self._tasks if t.get("kind") == "checkin" and t.get("project") == pid), None)

    def ran(self, tid: str, chat_id: str) -> None:
        with self._lock:
            for t in self._tasks:
                if t["id"] == tid:
                    t["last_chat"] = chat_id
                    t["last_run"] = time.time()
                    self._save()
                    return
