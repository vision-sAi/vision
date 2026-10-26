from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vision.brain import AgentRun, Turn
from vision.config import Config
from vision.supervisor import Supervisor, SupervisorError, announce, progress_label, report_lines, worker_config

TASK = dict(objective="Fix the failing check", context="ctx", constraints=[], success_criteria=["it passes"])
DONE = dict(status="completed", summary="Fixed the check.", changes=["parser.py"], checks=["tests: passed"], findings=[], question=None)
ASKS = dict(status="needs_input", summary="Need a choice.", changes=[], checks=[], findings=[], question="Which file, parser.py or lexer.py?")


def worker(*turns):
    w = Mock()
    w.cfg = SimpleNamespace(model="opus", effort="medium", denied_tools=["Bash(sudo:*)", "Bash(git push:*)"])
    w.session_id = None
    w.output_tokens = 0
    w.ask.side_effect = list(turns)
    return w


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.router.mode = "on"
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "routing.jsonl"
        self.workers = []
        self.factory = Mock(side_effect=self._make)
        self.sup = Supervisor(self.cfg, self.factory, log_path=self.log)
        self.cli = patch("vision.clis.find_cli", return_value="claude")
        self.cli.start()
        self.addCleanup(self.cli.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, model, effort, denied):
        w = self.workers.pop(0) if self.workers else worker(Turn(data=DONE, session_id="s1"))
        w.cfg.model, w.cfg.effort = model or w.cfg.model, effort or w.cfg.effort
        w.cfg.denied_tools = list(w.cfg.denied_tools) + [d for d in denied if d not in w.cfg.denied_tools]
        return w

    # -- validation ----------------------------------------------------------------------
    def test_invalid_agent_names_are_rejected_before_anything_starts(self):
        for agent in ("gemini", "haiku", ""):
            with self.subTest(agent=agent), self.assertRaises(SupervisorError):
                self.sup.launch(TASK, agent=agent, effort="medium")
        with self.assertRaises(SupervisorError):
            self.sup.launch(TASK, model="haiku", effort="low")  # a bare model must belong to an allowlisted agent
        self.factory.assert_not_called()
        self.assertEqual(self.sup.runs, {})

    def test_unsupported_effort_is_rejected_not_coerced(self):
        with self.assertRaises(SupervisorError) as ctx:
            self.sup.launch(TASK, agent="opus", effort="turbo")
        self.assertIn("turbo", str(ctx.exception))
        self.cfg.router.agents["haiku"] = "haiku"
        with self.assertRaises(SupervisorError) as ctx:
            self.sup.launch(TASK, agent="haiku", effort="high")  # Haiku has no effort setting: no silent downgrade
        self.assertIn("does not support", str(ctx.exception))
        self.factory.assert_not_called()

    def test_unavailable_model_is_a_visible_error_without_fallback(self):
        self.cli.stop()
        with patch("vision.clis.find_cli", side_effect=RuntimeError("Codex CLI ('codex') not found on PATH")):
            with self.assertRaises(SupervisorError) as ctx:
                self.sup.launch(TASK, agent="codex", effort="high")
        self.cli.start()
        self.assertIn("not found", str(ctx.exception))
        self.factory.assert_not_called()  # nothing else was launched instead
        self.cfg.router.agents["opus"] = "claude-99"
        with self.assertRaises(SupervisorError):
            self.sup.launch(TASK, agent="opus", effort="medium")

    def test_a_resolved_launch_runs_on_the_named_model_and_effort(self):
        run = self.sup.launch(TASK, agent="opus", effort="high")
        self.assertEqual((run.agent, run.model, run.effort, run.state), ("opus", "opus", "high", "completed"))
        model, effort, denied = self.factory.call_args.args
        self.assertEqual((model, effort), ("opus", "high"))
        self.assertIn("Bash(git push:*)", denied)  # approval-only commands are denied until granted
        self.assertEqual(json.loads(run.worker.ask.call_args.args[0]), {"type": "vision_task", "task": TASK})
        self.assertEqual(run.result, DONE)
        self.assertEqual(run.session_id, "s1")

    # -- duplicates -----------------------------------------------------------------------
    def test_duplicate_launches_return_the_run_already_made(self):
        w = worker(Turn(data=ASKS, session_id="s1"))
        self.workers.append(w)
        first = self.sup.launch(TASK, agent="opus", effort="medium")
        again = self.sup.launch(TASK, agent="opus", effort="medium")
        self.assertIs(again, first)
        w.ask.assert_called_once()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(first.state, "waiting_for_user")
        # once a run has finished, the same request is a new launch
        first.state = "completed"
        self.assertIsNot(self.sup.launch(TASK, agent="opus", effort="medium"), first)

    # -- questions -----------------------------------------------------------------------
    def test_a_question_pauses_and_the_answer_resumes_the_same_session(self):
        w = worker(Turn(data=ASKS, session_id="s1"), Turn(data=DONE, session_id="s1"))
        self.workers.append(w)
        rows = []
        run = self.sup.launch(TASK, agent="opus", effort="medium", row=AgentRun("r", "Opus 5", "Fix"), on_agent=rows.append)
        self.assertEqual(run.state, "waiting_for_user")
        self.assertEqual(run.question, ASKS["question"])
        self.assertIs(self.sup.waiting(), run)
        self.assertTrue(run.row.done)
        self.assertIn("asks: Which file", run.row.details[1])
        answered = self.sup.answer(run.id, "parser.py")
        self.assertIs(answered, run)
        self.assertEqual(run.state, "completed")
        self.assertEqual(self.factory.call_count, 1)  # the same worker, so the same session
        self.assertEqual(w.ask.call_count, 2)
        envelope = json.loads(w.ask.call_args.args[0])
        self.assertEqual((envelope["type"], envelope["answer"], envelope["run_id"]), ("vision_task_answer", "parser.py", run.id))
        self.assertIsNone(self.sup.waiting())
        self.assertEqual(run.rounds, 1)

    def test_a_finished_run_continues_in_the_same_session_with_a_fresh_budget(self):
        w = worker(Turn(data=DONE, session_id="s1"), Turn(data=DONE, session_id="s1"))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        run.output_tokens, run.rounds = 10**9, 3
        self.assertIs(self.sup.continue_run(run.id, "now the docs"), run)
        self.assertEqual(run.state, "completed")
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(json.loads(w.ask.call_args.args[0]), {"type": "vision_task_continue", "message": "now the docs"})
        self.assertEqual(run.rounds, 0)

    def test_continuing_a_waiting_run_answers_it_and_a_running_one_is_refused(self):
        w = worker(Turn(data=ASKS, session_id="s1"), Turn(data=DONE, session_id="s1"))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.sup.continue_run(run.id, "parser.py")
        self.assertEqual(json.loads(w.ask.call_args.args[0])["type"], "vision_task_answer")
        run.state = "running"
        with self.assertRaises(SupervisorError):
            self.sup.continue_run(run.id, "more")

    def test_a_session_from_before_a_restart_is_resumed_not_restarted(self):
        w = worker(Turn(data=DONE, session_id="s9"), Turn(data=DONE, session_id="s9"))
        self.workers.append(w)
        run = self.sup.launch(TASK, model="opus", allowlist=False, approval=True, session_id="s9", run_id="r1")
        w.resume.assert_called_once_with("s9")
        self.assertEqual(run.id, "r1")
        self.assertEqual(json.loads(w.ask.call_args.args[0])["type"], "vision_task_resume")
        self.assertIn("Bash(git push:*)", run.denied)  # approval rules hold off the allowlist too
        self.workers.append(w)
        self.sup.launch(TASK, model="opus", allowlist=False, session_id="s9", opening={"type": "vision_task_continue", "message": "go"})
        self.assertEqual(json.loads(w.ask.call_args.args[0]), {"type": "vision_task_continue", "message": "go"})

    def test_steer_one_reaches_only_that_run(self):
        gate = threading.Event()
        workers = []
        for _ in range(2):
            w = worker()
            w.ask.side_effect = lambda *a, **k: (gate.wait(5), Turn(data=DONE))[1]
            w.steer = Mock(return_value=True)
            workers.append(w)
        self.workers.extend(workers)
        threads = [threading.Thread(target=self.sup.launch, args=(dict(TASK, objective=f"job {i}"),), kwargs={"agent": "opus", "effort": "medium"})
                   for i in range(2)]
        for t in threads:
            t.start()
        for _ in range(500):
            if len(self.sup.running()) == 2:
                break
            time.sleep(0.01)
        first = next(r for r in self.sup.running() if r.worker is workers[0])
        self.assertTrue(self.sup.steer_one(first.id, "skip it"))
        gate.set()
        for t in threads:
            t.join(5)
        workers[0].steer.assert_called_once()
        workers[1].steer.assert_not_called()
        self.assertFalse(self.sup.steer_one(first.id, "too late"))  # it has stopped

    def test_only_a_waiting_run_takes_an_answer(self):
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        with self.assertRaises(SupervisorError):
            self.sup.answer(run.id, "yes")
        with self.assertRaises(SupervisorError):
            self.sup.answer("nope", "yes")

    def test_a_run_that_keeps_asking_is_stopped(self):
        self.cfg.router.max_rounds = 1
        w = worker(Turn(data=ASKS), Turn(data=ASKS), Turn(data=DONE))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.sup.answer(run.id, "parser.py")
        self.assertEqual(run.state, "waiting_for_user")
        self.sup.answer(run.id, "lexer.py")
        self.assertEqual(run.state, "failed")
        self.assertIn("asked", run.error)
        self.assertEqual(w.ask.call_count, 2)

    def test_a_run_over_its_token_budget_is_not_resumed(self):
        self.cfg.router.max_output_tokens = 1000
        w = worker(Turn(data=ASKS, usage={"output_tokens": 5000}), Turn(data=DONE))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.sup.answer(run.id, "go on")
        self.assertEqual(run.state, "failed")
        self.assertIn("budget", run.error)
        self.assertEqual(w.ask.call_count, 1)

    # -- approvals -----------------------------------------------------------------------
    def test_only_the_users_yes_lifts_an_approval_rule_named_in_the_question(self):
        asks = {**ASKS, "question": "May I run `git push origin main` to publish the fix?"}
        w = worker(Turn(data=asks), Turn(data=DONE))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.assertIn("Bash(git push:*)", w.cfg.denied_tools)
        self.assertEqual(self.sup.grants(run, "no, don't"), [])
        self.assertEqual(self.sup.grants(run, "yes please"), ["Bash(git push:*)"])
        self.sup.answer(run.id, "yes")
        self.assertEqual(run.granted, ["Bash(git push:*)"])
        self.assertNotIn("Bash(git push:*)", w.cfg.denied_tools)
        self.assertIn("Bash(sudo:*)", w.cfg.denied_tools)  # the base deny list is never lifted
        envelope = json.loads(w.ask.call_args.args[0])
        self.assertEqual(envelope["granted"], ["Bash(git push:*)"])
        self.assertNotIn("Bash(git push:*)", envelope["still_denied"])

    def test_a_yes_to_an_unrelated_question_grants_nothing(self):
        w = worker(Turn(data=ASKS), Turn(data=DONE))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.sup.answer(run.id, "yes")
        self.assertEqual(run.granted, [])
        self.assertIn("Bash(git push:*)", w.cfg.denied_tools)

    # -- failures -------------------------------------------------------------------------
    def test_failures_are_never_presented_as_completion(self):
        cases = {
            "prose only": Turn(text="All done, everything passed!"),
            "malformed result": Turn(data={"status": "completed"}),
            "worker error": Turn(is_error=True, error="quota exceeded"),
            "blocked": Turn(data={**DONE, "status": "blocked", "summary": "No write access."}),
            "failed": Turn(data={**DONE, "status": "failed", "summary": "Tests still fail."}),
        }
        for name, turn in cases.items():
            with self.subTest(name):
                w = worker(turn)
                self.workers.append(w)
                run = self.sup.launch({**TASK, "objective": name}, agent="opus", effort="medium")
                self.assertEqual(run.state, "failed")
                self.assertNotEqual(run.result["status"], "completed")
                self.assertTrue(run.error)
        w = Mock()
        w.cfg = SimpleNamespace(model="opus", effort="medium", denied_tools=[])
        w.ask.side_effect = RuntimeError("boom")
        self.workers.append(w)
        run = self.sup.launch({**TASK, "objective": "raises"}, agent="opus", effort="medium")
        self.assertEqual((run.state, run.error), ("failed", "boom"))

    def test_a_cancelled_worker_turn_is_a_cancelled_run(self):
        w = worker(Turn(is_error=True, error="cancelled"))
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.assertEqual(run.state, "cancelled")

    def test_timeout_stops_the_worker_and_reports_timed_out(self):
        self.cfg.router.timeout_s = 0.2
        release = threading.Event()
        w = Mock()
        w.cfg = SimpleNamespace(model="opus", effort="medium", denied_tools=[])

        def slow(*a, **k):
            release.wait(5)
            return Turn(data=DONE)

        w.ask.side_effect = slow
        w.cancel.side_effect = release.set
        self.workers.append(w)
        started = time.monotonic()
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(run.state, "timed_out")
        w.cancel.assert_called_once()
        self.assertEqual(run.result["status"], "failed")
        self.assertIsNone(self.sup.active)

    # -- cancellation -----------------------------------------------------------------------
    def test_cancel_terminates_the_named_run_only(self):
        a_w, b_w = worker(Turn(data=ASKS)), worker(Turn(data=ASKS))
        self.workers += [a_w, b_w]
        a = self.sup.launch({**TASK, "objective": "a"}, agent="opus", effort="medium")
        b = self.sup.launch({**TASK, "objective": "b"}, agent="opus", effort="medium")
        self.assertIs(self.sup.cancel(a.id), a)
        self.assertEqual((a.state, b.state), ("cancelled", "waiting_for_user"))
        self.assertIsNone(a.question)
        with self.assertRaises(SupervisorError):
            self.sup.answer(a.id, "parser.py")  # a cancelled run is over
        self.assertIs(self.sup.waiting(), b)
        self.assertIs(self.sup.cancel(), b)  # no id: the waiting run
        self.assertIsNone(self.sup.cancel())

    def test_cancel_while_running_stops_that_worker(self):
        release = threading.Event()
        w = Mock()
        w.cfg = SimpleNamespace(model="opus", effort="medium", denied_tools=[])

        def slow(*a, **k):
            self.assertIsNotNone(self.sup.active)
            self.sup.cancel(self.sup.active.id)
            release.wait(2)
            return Turn(is_error=True, error="cancelled")

        w.ask.side_effect = slow
        w.cancel.side_effect = release.set
        self.workers.append(w)
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        w.cancel.assert_called_once()
        self.assertEqual(run.state, "cancelled")

    # -- reporting -------------------------------------------------------------------------
    def test_status_and_report_carry_facts_not_prose(self):
        run = self.sup.launch(TASK, agent="opus", effort="medium")
        status = self.sup.status(run.id)
        self.assertEqual((status["state"], status["model"], status["effort"]), ("completed", "opus", "medium"))
        lines = report_lines(run)
        self.assertEqual(lines[0], "completed · Opus 5 · medium")
        self.assertEqual(lines[1], "Fixed the check.")
        self.assertIn("changed: parser.py", lines)
        self.assertIn("checks: tests: passed", lines)
        self.assertEqual(announce("opus", "medium"), "Launching Opus 5 · medium effort")
        call = SimpleNamespace(name="Bash", detail="pytest tests -q")
        self.assertEqual(progress_label("Opus 5", call), "Opus 5 is running tests…")
        self.assertEqual(progress_label("Opus 5", SimpleNamespace(name="Read", detail="x.py")), "Opus 5 is inspecting the workspace…")

    def test_the_log_records_decisions_without_secrets_or_worker_output(self):
        self.sup.launch({**TASK, "objective": "deploy with token=abc123secret"}, agent="opus", effort="medium")
        lines = [json.loads(l) for l in self.log.read_text().splitlines()]
        events = [l["event"] for l in lines]
        self.assertEqual(events, ["launch", "completed"])
        self.assertEqual(lines[1]["state"], "completed")
        text = self.log.read_text()
        self.assertNotIn("abc123secret", text)
        self.assertNotIn("Fixed the check", text)

    def test_worker_config_is_a_copy_with_the_approval_rules_added(self):
        base = Config().brain
        cfg = worker_config(base, "codex", "high", ["Bash(git push:*)", "Bash(sudo:*)"])
        self.assertIsNot(cfg, base)
        self.assertEqual((cfg.model, cfg.effort), ("codex", "high"))
        self.assertEqual(cfg.denied_tools.count("Bash(sudo:*)"), 1)
        self.assertIn("Bash(git push:*)", cfg.denied_tools)
        self.assertNotIn("Bash(git push:*)", base.denied_tools)
        self.assertEqual(cfg.approval_rules, ["Bash(git push:*)", "Bash(sudo:*)"])


if __name__ == "__main__":
    unittest.main()
