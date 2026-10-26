"""Project threads (vision/runs.py): the thread store, threads starting outside any turn, queueing behind
max_threads, follow-ups and answers in the same session, restart and resume, worktrees, the server's
thread endpoints and frames, and the `vision thread` commands."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vision import projects, runs
from vision.brain import Turn
from vision.config import BrainConfig, Config
from vision.runs import ProjectThreads, ThreadStore

DONE = dict(status="completed", summary="Added the endpoint.", changes=["server.py"], checks=["tests: passed"], findings=[], question=None)
LOOKED = dict(status="completed", summary="Read it all.", changes=[], checks=[], findings=["it's fine"], question=None)
ASKS = dict(status="needs_input", summary="Need a choice.", changes=[], checks=[], findings=[], question="Postgres or SQLite?")


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


class FakeWorker:
    """A worker brain: each ask() takes the next scripted result (a dict, or an Event to block on first)."""

    def __init__(self, *script, session="s1"):
        self.script = list(script)
        self.prompts: list[dict] = []
        self.session_id = None
        self.session = session
        self.resumed = None
        self.cfg = SimpleNamespace(model="opus", effort="medium", denied_tools=[])
        self.cancelled = threading.Event()

    def ask(self, prompt, on_question=None, on_tool=None):
        self.prompts.append(json.loads(prompt))
        step = self.script.pop(0) if self.script else DONE
        if isinstance(step, threading.Event):
            self.session_id = self.session  # a real worker has its session from its first event
            if on_tool:
                on_tool(SimpleNamespace(name="Bash", detail="pytest -q"))
            step.wait(5)
            if self.cancelled.is_set():
                self.cancelled.clear()
                return Turn(is_error=True, error="cancelled", session_id=self.session)
            step = self.script.pop(0) if self.script else DONE
        self.session_id = self.session
        return Turn(data=step, session_id=self.session)

    def resume(self, session_id):
        self.resumed = session_id

    def cancel(self):
        self.cancelled.set()

    def steer(self, text):
        self.prompts.append(json.loads(text))
        return True


class ThreadsTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        (self.tmp / "work").mkdir()
        self.workers: list[FakeWorker] = []
        self.built: list[tuple] = []
        p = patch.object(runs, "make_worker", side_effect=self._make)
        p.start()
        self.addCleanup(p.stop)
        self.changes: list[dict] = []
        self.stops: list[dict] = []
        self.addCleanup(self._join_workers)  # runs before the folder goes (cleanups run last-in first-out)

    @staticmethod
    def _join_workers():
        """Every thread's worker done writing its records before its project folder is removed."""
        for t in threading.enumerate():
            if t.name.startswith("thread-") and t is not threading.current_thread():
                t.join(5)

    def _make(self, cfg, workdir, model, effort, denied, note=""):
        w = self.workers.pop(0) if self.workers else FakeWorker()
        self.built.append((cfg, workdir, model, effort, list(denied or []), note, w))
        return w

    def threads(self, **settings) -> ProjectThreads:
        project = {"id": "p1", "title": "Calendar", "goal": "Add a calendar", "instructions": "Use CalDAV.",
                   "workdirs": [str(self.tmp / "work")], "status": "active",
                   "settings": {**projects.SETTINGS, "isolation": "shared", **settings}}
        base = BrainConfig(model="opus", effort="medium", workdir=str(self.tmp / "work"))
        return ProjectThreads(Config(), project, folder=self.tmp / "p1", base=lambda: base, on_change=self.changes.append,
                              on_stop=self.stops.append, worktrees=self.tmp / "worktrees")


class StoreTest(unittest.TestCase):
    def test_round_trip_and_the_run_log_is_capped(self):
        with tempfile.TemporaryDirectory() as d:
            store = ThreadStore(Path(d))
            store.update("t1", title="One", state="working", created=1)
            for i in range(runs.RUN_LOG + 5):
                store.log_run("t1", {"at": i, "kind": "continue"})
            again = ThreadStore(Path(d))
            t = again.get("t1")
            self.assertEqual(len(t["runs"]), runs.RUN_LOG)
            self.assertEqual(t["runs"][-1]["at"], runs.RUN_LOG + 4)  # the newest kept, the oldest dropped
            self.assertEqual(again.find("t")["id"], "t1")

    def test_a_broken_file_or_record_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "threads.json").write_text('[{"id": "x", "state": "dancing"}, {"id": "y", "state": "idle"}]')
            self.assertEqual([t["id"] for t in ThreadStore(Path(d)).list()], ["y"])
            (Path(d) / "threads.json").write_text("{nope")
            self.assertEqual(ThreadStore(Path(d)).list(), [])


class MakeWorkerTest(unittest.TestCase):
    def test_a_task_mode_worker_in_its_folder_with_the_note_in_its_prompt(self):
        from vision.delegation import worker_prompt

        with patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: SimpleNamespace(cfg=cfg, task_mode=False, workdir="")):
            w = runs.make_worker(BrainConfig(model="opus", effort="low"), "/tmp/x", "sonnet", "high", ["Bash(git push:*)"],
                                 note="PROJECT THREAD\nYou are the thread.")
        self.assertTrue(w.task_mode)
        self.assertEqual((w.workdir, w.cfg.model, w.cfg.effort), ("/tmp/x", "sonnet", "high"))
        self.assertIn("Bash(git push:*)", w.cfg.denied_tools)
        prompt = worker_prompt(w.cfg, "/tmp/x", "claude")
        self.assertIn("PROJECT THREAD", prompt)
        self.assertIn("vision_task_continue", prompt)
        self.assertIn("vision_task_resume", prompt)


class ThreadRunTest(ThreadsTestBase):
    def test_a_thread_runs_outside_any_turn_and_records_its_result(self):
        pt = self.threads()
        t = pt.start("Add the events endpoint")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        t = pt.find(t["id"])
        self.assertEqual(t["state"], "ready_for_review")  # finished with changes
        self.assertEqual(t["session_id"], "s1")
        self.assertEqual(t["last_result"]["summary"], "Added the endpoint.")
        self.assertEqual([(r["kind"], r["status"]) for r in t["runs"]], [("start", "completed")])
        wait_for(lambda: self.stops)  # the stop is reported just after the state is set
        self.assertEqual(self.stops[-1]["id"], t["id"])
        cfg, workdir, model, effort, denied, note, w = self.built[0]
        self.assertEqual((workdir, model, effort, cfg.mode), (str(self.tmp / "work"), "opus", "medium", "auto"))
        self.assertIn("Bash(git push:*)", denied)  # approval-only commands stay denied in the background
        self.assertIn('thread "Add the events endpoint" of the user\'s project "Calendar"', note)
        self.assertIn("Use CalDAV.", note)
        self.assertEqual(w.prompts[0]["type"], "vision_task")
        self.assertTrue(json.loads((self.tmp / "p1" / "threads.json").read_text()))

    def test_without_changes_it_is_idle_and_a_failure_says_why(self):
        self.workers = [FakeWorker(LOOKED)]
        pt = self.threads()
        t = pt.start("Look around", edits=False)
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertEqual(pt.find(t["id"])["state"], "idle")
        runs.make_worker.side_effect = RuntimeError("no CLI")
        t = pt.start("Another")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        t = pt.find(t["id"])
        self.assertEqual(t["state"], "idle")
        self.assertIn("no CLI", t["error"])
        self.assertEqual(t["runs"][-1]["status"], "failed")

    def test_extra_threads_queue_behind_max_threads(self):
        gate = threading.Event()
        self.workers = [FakeWorker(gate, DONE), FakeWorker(LOOKED)]
        pt = self.threads(max_threads=1)
        a = pt.start("First")
        wait_for(lambda: pt.find(a["id"])["activity"] == "running tests")
        b = pt.start("Second")
        self.assertTrue(pt.find(b["id"])["queued"])
        self.assertEqual(len(self.built), 1)
        gate.set()
        wait_for(lambda: pt.find(b["id"])["state"] == "idle")
        self.assertEqual(pt.find(a["id"])["state"], "ready_for_review")
        self.assertEqual(pt.find(a["id"])["tools"], 1)

    def test_an_answer_and_a_follow_up_go_into_the_same_session(self):
        w = FakeWorker(ASKS, DONE, LOOKED)
        self.workers = [w]
        pt = self.threads()
        t = pt.start("Set up storage")
        wait_for(lambda: pt.find(t["id"])["state"] == "waiting_on_you")
        self.assertEqual(pt.find(t["id"])["question"], "Postgres or SQLite?")
        wait_for(lambda: self.stops)  # the stop is reported just after the state is set
        self.assertEqual(self.stops[-1]["state"], "waiting_on_you")
        pt.tell(t["id"], "SQLite")
        wait_for(lambda: pt.find(t["id"])["state"] == "ready_for_review")
        pt.tell(t["id"], "now add an index")
        wait_for(lambda: len(pt.find(t["id"])["runs"]) == 3)
        self.assertEqual(len(self.built), 1)  # one worker the whole way
        self.assertEqual([p["type"] for p in w.prompts], ["vision_task", "vision_task_answer", "vision_task_continue"])
        self.assertEqual(w.prompts[1]["answer"], "SQLite")
        self.assertEqual(w.prompts[2]["message"], "now add an index")
        self.assertEqual([r["kind"] for r in pt.find(t["id"])["runs"]], ["start", "answer", "continue"])

    def test_a_message_steers_a_working_thread_and_stop_makes_it_idle(self):
        gate = threading.Event()
        w = FakeWorker(gate)
        self.workers = [w]
        pt = self.threads()
        t = pt.start("Long job")
        wait_for(lambda: pt.find(t["id"])["activity"] == "running tests")
        pt.tell(t["id"], "skip the docs")
        self.assertEqual(w.prompts[-1], {"type": "vision_task_update", "message": "skip the docs"})
        pt.stop(t["id"])
        gate.set()
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertEqual(pt.find(t["id"])["state"], "idle")

    def test_a_long_go_is_told_to_wrap_up_then_stopped_at_its_limit(self):
        class Busy(FakeWorker):
            def ask(self, prompt, on_question=None, on_tool=None):
                self.prompts.append(json.loads(prompt))
                self.session_id = self.session
                for _ in range(3):
                    on_tool(SimpleNamespace(name="Bash", detail="make"))
                self.cancelled.wait(5)
                return Turn(is_error=True, error="cancelled", session_id=self.session)

        w = Busy()
        self.workers = [w]
        with patch.dict(runs.LENGTHS, {"normal": ((1000, 1), (1000, 2))}):
            pt = self.threads()
            t = pt.start("Port everything")
            wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertIn("about 1000 minutes and 1 tool calls", self.built[0][5])  # it knows its budget
        self.assertEqual(w.prompts[1], {"type": "vision_task_update", "message": runs.WRAP_MESSAGE})  # told once
        self.assertEqual(len(w.prompts), 2)
        done = pt.find(t["id"])
        self.assertEqual(done["state"], "idle")  # nothing committed: it waits to be told to carry on
        self.assertIn("limit of 2 tool calls", done["last_result"]["summary"])
        self.assertEqual(done["runs"][-1]["status"], "stopped_at_limit")
        wait_for(lambda: self.stops)  # the stop is reported just after the state is set
        self.assertEqual(self.stops[-1]["id"], t["id"])  # it reports and pushes, unlike a stop on purpose

    def test_a_follow_up_to_a_heavy_session_starts_a_fresh_one_with_a_handover(self):
        first, second = FakeWorker(DONE), FakeWorker(LOOKED, session="s2")
        self.workers = [first, second]
        pt = self.threads()
        t = pt.start("Build the scanner")
        wait_for(lambda: pt.find(t["id"])["state"] == "ready_for_review")
        pt.store.update(t["id"], context_tokens=runs.FRESH_CONTEXT + 1)
        pt.tell(t["id"], "rename the sheet")
        wait_for(lambda: len(pt.find(t["id"])["runs"]) == 2)
        self.assertEqual(len(self.built), 2)  # a new worker, on no session
        self.assertEqual(second.prompts[0]["type"], "vision_task")
        recent = json.dumps(second.prompts[0])
        self.assertIn("rename the sheet", recent)
        self.assertIn("Added the endpoint.", recent)  # what the last session came back with
        self.assertEqual(pt.find(t["id"])["session_id"], "s2")

    def test_restart_leaves_it_working_and_the_next_process_resumes_its_session(self):
        gate = threading.Event()
        self.workers = [FakeWorker(DONE, gate, session="s9")]
        pt = self.threads()
        t = pt.start("Migrate")
        wait_for(lambda: pt.find(t["id"])["state"] == "ready_for_review")
        pt.tell(t["id"], "and the tests")
        wait_for(lambda: pt.find(t["id"])["activity"] == "running tests")
        pt.suspend()
        gate.set()
        time.sleep(0.1)
        self.assertEqual(pt.find(t["id"])["state"], "working")  # the restart didn't record it as stopped
        w = FakeWorker(DONE, session="s9")
        self.workers = [w]
        again = self.threads()
        again.restore()
        wait_for(lambda: again.find(t["id"])["state"] == "ready_for_review")
        self.assertEqual(w.resumed, "s9")
        self.assertEqual(w.prompts[0]["type"], "vision_task_resume")
        self.assertEqual(again.find(t["id"])["runs"][-1]["kind"], "resume")

    def test_an_answer_after_a_restart_goes_into_the_old_session(self):
        self.workers = [FakeWorker(ASKS, session="s4")]
        pt = self.threads()
        t = pt.start("Pick storage")
        wait_for(lambda: pt.find(t["id"])["state"] == "waiting_on_you")
        w = FakeWorker(DONE, session="s4")
        self.workers = [w]
        again = self.threads()  # a new process: no worker for it in memory
        again.tell(t["id"], "SQLite")
        wait_for(lambda: again.find(t["id"])["state"] == "ready_for_review")
        self.assertEqual(w.resumed, "s4")
        self.assertEqual(w.prompts[0], {"type": "vision_task_answer", "answer": "SQLite", "granted": [], "still_denied": []})

    def test_resolve_closes_it_and_a_message_starts_a_new_thread_after_it(self):
        first, second = FakeWorker(DONE), FakeWorker(LOOKED, session="s2")
        self.workers = [first, second]
        pt = self.threads()
        t = pt.start("Quick one", context="Keep it in the CLI.")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertEqual(pt.resolve(t["id"])["state"], "resolved")
        self.assertEqual(pt.counts()["resolved"], 1)
        after = pt.tell(t["id"], "one more thing")
        self.assertNotEqual(after["id"], t["id"])
        self.assertEqual((after["follows"], after["title"], after["objective"]), (t["id"], "Quick one", "one more thing"))
        wait_for(lambda: pt.find(after["id"])["state"] == "idle")
        old = pt.find(t["id"])
        self.assertEqual((old["state"], old["followed_by"], len(old["runs"])), ("resolved", after["id"], 1))  # never reopened
        handed = json.dumps(second.prompts[0])
        self.assertIn("Keep it in the CLI.", handed)  # the old brief
        self.assertIn("Added the endpoint.", handed)  # and what it came back with, not its transcript
        self.assertIsNone(second.resumed)

    def test_a_follow_up_after_many_goes_or_a_long_wait_starts_a_fresh_session(self):
        w = FakeWorker(*[DONE] * runs.SESSION_GOES)
        self.workers = [w, FakeWorker(LOOKED, session="s2"), FakeWorker(LOOKED, session="s3")]
        pt = self.threads()
        t = pt.start("Build the scanner")
        for n in range(2, runs.SESSION_GOES + 1):
            wait_for(lambda n=n: pt.find(t["id"])["state"] == "ready_for_review" and len(pt.find(t["id"])["runs"]) == n - 1)
            pt.tell(t["id"], f"step {n}")
        wait_for(lambda: len(pt.find(t["id"])["runs"]) == runs.SESSION_GOES)
        self.assertEqual(len(self.built), 1)  # every go so far in the one session
        self.assertIn("goes", runs.ProjectThreads.why_fresh(pt.find(t["id"])))
        pt.tell(t["id"], "and one more")
        # The session id lands as the run starts, its run only when it ends.
        wait_for(lambda: pt.find(t["id"])["session_id"] == "s2" and len(pt.find(t["id"])["runs"]) == runs.SESSION_GOES + 1)
        fresh = pt.find(t["id"])
        self.assertEqual((len(self.built), fresh["runs"][-1]["kind"], runs.session_goes(fresh)), (2, "start", 1))
        handed = json.dumps(self.built[1][6].prompts[0])
        self.assertIn(f"{runs.SESSION_GOES} goes", handed)  # why it's fresh
        self.assertIn("What's asked now: and one more", handed)
        self.assertIn("Goes before that: Added the endpoint.", handed)  # the last few goes, in brief
        self.assertEqual(runs.ProjectThreads.why_fresh(fresh), "")
        runs_ = fresh["runs"]
        runs_[-1]["at"] -= runs.STALE_S + 60
        pt.store.update(t["id"], runs=runs_)
        self.assertIn("sat over", runs.ProjectThreads.why_fresh(pt.find(t["id"])))
        pt.tell(t["id"], "after the weekend")
        wait_for(lambda: pt.find(t["id"])["session_id"] == "s3")

    def test_an_answer_always_goes_into_the_session_that_asked(self):
        w = FakeWorker(ASKS, DONE)
        self.workers = [w]
        pt = self.threads()
        t = pt.start("Pick storage")
        wait_for(lambda: pt.find(t["id"])["state"] == "waiting_on_you")
        pt.store.update(t["id"], context_tokens=runs.FRESH_CONTEXT + 1)
        pt.tell(t["id"], "SQLite")
        wait_for(lambda: pt.find(t["id"])["state"] == "ready_for_review")
        self.assertEqual((len(self.built), w.prompts[1]["type"]), (1, "vision_task_answer"))

    def test_sweep_closes_threads_long_finished_without_changes(self):
        pt = self.threads()
        long_ago, now = time.time() - runs.CLOSE_IDLE_S - 60, time.time()
        for tid, state, result, at, more in (
                ("look", "idle", LOOKED, long_ago, {}), ("built", "ready_for_review", DONE, long_ago, {}),
                ("recent", "idle", LOOKED, now, {}), ("asks", "waiting_on_you", ASKS, long_ago, {}),
                ("failed", "idle", {**LOOKED, "status": "failed"}, long_ago, {}),
                ("unheard", "idle", LOOKED, long_ago, {"reported": False})):
            pt.store.update(tid, title=tid, state=state, last_result=result, runs=[{"at": at, "kind": "start"}],
                            workdir=str(self.tmp / "work"), created=at, **more)
        closed = pt.sweep()
        self.assertEqual([c["id"] for c in closed], ["look"])
        self.assertIn("idle", pt.find("look")["note"])
        self.assertEqual([pt.find(x)["state"] for x in ("built", "recent", "asks", "failed", "unheard")],
                         ["ready_for_review", "idle", "waiting_on_you", "idle", "idle"])
        self.assertEqual(pt.sweep(), [])


class CoordinationTest(ThreadsTestBase):
    """What threads hand on: results to later threads, decisions to the project memory, artifacts to its
    Library; reports owed to the coordinator; pausing and resuming the project."""

    def test_a_thread_builds_on_an_earlier_ones_results(self):
        pt = self.threads()
        first = pt.start("Design the schema")
        wait_for(lambda: pt.find(first["id"])["state"] != "working")
        with self.assertRaises(ValueError):
            pt.start("Build it", uses=["nope"])
        second = pt.start("Build it", uses=[first["id"]])
        self.assertIn('Earlier thread "Design the schema"', second["context"])
        self.assertIn("Result: Added the endpoint.", second["context"])
        self.assertIn("Changed: server.py", second["context"])
        wait_for(lambda: pt.find(second["id"])["state"] != "working")
        task = self.built[-1][6].prompts[0]["task"]
        self.assertIn("Design the schema", task["context"])

    def test_decisions_go_to_the_memory_and_artifacts_to_the_library(self):
        from vision.library import project_entries

        report = self.tmp / "work" / "report.md"
        report.write_text("# findings")
        shot = self.tmp / "work" / "login-dark.png"
        shot.write_bytes(b"png")
        result = {**DONE, "decisions": ["Dates are stored in UTC."],
                  "artifacts": [str(report), "/nowhere/gone.png", f"{shot} — Login screen, dark"],
                  "summary": "Done, see ![the chart](" + str(self.tmp / "work" / "chart.png") + ") and ![](" + str(shot) + ")"}
        (self.tmp / "work" / "chart.png").write_bytes(b"png")
        self.workers = [FakeWorker(result), FakeWorker(DONE)]
        pt = self.threads()
        t = pt.start("Research dates")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertIn("Dates are stored in UTC. (thread Research dates)", projects.memory_lines(self.tmp / "p1")[0])
        shelf = {e["name"]: e for e in project_entries(self.tmp / "p1")}
        self.assertEqual(set(shelf), {"report.md", "chart.png", "login-dark.png"})  # what doesn't exist isn't indexed
        self.assertEqual((shelf["report.md"]["source"], shelf["report.md"]["thread"]), ("thread", t["id"]))
        self.assertEqual(shelf["chart.png"]["caption"], "the chart")
        self.assertEqual(shelf["login-dark.png"]["caption"], "Login screen, dark")  # a bare ![]() doesn't wipe it
        later = pt.start("Use them")
        wait_for(lambda: pt.find(later["id"])["state"] != "working")
        note = self.built[-1][5]
        self.assertIn("Dates are stored in UTC.", note)  # the next thread is told the memory and the Library
        self.assertIn("report.md", note)
        self.assertIn("`decisions`", note)

    def test_a_stop_is_owed_a_report_until_marked_and_a_stop_on_purpose_is_not(self):
        pt = self.threads()
        t = pt.start("Quick")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertEqual([x["id"] for x in pt.unreported()], [t["id"]])
        pt.mark_reported([t["id"]])
        self.assertEqual(pt.unreported(), [])
        pt.tell(t["id"], "more")  # carrying on clears what was owed
        wait_for(lambda: pt.find(t["id"])["state"] != "working" and len(pt.find(t["id"])["runs"]) == 2)
        self.assertEqual(len(pt.unreported()), 1)
        wait_for(lambda: sum(x["id"] == t["id"] for x in self.stops) == 2)  # both runs have reported their stop
        gate = threading.Event()
        self.workers = [FakeWorker(gate)]
        stops = len(self.stops)
        s = pt.start("Long")
        wait_for(lambda: pt.find(s["id"])["activity"] == "running tests")
        pt.stop(s["id"])
        gate.set()
        wait_for(lambda: pt.find(s["id"])["state"] != "working")
        self.assertEqual(len(self.stops), stops)  # the user stopped it: no report, no push
        self.assertTrue(pt.find(s["id"])["reported"])
        self.assertGreater(pt.tokens_out(), -1)

    def test_pause_holds_working_threads_and_resume_carries_them_on(self):
        gate = threading.Event()
        w = FakeWorker(gate, DONE)
        self.workers = [w]
        pt = self.threads(max_threads=1)
        a = pt.start("Long")
        wait_for(lambda: pt.find(a["id"])["activity"] == "running tests")
        b = pt.start("Next")
        pt.project = {**pt.project, "status": "paused"}
        pt.pause()
        gate.set()
        wait_for(lambda: pt.find(a["id"]).get("paused"))
        a_now = pt.find(a["id"])
        self.assertEqual((a_now["state"], a_now["queued"], a_now["pending"]["kind"]), ("working", True, "continue"))
        self.assertTrue(pt.find(b["id"])["paused"])
        pt.tell(a["id"], "and skip the docs")  # said while paused: it goes with the carry-on
        self.assertIn("and skip the docs", pt.find(a["id"])["pending"]["message"])
        pt.start("Third")
        time.sleep(0.1)
        self.assertEqual(len(self.built), 1)  # nothing new started while paused
        self.assertEqual(self.stops, [])
        pt.project = {**pt.project, "status": "active"}
        pt.resume()
        wait_for(lambda: pt.find(a["id"])["state"] == "ready_for_review")
        self.assertEqual(w.prompts[-1]["type"], "vision_task_continue")
        self.assertIn("paused the project", w.prompts[-1]["message"])
        wait_for(lambda: pt.counts()["working"] == 0, timeout=10)


@unittest.skipUnless(shutil.which("git"), "git is needed for worktrees")
class WorktreeTest(ThreadsTestBase):
    def setUp(self):
        super().setUp()
        repo = self.tmp / "work"
        # For landing's own merge commits too: a CI runner has no git identity of its own.
        ident = patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        ident.start()
        self.addCleanup(ident.stop)
        self.git = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
        self.git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a")
        self.git("add", ".")
        self.git("commit", "-qm", "first")

    def test_an_editing_thread_gets_its_own_worktree_and_branch(self):
        pt = self.threads(isolation="worktree")
        t = pt.start("Add events")
        self.assertTrue(t["branch"].startswith("vision/calendar/add-events-"))
        self.assertTrue(os.path.isfile(os.path.join(t["workdir"], "a.txt")))
        self.assertEqual(self.built[0][1] if self.built else t["workdir"], t["workdir"])
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        self.assertIn(t["branch"], self.built[0][5])  # the thread is told its branch
        r = pt.start("Just read", edits=False)
        self.assertEqual((r["workdir"], r["branch"]), (str(self.tmp / "work"), ""))

    def test_closing_removes_a_clean_worktree_and_keeps_one_with_work_in_it(self):
        pt = self.threads(isolation="worktree")
        clean = pt.start("Nothing to keep")
        wait_for(lambda: pt.find(clean["id"])["state"] != "working")
        self.assertEqual(pt.resolve(clean["id"])["note"], "worktree removed")
        self.assertFalse(os.path.exists(clean["worktree"]))
        kept = pt.start("Real work")
        wait_for(lambda: pt.find(kept["id"])["state"] != "working")
        Path(kept["workdir"], "b.txt").write_text("b")
        self.git("add", ".", cwd=kept["workdir"])
        self.git("commit", "-qm", "work", cwd=kept["workdir"])
        closed = pt.resolve(kept["id"])
        self.assertIn("aren't merged", closed["note"])
        self.assertTrue(os.path.isdir(kept["worktree"]))

    def _committed(self, pt, name, filename, text):
        t = pt.start(name)
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        Path(t["workdir"], filename).write_text(text)
        self.git("add", ".", cwd=t["workdir"])
        self.git("commit", "-qm", name, cwd=t["workdir"])
        return pt.find(t["id"])

    def test_the_diff_is_what_landing_would_bring_in(self):
        pt = self.threads(isolation="worktree")
        t = self._committed(pt, "Add b", "b.txt", "one\ntwo\n")
        (self.tmp / "work" / "a.txt").write_text("moved on")  # the base moving on isn't the thread's
        self.git("commit", "-qam", "base")
        out = runs.branch_diff(str(self.tmp / "work"), t["branch"], "b.txt")
        self.assertEqual((out["base"], out["commits"]), ("main", 1))
        self.assertEqual(out["files"], [{"path": "b.txt", "added": 2, "removed": 0}])
        self.assertIn("+two", out["patch"])
        self.assertFalse(out["cut"])
        self.assertEqual(runs.commits_ahead(t["worktree"], t["branch"]), 1)
        with self.assertRaises(RuntimeError):
            runs.branch_diff(str(self.tmp / "work"), "vision/nope")

    def test_landing_merges_the_branch_and_closes_the_thread(self):
        pt = self.threads(isolation="worktree")
        t = self._committed(pt, "Add b", "b.txt", "b")
        out = pt.land(t["id"])
        self.assertEqual(out["state"], "resolved")
        self.assertIn("landed", out["note"])
        self.assertEqual((self.tmp / "work" / "b.txt").read_text(), "b")  # on main now
        self.assertFalse(os.path.exists(t["worktree"]))  # merged, so its worktree and branch went
        self.assertNotIn(t["branch"], self.git("branch").stdout)
        with self.assertRaises(ValueError):
            pt.land(pt.start("Look", edits=False)["id"])  # no branch of its own

    def test_a_conflict_or_a_dirty_folder_leaves_everything_as_it_was(self):
        pt = self.threads(isolation="worktree")
        t = self._committed(pt, "Change a", "a.txt", "theirs")
        (self.tmp / "work" / "a.txt").write_text("dirty")
        with self.assertRaises(ValueError) as e:
            pt.land(t["id"])
        self.assertIn("uncommitted changes", str(e.exception))
        self.assertEqual(pt.find(t["id"])["state"], "ready_for_review")
        self.git("commit", "-qam", "ours")
        with self.assertRaises(ValueError) as e:
            pt.land(t["id"])
        self.assertIn("conflicts", str(e.exception))
        self.assertIn("a.txt", str(e.exception))
        self.assertEqual((self.tmp / "work" / "a.txt").read_text(), "dirty")  # the merge was aborted
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")
        t = pt.find(t["id"])
        self.assertEqual(t["state"], "ready_for_review")
        self.assertIn("not landed", t["note"])
        self.assertTrue(os.path.isdir(t["worktree"]))

    def test_a_folder_outside_git_falls_back_to_the_project_folder(self):
        shutil.rmtree(self.tmp / "work" / ".git")
        pt = self.threads(isolation="worktree")
        t = pt.start("Edit things")
        self.assertEqual((t["workdir"], t["branch"]), (str(self.tmp / "work"), ""))
        self.assertIn("no worktree", t["note"])
        wait_for(lambda: pt.find(t["id"])["state"] != "working")  # done writing before the folder goes


class _Brain:
    provider = "claude"
    asked: list = []  # every prompt a chat's brain was sent, across instances

    def __init__(self, cfg, session_id=None, **_):
        self.cfg, self.session_id, self.handoff = cfg, session_id, None
        self.workdir = os.path.abspath(cfg.workdir) if cfg.workdir else os.getcwd()

    def resolved_model(self):
        return self.cfg.model

    def ask(self, prompt, **_):
        _Brain.asked.append(prompt)
        return Turn(text="Noted.", session_id=self.session_id or "c1")

    def new_session(self):
        self.session_id, self.context = None, None

    def cancel(self):
        pass


class ServerTest(ThreadsTestBase):
    def setUp(self):
        super().setUp()
        from vision import server

        for p in (patch.object(projects, "STORE_DIR", self.tmp / "projects"),
                  patch.object(runs, "WORKTREE_DIR", self.tmp / "worktrees"),
                  patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Brain(cfg, **kw)),
                  patch("vision.link.LIVE_DIR", self.tmp / "live"),
                  patch.object(server.Hub, "warm_up", lambda self: None),
                  patch("vision.server.warm_soon", lambda brain: None)):
            p.start()
            self.addCleanup(p.stop)
        cfg = Config()
        self.hub = server.Hub(cfg, _Brain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        self.server = server
        self.auth = {"Authorization": "Bearer tok"}

    def _drain(self, ws, until, limit=60):
        seen = []
        for _ in range(limit):
            seen.append(ws.receive_json())
            if until(seen[-1]):
                return seen
        raise AssertionError(f"never saw the frame; got {[e['type'] for e in seen]}")

    def test_start_list_tell_and_close_over_http_with_frames_and_a_report(self):
        from fastapi.testclient import TestClient

        self.workers = [FakeWorker(ASKS, DONE)]
        _Brain.asked.clear()
        self.hub.REPORT_BATCH_S = 0.05
        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()  # hello
            p = tc.post("/projects", json={"goal": "Add a calendar", "workdir": str(self.tmp / "work"),
                                           "settings": {"isolation": "shared"}}, headers=self.auth).json()
            chat = tc.post(f"/projects/{p['id']}/open", json={}, headers=self.auth).json()
            t = tc.post(f"/projects/{p['id']}/threads", json={"objective": "Set up storage"}, headers=self.auth).json()
            self.assertEqual(t["title"], "Set up storage")
            self.assertNotIn("pending", t)
            frames = self._drain(ws, lambda e: e["type"] == "thread" and e["thread"]["state"] == "waiting_on_you")
            self.assertNotIn("runs", frames[-1]["thread"])  # frames stay small
            # The coordinator gets a report turn: the phone shows its line as a note, the brain gets the data.
            start = self._drain(ws, lambda e: e["type"] == "start" and e.get("note"))[-1]
            self.assertEqual(start["chat"], chat["chat"])
            self.assertIn("needs you: Postgres or SQLite?", start["note"])
            self.assertTrue(start["text"].startswith(projects.REPORT_PREFIX))
            self._drain(ws, lambda e: e["type"] == "done")
            sent = _Brain.asked[-1]
            self.assertTrue(sent.startswith(projects.STATUS_OPEN))  # the live status block rides ahead of it
            self.assertIn("waiting on you · Set up storage", sent)
            self.assertIn('"question": "Postgres or SQLite?"', sent)
            history = tc.get(f"/history?chat={chat['chat']}", headers=self.auth).json()
            self.assertEqual([m["role"] for m in history][-2:], ["note", "assistant"])  # never shown as the user's words
            self.assertEqual(self.hub.threads[p["id"]].unreported(), [])
            listed = tc.get(f"/projects/{p['id']}/threads", headers=self.auth).json()
            self.assertEqual(len(listed[0]["runs"]), 1)
            self.assertEqual(tc.get(f"/projects/{p['id']}", headers=self.auth).json()["threads"]["waiting_on_you"], 1)
            tc.post(f"/projects/{p['id']}/threads/{t['id']}/message", json={"text": "SQLite"}, headers=self.auth)
            self._drain(ws, lambda e: e["type"] == "thread" and e["thread"]["state"] == "ready_for_review")
            closed = tc.post(f"/projects/{p['id']}/threads/{t['id'][:3]}/resolve", json={}, headers=self.auth).json()
            self.assertEqual(closed["state"], "resolved")
            self.assertEqual(tc.get(f"/projects/{p['id']}/threads/nope", headers=self.auth).status_code, 404)
            self.assertEqual(tc.get(f"/projects?chat={chat['chat']}", headers=self.auth).json()[0]["id"], p["id"])

    def test_a_heavy_coordinator_session_starts_afresh_with_a_recap(self):
        from fastapi.testclient import TestClient

        _Brain.asked.clear()
        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            p = tc.post("/projects", json={"goal": "Add a calendar", "workdir": str(self.tmp / "work"),
                                           "settings": {"isolation": "shared"}}, headers=self.auth).json()
            chat_id = tc.post(f"/projects/{p['id']}/open", json={}, headers=self.auth).json()["chat"]
            chat = self.hub.chats[chat_id]
            chat._transcript = [{"role": "user", "text": "Keep dates in UTC"}, {"role": "assistant", "text": "Will do."},
                                {"role": "note", "text": "“Storage” finished: tables in place"}]
            chat.brain.session_id, chat.brain.context = "old", (60_000, 200_000)
            ws.send_json({"type": "message", "chat": chat_id, "text": "How's it going?"})
            self._drain(ws, lambda e: e["type"] == "done")
            self.assertNotIn("fresh session", _Brain.asked[-1])  # light: its own session carries on
            self.assertEqual(chat.brain.session_id, "old")

            chat.brain.context = (130_000, 200_000)
            ws.send_json({"type": "message", "chat": chat_id, "text": "And now?"})
            self._drain(ws, lambda e: e["type"] == "done")
            sent = _Brain.asked[-1]
            self.assertIsNone(chat.brain.session_id)  # a new session, not --resume of the heavy one
            status, said = sent.split(projects.STATUS_CLOSE, 1)
            self.assertIn("fresh session", status)
            self.assertIn("User: Keep dates in UTC", status)  # the recap rides in the hidden block
            self.assertNotIn("tables in place", status)  # reports aren't recapped: the threads are listed anyway
            self.assertEqual(said.strip(), "And now?")
            history = tc.get(f"/history?chat={chat_id}", headers=self.auth).json()
            self.assertEqual(history[0]["text"], "Keep dates in UTC")  # the phone still shows what came before

    def test_finished_threads_wait_to_report_together_but_a_question_comes_at_once(self):
        p = self.hub.projects.upsert({"goal": "Calendar", "workdirs": [str(self.tmp / "work")]})
        with patch.object(self.hub, "push"), patch.object(self.hub, "report_soon") as soon:
            self.hub.thread_stopped(p["id"], {"id": "t1", "title": "A", "state": "ready_for_review", "last_result": DONE})
            self.assertEqual(soon.call_args[1]["delay"], self.hub.REPORT_BATCH_S)
            self.hub.thread_stopped(p["id"], {"id": "t2", "title": "B", "state": "waiting_on_you", "question": "Which?"})
            self.assertEqual(soon.call_args[1]["delay"], self.hub.REPORT_URGENT_S)

    def test_a_push_goes_out_only_when_a_thread_needs_you(self):
        p = self.hub.projects.upsert({"goal": "Calendar", "workdirs": [str(self.tmp / "work")]})
        with patch.object(self.hub, "push") as push:
            self.hub.thread_stopped(p["id"], {"id": "t1", "title": "A", "state": "ready_for_review", "last_result": DONE})
            push.assert_not_called()
            self.hub.thread_stopped(p["id"], {"id": "t2", "title": "B", "state": "waiting_on_you", "question": "Which?"})
            push.assert_called_once()
            self.assertIn("“B” needs you: Which?", push.call_args[0][1])
            self.assertEqual(push.call_args[1], {"project": p["id"], "thread": "t2"})  # a tap opens the thread
            failing = {"id": "t3", "title": "C", "state": "idle", "error": "tests failed"}
            self.hub.thread_stopped(p["id"], failing)
            self.assertEqual(push.call_count, 2)
            self.hub.thread_stopped(p["id"], {**failing, "last_stop": "failed"})  # still failing: no second push
            self.assertEqual(push.call_count, 2)

    def test_memory_pause_and_the_project_library_over_http(self):
        from fastapi.testclient import TestClient

        upload_dir = self.tmp / "uploads"
        upload_dir.mkdir()
        sent = upload_dir / "20261011-101010-abcdef-brief.pdf"
        sent.write_bytes(b"pdf")
        made = self.tmp / "work" / "report.md"
        made.write_text("r")
        with patch.object(self.server, "UPLOAD_DIR", upload_dir), TestClient(self.server.create_app(self.hub)) as tc, \
                tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            p = tc.post("/projects", json={"goal": "Calendar", "workdir": str(self.tmp / "work"),
                                           "settings": {"isolation": "shared"}}, headers=self.auth).json()
            # memory
            r = tc.post(f"/projects/{p['id']}/memory", json={"fact": "Use UTC."}, headers=self.auth).json()
            self.assertEqual(len(r["added"]), 1)
            self.assertEqual(tc.get(f"/projects/{p['id']}/memory", headers=self.auth).json()["facts"][0][-8:], "Use UTC.")
            self.assertEqual(tc.delete(f"/projects/{p['id']}/memory/1", headers=self.auth).json()["facts"], [])
            self.assertEqual(tc.delete(f"/projects/{p['id']}/memory/1", headers=self.auth).status_code, 404)
            # pause and resume
            self.assertEqual(tc.post(f"/projects/{p['id']}/pause", json={}, headers=self.auth).json()["status"], "paused")
            t = tc.post(f"/projects/{p['id']}/threads", json={"objective": "Wait"}, headers=self.auth).json()
            time.sleep(0.1)
            self.assertTrue(self.hub.threads[p["id"]].find(t["id"])["queued"])  # nothing starts while paused
            self.assertIn("PAUSED", self.hub.project_status(p["id"]))
            self.assertEqual(tc.post(f"/projects/{p['id']}/resume", json={}, headers=self.auth).json()["status"], "active")
            wait_for(lambda: self.hub.threads[p["id"]].find(t["id"])["state"] != "working")
            # the project's Library: sent in its chat, and made by a thread
            chat = tc.post(f"/projects/{p['id']}/open", json={}, headers=self.auth).json()
            ws.send_json({"type": "message", "chat": chat["chat"], "text": "see this", "images": [str(sent)]})
            wait_for(lambda: (self.tmp / "projects" / p["id"] / "library.json").exists())
            from vision.library import index_project

            index_project(self.tmp / "projects" / p["id"], [{"path": str(made), "source": "thread", "thread": t["id"]}])
            items = tc.get(f"/library?project={p['id']}", headers=self.auth).json()
            self.assertEqual({i["name"]: i["source"] for i in items}, {"brief.pdf": "you", "report.md": "thread"})
            self.assertEqual(tc.delete(f"/library?project={p['id']}&path={made}", headers=self.auth).status_code, 200)
            self.assertTrue(made.exists())  # a thread's file stays: only its entry went
            self.assertEqual(tc.delete(f"/library?project={p['id']}&path={sent}", headers=self.auth).status_code, 200)
            self.assertFalse(sent.exists())  # something sent from the phone goes for good
            self.assertEqual(tc.get(f"/library?project={p['id']}", headers=self.auth).json(), [])

    def test_a_report_on_a_call_is_spoken_by_the_calls_model(self):
        p = self.hub.projects.upsert({"goal": "Calendar", "workdirs": [str(self.tmp / "work")], "settings": {"isolation": "shared"}})
        pt = self.hub.project_threads(p)
        t = pt.start("Quick")
        wait_for(lambda: pt.find(t["id"])["state"] != "working")
        chat = SimpleNamespace(busy=False, _pending=[], closed=False, auto_steps=0, on_call=lambda: True, queued=[])
        chat.queue = lambda text, speak, voice: chat.queued.append((text, speak, voice))
        import asyncio

        with patch.object(self.hub, "project_chat", return_value=chat):
            asyncio.run(self.hub.report(p["id"]))
        text, speak, voice = chat.queued[0]
        self.assertTrue(text.startswith(projects.REPORT_PREFIX))
        self.assertEqual((speak, voice), (True, True))
        self.assertEqual(chat.auto_steps, 1)
        self.assertEqual(pt.unreported(), [])
        asyncio.run(self.hub.report(p["id"]))  # nothing more owed: no second turn
        self.assertEqual(len(chat.queued), 1)

    def test_a_restarted_hub_resumes_working_threads(self):
        p = self.hub.projects.upsert({"goal": "Calendar", "workdirs": [str(self.tmp / "work")], "settings": {"isolation": "shared"}})
        ThreadStore(self.tmp / "projects" / p["id"]).update(
            "t1", project=p["id"], title="Migrate", objective="Migrate", state="working", queued=False, model="opus",
            effort="medium", session_id="s7", workdir=str(self.tmp / "work"), created=1, runs=[])
        w = FakeWorker(DONE, session="s7")
        self.workers = [w]
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)):
            pt = self.hub.threads[p["id"]]
            wait_for(lambda: pt.find("t1")["state"] == "ready_for_review")
        self.assertEqual(w.resumed, "s7")

    def test_restart_does_not_wait_for_threads(self):
        gate = threading.Event()
        self.workers = [FakeWorker(gate)]
        p = self.hub.projects.upsert({"goal": "Calendar", "workdirs": [str(self.tmp / "work")], "settings": {"isolation": "shared"}})
        pt = self.hub.project_threads(p)
        t = pt.start("Long")
        wait_for(lambda: pt.find(t["id"])["activity"] == "running tests")
        self.assertEqual(self.hub.request_restart("test"), 0)
        self.hub.cancel()
        gate.set()
        time.sleep(0.1)
        self.assertEqual(pt.find(t["id"])["state"], "working")


class CliTest(unittest.TestCase):
    def _run(self, *args, answers=(), env=None):
        from typer.testing import CliRunner

        from vision import cli

        calls, answers = [], list(answers)

        def call(path, body=None, method="", why=""):
            calls.append((path, body, method))
            return answers.pop(0)

        with patch.object(cli, "_serve_call", side_effect=call), patch.dict(os.environ, env or {}, clear=False):
            result = CliRunner().invoke(cli.app, ["thread", *args])
        return result, calls

    def test_new_from_a_project_chat_finds_its_project(self):
        p = {"id": "ab12", "title": "Calendar"}
        t = {"id": "t1", "title": "Add events", "state": "working", "queued": True}
        result, calls = self._run("new", "Add", "events", "--read-only", answers=[[p], t], env={"VISION_CHAT": "c9"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0][0], "/projects?chat=c9")
        self.assertEqual(calls[1][:2], ("/projects/ab12/threads", {"objective": "Add events", "title": "", "edits": False, "after": []}))
        self.assertIn("queued behind the others", result.output)

    def test_list_show_tell_and_close(self):
        p = {"id": "ab12", "title": "Calendar"}
        t = {"id": "t1", "title": "Add events", "objective": "Add events", "state": "waiting_on_you", "question": "Which?",
             "folder": "~/x", "model": "opus", "effort": "high", "updated": 0,
             "runs": [{"at": 0, "kind": "start", "status": "waiting_for_user", "duration_s": 3, "summary": "Need a choice."}]}
        result, calls = self._run("list", "-p", "ab", answers=[p, [t]], env={"VISION_CHAT": ""})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[1][0], "/projects/ab12/threads")
        self.assertIn("Add events", result.output)
        result, _ = self._run("show", "t1", "-p", "ab", answers=[p, t])
        self.assertIn("Which?", result.output)
        result, calls = self._run("tell", "t1", "SQLite", "-p", "ab", answers=[p, {**t, "state": "working"}], env={"VISION_CHAT": ""})
        self.assertEqual(calls[1][:2], ("/projects/ab12/threads/t1/tell", {"text": "SQLite", "direct": True}))  # from a terminal of the user's own
        result, calls = self._run("close", "t1", "-p", "ab", answers=[p, {**t, "state": "resolved", "note": "worktree removed"}])
        self.assertIn("worktree removed", result.output)

    def test_with_several_projects_it_asks_which(self):
        result, _ = self._run("list", answers=[[{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]], env={"VISION_CHAT": ""})
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--project", result.output)


if __name__ == "__main__":
    unittest.main()
