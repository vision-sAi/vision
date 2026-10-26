"""Project threads: work that belongs to a project, not to a turn.

A thread is one supervised worker (supervisor.py) with its own provider session. It starts outside any
chat turn, keeps going while nobody is looking, and can be handed more work after it stops: while its
session is still light the follow-up goes into it, so it carries on from what it already knows.

Each project keeps its threads in <project folder>/threads.json:
{id, project, title, objective, state, queued, edits, run_id, provider, model, effort, session_id,
workdir, branch, worktree, created, updated, activity, tools, last_result, question, error, runs: [RUN],
tokens_out, context_tokens (its context when the last go ended), brief (the context it started with), note,
follows / followed_by (the closed thread it carries on from, the one that carried it on), landed (when it was),
allowed (calls the user approved, each let through once: a scout's approvals, projects.py)}
A RUN is one start, continuation or resume: {at, kind, status, summary, findings, changes, duration_s,
tokens_out}, the newest last and at most RUN_LOG of them.

Usage: a go runs to the project's `length` limits (LENGTHS): told to wrap up, then stopped, which leaves it
ready for review when it has committed something. A follow-up to a thread whose context grew past
FRESH_CONTEXT, whose session has had SESSION_GOES goes, or that has sat STALE_S starts a fresh session with
a short handover (its brief, latest result, earlier goes, files touched) instead of rereading the old one.
A closed thread isn't reopened: a message to it starts a new thread (`follows` it) with that handover, and
`sweep()` closes threads that finished without changes and sat CLOSE_IDLE_S.

States: working (a worker is at it, or `queued` for a slot), waiting_on_you (a question or an approval),
ready_for_review (finished with changes), landing (its branch being merged into the project's folder),
idle (finished without changes, failed or stopped: it can be continued), resolved (closed by the user,
landed or swept; a message starts a new thread after it).

When a thread stops it's marked `reported: false` until the project's coordinator has had a report turn
about it (server.py), so a report owed when Vision stops is still made after the restart. A result's
`decisions` go into the project memory (projects.py), and its `artifacts` into the project's Library. Every
change of state is a line in the project's activity log (projects.log_activity), for the apps' scout page.

Pausing a project (`pause`) stops its workers and leaves their threads `working` and `paused`; nothing
starts until `resume`, which carries each on in its own session.

With the project's `isolation = "worktree"` and a folder in a git repo, a thread that may edit gets a
git worktree of its own on a branch of its own (vision/<project>/<thread>), so threads never trip over
each other's files; read-only threads share the project's folder. A worktree is only removed when its
thread is resolved with nothing in it that isn't on another branch already.

A restart doesn't wait for threads (they may run for hours): `suspend()` stops the workers and leaves
their records `working`, and the next process's `restore()` picks each up in its own session.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from vision.config import DATA_DIR

WORKTREE_DIR = DATA_DIR / "worktrees"
STATES = ("working", "waiting_on_you", "ready_for_review", "landing", "idle", "resolved")
RUN_LOG = 30
STEP_TIMEOUT_S = 4 * 3600  # one start or follow-up; a voice agent's 30 minutes is too short for background work
SUMMARY_CHARS = 300
FINDING_CHARS = 200
PAUSED_MESSAGE = ("The user paused the project while you were working and has now resumed it. Check where your work "
                  "stands (a command may have been cut off), carry on with the task, then return the result.")
# How far one go of a thread may run, by the project's `length` setting: (minutes, tool calls) before it's
# told to wrap up, then (minutes, tool calls) before it's stopped. Long goes are what eat a plan: every
# call rereads the whole conversation, so the hundredth costs far more than the first.
LENGTHS = {"short": ((15, 80), (25, 120)), "normal": ((30, 150), (45, 220)), "long": ((90, 400), (120, 550))}
# A follow-up starts a fresh session, handed what the thread did, once the last go ended with more than
# this in its context: carrying a bloated conversation on is the other way a thread eats a plan.
FRESH_CONTEXT = 120_000
# So does one whose session has had SESSION_GOES goes already, or has sat STALE_S since its last (its prompt
# cache long gone, carrying on would reread all of it at full price). An answer to the thread's own question
# always goes into the session that asked it.
SESSION_GOES = 6
STALE_S = 12 * 3600
# A thread that finished without changes (a look, an answer) and has sat this long is closed, not kept
# open to be reused; a message to a closed thread starts a new one, handed what the old one did.
CLOSE_IDLE_S = 24 * 3600
HANDOVER_FILES = 12
WRAP_MESSAGE = ("You are near the limit for this go. Stop starting new work: finish the step you're on, commit what "
                "works, and return your result now, with anything left undone said plainly.")
DIFF_FILES = 200
PATCH_CHARS = 60_000


def make_worker(base, workdir: str, model: str | None, effort: str | None, denied: list[str] | None, note: str = ""):
    """A task-mode worker brain on a copy of `base` (a BrainConfig): the model and effort asked for, the
    approval-only rules on its deny list, `note` added to its prompt, working in `workdir`. The voice
    conversation's supervised runs and project threads both get theirs here."""
    from vision.brain import create_brain
    from vision.supervisor import worker_config

    cfg = worker_config(base, model, effort, denied)
    if note:
        cfg.thread_note = note
    worker = create_brain(cfg, voice_mode=False)
    worker.task_mode = True
    worker.workdir = workdir
    return worker


def thread_state(run) -> str:
    """A thread's state from its supervisor run's."""
    if run.state in ("queued", "starting", "running"):
        return "working"
    if run.state == "waiting_for_user":
        return "waiting_on_you"
    if run.state == "completed" and (run.result or {}).get("changes"):
        return "ready_for_review"
    return "idle"


def session_goes(thread: dict) -> int:
    """The goes a thread's current session has had: its runs back to the newest that began a session."""
    n = 0
    for r in reversed(thread.get("runs") or []):
        n += 1
        if r.get("kind") == "start":
            break
    return n


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n].rstrip("-") or "work"


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)


def git_root(path: str) -> str:
    """The top of the git repo `path` is in, "" when it isn't in one with a commit to branch from."""
    try:
        top = _git("rev-parse", "--show-toplevel", cwd=path)
        head = _git("rev-parse", "--verify", "HEAD", cwd=path)
    except (OSError, subprocess.SubprocessError):
        return ""
    return top.stdout.strip() if top.returncode == 0 and head.returncode == 0 else ""


def add_worktree(workdir: str, dest: Path, branch: str) -> tuple[str, str]:
    """A worktree of `workdir`'s repo at `dest` on a new `branch` from HEAD. Returns (the folder to work
    in: the same place inside the repo as `workdir`, the branch). Raises RuntimeError."""
    root = git_root(workdir)
    if not root:
        raise RuntimeError("not a git repo with a commit")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = _git("worktree", "add", "-b", branch, str(dest), "HEAD", cwd=root)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout).strip() else "git worktree add failed")
    rel = os.path.relpath(os.path.realpath(workdir), os.path.realpath(root))
    return (str(dest) if rel == "." else str(dest / rel)), branch


def remove_worktree(worktree: str, branch: str) -> str:
    """Remove a thread's worktree and branch when nothing in them would be lost: no uncommitted changes
    and no commits that no other branch has. Returns "" when removed, else why it was kept."""
    if not worktree or not os.path.isdir(worktree):
        return ""
    try:
        dirty = _git("status", "--porcelain", cwd=worktree)
        if dirty.returncode != 0:
            return "git couldn't read it"
        if dirty.stdout.strip():
            return "it has uncommitted changes"
        if commits_ahead(worktree, branch):
            return f"{branch} has commits that aren't merged anywhere"
        root = _git("rev-parse", "--git-common-dir", cwd=worktree).stdout.strip()
        main = str(Path(worktree, root).resolve().parent) if root else ""
        if _git("worktree", "remove", worktree, cwd=main or worktree).returncode != 0:
            return "git wouldn't remove it"
        if branch and main:
            _git("branch", "-D", branch, cwd=main)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return str(e)
    return ""


def merge_branch(workdir: str, branch: str, worktree: str = "") -> str:
    """Merge `branch` into whatever `workdir`'s repo has checked out (a merge commit, so it can be undone
    in one step). Refuses when either side has uncommitted changes; a conflict is aborted, leaving
    everything as it was. Returns "" when merged, else why not. Raises RuntimeError for no repo."""
    root = git_root(workdir)
    if not root:
        raise RuntimeError("the project's folder isn't a git repo")
    if worktree and os.path.isdir(worktree):
        mine = _git("status", "--porcelain", "--untracked-files=no", cwd=worktree)
        if mine.returncode == 0 and mine.stdout.strip():
            return "the thread has uncommitted changes in its worktree: tell it to commit them first"
    dirty = _git("status", "--porcelain", "--untracked-files=no", cwd=root)
    if dirty.returncode != 0:
        return "git couldn't read the project's folder"
    if dirty.stdout.strip():
        return "the project's folder has uncommitted changes: commit or stash them first"
    if _git("rev-parse", "--verify", branch, cwd=root).returncode != 0:
        return f"there's no branch {branch}"
    base = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip() or "HEAD"
    ahead = _git("rev-list", "--count", f"HEAD..{branch}", cwd=root)
    if ahead.returncode == 0 and int(ahead.stdout.strip() or 0) == 0:
        return f"{branch} has nothing that isn't on {base} already"
    done = _git("merge", "--no-ff", "--no-edit", "-m", f"Merge {branch}", branch, cwd=root)
    if done.returncode != 0:
        conflicts = _git("diff", "--name-only", "--diff-filter=U", cwd=root).stdout.split()
        _git("merge", "--abort", cwd=root)
        if conflicts:
            return (f"it conflicts with {base} in " + ", ".join(conflicts[:8]) + (" and more" if len(conflicts) > 8 else "")
                    + f": tell the thread to merge {base} into its branch and resolve them")
        return (done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout).strip() else "git merge failed"
    return ""


def commits_ahead(worktree: str, branch: str) -> int:
    """Commits on a thread's `branch` that no other branch has: what it has committed of its own."""
    if not worktree or not branch or not os.path.isdir(worktree):
        return 0
    try:
        others = [b for b in _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=worktree).stdout.split() if b != branch]
        ahead = _git("rev-list", "--count", branch, *[f"^{b}" for b in others], cwd=worktree)
        return int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def branch_diff(workdir: str, branch: str, path: str = "") -> dict:
    """What landing `branch` would bring into `workdir`'s checked-out branch: {base, commits, files:
    [{path, added, removed}] (binary files count -1), more: files left out past DIFF_FILES}; with `path`,
    that one file's patch too ({patch, cut}). Raises RuntimeError when there's no repo or branch."""
    root = git_root(workdir)
    if not root:
        raise RuntimeError("the project's folder isn't a git repo")
    if _git("rev-parse", "--verify", branch, cwd=root).returncode != 0:
        raise RuntimeError(f"there's no branch {branch}")
    base = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip() or "HEAD"
    span = f"HEAD...{branch}"  # since they parted: what the branch adds, not what the base has moved on by
    commits = _git("rev-list", "--count", f"HEAD..{branch}", cwd=root)
    out: dict = {"base": base, "commits": int(commits.stdout.strip() or 0) if commits.returncode == 0 else 0, "files": [], "more": 0}
    for line in _git("diff", "--numstat", "-M", span, cwd=root).stdout.splitlines():
        added, removed, name = (line.split("\t", 2) + ["", ""])[:3]
        if " => " in name:  # a rename: {old => new} or old => new; the phone shows where it went
            name = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", name).split(" => ")[-1].replace("//", "/")
        out["files"].append({"path": name, "added": int(added) if added.isdigit() else -1,
                             "removed": int(removed) if removed.isdigit() else -1})
    out["files"].sort(key=lambda f: -(max(f["added"], 0) + max(f["removed"], 0)))
    out["more"] = max(0, len(out["files"]) - DIFF_FILES)
    out["files"] = out["files"][:DIFF_FILES]
    if path:
        patch = _git("diff", "-M", span, "--", path, cwd=root).stdout
        out["patch"], out["cut"] = patch[:PATCH_CHARS], len(patch) > PATCH_CHARS
    return out


def _task_words(text: str) -> str:
    """A message Vision sent a thread's worker, in plain words."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict):
        return text
    kind = data.get("type")
    if kind == "vision_task":
        task = data.get("task") or {}
        parts = [str(task.get("objective") or "").strip()]
        if ctx := str(task.get("context") or "").strip():
            parts.append(ctx)
        return "\n\n".join(p for p in parts if p)
    if kind in ("vision_task_continue", "vision_task_update"):
        return str(data.get("message") or "")
    if kind == "vision_task_answer":
        return str(data.get("answer") or "")
    if kind == "vision_task_resume":
        return "Vision restarted: carry on where you were."
    return ""  # vision_peer_started and the like: Vision's own business


def _result_words(text: str) -> str:
    """A result a thread's worker returned, in plain words."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict) or "summary" not in data:
        return text
    lines = [str(data.get("summary") or "").strip()]
    for label, key in (("Changed", "changes"), ("Checked", "checks"), ("Found", "findings"), ("Decided", "decisions")):
        if items := [str(x) for x in data.get(key) or [] if str(x).strip()]:
            lines.append(f"{label}:\n" + "\n".join(f"- {x}" for x in items))
    if q := str(data.get("question") or "").strip():
        lines.append(f"Asks: {q}")
    return "\n\n".join(x for x in lines if x)


def thread_history(thread: dict, limit: int = 200) -> list[dict]:
    """A thread's conversation as its provider keeps it ({role, text}, oldest first), in plain words: the
    task and messages it was given, the results it returned. [] when its session can't be read."""
    from vision.sessions import session_history

    sid = thread.get("session_id") or ""
    if not sid:
        return []
    try:
        messages = session_history(thread.get("provider") or "claude", sid, 0, include_context=True)
    except Exception:  # noqa: BLE001  (a store that can't be read: the run log tells the story instead)
        return []
    out = []
    for m in messages:
        text = (_task_words if m.get("role") == "user" else _result_words)(str(m.get("text") or "")).strip()
        if text:
            out.append({"role": m.get("role") or "assistant", "text": text})
    return out[-limit:]


class ThreadStore:
    """One project's threads.json. Safe to call from any thread."""

    def __init__(self, folder: Path):
        self.path = folder / "threads.json"
        self._lock = threading.Lock()
        self._threads: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        for t in data if isinstance(data, list) else []:
            if isinstance(t, dict) and t.get("id") and t.get("state") in STATES:
                self._threads[t["id"]] = t

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A temp file of its own each time: two stores on one project (the process before a restart and the
        # one after) saving at once would otherwise rename the same temp file out from under each other.
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(list(self._threads.values()), indent=1))
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def list(self) -> list[dict]:
        """Oldest first, as they were started."""
        with self._lock:
            return sorted((dict(t) for t in self._threads.values()), key=lambda t: t.get("created") or 0)

    def get(self, tid: str) -> dict | None:
        with self._lock:
            t = self._threads.get(tid)
            return dict(t) if t else None

    def find(self, ref: str) -> dict | None:
        """By id or the start of one; ValueError when the start fits several."""
        ref = (ref or "").strip().lower()
        with self._lock:
            if ref in self._threads:
                return dict(self._threads[ref])
            hits = [t for tid, t in self._threads.items() if ref and tid.startswith(ref)]
        if len(hits) > 1:
            raise ValueError(f"{ref!r} fits more than one thread")
        return dict(hits[0]) if hits else None

    def update(self, tid: str, **changes) -> dict:
        with self._lock:
            t = self._threads.setdefault(tid, {"id": tid})
            t.update(changes)
            t["updated"] = time.time()
            self._save()
            return dict(t)

    def log_run(self, tid: str, entry: dict) -> dict:
        with self._lock:
            t = self._threads[tid]
            t["runs"] = (t.get("runs") or [])[-(RUN_LOG - 1):] + [entry]
            t["updated"] = time.time()
            self._save()
            return dict(t)


class ProjectThreads:
    """One project's threads at work. `base()` returns the BrainConfig threads copy (the project chat's,
    else the server's default); `on_change(thread)` is called after every change, from any thread;
    `on_stop(thread)` once a thread stops (finished, failed, asking), for the project chat's note and the
    push. Every launch runs on a daemon thread: nothing here blocks the caller."""

    def __init__(self, cfg, project: dict, *, folder: Path, base: Callable, on_change: Callable, on_stop: Callable,
                 worktrees: Path | None = None):
        from vision.supervisor import Supervisor

        self.cfg = cfg
        self.project = project
        self.folder = folder
        self.store = ThreadStore(folder)
        self.base = base
        self.on_change = on_change
        self.on_stop = on_stop
        self.worktrees = (worktrees or WORKTREE_DIR) / project["id"]
        self.supervisor = Supervisor(cfg, lambda *a: None)  # every launch brings its own factory
        self.supervisor.timeout_s = STEP_TIMEOUT_S
        self._lock = threading.Lock()
        self._live: set[str] = set()  # thread ids with a worker going now
        self._suspended = False  # a restart is under way: a stopping worker leaves its record as it is
        self._held: set[str] = set()  # threads being paused: their stopping worker leaves them `working`, to resume
        self._stopping: set[str] = set()  # threads stopped on purpose: no report, no push, when their worker stops
        self._goes: dict[str, dict] = {}  # thread id → this go's {began, tools, wrapped}, for its length limits
        self._over: dict[str, str] = {}  # threads stopped at their limit → which limit, for their record
        self._kept: dict[str, float] = {}  # sweep: thread id → the run (its `at`) it was last found to have commits at
        self.on_activity: Callable[[dict], None] | None = None  # an event went into the project's activity log
        # A scout's rules (projects.py): (ask, deny) permission rules for its threads, and where a call that asks
        # first files its approval request: (thread id, tool, input, rule) → the approval's id.
        self.rules: Callable[[], tuple[list[str], list[str]]] | None = None
        self.on_approval: Callable[[str, str, dict, str | None], str | None] | None = None
        self._allowed: dict[str, list[str]] = {}  # thread id → calls the user approved once, shared with its live worker
        self._states = {t["id"]: t["state"] for t in self.store.list()}  # as last seen, for the activity log
        self._states_lock = threading.Lock()  # its own: _changed is called with self._lock held at times

    # -- what's there
    def list(self) -> list[dict]:
        return self.store.list()

    def find(self, ref: str) -> dict | None:
        return self.store.find(ref)

    def counts(self) -> dict:
        out = dict.fromkeys(STATES, 0)
        for t in self.store.list():
            out[t["state"]] += 1
        return out

    def tokens_out(self) -> int:
        """Output tokens every thread of the project has written, all told."""
        return sum(int(t.get("tokens_out") or 0) for t in self.store.list())

    def unreported(self) -> list[dict]:
        """Threads that stopped and haven't been reported to the coordinator yet, oldest first."""
        return [t for t in self.store.list() if t.get("reported") is False and t["state"] not in ("working", "landing")]

    def mark_reported(self, ids) -> None:
        for tid in ids:
            if self.store.get(tid) is not None:
                self.store.update(tid, reported=True)

    @property
    def settings(self) -> dict:
        return self.project.get("settings") or {}

    @property
    def limits(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """((minutes, tools) to wrap up at, (minutes, tools) to stop at) for one go, from `length`."""
        return LENGTHS.get(self.settings.get("length") or "normal", LENGTHS["normal"])

    def _changed(self, thread: dict) -> dict:
        self._log_change(thread)
        try:
            self.on_change(thread)
        except Exception:  # noqa: BLE001  (the UI missing a frame never stops a thread)
            pass
        return thread

    def _log_change(self, thread: dict) -> None:
        """A line in the project's activity (projects.log_activity) when the thread changed state."""
        from vision.projects import log_activity, thread_event

        with self._states_lock:
            before = self._states.get(thread["id"])
            self._states[thread["id"]] = thread["state"]
        found = thread_event(before, thread)
        if found is None:
            return
        try:
            event = log_activity(self.folder, *found, thread=thread["id"])
        except OSError:
            return
        if self.on_activity is not None:
            try:
                self.on_activity(event)
            except Exception:  # noqa: BLE001
                pass

    # -- starting
    def start(self, objective: str, *, title: str = "", edits: bool = True, context: str = "", uses: list[str] | None = None,
              follows: str = "") -> dict:
        """A new thread on `objective`. It starts now, or waits for a slot when max_threads are busy (or
        the project is paused). `uses`: earlier threads whose results it builds on (their summaries,
        changes and branches go into its context). `follows`: the closed thread it carries on from."""
        from vision.models import coerce_effort

        objective = objective.strip()
        if not objective:
            raise ValueError("say what the thread is for")
        if self.project.get("status") == "archived":
            raise ValueError("that project is archived")
        earlier = []
        for ref in uses or []:
            if not ref.strip():
                continue
            t = self.store.find(ref)
            if t is None:
                raise ValueError(f"no thread {ref!r} to build on")
            earlier.append(self._handover(t))
        context = "\n\n".join(x for x in (context.strip(), *earlier) if x)
        base = self.base()
        model = self.settings.get("thread_model") or base.model
        effort, _ = coerce_effort(model, self.settings.get("thread_effort") or base.effort)
        from vision.models import provider_for

        tid = uuid.uuid4().hex[:6]
        title = _cut(title.strip() or objective.splitlines()[0].rstrip("."), 60)
        thread = self.store.update(
            tid, project=self.project["id"], title=title, objective=objective, context=context.strip(), state="working",
            queued=True, edits=edits, run_id="", provider=provider_for(model), model=model, effort=effort, session_id="",
            created=time.time(), activity="", tools=0, last_result=None, question="", error="", runs=[], tokens_out=0,
            brief=context.strip()[:2000], **({"follows": follows} if follows else {}), **self._place(title, tid, edits))
        self._changed(thread)
        self._pump()
        return self.store.get(tid) or thread

    def _place(self, title: str, tid: str, edits: bool, suffix: str = "") -> dict:
        """Where a thread works: a worktree of its own when it edits and the project isolates threads,
        else the project's folder."""
        dirs = self.project.get("workdirs") or []
        workdir = dirs[0] if dirs else self.base().workdir or os.getcwd()
        out = {"workdir": workdir, "branch": "", "worktree": "", "note": ""}
        if edits and self.settings.get("isolation", "worktree") == "worktree":
            branch = f"vision/{slug(self.project.get('title', ''), 24)}/{slug(title, 24)}-{tid}{suffix}"
            dest = self.worktrees / f"{tid}{suffix}"
            try:
                out["workdir"], out["branch"] = add_worktree(workdir, dest, branch)
                out["worktree"] = str(dest)
            except RuntimeError as e:
                out["note"] = f"no worktree ({e}): working in the project's folder"
        return out

    @staticmethod
    def _handover(t: dict) -> str:
        """What a thread brought back, for a thread that builds on it: results, never its transcript."""
        return "\n".join([f'Earlier thread "{t.get("title")}" ({t.get("state", "").replace("_", " ")})'
                          + (f", on branch {t['branch']} (merge or read it from there)" if t.get("branch") else "") + ":",
                          f"Objective: {_cut(t.get('objective'), 400)}", *ProjectThreads._result_lines(t.get("last_result") or {})])

    @staticmethod
    def _result_lines(r: dict) -> list[str]:
        lines = [f"Result: {_cut(r['summary'], 600)}"] if r.get("summary") else []
        for label, key, n in (("Changed", "changes", 12), ("Found", "findings", 6), ("Decided", "decisions", 6), ("Produced", "artifacts", 8)):
            if r.get(key):
                lines.append(f"{label}: " + "; ".join(_cut(x, 200) for x in r[key][:n]))
        return lines

    def _files(self, thread: dict) -> list[str]:
        """The files a thread's branch has touched, the most changed first ([] when it has no branch)."""
        dirs = self.project.get("workdirs") or []
        if not thread.get("branch") or not dirs:
            return []
        try:
            return [f["path"] for f in branch_diff(dirs[0], thread["branch"])["files"][:HANDOVER_FILES]]
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return []

    def _fresh(self, thread: dict, why: str, message: str) -> dict:
        """What carries a thread on in a fresh session: no session, and a context of its brief, a short
        handover (latest result, earlier goes, files touched) and what's asked now, never the transcript."""
        lines = [(f"This thread has worked on that objective before, in a session you can't see ({why}, so this is a "
                  "fresh one). Check the folder and its git log for where things stand."),
                 *self._result_lines(thread.get("last_result") or {})]
        if earlier := [r["summary"] for r in (thread.get("runs") or [])[-4:-1] if r.get("summary")]:
            lines.append("Goes before that: " + " · ".join(_cut(s, 200) for s in earlier))
        if files := self._files(thread):
            lines.append(f"Files its branch {thread['branch']} touched: " + ", ".join(files))
        brief = thread["brief"] if "brief" in thread else (thread.get("context") or "")[:2000]
        context = "\n\n".join(x for x in (brief, "\n".join(lines), f"What's asked now: {message}") if x)
        return {"context": context, "session_id": "", "run_id": "", "context_tokens": 0}

    @staticmethod
    def why_fresh(thread: dict) -> str:
        """Why a follow-up to `thread` should start a fresh session rather than carry its own on ("" when
        it shouldn't): its context grew heavy, its session has had SESSION_GOES goes, or it has sat STALE_S."""
        if not thread.get("session_id"):
            return ""
        if (tokens := int(thread.get("context_tokens") or 0)) > FRESH_CONTEXT:
            return f"its context had grown to {tokens // 1000}k tokens"
        if (goes := session_goes(thread)) >= SESSION_GOES:
            return f"its session had had {goes} goes"
        last = float(((thread.get("runs") or [{}])[-1]).get("at") or 0)
        if last and time.time() - last > STALE_S:
            return f"its session had sat over {STALE_S // 3600} hours"
        return ""

    def _busy(self) -> int:
        return sum(1 for t in self.store.list() if t["state"] == "working" and not t.get("queued"))

    def _pump(self) -> None:
        """Start queued work while there are free slots, oldest first (nothing while the project is paused)."""
        if self._suspended or self.project.get("status") in ("paused", "archived"):
            return
        cap = int(self.settings.get("max_threads") or 3)
        with self._lock:
            for t in self.store.list():
                if self._busy() >= cap:
                    break
                if t["state"] == "working" and t.get("queued") and t["id"] not in self._live:
                    self._live.add(t["id"])
                    t = self.store.update(t["id"], queued=False, paused=False, activity="starting")
                    self._changed(t)
                    pending = t.get("pending") or {}
                    threading.Thread(target=self._go, args=(t, pending), daemon=True, name=f"thread-{t['id']}").start()

    def _note(self, thread: dict) -> str:
        others = [t["title"] for t in self.store.list() if t["id"] != thread["id"] and t["state"] in ("working", "waiting_on_you")]
        lines = [(f'You are the thread "{thread["title"]}" of the user\'s project "{self.project.get("title", "")}". '
                  f"The project's goal: {self.project.get('goal', '').strip()}")]
        if instructions := (self.project.get("instructions") or "").strip():
            lines.append("The user's standing instructions for the project:\n" + instructions)
        if thread.get("branch"):
            from vision.projects import effective_rules

            publish = effective_rules(self.project)["publish"]
            push = {"does": "You may push your branch and open a draft PR when the work is ready; never merge into another "
                            "branch or delete branches.",
                    "asks": "Pushing or opening a PR asks the user first (Vision files the request); never merge into "
                            "another branch or delete branches.",
                    "never": "Never push, merge into another branch or delete branches."}[publish]
            lines.append(f"You work in a git worktree of your own on the branch {thread['branch']}: commit your work there "
                         f"when it's done and tested. {push}")
        elif others:
            lines.append("Other threads share this folder: keep off the files their work is likely to change.")
        if others:
            lines.append("Other threads at work now: " + "; ".join(others[:6]) + ".")
        from vision.projects import memory_lines, memory_shown

        if memory := memory_shown(memory_lines(self.folder)):
            lines.append("The project memory (what earlier work settled):\n" + "\n".join(memory))
        try:
            from vision.library import project_entries

            if shelf := project_entries(self.folder)[-12:]:
                lines.append("The project's Library (files the user added and earlier threads produced): "
                             + "; ".join(f"{e.get('name')} ({e.get('path')})" for e in shelf))
        except Exception:  # noqa: BLE001  (an unreadable index never stops a thread)
            pass
        (wrap_min, wrap_tools), (stop_min, _) = self.limits
        lines.append(f"Each go has a budget: about {wrap_min} minutes and {wrap_tools} tool calls. Keep to the task, commit "
                     "as you go, and return early rather than polish; past the budget you'll be asked to wrap up, and at "
                     f"{stop_min} minutes you're stopped. If the task is bigger than one go, do the most useful part, say "
                     "what's left in your result, and stop there.")
        lines.append("Your result is reviewed on a phone. `summary`: two or three short sentences on what's different now, "
                     "in plain words, no file paths or test counts. `changes` and `checks`: one short line each.")
        lines.append("In your result, `decisions` holds what later work in the project must know (a convention chosen, "
                     "an approach ruled out and why, a fact found that others will need), one short line each, or []; "
                     "`artifacts` lists the absolute paths of files you made for the user to look at (a report, an "
                     "image, a document), not the code you changed, or []. Follow each picture's path with \" — \" and "
                     "what it shows, in a few plain words the user can read under its thumbnail "
                     "(\"/abs/shots/login-dark.png — Login screen with the new Google button, dark mode\").")
        return "PROJECT THREAD\n" + "\n".join(lines)

    def _factory(self, thread: dict) -> Callable:
        base = self.base()

        def build(model, effort, denied):
            import dataclasses

            cfg = dataclasses.replace(base, mode="auto")  # nobody is there to approve a plan; approval rules still hold
            if self.rules is not None:
                ask, deny = self.rules()
                cfg = dataclasses.replace(cfg, ask_tools=list(ask), denied_tools=list(deny))
            worker = make_worker(cfg, thread["workdir"], model, effort, denied, note=self._note(thread))
            tid = thread["id"]
            worker.allowed_once = self._allowed_list(tid)
            worker.on_allowed_used = lambda call: self.store.update(tid, allowed=list(self._allowed_list(tid)))
            if self.on_approval is not None:
                worker.on_approval = lambda tool, args, rule: self.on_approval(tid, tool, args, rule)
            return worker
        return build

    def _allowed_list(self, tid: str) -> list[str]:
        with self._states_lock:
            if tid not in self._allowed:
                self._allowed[tid] = list((self.store.get(tid) or {}).get("allowed") or [])
            return self._allowed[tid]

    def allow(self, tid: str, call: str) -> None:
        """Let one call the user approved through, once: in the worker running now, and in the next go's."""
        allowed = self._allowed_list(tid)
        allowed.append(call)
        self.store.update(tid, allowed=list(allowed))

    def _task(self, thread: dict) -> dict:
        from vision.delegation import build_task

        return build_task(thread["objective"], agent_label=thread["model"], effort=thread["effort"], workdir=thread["workdir"],
                          mode="auto", channel="text", recent=[thread["context"]] if thread.get("context") else None,
                          constraints=[("This is a project thread: it runs in the background while the user is away. "
                                        "Return needs_input only for a real decision or an approval you need.")])

    def _hooks(self, thread: dict) -> dict:
        from vision.models import model_label
        from vision.supervisor import progress_label

        tid = thread["id"]
        label = model_label(thread["model"]) or thread["model"] or "It"

        def on_tool(call) -> None:
            t = self.store.get(tid) or {}
            t = self.store.update(tid, activity=progress_label(label, call).split(" is ", 1)[-1].rstrip("…"),
                                  tools=int(t.get("tools") or 0) + 1)
            self._changed(t)
            self._watch(tid, t.get("run_id") or "")
        return {"on_tool": on_tool}

    def _watch(self, tid: str, run_id: str) -> None:
        """At each tool call of a go: past its wrap-up limit it's told once to wrap up, past its stop limit
        it's stopped (its record says which limit; what it committed stays on its branch)."""
        (wrap_min, wrap_tools), (stop_min, stop_tools) = self.limits
        with self._lock:
            go = self._goes.get(tid)
            if go is None or tid in self._over:
                return
            go["tools"] += 1
            minutes = (time.monotonic() - go["began"]) / 60
            if minutes >= stop_min or go["tools"] >= stop_tools:
                self._over[tid] = f"{stop_min} minutes" if minutes >= stop_min else f"{stop_tools} tool calls"
                stop = True
            elif not go["wrapped"] and (minutes >= wrap_min or go["tools"] >= wrap_tools):
                go["wrapped"], stop = True, False
            else:
                return
        if stop:
            threading.Thread(target=self.supervisor.cancel, args=(run_id,), daemon=True, name=f"stop-{tid}").start()
        else:
            self.supervisor.steer_one(run_id, WRAP_MESSAGE)

    def _go(self, thread: dict, pending: dict) -> None:
        """Run one step of a thread (start, continue, answer or resume) to its next stop, then record it."""
        tid, kind, message = thread["id"], pending.get("kind") or "start", pending.get("message") or ""
        began = time.monotonic()
        (_, (stop_min, _)) = self.limits
        self.supervisor.timeout_s = (stop_min + 15) * 60  # a backstop for a go stuck in one long call
        with self._lock:
            self._goes[tid] = {"began": began, "tools": 0, "wrapped": False}
            self._over.pop(tid, None)
        run = self.supervisor.runs.get(thread.get("run_id") or "")
        try:
            if run is not None and run.worker is not None and kind in ("continue", "answer"):
                run = self.supervisor.continue_run(run.id, message, **self._hooks(thread))
            else:
                # A new thread, or one with no worker in this process (a restart since): a fresh run, on
                # the thread's own session when it has one.
                session = thread.get("session_id") if kind != "start" else ""
                opening = None
                if session and kind == "answer":
                    opening = {"type": "vision_task_answer", "answer": message, "granted": [], "still_denied": []}
                elif session and kind == "continue":
                    opening = {"type": "vision_task_continue", "message": message}
                run_id = uuid.uuid4().hex[:12]
                self.store.update(tid, run_id=run_id)
                run = self.supervisor.launch(self._task(thread), model=thread["model"], effort=thread["effort"], allowlist=False,
                                             approval=True, factory=self._factory(thread), key=f"thread:{tid}:{run_id}",
                                             channel="thread", session_id=session or None, opening=opening, run_id=run_id,
                                             **self._hooks(thread))
        except Exception as e:  # noqa: BLE001  (a thread that can't start says why)
            if self._suspended:
                return
            if self._hold(tid, kind, message):
                return
            with self._lock:
                self._live.discard(tid)
            t = self.store.update(tid, state="idle", queued=False, pending=None, activity="", error=str(e), reported=False)
            t = self.store.log_run(tid, {"at": time.time(), "kind": kind, "status": "failed", "summary": _cut(str(e), SUMMARY_CHARS),
                                         "findings": [], "changes": [], "duration_s": round(time.monotonic() - began, 1), "tokens_out": 0})
            self._changed(t)
            self._stopped(t)
            self._pump()
            return
        if self._suspended:
            return  # the restart keeps the record `working`; the next process resumes it
        if self._hold(tid, kind, message, run):
            return
        self._record(tid, run, kind, began)

    def _hold(self, tid: str, kind: str, message: str, run=None) -> bool:
        """A worker stopped because its project was paused: the thread stays `working` and `paused`, queued
        to carry on in its session at resume. False when this thread isn't being paused."""
        with self._lock:
            if tid not in self._held:
                return False
            self._held.discard(tid)
            if run is not None and run.state != "cancelled":
                return False  # it got to the end of its step before the pause reached it: that result stands
            self._live.discard(tid)
        prev = self.store.get(tid) or {}
        session = (run.session_id if run is not None else "") or prev.get("session_id") or ""
        tokens = int(prev.get("tokens_out") or 0) + (run.output_tokens if run is not None else 0)
        # A step that hadn't begun keeps what it was for; one under way carries on, told why it stopped.
        if kind in ("continue", "answer") and run is None:
            pending = {"kind": kind, "message": message}
        elif session:
            pending = {"kind": "continue", "message": PAUSED_MESSAGE}
        else:
            pending = {"kind": "start"}
        t = self.store.update(tid, state="working", queued=True, paused=True, activity="paused", pending=pending,
                              session_id=session, run_id=run.id if run is not None else prev.get("run_id") or "", tokens_out=tokens)
        self._changed(t)
        return True

    def _record(self, tid: str, run, kind: str, began: float) -> None:
        r = run.result or {}
        state = thread_state(run)
        prev = self.store.get(tid) or {}
        if prev.get("state") == "resolved":  # closed while it was stopping
            state = "resolved"
        with self._lock:
            self._live.discard(tid)
            on_purpose = tid in self._stopping
            self._stopping.discard(tid)
            self._goes.pop(tid, None)
            over = self._over.pop(tid, None) if run.state == "cancelled" and not on_purpose else None
        error = run.error if state == "idle" else ""
        status = run.state
        if over:
            # Stopped at its limit: what it committed is there to review; else it waits to be told to carry on.
            ahead = commits_ahead(prev.get("worktree") or "", prev.get("branch") or "")
            r = {**(prev.get("last_result") or {}), "status": "completed" if ahead else "failed", "question": None,
                 "summary": (f"Stopped at its limit of {over} for one go. "
                             + ("What it committed is on its branch to review; message it to carry on." if ahead
                                else "It hadn't committed anything yet; message it to carry on."))}
            state, error, status = ("ready_for_review" if ahead else "idle"), "", "stopped_at_limit"
        # The memory and Library first: whoever sees the new state can already read what the go left.
        self._keep({**prev, "id": tid}, r)
        context = getattr(getattr(run, "worker", None), "context", None)
        t = self.store.update(
            tid, state=state, queued=False, pending=None, run_id=run.id, session_id=run.session_id or prev.get("session_id") or "",
            activity="", last_result=r or None, question=run.question or "", error=error,
            context_tokens=int(context[0]) if isinstance(context, tuple) and context else int(prev.get("context_tokens") or 0),
            tokens_out=int(prev.get("tokens_out") or 0) + run.output_tokens, reported=state == "resolved" or on_purpose, told="")
        t = self.store.log_run(tid, {
            "at": time.time(), "kind": kind, "status": status, "summary": _cut(r.get("summary") or run.error, SUMMARY_CHARS),
            "findings": [_cut(f, FINDING_CHARS) for f in (r.get("findings") or [])[:5]], "changes": list(r.get("changes") or [])[:20],
            "duration_s": round(time.monotonic() - began, 1), "tokens_out": run.output_tokens})
        self._changed(t)
        if not on_purpose:
            self._stopped(t)
        self._pump()

    def _keep(self, thread: dict, result: dict) -> None:
        """What a result leaves the project: its decisions in the project memory, its artifacts (and the
        pictures its summary shows) in the project's Library."""
        from vision.projects import remember

        try:
            remember(self.folder, result.get("decisions") or [], source=f"thread {thread.get('title')}")
        except OSError:
            pass
        try:
            from vision.library import index_thread_result

            index_thread_result(self.folder, thread, result)
        except Exception:  # noqa: BLE001  (a Library that can't be written never loses the result)
            pass

    def _stopped(self, thread: dict) -> None:
        try:
            self.on_stop(thread)
        except Exception:  # noqa: BLE001
            pass

    # -- talking to a thread
    def tell(self, ref: str, message: str) -> dict:
        """A message to one thread: steers it while it works, answers it when it asks, gives it more work
        when it has stopped, in a fresh session when its own has grown heavy or cold (why_fresh). A closed
        one isn't reopened: a new thread follows it, and that is what's returned."""
        message = message.strip()
        if not message:
            raise ValueError("say something to it")
        thread = self.store.find(ref)
        if thread is None:
            raise KeyError(ref)
        if thread["state"] == "landing":
            raise ValueError("it's being landed: try again in a moment")
        if thread["state"] == "working":
            if thread.get("queued"):
                pending = dict(thread.get("pending") or {})
                if pending.get("kind") in ("continue", "answer"):  # waiting to carry on (paused, or behind others)
                    pending["message"] = "\n\n".join(x for x in (pending.get("message"), f"The user adds: {message}") if x)
                    return self._changed(self.store.update(thread["id"], pending=pending))
                if pending.get("kind") == "resume":
                    pending = {"kind": "continue", "message": f"{PAUSED_MESSAGE}\n\nThe user adds: {message}"}
                    return self._changed(self.store.update(thread["id"], pending=pending))
                ctx = "\n".join(x for x in (thread.get("context"), f"Added before it started: {message}") if x)
                return self._changed(self.store.update(thread["id"], context=ctx))
            if self.supervisor.steer_one(thread.get("run_id") or "", message):
                return thread
            raise ValueError("it can't take a message while it works: try again once it stops")
        if thread["state"] == "resolved":
            # A closed thread stays closed: the work carries on in a new thread, handed what this one did
            # (never its transcript), so a thread isn't reopened again and again into one huge session.
            t = self.start(message, title=thread.get("title") or "", edits=bool(thread.get("edits")),
                           context=thread.get("brief") or "", uses=[thread["id"]], follows=thread["id"])
            self._changed(self.store.update(thread["id"], followed_by=t["id"]))
            return t
        kind = "answer" if thread["state"] == "waiting_on_you" else "continue"
        changes: dict = {}
        if not os.path.isdir(thread["workdir"]):
            # Its worktree went: a fresh session in a new one (a session can't move folders), handed what it did.
            changes = self._fresh(thread, "its worktree was removed", message)
            changes.update(self._place(thread["title"], thread["id"], bool(thread.get("edits")), suffix=f"-{int(time.time()) % 10000}"))
            kind = "start"
        elif kind == "continue" and (why := self.why_fresh(thread)):
            # Its session has grown heavy or gone cold: carrying it on would reread all of it at every call.
            # A fresh session in the same folder, handed what the thread did and what's asked now, costs a fraction.
            changes, kind = self._fresh(thread, why, message), "start"
        t = self.store.update(thread["id"], state="working", queued=True, question="", error="", reported=True,
                              pending={"kind": kind, "message": message}, **changes)
        self._changed(t)
        self._pump()
        return t

    def stop(self, ref: str) -> dict:
        """Stop a working thread: it becomes idle and can be continued."""
        thread = self.store.find(ref)
        if thread is None:
            raise KeyError(ref)
        if thread["state"] != "working":
            return thread
        if thread.get("queued"):
            t = self.store.update(thread["id"], state="idle", queued=False, paused=False, pending=None, activity="",
                                  error="stopped before it started" if not thread.get("session_id") else "stopped")
            return self._changed(t)
        if thread.get("run_id"):
            with self._lock:
                self._stopping.add(thread["id"])
            self.supervisor.cancel(thread["run_id"])
        return self.store.get(thread["id"]) or thread

    def resolve(self, ref: str) -> dict:
        """Close a thread (stopping it first). Its worktree goes when nothing in it would be lost."""
        thread = self.store.find(ref)
        if thread is None:
            raise KeyError(ref)
        if thread["state"] == "working":
            self.stop(thread["id"])
        kept = remove_worktree(thread.get("worktree") or "", thread.get("branch") or "") if thread.get("worktree") else ""
        changes = {"state": "resolved", "queued": False, "pending": None, "activity": ""}
        if thread.get("worktree") and not kept:
            changes.update(worktree="", note="worktree removed")
        elif kept:
            changes["note"] = f"worktree kept: {kept}"
        return self._changed(self.store.update(thread["id"], **changes))

    def sweep(self) -> list[dict]:
        """Close threads that finished without changes (a look, an answer), have been reported, and sat
        CLOSE_IDLE_S since, so they're not kept open to be reused; one with a question, a failure or
        commits of its own stays. Returns those closed. Runs git for ones with a worktree, once per run of
        theirs: one found to have commits isn't asked again until it runs again."""
        closed = []
        now = time.time()
        for t in self.store.list():
            r = t.get("last_result") or {}
            last = float(((t.get("runs") or [{}])[-1]).get("at") or 0)
            if (t["state"] != "idle" or t.get("reported") is False or t.get("error") or r.get("status") != "completed"
                    or r.get("changes") or not last or now - last < CLOSE_IDLE_S or self._kept.get(t["id"]) == last):
                continue
            if commits_ahead(t.get("worktree") or "", t.get("branch") or ""):
                self._kept[t["id"]] = last
                continue
            done = self.resolve(t["id"])
            note = "; ".join(x for x in (f"closed after {CLOSE_IDLE_S // 3600} hours idle",
                                         done.get("note") if done.get("note") != t.get("note") else "") if x)
            closed.append(self._changed(self.store.update(t["id"], note=note)))
        return closed

    def land(self, ref: str) -> dict:
        """Merge a finished thread's branch into the project folder's checked-out branch, then close it
        (its worktree goes, as the branch is merged now). A refusal or a conflict leaves everything as it
        was and the thread back where it stood, with why in its note. Runs git: call it off the loop."""
        thread = self.store.find(ref)
        if thread is None:
            raise KeyError(ref)
        if thread["state"] in ("working", "landing"):
            raise ValueError("it's still working: wait for it to stop" if thread["state"] == "working" else "it's already landing")
        if not thread.get("branch"):
            raise ValueError("it has no branch of its own (it worked in the project's folder): nothing to land")
        dirs = self.project.get("workdirs") or []
        workdir = dirs[0] if dirs else ""
        before = thread["state"]
        self._changed(self.store.update(thread["id"], state="landing", activity="merging"))
        try:
            why = merge_branch(workdir, thread["branch"], thread.get("worktree") or "")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            why = str(e)
        if why:
            t = self.store.update(thread["id"], state=before if before != "resolved" else "ready_for_review", activity="",
                                  note=f"not landed: {why}")
            self._changed(t)
            raise ValueError(f"not landed: {why}")
        if thread.get("worktree") and (root := git_root(workdir)):
            try:
                from vision.library import relocate_project

                relocate_project(self.folder, thread["worktree"], root)  # its Library files live in the project's folder now
            except OSError:
                pass
        kept = remove_worktree(thread.get("worktree") or "", thread["branch"]) if thread.get("worktree") else ""
        t = self.store.update(thread["id"], state="resolved", activity="", reported=True, landed=round(time.time(), 1),
                              worktree="" if not kept else thread.get("worktree"),
                              note=f"landed {thread['branch']}" + (f" (worktree kept: {kept})" if kept else ""))
        return self._changed(t)

    # -- pausing the whole project
    def pause(self) -> None:
        """The project was paused: stop every worker, leaving each thread `working` and `paused`, queued to
        carry on at resume. (The caller has set the project's status, so nothing new starts meanwhile.)"""
        for t in self.store.list():
            if t["state"] != "working":
                continue
            if t.get("queued"):
                self._changed(self.store.update(t["id"], paused=True, activity="paused"))
                continue
            run = self.supervisor.runs.get(t.get("run_id") or "")
            with self._lock:
                self._held.add(t["id"])
            if run is not None and not run.final:
                self.supervisor.cancel(run.id)

    def resume(self) -> None:
        """The project was resumed: paused threads carry on, as slots allow."""
        for t in self.store.list():
            if t["state"] == "working" and t.get("paused"):
                self._changed(self.store.update(t["id"], activity="", paused=False))
        self._pump()

    # -- restarts
    def suspend(self) -> None:
        """A restart: stop every worker and leave its thread `working`, for restore() to pick up."""
        self._suspended = True
        for run in self.supervisor.running():
            try:
                run.worker.cancel()
            except Exception:  # noqa: BLE001
                pass

    def restore(self) -> None:
        """After a restart: every thread that was working goes back in the queue, to resume its session."""
        for t in self.store.list():
            if t["state"] == "working" and not t.get("queued"):
                t = self.store.update(t["id"], queued=True, activity="resuming after a restart", pending={"kind": "resume"})
                self._changed(t)
        self._pump()
