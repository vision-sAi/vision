"""Failover when a subscription runs out, and the quota figure in the input box's corner."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vision import failover
from vision.config import Config
from vision.theme import WARN


def _cfg(setting="ask", warn=15):
    cfg = Config()
    cfg.brain.model, cfg.brain.effort, cfg.brain.fast = "sonnet", "high", False
    cfg.brain.failover, cfg.brain.quota_warn_pct = setting, warn
    return cfg


class LimitHitTests(unittest.TestCase):
    def test_the_three_clis_wordings_count(self):
        for error in ("You've hit your limit · resets 3pm", "Session limit reached", "usage_limit_reached",
                      "rate limit reached for gpt-6", "Rate-limit exceeded", "HTTP 429 Too Many Requests",
                      "You are out of extra usage", "quota exceeded"):
            self.assertTrue(failover.limit_hit(error), error)

    def test_other_failures_and_cancels_do_not(self):
        for error in ("", "cancelled", "the brain stalled: no output for 900s", "context limit exceeded",
                      "claude exited with code 1", "the time limit is 30s"):
            self.assertFalse(failover.limit_hit(error), error)

    def test_claudes_rejected_rate_limit_info_counts_whatever_the_text(self):
        usage = {"info": {"status": "rejected", "unifiedWindows": {}}}
        self.assertTrue(failover.limit_hit("something went wrong", usage))
        self.assertFalse(failover.limit_hit("something went wrong", {"info": {"status": "allowed_warning"}}))


class OrderTests(unittest.TestCase):
    def test_words_and_lists(self):
        self.assertEqual(failover.order("ask"), "ask")
        self.assertEqual(failover.order(" OFF "), "off")
        self.assertEqual(failover.order("nonsense"), "ask")
        self.assertEqual(failover.order(None), "ask")
        self.assertEqual(failover.order(["Codex", " grok", ""]), ["codex", "grok"])


class CandidateTests(unittest.TestCase):
    """The other providers that could take the conversation: enabled, ready, never the current one."""

    def _enabled(self, *names):
        return [SimpleNamespace(name=n) for n in names]

    def test_enabled_order_skipping_the_current_and_the_unready(self):
        cfg = _cfg("ask")
        with patch("vision.providers.enabled", return_value=self._enabled("claude", "codex", "grok")), \
             patch("vision.providers.ready", side_effect=lambda n, c: n != "grok"), \
             patch("vision.models.provider_default", side_effect=lambda n: {"claude": "opus", "codex": "gpt-6-astra", "grok": "grok-4.6"}[n]):
            options = failover.candidates(cfg, "claude")
        self.assertEqual([m for m, _ in options], ["gpt-6-astra"])
        self.assertTrue(options[0][1].startswith("Codex · "))

    def test_a_list_sets_the_order_and_tried_ones_drop_out(self):
        cfg = _cfg(["grok", "codex"])
        with patch("vision.providers.enabled", return_value=self._enabled("claude", "codex", "grok")), \
             patch("vision.providers.ready", return_value=True), \
             patch("vision.models.provider_default", side_effect=lambda n: {"claude": "opus", "codex": "gpt-6-astra", "grok": "grok-4.6"}[n]):
            self.assertEqual([m for m, _ in failover.candidates(cfg, "claude")], ["grok-4.6", "gpt-6-astra"])
            self.assertEqual([m for m, _ in failover.candidates(cfg, "grok", tried={"claude"})], ["gpt-6-astra"])

    def test_the_form_and_the_pick(self):
        options = [("gpt-6-astra", "Codex · GPT-6 Astra"), ("grok-4.6", "Grok · Grok 4.6")]
        q = failover.question("Claude", options)
        self.assertIn("Claude's usage limit is hit", q["question"])
        self.assertEqual([o["label"] for o in q["options"]], ["Codex · GPT-6 Astra", "Grok · Grok 4.6", failover.STAY])
        self.assertEqual(q["options"][0]["description"], "continue on Codex from the transcript so far")
        self.assertEqual(failover.chosen({q["question"]: "Grok · Grok 4.6"}, q, options), "grok-4.6")
        self.assertIsNone(failover.chosen({q["question"]: failover.STAY}, q, options))
        self.assertIsNone(failover.chosen(None, q, options))


class QuotaTests(unittest.TestCase):
    def test_claude_takes_the_tightest_window(self):
        usage = {"info": {"unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 100}, "seven_day": {"utilization": 0.86, "resetsAt": 200}}}}
        q = failover.quota("claude", usage)
        self.assertEqual(q, {"left": 14.0, "window": "weekly window", "resets_at": 200})
        self.assertAlmostEqual(failover.quota("claude", usage)["left"], 14.0)

    def test_codex_and_grok_shapes(self):
        rl = {"primary": {"used_percent": 7, "window_minutes": 300, "resets_at": 1}, "secondary": {"used_percent": 31, "window_minutes": 10080, "resets_at": 2}}
        self.assertEqual(failover.quota("codex", rl), {"left": 69.0, "window": "weekly window", "resets_at": 2})
        sub = {"used_percent": 92.5, "period": "weekly", "resets_at": 3}
        self.assertEqual(failover.quota("grok", sub), {"left": 7.5, "window": "weekly allowance", "resets_at": 3})

    def test_nothing_known_is_none(self):
        self.assertIsNone(failover.quota("claude", None))
        self.assertIsNone(failover.quota("claude", {"info": {}}))
        self.assertIsNone(failover.quota("codex", {}))
        self.assertIsNone(failover.quota("local", {"used_percent": 3}))

    def test_source_reads_memory_or_a_local_file_only(self):
        claude = SimpleNamespace(provider="claude", last_usage={"info": {"unifiedWindows": {"five_hour": {"utilization": 0.2}}}},
                                 cached_usage=lambda: None)
        self.assertEqual(failover.source(claude), claude.last_usage)
        codex = SimpleNamespace(provider="codex", _rollout_rate_limits=lambda: {"rate_limits": {"primary": {"used_percent": 1}}})
        self.assertEqual(failover.source(codex), {"primary": {"used_percent": 1}})
        with patch("vision.grok.last_subscription", return_value={"used_percent": 4}):
            self.assertEqual(failover.source(SimpleNamespace(provider="grok")), {"used_percent": 4})
        self.assertIsNone(failover.source(SimpleNamespace(provider="local")))

    def test_words(self):
        now = 1_000_000
        self.assertEqual(failover.in_words(now + 40 * 60, now), "40m")
        self.assertEqual(failover.in_words(now + 3 * 3600 + 10 * 60, now), "3h 10m")
        self.assertEqual(failover.in_words(now + 2 * 86400, now), "2d")
        self.assertEqual(failover.in_words(now - 5, now), "")
        self.assertEqual(failover.in_words(None, now), "")
        q = {"left": 12.4, "window": "5-hour window", "resets_at": None}
        self.assertEqual(failover.corner_text(q), "12% left")
        self.assertEqual(failover.corner_text(None), "")
        self.assertTrue(failover.low(q, 15))
        self.assertFalse(failover.low(q, 10))
        self.assertFalse(failover.low(None, 15))
        with patch("vision.failover.time.time", return_value=now):
            self.assertEqual(failover.warning("Claude", {**q, "resets_at": now + 40 * 60}, 15),
                             "Claude's 5-hour window is nearly used up (12% left, resets in 40m)")
        self.assertEqual(failover.warning("Claude", q, 15), "Claude's 5-hour window is nearly used up (12% left)")
        self.assertEqual(failover.warning("Claude", {**q, "left": 0}, 15), "Claude's 5-hour window is used up (0% left)")
        self.assertEqual(failover.warning("Claude", q, 10), "")
        self.assertEqual(failover.key(q), ("5-hour window", None))
        self.assertIsNone(failover.key(None))


class CornerTests(unittest.TestCase):
    """The corner label gains `· 38% left` once a quota is known, and the figure turns yellow when low."""

    def _brain(self, cfg):
        return SimpleNamespace(cfg=cfg.brain, resolved_model=lambda: "claude-sonnet-5", provider="claude")

    def test_label_with_and_without_a_quota(self):
        from vision.cli import _corner_fragments, _corner_label

        cfg = _cfg()
        self.assertEqual(_corner_label(cfg, self._brain(cfg)), "Sonnet 5 (high)")
        q = {"left": 38.0, "window": "weekly window", "resets_at": None}
        self.assertEqual(_corner_label(cfg, self._brain(cfg), q), "Sonnet 5 (high) · 38% left")
        self.assertEqual(_corner_fragments(cfg, self._brain(cfg), q), "Sonnet 5 (high) · 38% left")
        low = {"left": 9.0, "window": "weekly window", "resets_at": None}
        self.assertEqual(_corner_fragments(cfg, self._brain(cfg), low),
                         [("class:frame.corner", "Sonnet 5 (high) · "), (f"class:frame.corner fg:{WARN}", "9% left")])

    def test_the_frame_draws_fragments_too(self):
        from prompt_toolkit.layout import to_container
        from prompt_toolkit.widgets import TextArea

        from vision.ui import CornerFrame

        frags = [("class:frame.corner", "Sonnet 5 · "), (f"class:frame.corner fg:{WARN}", "9% left")]
        for text in (frags, "Sonnet 5 (high)", "", []):
            frame = CornerFrame(TextArea(), lambda text=text: text)
            self.assertIsNotNone(to_container(frame))


class ConfigTests(unittest.TestCase):
    def test_settings_are_read_and_tidied(self):
        import tempfile
        from pathlib import Path

        from vision import config

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[brain]\nfailover = ["Codex", "grok"]\nquota_warn_pct = 250\n', encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "ensure_dirs", lambda: None):
                cfg = config.load_config()
            self.assertEqual(cfg.brain.failover, ["codex", "grok"])
            self.assertEqual(cfg.brain.quota_warn_pct, 100.0)
            path.write_text('[brain]\nfailover = "nonsense"\nquota_warn_pct = "x"\n', encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "ensure_dirs", lambda: None):
                cfg = config.load_config()
            self.assertEqual(cfg.brain.failover, "ask")
            self.assertEqual(cfg.brain.quota_warn_pct, 15)


if __name__ == "__main__":
    unittest.main()
