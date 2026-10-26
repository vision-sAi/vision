"""[brain].ask_tools: the rule matcher, the flags Claude Code gets, the approval form round trip, and
the local brain refusing a declined call."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vision import toolrules
from vision.brain import Brain, Turn
from vision.config import Config


class MatcherTests(unittest.TestCase):
    def test_bash_prefix_exact_and_bare_forms(self):
        m = toolrules.matches
        self.assertTrue(m("Bash(git push:*)", "Bash", {"command": "git push origin main"}))
        self.assertTrue(m("Bash(git push *)", "Bash", {"command": "cd x && git push"}))  # Claude Code's newer spelling, nested
        self.assertFalse(m("Bash(git push:*)", "Bash", {"command": "git pull"}))
        self.assertTrue(m("Bash(npm run build)", "Bash", {"command": "npm run build"}))
        self.assertFalse(m("Bash(npm run build)", "Bash", {"command": "npm run build --watch"}))
        self.assertTrue(m("Bash", "Bash", {"command": "ls"}))
        self.assertTrue(m("Bash(*)", "Bash", {"command": "ls"}))
        self.assertFalse(m("Bash", "Read", {"file_path": "x"}))
        self.assertFalse(m("not a rule(", "Bash", {"command": "ls"}))

    def test_path_globs_match_relative_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as d:
            m = toolrules.matches
            self.assertTrue(m("Edit(.env)", "Edit", {"file_path": os.path.join(d, ".env")}, d))
            self.assertTrue(m("Edit(docs/**)", "Edit", {"file_path": "docs/guide.md"}, d))
            self.assertTrue(m("Edit(docs/**)", "Edit", {"file_path": os.path.join(d, "docs/a/b.md")}, d))
            self.assertFalse(m("Edit(docs/**)", "Edit", {"file_path": "src/a.py"}, d))
            self.assertTrue(m("Write", "Write", {"file_path": "anything"}, d))
            self.assertFalse(m("Read(secrets/**)", "Read", {"file_path": "README.md"}, d))

    def test_parameter_and_domain_specifiers(self):
        m = toolrules.matches
        self.assertTrue(m("WebFetch(domain:example.com)", "WebFetch", {"url": "https://docs.example.com/x"}))
        self.assertFalse(m("WebFetch(domain:example.com)", "WebFetch", {"url": "https://example.org/"}))
        self.assertTrue(m("WebSearch(query:how to*)", "WebSearch", {"query": "how to fly"}))
        self.assertFalse(m("WebSearch(query:how to*)", "WebSearch", {"query": "why fly"}))
        self.assertTrue(m("mcp__*", "mcp__github__push", {}))

    def test_first_match_and_summary(self):
        rules = ["Bash(git push:*)", "Write"]
        self.assertEqual(toolrules.first_match(rules, "Bash", {"command": "git push"}), "Bash(git push:*)")
        self.assertEqual(toolrules.first_match(rules, "Write", {"file_path": "a"}), "Write")
        self.assertIsNone(toolrules.first_match(rules, "Bash", {"command": "ls"}))
        self.assertEqual(toolrules.summary("Bash", {"command": "git push", "description": "push it"}), "git push")
        self.assertTrue(toolrules.summary("Edit", {"file_path": "/tmp/x.py"}).startswith("Edit: "))
        q = toolrules.ask_question("Bash", {"command": "git push"}, "Bash(git push:*)")
        self.assertIn("git push", q["question"])
        self.assertEqual([o["label"] for o in q["options"]][0], "Yes")


def _brain(ask_tools, mode="auto") -> Brain:
    cfg = Config().brain
    cfg.ask_tools = list(ask_tools)
    cfg.mode = mode
    with patch("vision.brain.find_claude", return_value="claude"):
        return Brain(cfg)


def _control(brain: Brain, tool: str, tool_input: dict, answer):
    """One can_use_tool request through _answer_control; returns (question asked or None, Claude's reply)."""
    proc = SimpleNamespace(stdin=io.StringIO())
    asked = []

    def on_question(qs):
        asked.extend(qs)
        return {qs[0]["question"]: answer} if answer else None

    brain._answer_control(proc, {"request_id": "r1", "request": {"tool_name": tool, "input": tool_input}},
                          on_question, lambda _: None, Turn())
    return (asked[0] if asked else None), json.loads(proc.stdin.getvalue())["response"]["response"]


class ClaudeAskTests(unittest.TestCase):
    def test_ask_rules_go_in_as_claude_code_ask_permissions_and_auto_flags_stay(self):
        cmd = _brain(["Bash(git push:*)"])._command()
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings["permissions"], {"ask": ["Bash(git push:*)"]})
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--permission-prompt-tool", cmd)
        plain = _brain([])._command()
        self.assertNotIn("permissions", json.loads(plain[plain.index("--settings") + 1]))

    def test_the_form_allows_denies_and_remembers_for_the_turn(self):
        brain = _brain(["Bash(git push:*)"])
        call = {"command": "git push origin main"}
        q, reply = _control(brain, "Bash", call, "Yes")
        self.assertIn("git push origin main", q["question"])
        self.assertEqual(reply["behavior"], "allow")
        q, reply = _control(brain, "Bash", call, "No")
        self.assertEqual(reply["behavior"], "deny")
        self.assertIn("declined", reply["message"])
        q, reply = _control(brain, "Bash", call, "Yes, for the rest of this turn")
        self.assertEqual(reply["behavior"], "allow")
        q, reply = _control(brain, "Bash", {"command": "git push --tags"}, "No")
        self.assertIsNone(q)  # not asked again this turn
        self.assertEqual(reply["behavior"], "allow")
        brain._turn_allowed.clear()  # what ask() does at the next turn
        q, reply = _control(brain, "Bash", call, "No")
        self.assertIsNotNone(q)
        self.assertEqual(reply["behavior"], "deny")

    def test_a_dismissed_form_or_no_form_denies(self):
        brain = _brain(["Write"])
        q, reply = _control(brain, "Write", {"file_path": "/tmp/x"}, None)
        self.assertEqual(reply["behavior"], "deny")
        proc = SimpleNamespace(stdin=io.StringIO())
        brain._answer_control(proc, {"request_id": "r2", "request": {"tool_name": "Write", "input": {}}}, None, lambda _: None, Turn())
        self.assertEqual(json.loads(proc.stdin.getvalue())["response"]["response"]["behavior"], "deny")

    def test_plan_mode_and_workers_keep_their_answers(self):
        brain = _brain(["Bash(git push:*)"], mode="plan")
        q, reply = _control(brain, "Bash", {"command": "git push"}, "Yes")
        self.assertIsNone(q)
        self.assertEqual(reply["behavior"], "deny")
        self.assertIn("plan mode", reply["message"])
        brain._plan_approved = True
        q, reply = _control(brain, "Bash", {"command": "ls"}, "No")
        self.assertIsNone(q)  # an approved plan runs its tools without asking
        self.assertEqual(reply["behavior"], "allow")
        q, reply = _control(brain, "Bash", {"command": "git push"}, "No")
        self.assertIsNotNone(q)  # ...except the ones the ask list names
        self.assertEqual(reply["behavior"], "deny")
        worker = _brain(["Bash(git push:*)"])
        worker.task_mode = True
        q, reply = _control(worker, "Bash", {"command": "git push"}, "Yes")
        self.assertIsNone(q)
        self.assertEqual(reply["behavior"], "deny")
        self.assertIn("needs_input", reply["message"])


class LocalAskTests(unittest.TestCase):
    def _local(self, ask_tools, mode="auto", task_mode=False):
        from vision.local import LocalBrain

        cfg = Config().brain
        cfg.ask_tools = list(ask_tools)
        cfg.mode = mode
        brain = LocalBrain.__new__(LocalBrain)  # no server needed: only the ask check is under test
        brain.cfg, brain.workdir, brain.task_mode = cfg, os.getcwd(), task_mode
        return brain

    def test_declined_runs_allows_and_refuses(self):
        brain = self._local(["Bash(rm -rf:*)"])
        allowed: set[str] = set()
        self.assertIsNone(brain._declined("Bash", {"command": "ls"}, lambda qs: {qs[0]["question"]: "No"}, allowed))
        err = brain._declined("Bash", json.dumps({"command": "rm -rf build"}), lambda qs: {qs[0]["question"]: "No"}, allowed)
        self.assertTrue(err.startswith("Error:"))
        self.assertIn("rm -rf build", err)
        self.assertIsNone(brain._declined("Bash", {"command": "rm -rf build"}, lambda qs: {qs[0]["question"]: "Yes"}, allowed))
        self.assertEqual(allowed, set())
        self.assertIsNone(brain._declined("Bash", {"command": "rm -rf build"}, lambda qs: {qs[0]["question"]: "Yes, for the rest of this turn"}, allowed))
        self.assertEqual(allowed, {"Bash(rm -rf:*)"})
        self.assertIsNone(brain._declined("Bash", {"command": "rm -rf dist"}, lambda qs: self.fail("asked again"), allowed))
        self.assertIn("nobody is here", brain._declined("Bash", {"command": "rm -rf x"}, None, set()))
        self.assertIsNone(self._local(["Bash(rm -rf:*)"], mode="plan")._declined("Bash", {"command": "rm -rf x"}, None, set()))
        self.assertIn("needs_input", self._local(["Bash(rm -rf:*)"], task_mode=True)._declined("Bash", {"command": "rm -rf x"}, None, set()))


class PromptTests(unittest.TestCase):
    def test_enforced_providers_are_told_to_wait_and_the_rest_to_ask(self):
        from vision.persona import system_prompt

        rules = ["Bash(git push:*)"]
        claude = system_prompt(False, tools=["Bash"], provider="claude", ask_tools=rules)
        self.assertIn("pause for the user's approval", claude)
        codex = system_prompt(False, tools=["Bash"], provider="codex", ask_tools=rules)
        self.assertIn("Nothing enforces this for you", codex)
        self.assertIn("Bash(git push:*)", codex)
        self.assertNotIn("git push", system_prompt(False, tools=["Bash"], provider="codex", ask_tools=rules, mode="plan"))
        self.assertNotIn("git push", system_prompt(False, tools=["Bash"], provider="codex"))

    def test_config_reads_and_cleans_the_list(self):
        from vision import config

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('[brain]\nask_tools = ["Bash(git push:*)", " ", "Write"]\n')
            with patch.object(config, "CONFIG_PATH", config.Path(path)), patch.object(config, "ensure_dirs"):
                cfg = config.load_config()
            self.assertEqual(cfg.brain.ask_tools, ["Bash(git push:*)", "Write"])


if __name__ == "__main__":
    unittest.main()
