"""Grok over agent mode (vision/grok_acp.py), against tests/fake_grok_agent.py."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_exe import python_command
from vision.config import BrainConfig, GrokConfig
from vision.grok import GrokBrain

FAKE = Path(__file__).with_name("fake_grok_agent.py")
SID = "01a0e2cd-0000-7000-8000-000000000001"


class GrokAgentModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.log = tmp / "log.jsonl"
        exe = python_command(tmp / "grok", f"import runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
        self._patches = [patch("vision.grok.find_grok", return_value=str(exe)), patch("vision.grok.STATE_DIR", tmp),
                         patch("vision.grok.LAST_SESSION_FILE", tmp / "last"), patch("vision.grok.USAGE_FILE", tmp / "usage"),
                         patch.dict(os.environ, {"FAKE_GROK_LOG": str(self.log)})]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def brain(self, mode="auto", session_id=None) -> GrokBrain:
        cfg = BrainConfig(model="grok-4.6", effort="low", mode=mode, workdir=self.tmp.name)
        cfg.grok = GrokConfig()
        brain = GrokBrain(cfg, session_id=session_id)
        self.addCleanup(brain.close)  # the agent kept between turns
        return brain

    def test_the_next_turn_reuses_the_kept_agent(self):
        brain = self.brain()
        self.assertEqual(brain.ask("hi").text, "Hello there.")
        kept = brain._acp
        self.assertIsNotNone(kept)
        self.assertEqual(brain.ask("hi").text, "Hello there.")
        self.assertIs(brain._acp, kept)
        methods = [m.get("method") for m in self.sent()]
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods.count("session/prompt"), 2)
        self.assertNotIn("session/load", methods)
        brain.cfg.effort = "high"  # another command line: a new agent, loading the session
        self.assertEqual(brain.ask("hi").text, "Hello there.")
        self.assertIsNot(brain._acp, kept)
        methods = [m.get("method") for m in self.sent()]
        self.assertEqual((methods.count("initialize"), methods.count("session/load")), (2, 1))

    def sent(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_streams_the_reply_and_opens_a_session_without_yolo(self):
        brain = self.brain()
        pieces, statuses = [], []
        turn = brain.ask("hi", on_text=pieces.append, on_status=statuses.append)
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(turn.text, "Hello there.")
        self.assertEqual(turn.session_id, SID)
        self.assertEqual(brain.session_id, SID)
        self.assertEqual(turn.usage["input_tokens"], 100)
        self.assertEqual(turn.usage["cache_read_input_tokens"], 50)
        self.assertEqual(brain.context[0], 1234)
        self.assertIn("thinking", " ".join(statuses).lower())
        log = self.sent()
        self.assertEqual(log[0]["argv"], ["agent", "--no-leader", "--model", "grok-4.6", "--reasoning-effort", "low", "stdio"])
        self.assertEqual(log[0]["sandbox"], "off")
        new = next(m for m in log if m.get("method") == "session/new")
        self.assertIs(new["params"]["_meta"]["yoloMode"], False)
        self.assertIn("Vision", new["params"]["_meta"]["rules"])
        self.assertEqual(new["params"]["cwd"], self.tmp.name)

    def test_resume_loads_the_session_and_skips_its_replay(self):
        pieces = []
        turn = self.brain(session_id=SID).ask("hi", on_text=pieces.append)
        self.assertEqual(turn.text, "Hello there.")
        self.assertNotIn("OLD REPLY", "".join(pieces))
        methods = [m.get("method") for m in self.sent()]
        self.assertIn("session/load", methods)
        self.assertNotIn("session/new", methods)

    def test_a_session_that_cannot_load_starts_fresh(self):
        turn = self.brain(session_id="01a0e2cd-dead-7000-8000-00000000beef").ask("hi")
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(turn.session_id, SID)
        self.assertIn("session/new", [m.get("method") for m in self.sent()])

    def test_permission_is_granted_once_and_the_call_is_a_live_row(self):
        rows = []
        turn = self.brain().ask("run echo hi", on_tool=lambda c: rows.append((c.name, c.detail, c.done, c.is_error, c.output)))
        self.assertEqual(turn.text, "permission yes.")
        self.assertEqual(turn.tools_used, ["Bash"])
        self.assertEqual(rows[0][:3], ("Bash", "the command", False))
        self.assertEqual(rows[-1], ("Bash", "the command", True, False, "hi\n"))

    def test_deny_rules_reject_the_command(self):
        seen = []
        brain = self.brain()
        turn = brain.ask("run sudo rm -rf /opt", on_text=seen.append)
        # Grok cancels the turn on a rejection; Vision says why instead of showing a bare "cancelled"
        self.assertFalse(turn.is_error)
        self.assertEqual(turn.text, "I didn't run `sudo rm -rf /opt`: it matches your Bash(sudo:*) rule.")
        self.assertEqual("".join(seen), turn.text)
        self.assertTrue(turn.tools[0].is_error)
        self.assertTrue(brain.session_id)

    def test_plan_mode_runs_in_the_read_only_sandbox(self):
        self.brain(mode="plan").ask("hi")
        self.assertEqual(self.sent()[0]["sandbox"], "read-only")

    def test_a_message_steered_mid_reply_joins_the_turn(self):
        brain = self.brain()
        self.assertFalse(brain.steer("too early"))  # nothing running: the caller queues it
        steered = []

        def on_status(status):  # the thought before the reply: the turn is running
            if not steered:
                steered.append(None)
                threading.Thread(target=lambda: steered.__setitem__(0, brain.steer("use the blue one"))).start()

        turn = brain.ask("slow", on_status=on_status)
        self.assertEqual(steered, [True])
        self.assertEqual(turn.text, "Working on it. Noted: use the blue one.")
        interject = next(m for m in self.sent() if m.get("method") == "_x.ai/interject")
        self.assertEqual(interject["params"], {"sessionId": SID, "text": "use the blue one"})
        self.assertFalse(brain.steer("after"))  # the turn is over

    def test_a_message_that_lands_as_grok_finishes_is_waited_for(self):
        brain = self.brain()
        steered = []

        def on_tool(call):
            if call.done and not steered:
                steered.append(None)
                threading.Thread(target=lambda: steered.__setitem__(0, brain.steer("use red"))).start()

        turn = brain.ask("late", on_tool=on_tool)
        self.assertEqual(steered, [True])
        self.assertIn("Late: use red.", turn.text)

    def test_a_rate_limit_is_the_turn_error(self):
        turn = self.brain().ask("limit")
        self.assertTrue(turn.is_error)
        self.assertIn("429", turn.error)

    def test_worker_turns_and_the_headless_setting_use_grok_p(self):
        brain = self.brain()
        brain.cfg.grok.transport = "headless"
        with patch("vision.grok.subprocess.Popen", side_effect=OSError("headless")) as popen:
            with self.assertRaises(OSError):
                brain.ask("hi")
        self.assertIn("--prompt-file", popen.call_args[0][0])
        brain.cfg.grok.transport = "acp"
        brain.task_mode = True
        with patch("vision.grok.subprocess.Popen", side_effect=OSError("headless")) as popen:
            with self.assertRaises(OSError):
                brain.ask("hi")
        self.assertIn("--prompt-file", popen.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
