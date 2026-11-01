"""Settings' Permissions page: the three rule lists and scouts' defaults in config.toml's [brain] (permissions.py),
the exceptions that let a call an ask rule caught run without asking, and `GET/POST /permissions`."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from test_leash import _Recorder, _project
from test_toolrules import _brain, _control

from vision import config, permissions, projects
from vision.config import Config
from vision.projects import effective_rules, rule_patterns


class CleanTest(unittest.TestCase):
    def test_lists_are_checked_and_replace_what_was_there(self):
        brain = Config().brain
        brain.ask_tools = ["Write"]
        out = permissions.clean({"ask": ["Bash(git push:*)", " Bash(git push:*) "], "allow": ["Write(docs/**)"]}, brain)
        self.assertEqual(out["ask_tools"], ["Bash(git push:*)"])
        self.assertEqual(out["allow_tools"], ["Write(docs/**)"])
        self.assertEqual(out["denied_tools"], brain.denied_tools)  # not sent: kept
        self.assertEqual(out["scout_rules"], {})
        for body, says in (({"ask": ["git push("]}, "isn't a rule"), ({"ask": [""]}, "empty"), ({"ask": "Write"}, "list"),
                           ({"ask": ["Bash(a\nb)"]}, "one line"), ({"nope": []}, "unknown nope"),
                           ({"allow": ["Bash(sudo:*)"]}, "can't both be never allowed and be allowed"),
                           ({"scouts": {"publish": "sometimes"}}, "publish must be"), ({"scouts": {"fly": "asks"}}, "no such action")):
            with self.assertRaises(ValueError) as e:
                permissions.clean(body, brain)
            self.assertIn(says, str(e.exception), body)

    def test_scouts_defaults_set_and_go_back_to_the_leash(self):
        brain = Config().brain
        brain.scout_rules = {"send": "never"}
        self.assertEqual(permissions.clean({"scouts": {"publish": "asks"}}, brain)["scout_rules"], {"send": "never", "publish": "asks"})
        self.assertEqual(permissions.clean({"scouts": {"send": "default"}}, brain)["scout_rules"], {})


class SaveTest(unittest.TestCase):
    def test_written_in_place_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('[brain]\n# mine\nask_tools = ["Write"]\nmodel = "opus"\n')
            with patch.object(config, "CONFIG_DIR", Path(d)), patch.object(config, "CONFIG_PATH", path):
                brain = config.load_config().brain
                values = permissions.clean({"ask": ['Edit("x")'], "allow": ["Write(docs/**)"], "scouts": {"publish": "asks"}}, brain)
                permissions.save(values, brain)
                text = path.read_text()
                again = config.load_config().brain
        self.assertIn("# mine", text)
        self.assertNotIn("denied_tools", text)  # unchanged lists aren't written
        self.assertEqual((again.ask_tools, again.allow_tools, again.scout_rules), (['Edit("x")'], ["Write(docs/**)"], {"publish": "asks"}))

    def test_claude_codes_own_rules_are_read_from_its_folder(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertIsNone(permissions.claude_rules())
            (Path(d) / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(npm test:*)"], "deny": ["Read(.env)", 3]}}))
            rules = permissions.claude_rules()
        self.assertEqual((rules["allow"], rules["ask"], rules["deny"]), (["Bash(npm test:*)"], [], ["Read(.env)"]))
        self.assertTrue(rules["path"].endswith("settings.json"))


class ExceptionTest(unittest.TestCase):
    def test_claude_runs_an_allowed_exception_without_asking(self):
        brain = _brain(["Bash(git push:*)"])
        brain.cfg.allow_tools = ["Bash(git push origin feature:*)"]
        q, reply = _control(brain, "Bash", {"command": "git push origin feature"}, "No")
        self.assertIsNone(q)
        self.assertEqual(reply["behavior"], "allow")
        q, reply = _control(brain, "Bash", {"command": "git push origin main"}, "No")
        self.assertIsNotNone(q)  # the rest of the ask rule still asks
        self.assertEqual(reply["behavior"], "deny")
        brain.cfg.allow_tools = ["Bash"]
        q, reply = _control(brain, "Write", {"file_path": "/tmp/x"}, "No")
        self.assertIsNotNone(q)  # what Claude Code asks about itself (no rule of Vision's) isn't an exception

    def test_a_local_brain_too(self):
        from vision.local import LocalBrain

        cfg = Config().brain
        cfg.ask_tools, cfg.allow_tools = ["Write"], ["Write(docs/**)"]
        brain = LocalBrain.__new__(LocalBrain)
        brain.cfg, brain.workdir, brain.task_mode = cfg, os.getcwd(), False
        self.assertIsNone(brain._declined("Write", {"file_path": "docs/a.md"}, lambda qs: self.fail("asked"), set()))
        self.assertIn("declined", brain._declined("Write", {"file_path": "src/a.py"}, lambda qs: {qs[0]["question"]: "No"}, set()))


class ScoutDefaultsTest(unittest.TestCase):
    def tearDown(self):
        projects.set_default_rules({})

    def test_every_scout_takes_them_unless_it_set_its_own(self):
        projects.set_default_rules({"publish": "never", "land": "bogus", "fly": "asks"})
        self.assertEqual(projects.DEFAULT_RULES, {"publish": "never"})
        self.assertEqual(effective_rules(_project(leash="autonomous"))["publish"], "never")
        self.assertIn("Bash(git push:*)", rule_patterns(_project(leash="autonomous"))[1])
        self.assertEqual(effective_rules(_project(leash="autonomous", rules={"publish": "does"}))["publish"], "does")
        self.assertEqual(effective_rules(_project())["send"], "asks")  # the leash still decides the rest


class ServerTest(unittest.TestCase):
    def setUp(self):
        from vision import server
        from vision.scheduled import Schedule

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.path = self.tmp / "config.toml"
        self.path.write_text('[brain]\nask_tools = ["Edit(.env)"]\n')
        for p in (patch.object(projects, "STORE_DIR", self.tmp / "projects"),
                  patch.object(config, "CONFIG_DIR", self.tmp), patch.object(config, "CONFIG_PATH", self.path),
                  patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}),
                  patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Recorder(cfg, **kw)),
                  patch("vision.link.LIVE_DIR", self.tmp / "live"),
                  patch("vision.agentlog.AGENTS_DIR", self.tmp / "agents"),
                  patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []),
                  patch.object(server.Hub, "warm_up", lambda self: None),
                  patch("vision.server.warm_soon", lambda brain: None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(projects.set_default_rules, {})
        cfg = Config()
        cfg.brain.ask_tools = ["Edit(.env)"]
        cfg.remote.suggestions = False  # a finished turn's guess runs on a thread that outlives the test
        self.hub = server.Hub(cfg, _Recorder(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        self.hub.schedule = Schedule(self.tmp / "scheduled.json")
        self.hub.push = lambda *a, **kw: None
        self.server = server
        self.auth = {"Authorization": "Bearer tok"}

    def test_round_trip_and_open_chats_take_the_rules(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc:
            got = tc.get("/permissions", headers=self.auth).json()
            self.assertEqual((got["ask"], got["allow"], got["claude"]), (["Edit(.env)"], [], None))
            self.assertIn("Bash(sudo:*)", got["deny"])
            self.assertIn("Bash(sudo:*)", got["builtin"])
            self.assertEqual([s["action"] for s in got["scouts"]], ["land", "publish", "send", "destructive"])
            self.assertEqual(got["scouts"][1]["leash"], {"checkins": "asks", "autonomous": "does"})

            chat = self.hub.open_chat()
            p = tc.post("/projects", json={"goal": "Ship it", "workdir": self.dir.name, "settings": {"leash": "autonomous"}},
                        headers=self.auth).json()
            scout = self.hub.chats[tc.post(f"/projects/{p['id']}/open", headers=self.auth).json()["chat"]]

            saved = tc.post("/permissions", json={"ask": ["Edit(.env)", "Bash(git push:*)"], "allow": ["Bash(git push origin dev:*)"],
                                                  "scouts": {"publish": "never"}}, headers=self.auth).json()
            self.assertEqual(saved["allow"], ["Bash(git push origin dev:*)"])
            self.assertEqual(saved["scouts"][1]["rule"], "never")
            text = self.path.read_text()
            self.assertIn('allow_tools = ["Bash(git push origin dev:*)"]', text)
            self.assertIn('scout_rules = { publish = "never" }', text)
            self.assertIn("Bash(git push:*)", chat.brain.cfg.ask_tools)  # an open chat takes them at once
            self.assertEqual(chat.brain.cfg.allow_tools, ["Bash(git push origin dev:*)"])
            self.assertEqual(scout.brain.cfg.allow_tools, [])  # a scout's chat has no exceptions
            self.assertIn("Bash(git push:*)", scout.brain.cfg.denied_tools)  # and its default publish rule is now never
            self.assertEqual({r["action"]: r["rule"] for r in tc.get(f"/projects/{p['id']}", headers=self.auth).json()["rules"]}["publish"], "never")
            self.assertIn("Bash(git push:*)", self.hub.thread_rules(p["id"])[0])  # the user's own ask rule reaches threads
            self.assertEqual(self.hub.open_chat().brain.cfg.allow_tools, ["Bash(git push origin dev:*)"])  # and new chats

            bad = tc.post("/permissions", json={"deny": ["Bash(git push:*)"]}, headers=self.auth)
            self.assertEqual(bad.status_code, 400)
            self.assertIn("can't both", bad.json()["detail"])
            self.assertEqual(tc.get("/permissions").status_code, 401)

    def test_claude_codes_own_rules_come_along_read_only(self):
        from fastapi.testclient import TestClient

        (self.tmp / "claude").mkdir()
        (self.tmp / "claude" / "settings.json").write_text(json.dumps({"permissions": {"ask": ["Bash(git push:*)"]}}))
        with TestClient(self.server.create_app(self.hub)) as tc:
            self.assertEqual(tc.get("/permissions", headers=self.auth).json()["claude"]["ask"], ["Bash(git push:*)"])


if __name__ == "__main__":
    unittest.main()
