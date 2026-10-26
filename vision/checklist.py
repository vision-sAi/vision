"""The model's own task list, drawn under the live line the way Claude Code draws its todos:
`■ Adding scheduled backups` for the one at work, `□` for those to come, the finished ones folded
into `… +2 completed`. Every brain reports it differently, so each feeds the same Checklist:

- Claude: `TodoWrite` hands over the whole list each time; the newer `TaskCreate` / `TaskUpdate`
  build it up one task at a time (the new task's id only comes back in the result).
- Codex: `turn/plan/updated` carries the whole plan (`step`, `status` in camelCase).
- ACP (Grok and the rest): a `plan` session update carries the whole list (`content`, `status`).

The list belongs to the conversation, not the turn: Claude ticks off tasks it made three turns ago.
checklist_of() keeps one per brain and starts over when the brain moves to another session."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

PENDING, ACTIVE, DONE = "pending", "in_progress", "completed"
TODO_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate")  # their tool rows are not drawn: the checklist says it
_STATUS = {"pending": PENDING, "inprogress": ACTIVE, "active": ACTIVE, "completed": DONE, "complete": DONE, "done": DONE}


@dataclass
class Task:
    id: str
    text: str  # what it is: "Add scheduled backups"
    active: str = ""  # what it says while at work: "Adding scheduled backups" (Claude's activeForm)
    status: str = PENDING


def _status(value) -> str:
    """`in_progress`, `inProgress`, `completed`…: one of PENDING, ACTIVE, DONE."""
    return _STATUS.get(str(value or "").replace("_", "").replace("-", "").lower(), PENDING)


class Checklist:
    def __init__(self):
        self.tasks: list[Task] = []
        self.session: str | None = None
        self._lock = threading.Lock()  # the brain's reader thread writes, the screen's repaint reads

    def items(self) -> list[Task]:
        """A copy to draw from; empty once everything is done (Claude Code drops a finished list)."""
        with self._lock:
            tasks = list(self.tasks)
        return tasks if any(t.status != DONE for t in tasks) else []

    def active(self) -> Task | None:
        return next((t for t in self.items() if t.status == ACTIVE), None)

    def replace(self, entries: list[tuple[str, str, str]]) -> None:
        """The whole list at once: [(text, status, active text)]."""
        with self._lock:
            self.tasks = [Task(str(i + 1), text, active, _status(status)) for i, (text, status, active) in enumerate(entries) if text]

    def add(self, task_id: str, text: str, active: str = "") -> None:
        with self._lock:
            if not any(t.id == task_id for t in self.tasks):
                self.tasks.append(Task(task_id, text, active))

    def update(self, task_id: str, status: str = "", text: str = "", active: str = "") -> None:
        with self._lock:
            for t in self.tasks:
                if t.id == task_id:
                    if status == "deleted":
                        self.tasks.remove(t)
                    else:
                        t.status = _status(status) if status else t.status
                        t.text = text or t.text
                        t.active = active or t.active
                    return

    def claude_call(self, name: str, args: dict, result=None, output: str = "") -> None:
        """A finished TodoWrite / TaskCreate / TaskUpdate call of Claude's: `args` its input, `result`
        the structured tool result (TaskCreate's `{"task": {"id": "3", …}}`), `output` its text."""
        if not isinstance(args, dict):
            return
        if name == "TodoWrite":
            todos = args.get("todos") or []
            self.replace([(t.get("content") or t.get("subject") or "", t.get("status"), t.get("activeForm") or "")
                          for t in todos if isinstance(t, dict)])
        elif name == "TaskCreate":
            task = (result or {}).get("task") if isinstance(result, dict) else None
            task_id = str(task.get("id")) if isinstance(task, dict) and task.get("id") is not None else ""
            if not task_id:
                m = re.search(r"#(\d+)", output or "")
                task_id = m.group(1) if m else ""
            if task_id:
                self.add(task_id, args.get("subject") or "", args.get("activeForm") or "")
        elif name == "TaskUpdate" and args.get("taskId") is not None:
            self.update(str(args["taskId"]), args.get("status") or "", args.get("subject") or "", args.get("activeForm") or "")

    def codex_plan(self, plan) -> None:
        """Codex's `turn/plan/updated`: [{"step": …, "status": "pending" | "inProgress" | "completed"}]."""
        self.replace([(p.get("step") or "", p.get("status"), "") for p in plan or [] if isinstance(p, dict)])

    def acp_plan(self, entries) -> None:
        """An ACP `plan` update: [{"content": …, "status": "pending" | "in_progress" | "completed"}]."""
        self.replace([(e.get("content") or "", e.get("status"), "") for e in entries or [] if isinstance(e, dict)])


def checklist_of(brain) -> Checklist:
    """The brain's checklist, fresh when it is on another session than the list was made in (a new
    chat, a resumed one). A brand-new session has no id until its first turn; the list made in that
    turn keeps going once it gets one."""
    cl = getattr(brain, "_checklist", None)
    if cl is None:
        cl = Checklist()
        try:
            brain._checklist = cl
        except AttributeError:
            return cl
    sid = getattr(brain, "session_id", None)
    if sid != cl.session:
        if cl.session is not None:
            with cl._lock:
                cl.tasks = []
        cl.session = sid
    return cl
