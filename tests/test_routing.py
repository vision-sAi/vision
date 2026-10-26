from __future__ import annotations

import unittest

from vision import routing
from vision.config import Config
from vision.models import CODEX_MODELS
from vision.routing import CANCEL, DELEGATE, LOCAL, SEARCH, WEATHER, OverrideError, parse_command, parse_override, redact, route


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def kind(self, text):
        return route(text, self.cfg).kind

    def test_basic_questions_stay_with_the_front_end(self):
        for text in ("What does HTTP 404 mean?", "Explain recursion simply.", "What is JSON?", "Rewrite this short sentence: the cat sat",
                     "hi", "thanks", "what's the capital of France?", "who was Ada Lovelace"):
            with self.subTest(text=text):
                r = route(text, self.cfg)
                self.assertEqual(r.kind, LOCAL, r.reason)
                self.assertFalse(r.explicit)

    def test_weather_requests_go_to_weatherkit(self):
        for text in ("what's the weather tomorrow", "is it going to rain in Leeds", "how cold is it outside"):
            with self.subTest(text=text):
                self.assertEqual(self.kind(text), WEATHER)

    def test_current_information_goes_to_search_only(self):
        for text in ("what's the latest news on the election", "who won the match last night", "is github down right now",
                     "what's the price of bitcoin today"):
            with self.subTest(text=text):
                self.assertEqual(self.kind(text), SEARCH)

    def test_coding_tasks_default_to_opus_medium(self):
        for text in ("fix the failing test in parser.py", "add a --json flag to the cli", "why does my for loop only run once, here's the code: ```for x in xs: return x```",
                     "write me an essay on roman history", "should I use postgres or sqlite for this project"):
            with self.subTest(text=text):
                r = route(text, self.cfg)
                self.assertEqual((r.kind, r.agent, r.effort), (DELEGATE, "opus", "medium"), r.reason)

    def test_complex_work_selects_opus_high(self):
        for text, why in (("Design a schema for a multi-tenant billing system", "architecture"),
                          ("the build fails intermittently and I have no idea why", "hard debugging"),
                          ("rename the config loader across the whole repo", "repository-wide"),
                          ("add oauth login with jwt tokens", "security"),
                          ("do a thorough comparison of all the queue libraries", "multi-stage research"),
                          ("migrate the sessions from the sqlite database to the api server", "several systems")):
            with self.subTest(text=text):
                r = route(text, self.cfg)
                self.assertEqual((r.kind, r.agent, r.effort), (DELEGATE, "opus", "high"), r.reason)
                self.assertIn(why, r.reason)

    def test_a_task_that_failed_at_medium_is_retried_at_high(self):
        self.assertEqual(route("fix the parser", self.cfg).effort, "medium")
        r = route("fix the parser", self.cfg, failed_before=True)
        self.assertEqual(r.effort, "high")
        self.assertIn("failed at medium", r.reason)

    def test_uncertain_requests_delegate_at_medium(self):
        r = route("Something about the thing we discussed, with the numbers, you know the one", self.cfg)
        self.assertEqual((r.kind, r.effort), (DELEGATE, "medium"))
        self.assertIn("uncertain", r.reason)

    def test_state_changes_are_not_answered_locally(self):
        self.assertEqual(self.kind("remember that my dog is called Rex"), DELEGATE)

    def test_a_waiting_agent_gets_the_plain_answer(self):
        r = route("the second one", self.cfg, waiting=True)
        self.assertEqual(r.kind, routing.ANSWER)
        # ... unless the user explicitly chooses something else
        r = route("what is JSON", self.cfg, parse_override("/local what is JSON", self.cfg.router), waiting=True)
        self.assertEqual(r.kind, LOCAL)

    def test_routing_is_configurable(self):
        self.cfg.router.default_agent, self.cfg.router.default_effort, self.cfg.router.high_effort = "codex", "low", "xhigh"
        self.assertEqual((route("fix the parser", self.cfg).agent, route("fix the parser", self.cfg).effort), ("codex", "low"))
        self.assertEqual(route("redesign the whole architecture", self.cfg).effort, "xhigh")


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.rc = Config().router

    def test_natural_language_choices(self):
        cases = {
            "Use Opus high for this.": (DELEGATE, "opus", "high", ""),
            "Use Opus medium.": (DELEGATE, "opus", "medium", ""),
            "Launch Codex with high effort.": (DELEGATE, "codex", "high", ""),
            "Have Codex handle this.": (DELEGATE, "codex", "", ""),
            "get opus to do it": (DELEGATE, "opus", "", ""),
            "Use opus high for this: refactor the parser": (DELEGATE, "opus", "high", "refactor the parser"),
            "refactor the parser, use codex for this": (DELEGATE, "codex", "", "refactor the parser"),
            "Answer this locally with the local model.": (LOCAL, "", "", ""),
            "answer locally: what is JSON?": (LOCAL, "", "", "what is JSON?"),
            "don't delegate, what's a monad": (LOCAL, "", "", "what's a monad"),
            "Only search the web for this.": (SEARCH, "", "", ""),
            "search only: python 3.14 release date": (SEARCH, "", "", "python 3.14 release date"),
            "cancel that": (CANCEL, "", "", ""),
            "stop the agent": (CANCEL, "", "", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                o = parse_override(text, self.rc)
                self.assertIsNotNone(o)
                self.assertEqual((o.kind, o.agent, o.effort, o.text), expected)

    def test_plain_requests_are_not_overrides(self):
        for text in ("fix the failing test in parser.py", "use the gpt-4 tokenizer in the script", "what does 'use strict' do",
                     "use gpt-9-ultra", "the local variable is shadowed", "search for the bug in the parser", "please stop the server on port 80"):
            with self.subTest(text=text):
                self.assertIsNone(parse_override(text, self.rc))

    def test_commands(self):
        cases = {
            "/agent opus --effort medium": (DELEGATE, "opus", "medium", ""),
            "/agent opus --effort high": (DELEGATE, "opus", "high", ""),
            "/agent codex --effort=medium": (DELEGATE, "codex", "medium", ""),
            "/agent codex --effort high fix the tests": (DELEGATE, "codex", "high", "fix the tests"),
            "/agent codex high fix the tests": (DELEGATE, "codex", "high", "fix the tests"),
            "/opus": (DELEGATE, "opus", "", ""),
            "/codex high write the migration": (DELEGATE, "codex", "high", "write the migration"),
            "/agent opus fix it --effort low": (DELEGATE, "opus", "low", "fix it"),
            "/local what does HTTP 404 mean": (LOCAL, "", "", "what does HTTP 404 mean"),
            "/local": (LOCAL, "", "", ""),
            "/search latest python release": (SEARCH, "", "", "latest python release"),
            "/weather": (WEATHER, "", "", "what's the weather"),
            "/weather Leeds": (WEATHER, "", "", "what's the weather in Leeds"),
            "/cancel": (CANCEL, "", "", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                o = parse_command(text, self.rc)
                self.assertEqual((o.kind, o.agent, o.effort, o.text), expected)
        self.assertIsNone(parse_command("/model opus", self.rc))
        self.assertIsNone(parse_command("fix it", self.rc))

    def test_invalid_agent_names_are_rejected(self):
        for text in ("/agent gemini fix it", "/agent", "/agent haiku --effort low"):
            with self.subTest(text=text), self.assertRaises(OverrideError) as ctx:
                parse_command(text, self.rc)
            self.assertIn("opus", str(ctx.exception))
        with self.assertRaises(OverrideError):
            route("fix it", Config(), routing.Override(DELEGATE, agent="gemini", text="fix it"))

    def test_unsupported_effort_values_are_rejected(self):
        for text in ("/agent opus --effort turbo", "/agent codex --effort", "/opus --effort=11"):
            with self.subTest(text=text), self.assertRaises(OverrideError) as ctx:
                parse_command(text, self.rc)
            self.assertIn("effort", str(ctx.exception))

    def test_explicit_choices_override_the_classifier(self):
        cfg = Config()
        o = parse_override("use codex high for this: what is JSON?", cfg.router)
        r = route(o.text, cfg, o)
        self.assertEqual((r.kind, r.agent, r.effort, r.explicit), (DELEGATE, "codex", "high", True))
        o = parse_override("/local design a distributed billing system", cfg.router)
        r = route(o.text, cfg, o)
        self.assertEqual((r.kind, r.explicit), (LOCAL, True))
        o = parse_override("have opus handle this: redesign the whole architecture", cfg.router)
        self.assertEqual(route(o.text, cfg, o).effort, "high")  # no effort named: the classifier's level applies

    def test_agent_aliases_come_from_config(self):
        rc = Config().router
        self.assertEqual(routing.agent_model(rc, "opus"), "opus")
        self.assertEqual(routing.agent_model(rc, "codex"), CODEX_MODELS[0].alias)
        rc.agents = {"opus": "sonnet", "codex": "gpt-5.5", "grok": "grok-4.6"}
        self.assertEqual(routing.agent_model(rc, "opus"), "sonnet")
        self.assertEqual(routing.agent_model(rc, "codex"), "gpt-5.5")
        self.assertEqual(routing.agent_for_model(rc, "grok-4.6"), "grok")
        self.assertEqual(routing.agent_model(rc, "gemini"), "")
        self.assertEqual(routing.agent_names(rc), ["opus", "codex", "grok"])

    def test_log_lines_are_redacted_and_short(self):
        self.assertEqual(redact("deploy with token=abc123secret and password: hunter2"), "deploy with [redacted] and [redacted]")
        self.assertNotIn("sk-", redact("key sk-abcdefghijklmnop please"))
        self.assertEqual(len(redact("many words " * 50)), 120)
        self.assertIn("[redacted]", redact("blob " + "a1b2c3d4" * 6))
        self.assertNotIn("\n", redact("one\ntwo"))


if __name__ == "__main__":
    unittest.main()
