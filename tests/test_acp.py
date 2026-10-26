"""A config-defined ACP agent (vision/acp.py) against tests/fake_acp_agent.py."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vision import config, providers
from vision.brain import create_brain

FAKE = Path(__file__).with_name("fake_acp_agent.py")
SID = "acp-session-0001"


class AcpProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.log = tmp / "log.jsonl"
        bindir = tmp / "bin"
        bindir.mkdir()
        exe = bindir / "gem"
        exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        exe.chmod(0o755)
        (tmp / "config.toml").write_text(
            "[providers.gem]\ntype = \"acp\"\ncommand = [\"gem\", \"--acp\"]\nlabel = \"Gem\"\nmodel_flag = \"--model\"\n"
            "models = [\"default\", \"flash\"]\n[providers.gem.env]\nFAKE_ACP_MARK = \"yes\"\n", encoding="utf-8")
        self._patches = [patch.object(config, "CONFIG_PATH", tmp / "config.toml"), patch.object(config, "ensure_dirs"),
                         patch("vision.local.SESSIONS_DIR", tmp / "sessions"),
                         patch.dict(os.environ, {"FAKE_ACP_LOG": str(self.log), "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"})]
        for p in self._patches:
            p.start()
        self.cfg = config.load_config()
        self.cfg.brain.model, self.cfg.brain.effort, self.cfg.brain.mode, self.cfg.brain.workdir = "gem/default", "", "auto", self.tmp.name
        providers.forget_ready()

    def tearDown(self):
        providers.register_endpoints({})
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def sent(self, method):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip() and json.loads(l).get("method") == method]

    def test_registered_ready_and_listed(self):
        from vision import clis, models

        p = providers.REGISTRY["gem"]
        self.assertEqual((p.source, p.cli, p.acp.command, p.acp.model_flag, p.acp.env), ("config", "gem", ("gem", "--acp"), "--model", (("FAKE_ACP_MARK", "yes"),)))
        self.assertTrue(providers.ready("gem", self.cfg))
        self.assertTrue(clis.refresh_acp_models("gem"))
        self.assertEqual([m.alias for m in models._LISTS["gem"]], ["gem/default", "gem/flash"])
        self.assertEqual(models.provider_for("gem/flash"), "gem")
        self.assertIn(("Gem", [("gem/default", "default", "Gem's own default model"), ("gem/flash", "flash", "one of Gem's models")], "via `gem --acp` · an ACP agent"),
                      models.MODEL_TABS)
        self.assertEqual(clis.find_cli("gem").split(os.sep)[-1], "gem")
        self.assertIn("gem", providers.REGISTRY["gem"].describe(self.cfg)["setup"])

    def test_a_turn_streams_text_and_tools_with_the_persona_first(self):
        brain = create_brain(self.cfg.brain)
        self.assertEqual((type(brain).__name__, brain.provider), ("AcpBrain", "gem"))
        pieces, statuses, rows = [], [], []
        turn = brain.ask("hello", on_text=pieces.append, on_status=statuses.append, on_tool=lambda c: rows.append((c.name, c.detail, c.done, c.output)))
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual("".join(pieces), "Hello with instructions.")
        self.assertEqual(turn.session_id, SID)
        self.assertEqual(turn.tools_used, ["Read"])
        self.assertEqual(rows[0][:3], ("Read", "README.md", False))
        self.assertEqual(rows[-1], ("Read", "README.md", True, "# Hi"))
        self.assertIn("thinking", statuses)
        self.assertEqual(brain.context, (321, brain.context_window()))
        prompt = self.sent("session/prompt")[0]["params"]["prompt"][0]["text"]
        self.assertTrue(prompt.startswith("<instructions>\nYou are Vision"))
        self.assertIn("Gem, an agent Vision drives over ACP", prompt)
        self.assertTrue(prompt.endswith("\nhello"))
        first = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual((first["argv"], first["env_mark"]), (["--acp"], "yes"))  # the default model: no --model
        # the second turn goes to the same, kept process: no new start, no replay, the prompt bare
        turn2 = brain.ask("again", on_text=pieces.append)
        self.assertFalse(turn2.is_error)
        self.assertEqual(len(self.sent("initialize")), 1)
        self.assertEqual(self.sent("session/load"), [])
        self.assertEqual(self.sent("session/prompt")[1]["params"]["prompt"][0]["text"], "again")
        self.assertEqual(turn2.session_id, SID)
        # not kept (keep_alive_s = 0): the next turn starts the agent again and resumes the session
        brain.cfg.keep_alive_s = 0
        turn3 = brain.ask("once more", on_text=pieces.append)
        self.assertFalse(turn3.is_error)
        self.assertEqual(len(self.sent("initialize")), 2)
        self.assertEqual(self.sent("session/load")[0]["params"]["sessionId"], SID)
        self.assertEqual(self.sent("session/prompt")[2]["params"]["prompt"][0]["text"], "once more")
        self.assertNotIn("OLD REPLY", "".join(pieces))
        self.assertIsNone(brain._acp)  # one turn, then gone
        brain.close()

    def test_a_kept_agent_is_closed_when_idle_or_on_a_new_session(self):
        self.cfg.brain.keep_alive_s = 0.05
        brain = create_brain(self.cfg.brain)
        brain.ask("hi")
        for _ in range(200):
            if brain._acp is None:
                break
            time.sleep(0.01)
        self.assertIsNone(brain._acp)  # idle long enough: gone
        self.cfg.brain.keep_alive_s = 600
        brain.ask("hi")
        kept = brain._acp
        self.assertIsNotNone(kept)
        brain.cancel()  # nothing running: a cancel only closes the kept process
        self.assertIsNone(brain._acp)
        self.assertIsNotNone(kept.proc.poll())
        brain.ask("hi")
        brain.new_session()  # /new: the process on the old session goes
        self.assertIsNone(brain._acp)

    def test_a_named_model_is_passed_with_its_flag(self):
        self.cfg.brain.model = "gem/flash"
        brain = create_brain(self.cfg.brain)
        brain.ask("hi")
        brain.close()
        self.assertEqual(json.loads(self.log.read_text().splitlines()[0])["argv"], ["--acp", "--model", "flash"])
        self.assertEqual(brain.resolved_model(), "flash")

    def test_sessions_are_visions_own_transcript(self):
        from vision import sessions

        brain = create_brain(self.cfg.brain)
        turn = brain.ask("hello")
        listed = sessions.list_sessions("gem")
        self.assertEqual([(s.id, s.provider, s.title) for s in listed], [(SID, "gem", "hello")])
        self.assertEqual([m["text"] for m in sessions.session_history("gem", SID)], ["hello", "Hello with instructions."])
        self.assertEqual(sessions.list_sessions("local"), [])
        # a session the agent has lost: a fresh one, with the earlier turns handed over in the first prompt
        lost = create_brain(self.cfg.brain, session_id="acp-gone")
        self.assertEqual(lost._messages, [])  # nothing saved under that id here either
        lost._messages = [{"role": "user", "content": "earlier q"}, {"role": "assistant", "content": "earlier a"}]
        turn2 = lost.ask("next")
        self.assertFalse(turn2.is_error, turn2.error)
        self.assertEqual(turn2.session_id, SID)
        prompt = self.sent("session/prompt")[-1]["params"]["prompt"][0]["text"]
        self.assertIn("<earlier_conversation>\nUser: earlier q\nAssistant: earlier a\n</earlier_conversation>", prompt)
        self.assertEqual([m["content"] for m in lost._messages][-2:], ["next", "Hello with instructions."])
        self.assertEqual(len(lost._messages), 4)

    def test_denied_command_is_refused_and_explained(self):
        brain = create_brain(self.cfg.brain)
        seen = []
        turn = brain.ask("run sudo rm -rf /", on_text=seen.append)
        self.assertFalse(turn.is_error)
        self.assertEqual(turn.text, "I didn't run `sudo rm -rf /`: it matches your Bash(sudo:*) rule.")
        self.assertEqual("".join(seen), turn.text)
        self.assertTrue(turn.tools[0].is_error)
        answer = [json.loads(l) for l in self.log.read_text().splitlines() if "result" in l and "outcome" in l][-1]
        self.assertEqual(answer["result"]["outcome"], {"outcome": "selected", "optionId": "deny"})
        turn = brain.ask("run ls")
        self.assertEqual(turn.text, "Done: it ran.")
        self.assertFalse(turn.tools[0].is_error)

    def test_plan_mode_refuses_writes_and_steer_queues(self):
        self.cfg.brain.mode = "plan"
        brain = create_brain(self.cfg.brain)
        self.assertFalse(brain.steer("later"))
        turn = brain.ask("hello")
        self.assertFalse(turn.is_error)
        self.assertIn("PLAN MODE", self.sent("session/prompt")[0]["params"]["prompt"][0]["text"])

    def test_cancel_ends_the_turn(self):
        brain = create_brain(self.cfg.brain)
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("turn", brain.ask("slow")))
        t.start()
        for _ in range(100):
            if brain._acp is not None and brain._acp.session_id:
                break
            time.sleep(0.05)
        time.sleep(0.3)
        brain.cancel()
        t.join(10)
        self.assertEqual((box["turn"].is_error, box["turn"].error), (True, "cancelled"))
        self.assertTrue(self.sent("session/cancel"))

    def test_missing_agent_is_not_ready_and_a_turn_says_so(self):
        providers.forget_ready()
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            providers.forget_ready()
            self.assertFalse(providers.ready("gem", self.cfg))
            self.assertIn("isn't set up", providers.unavailable_reason("gem", self.cfg))


if __name__ == "__main__":
    unittest.main()
