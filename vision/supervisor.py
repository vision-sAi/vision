"""The supervisor: launches agents for the front end and enforces the policy in code, not in a prompt.

The conversation model never runs anything itself. When a request is delegated (by the router, or by
the model asking for it) the supervisor:

- checks the agent and model against the allowlist in `[router.agents]` and the effort against what
  that model supports (nothing is swapped for something else: an unavailable choice is an error the
  user sees);
- builds the worker (the same task-mode brain the voice conversation always used) on a copy of the
  session's brain config, with the approval-only commands in `[router.permissions]` added to its deny
  rules until the user grants one;
- runs the task on a thread with the `[router.limits]` timeout, tracks the run's state
  (queued → starting → running → waiting_for_user | completed | failed | cancelled | timed_out),
  keeps the worker's own session id so a question is answered in the same session, and stops a run
  that keeps asking or has written more than its token budget;
- refuses to launch the same task twice (a retry of the same request returns the run already made);
- writes one line per decision to ~/.local/state/vision/routing.jsonl, with the request redacted.

A result is only ever what the worker's structured result said (vision/delegation.py): a run that
did not return a valid `completed` result is not completed, whatever the worker wrote in prose.
"""
from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from vision.brain import AgentRun, Turn
from vision.config import STATE_DIR, Config
from vision.delegation import failure, task_result, worker_task
from vision.routing import Route, agent_for_model, agent_model, agent_names, redact

ROUTING_LOG = STATE_DIR / "routing.jsonl"
STATES = ("queued", "starting", "running", "waiting_for_user", "completed", "failed", "cancelled", "timed_out")
FINAL = ("completed", "failed", "cancelled", "timed_out")
_YES = re.compile(r"^\s*(?:yes|yep|yeah|yup|sure|ok|okay|go ahead|go on|do it|approved?|fine|please do|allowed?|granted|y)\b", re.IGNORECASE)
_RULE = re.compile(r"^Bash\((.*?)(?::\*)?\)$")
RESUME_MESSAGE = ("Vision restarted while you were working, and your last step may have been cut off. Check the state "
                  "of your work (files, builds, anything half-done), carry on with the task, then return the result.")


class SupervisorError(RuntimeError):
    """A launch the policy refuses. The message is shown to the user as it is."""


@dataclass
class Run:
    id: str
    agent: str  # allowlist name ("opus", "codex"), or "" for the session's own brain (legacy path)
    model: str
    effort: str
    task: dict  # the validated task envelope (the worker never sees model/effort)
    key: str  # what makes two launches the same request
    state: str = "queued"
    worker: object = None
    session_id: str | None = None
    result: dict | None = None  # the last validated worker result
    question: str | None = None  # the agent's question while waiting_for_user
    error: str = ""
    rounds: int = 0  # answers relayed so far
    output_tokens: int = 0
    granted: list[str] = field(default_factory=list)  # approval rules the user lifted for this run
    denied: list[str] = field(default_factory=list)  # the worker's deny rules right now
    row: AgentRun | None = None  # the UI's view of the run
    started: float = 0.0
    finished: float = 0.0
    history: list[dict] = field(default_factory=list)  # state changes, for get_agent_status

    @property
    def final(self) -> bool:
        return self.state in FINAL

    def status(self) -> dict:
        """What `get_agent_status` returns: the state and the facts, never prose from the worker."""
        return {"run_id": self.id, "agent": self.agent, "model": self.model, "effort": self.effort, "state": self.state,
                "question": self.question, "error": self.error, "rounds": self.rounds,
                "result": self.result, "seconds": round((self.finished or time.monotonic()) - self.started, 1) if self.started else 0.0}


def _set(run: Run, state: str) -> None:
    assert state in STATES, state
    run.state = state
    run.history.append({"state": state, "at": time.time()})
    if state in FINAL:
        run.finished = time.monotonic()


def _prefix(rule: str) -> str:
    m = _RULE.match(rule.strip())
    return m.group(1) if m else ""


class Supervisor:
    """One per conversation. `worker_factory(model, effort, denied)` returns a task-mode brain
    (the conversation's `_task_worker`); the supervisor never builds a brain itself."""

    def __init__(self, cfg: Config, worker_factory: Callable, log_path=None):
        self.cfg = cfg
        self.factory = worker_factory
        self.log_path = ROUTING_LOG if log_path is None else log_path
        self.runs: dict[str, Run] = {}
        self._by_key: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._running: dict[str, Run] = {}  # runs executing right now, in launch order (several may at once)
        self.timeout_s: float = 0.0  # each step's limit when set; else [router.limits] timeout_s

    @property
    def active(self) -> Run | None:
        """The run launched most recently of those executing now."""
        with self._lock:
            return next(reversed(self._running.values()), None)

    def running(self) -> list[Run]:
        with self._lock:
            return list(self._running.values())

    # -- validation -----------------------------------------------------------------
    def resolve(self, agent: str | None, model: str | None, effort: str) -> tuple[str, str, str]:
        """(agent, model, effort) as they will run, or SupervisorError. An agent name maps to its
        model; a bare model must belong to an allowlisted agent; the effort must be one the model
        supports (a top tier included). Nothing is coerced."""
        from vision.models import find, model_label, supports_effort

        rc = self.cfg.router
        allow = agent_names(rc)
        name = (agent or "").strip().lower()
        alias = (model or "").strip().lower()
        if name:
            alias = agent_model(rc, name)
            if not alias:
                raise SupervisorError(f"{agent!r} is not an agent Vision may launch; the allowlist is " + (", ".join(allow) or "empty"))
        elif alias:
            name = agent_for_model(rc, alias)
            if not name:
                raise SupervisorError(f"{model!r} is not on the agent allowlist ({', '.join(allow) or 'empty'})")
        else:
            raise SupervisorError("no agent named")
        info = find(alias)
        if info is None:
            raise SupervisorError(f"{name} is configured to run on {alias!r}, which is not a model Vision knows")
        level = (effort or "").strip().lower()
        if level and not supports_effort(alias, level):
            levels = ", ".join(info.levels()) or "none"
            raise SupervisorError(f"{model_label(alias)} does not support effort {level!r} (it supports: {levels}); nothing was launched")
        if not level and info.efforts:
            level = info.resting_effort()
        self._available(info.provider, alias)
        return name, alias, level

    @staticmethod
    def _available(provider: str, alias: str) -> None:
        """The provider must be usable now: its CLI on PATH, or the local server configured. A
        missing one is an error, never a fallback to another brain."""
        from vision import clis
        from vision.models import model_label
        from vision.providers import cap

        if not cap(provider, "cli"):
            return  # no CLI to find (a local server is checked when it's asked)
        try:
            clis.find_cli(provider)
        except Exception as e:  # noqa: BLE001  (that provider's own "not installed" message)
            raise SupervisorError(f"{model_label(alias)} is unavailable: {e}") from e

    # -- launching -----------------------------------------------------------------------
    def existing(self, key: str) -> Run | None:
        """The run already made for this request, unless it has finished."""
        with self._lock:
            run = self._by_key.get(key)
        return run if run is not None and not run.final else None

    def launch(self, task: dict, *, agent: str | None = None, model: str | None = None, effort: str = "", key: str = "",
               allowlist: bool = True, row: AgentRun | None = None, on_agent=None, on_question=None, on_tool=None,
               on_status=None, channel: str = "voice", approval: bool = False, factory: Callable | None = None,
               session_id: str | None = None, run_id: str = "", opening: dict | None = None) -> Run:
        """Start an agent on a task and run it to its first stop (a result, a question, a failure, the
        timeout). `allowlist=False` is the legacy path: the session's own brain, whatever it is, as the
        voice conversation always ran (the router is off or auditing); `approval=True` keeps the
        approval-only rules on it all the same (a project thread runs on the project's own pick of model).
        `factory` builds this run's worker instead of the supervisor's own (a thread in its own folder).
        `session_id` picks up a worker session from before a restart: the worker is told Vision restarted
        rather than given the task again, or gets `opening` (an answer, a follow-up) when there is one.
        Same `key` while a run is alive → that run, no second launch."""
        key = key or json.dumps(worker_task(task), sort_keys=True)
        dup = self.existing(key)
        if dup is not None:
            self.log("duplicate", run=dup, note="the same task was asked for again while the run is alive")
            return dup
        if allowlist:
            agent, model, effort = self.resolve(agent, model, effort)
        else:
            agent = agent or ""
        run = Run(run_id or uuid.uuid4().hex[:12], agent, model or "", effort or "", task, key, row=row)
        with self._lock:
            self.runs[run.id] = run
            self._by_key[key] = run
        _set(run, "starting")
        run.started = time.monotonic()
        try:
            run.denied = list(self.cfg.router.approval) if allowlist or approval else []
            run.worker = (factory or self.factory)(run.model or None, run.effort or None, run.denied)
            if session_id:
                run.worker.resume(session_id)
                run.session_id = session_id
        except SupervisorError:
            raise
        except Exception as e:  # noqa: BLE001
            run.error = str(e)
            _set(run, "failed")
            run.result = failure(str(e))
            self.log("launch_failed", run=run, note=str(e))
            raise SupervisorError(f"could not start {agent or 'the worker'}: {e}") from e
        wcfg = getattr(run.worker, "cfg", None)
        if isinstance(getattr(wcfg, "model", None), str):
            run.model, run.effort = wcfg.model, wcfg.effort or ""  # as fitted to the model (legacy path)
        if row is not None:
            row.model, row.effort = run.model, run.effort
        if run.state == "cancelled":  # stopped while its worker was being built
            return run
        self.log("resume" if session_id else "launch", run=run, channel=channel)
        prompt = json.dumps(opening if session_id and opening else
                            {"type": "vision_task_resume", "message": RESUME_MESSAGE} if session_id else
                            {"type": "vision_task", "task": worker_task(task)})
        self._execute(run, prompt, on_agent=on_agent, on_question=on_question, on_tool=on_tool, on_status=on_status)
        return run

    def continue_run(self, run_id: str, message: str, *, on_agent=None, on_question=None, on_tool=None, on_status=None) -> Run:
        """More work for a run that has stopped (finished, failed, timed out or cancelled), sent into the
        same worker session so it carries on from what it knows: how a project thread takes a follow-up.
        A run waiting for an answer gets `message` as its answer instead. Each continuation starts with
        fresh rounds and a fresh token budget."""
        run = self.runs.get(run_id)
        if run is None:
            raise SupervisorError(f"no run {run_id}")
        if run.state == "waiting_for_user":
            return self.answer(run_id, message, on_agent=on_agent, on_question=on_question, on_tool=on_tool, on_status=on_status)
        if not run.final:
            raise SupervisorError(f"the {run.agent or 'worker'} run is still {run.state.replace('_', ' ')}")
        if run.worker is None:
            raise SupervisorError("that run has no worker session to continue")
        run.rounds = run.output_tokens = 0
        run.error, run.question = "", None
        run.started, run.finished = time.monotonic(), 0.0
        self.log("continue", run=run)
        self._execute(run, json.dumps({"type": "vision_task_continue", "message": message}),
                      on_agent=on_agent, on_question=on_question, on_tool=on_tool, on_status=on_status)
        return run

    def answer(self, run_id: str, text: str, *, on_agent=None, on_question=None, on_tool=None, on_status=None) -> Run:
        """The user's answer to a waiting run, sent back into the same worker session. A `yes` to a
        question about an approval-only command lifts that rule for the rest of the run; only this
        method, fed by the user's own words, ever grants one."""
        run = self.runs.get(run_id)
        if run is None:
            raise SupervisorError(f"no run {run_id}")
        if run.state != "waiting_for_user":
            raise SupervisorError(f"the {run.agent or 'worker'} run is {run.state.replace('_', ' ')}, not waiting for an answer")
        rc = self.cfg.router
        if run.rounds + 1 > rc.max_rounds:
            run.error = f"stopped: it asked {run.rounds} times (the limit is {rc.max_rounds})"
            _set(run, "failed")
            run.result = failure(run.error)
            self.log("too_many_rounds", run=run)
            return run
        if run.output_tokens > rc.max_output_tokens:
            run.error = f"stopped: it has written {run.output_tokens:,} tokens (the budget is {rc.max_output_tokens:,})"
            _set(run, "failed")
            run.result = failure(run.error)
            self.log("over_budget", run=run)
            return run
        granted = self.grants(run, text)
        if granted:
            run.granted += granted
            run.denied = [d for d in run.denied if d not in granted]
            wcfg = getattr(run.worker, "cfg", None)
            if wcfg is not None and hasattr(wcfg, "denied_tools"):
                wcfg.denied_tools = [d for d in wcfg.denied_tools if d not in granted]
            self.log("granted", run=run, note=", ".join(granted))
        run.rounds += 1
        run.question = None
        prompt = json.dumps({"type": "vision_task_answer", "run_id": run.id, "answer": text,
                             "granted": granted, "still_denied": run.denied})
        self.log("answer", run=run)
        self._execute(run, prompt, on_agent=on_agent, on_question=on_question, on_tool=on_tool, on_status=on_status)
        return run

    def grants(self, run: Run, text: str) -> list[str]:
        """The approval rules a `yes` from the user lifts: those whose command the pending question
        names. A `no`, or a question that names none, grants nothing."""
        if not run.question or not _YES.match(text or ""):
            return []
        q = run.question.lower()
        return [rule for rule in run.denied if (p := _prefix(rule)) and p.lower() in q]

    # -- running -------------------------------------------------------------------------
    def _execute(self, run: Run, prompt: str, *, on_agent, on_question, on_tool, on_status) -> None:
        from vision.models import model_label

        worker = run.worker
        label = model_label(run.model) or run.model or "the worker"
        rc = self.cfg.router
        _set(run, "running")
        with self._lock:
            self._running[run.id] = run
        if on_status:
            on_status(f"{label} is starting…")
        box: dict = {}

        def go() -> None:
            try:
                box["turn"] = worker.ask(prompt, on_question=on_question, on_tool=on_tool)
            except BaseException as e:  # noqa: BLE001  (a worker bug must still finish the run)
                box["exc"] = e

        limit = self.timeout_s or rc.timeout_s
        t = threading.Thread(target=go, daemon=True, name=f"agent-{run.id}")
        t.start()
        t.join(limit)
        timed_out = t.is_alive()
        if timed_out:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
            t.join(5)
        with self._lock:
            self._running.pop(run.id, None)
        turn: Turn | None = box.get("turn")
        if timed_out:
            run.error = f"timed out after {limit:.0f} s"
            run.result = failure(run.error)
            _set(run, "timed_out")
        elif "exc" in box:
            run.error = str(box["exc"])
            run.result = failure(run.error)
            _set(run, "failed")
        else:
            assert turn is not None
            run.session_id = turn.session_id or getattr(worker, "session_id", None) or run.session_id
            if turn.usage:
                run.output_tokens += int(turn.usage.get("output_tokens") or 0)
            result = task_result(turn)
            run.result = result
            if turn.is_error and turn.error == "cancelled":
                run.error = "cancelled"
                _set(run, "cancelled")
            elif result["status"] == "completed":
                _set(run, "completed")
            elif result["status"] == "needs_input":
                run.question = (result.get("question") or "").strip() or result["summary"].strip() or "The agent needs more information."
                _set(run, "waiting_for_user")
            else:  # failed, blocked, or an unconfirmed result
                run.error = result["summary"]
                _set(run, "failed")
        if run.row is not None:
            row = run.row
            row.done = run.final or run.state == "waiting_for_user"
            row.failed = run.state in ("failed", "timed_out", "cancelled")
            row.cut_off = run.state in ("timed_out", "cancelled")
            if turn is not None:
                row.usage = turn.usage
                row.tool_uses = len(turn.tools_used)
            row.duration_ms = int(((run.finished or time.monotonic()) - run.started) * 1000)
            row.details = report_lines(run)
            if on_agent:
                on_agent(row)
        if on_status:
            on_status("")
        self.log(run.state, run=run)

    def cancel(self, run_id: str | None = None) -> Run | None:
        """Stop a run: the one named, else the one executing, else the one waiting for an answer."""
        run = self.runs.get(run_id) if run_id else (self.active or self.waiting())
        if run is None:
            return None
        if run.worker is not None and run.state == "running":
            try:
                run.worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        if not run.final:
            run.error = "cancelled"
            run.result = failure("Cancelled by the user; anything it had done stands as it is.")
            run.question = None
            _set(run, "cancelled")
            if run.row is not None:
                run.row.done = run.row.failed = run.row.cut_off = True
                run.row.details = report_lines(run)
            self.log("cancelled", run=run)
        return run

    def steer(self, text: str) -> bool:
        """Pass what the user said mid-reply into every run working right now, as a task update each
        folds into its own task at its next step (one it doesn't concern carries on). False when nothing
        is running or no running brain can take one (a local worker); the conversation model still hears
        it either way."""
        heard = False
        for run in self.running():
            steer = getattr(run.worker, "steer", None) if run.state == "running" else None
            if steer is None:
                continue
            try:
                ok = bool(steer(json.dumps({"type": "vision_task_update", "message": text})))
            except Exception:  # noqa: BLE001  (a worker that can't take it now just misses the update)
                ok = False
            if ok:
                self.log("steered", run=run, text=text)
                heard = True
        return heard

    def steer_one(self, run_id: str, text: str) -> bool:
        """`steer` for one run alone (a message to one project thread). False when it isn't working now
        or its brain can't take a message mid-run."""
        run = self.runs.get(run_id)
        steer = getattr(run.worker, "steer", None) if run is not None and run.state == "running" else None
        if steer is None:
            return False
        try:
            ok = bool(steer(json.dumps({"type": "vision_task_update", "message": text})))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self.log("steered", run=run, text=text)
        return ok

    def tell_peers(self, objective: str) -> None:
        """Tell every run at work that another agent has started in the same folder, so each keeps off
        the other's files and builds. One that can't take a message mid-run just misses it."""
        for run in self.running():
            steer = getattr(run.worker, "steer", None) if run.state == "running" else None
            if steer is None:
                continue
            try:
                steer(json.dumps({"type": "vision_peer_started", "objective": objective}))
            except Exception:  # noqa: BLE001
                pass

    def waiting(self) -> Run | None:
        """The run waiting for the user's answer (the most recent one, if several)."""
        for run in reversed(list(self.runs.values())):
            if run.state == "waiting_for_user":
                return run
        return None

    def failed_before(self, key: str, effort: str) -> bool:
        """A run of the same request already failed (or timed out) at this effort: the router then
        picks the high effort for the retry."""
        for run in self.runs.values():
            if run.key == key and run.state in ("failed", "timed_out") and run.effort == effort:
                return True
        return False

    def status(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise SupervisorError(f"no run {run_id}")
        return run.status()

    # -- the log -------------------------------------------------------------------------
    def log(self, event: str, *, run: Run | None = None, route: Route | None = None, channel: str = "", mode: str = "",
            note: str = "", text: str = "") -> None:
        """One JSON line per event. The request is redacted and cut short; worker output is never
        logged, only states, choices and reasons."""
        entry: dict = {"at": time.time(), "event": event}
        if channel:
            entry["channel"] = channel
        if mode:
            entry["mode"] = mode
        if route is not None:
            entry.update({"route": route.kind, "agent": route.agent, "effort": route.effort, "explicit": route.explicit,
                          "reason": route.reason, "signals": list(route.signals)})
            text = text or route.text
        if run is not None:
            entry.update({"run": run.id, "agent": run.agent, "model": run.model, "effort": run.effort, "state": run.state,
                          "rounds": run.rounds, "output_tokens": run.output_tokens})
            if run.error:
                entry["error"] = redact(run.error, 200)
            if run.granted:
                entry["granted"] = run.granted
        if text:
            entry["text"] = redact(text)
        if note:
            entry["note"] = redact(note, 200)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def report_lines(run: Run) -> list[str]:
    """What the UI shows under the agent's row once it stops: the state, then the result's facts."""
    from vision.models import model_label

    what = f"{model_label(run.model) or run.model or 'worker'} · {run.effort or 'default effort'}".strip()
    lines = [f"{run.state.replace('_', ' ')} · {what}"]
    r = run.result or {}
    if run.state == "waiting_for_user" and run.question:
        lines.append("asks: " + run.question.splitlines()[0][:200])
        return lines
    if run.error and run.state in ("failed", "timed_out", "cancelled"):
        lines.append(run.error.splitlines()[0][:200])
    elif r.get("summary"):
        lines.append(r["summary"].strip().splitlines()[0][:200])
    for label, key in (("changed", "changes"), ("checks", "checks"), ("findings", "findings")):
        items = [str(x).strip() for x in (r.get(key) or []) if str(x).strip()]
        if items:
            lines.append(f"{label}: " + "; ".join(items)[:300])
    if r.get("status") in ("blocked", "needs_input") and r.get("question") and run.state != "waiting_for_user":
        lines.append("open: " + str(r["question"]).splitlines()[0][:200])
    return lines


def progress_label(agent: str, call) -> str:
    """A verified state for the live line, from the tool the agent just called: `Opus 5 is running
    tests…`, `Opus 5 is inspecting the workspace…`. Never a guess: it names what the call does."""
    name = getattr(call, "name", "") or ""
    detail = (getattr(call, "detail", "") or "").lower()
    if name == "Bash" and re.search(r"\b(?:pytest|unittest|npm test|cargo test|go test|jest|vitest|make test|tests?)\b", detail):
        what = "running tests"
    elif name == "Bash" and re.search(r"\b(?:git (?:commit|push|status|diff|log)|gh )", detail):
        what = "working with git"
    elif name == "Bash":
        what = "running a command"
    elif name in ("Read", "Glob", "Grep", "LS", "ls"):
        what = "inspecting the workspace"
    elif name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        what = "editing files"
    elif name in ("WebSearch", "WebFetch"):
        what = "searching the web"
    elif name in ("Agent", "Task"):
        what = "delegating to a subagent"
    elif name == "AskUserQuestion":
        what = "asking a question"
    else:
        what = f"using {name}" if name else "working"
    return f"{agent} is {what}…"


def announce(run_model: str, effort: str) -> str:
    """`Launching Opus 5 · medium effort`, the line shown before an agent starts."""
    from vision.models import model_label

    return f"Launching {model_label(run_model) or run_model}" + (f" · {effort} effort" if effort else "")


def worker_config(base, model: str | None, effort: str | None, denied: list[str] | None):
    """The launched worker's config: a copy of the session's brain config with the chosen model and
    effort, and the approval-only rules added to its deny list (the base rules stay)."""
    extra = [d for d in (denied or []) if d not in base.denied_tools]
    changes = {"denied_tools": list(base.denied_tools) + extra}
    if model:
        changes["model"] = model
    if effort:
        changes["effort"] = effort
    cfg = dataclasses.replace(base, **changes)
    cfg.approval_rules = list(denied or [])  # read by worker_prompt: the rules the user can lift
    return cfg
