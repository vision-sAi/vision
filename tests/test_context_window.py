from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vision.brain import Brain
from vision.config import BrainConfig


def _brain(model: str) -> Brain:
    with patch("vision.brain.find_claude", return_value="claude"):
        brain = Brain(BrainConfig(model=model, effort="high"))
    return brain


class ContextWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "claude_windows.json"
        for target, value in (("vision.brain.WINDOWS_FILE", self.file), ("vision.brain.STATE_DIR", Path(self.tmp.name))):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_before_any_report_opus_is_1m_and_haiku_200k(self):
        self.assertEqual(_brain("opus").context_window(), 1_000_000)
        self.assertEqual(_brain("haiku").context_window(), 200_000)

    def test_own_model_wins_over_a_helper_listed_first(self):
        brain = _brain("opus")
        brain.model, brain._model_seen_for = "claude-opus-5-5", "opus"
        brain._note_window({"claude-haiku-4-5-20251001": {"contextWindow": 200_000},
                            "claude-opus-5-5[1m]": {"contextWindow": 1_000_000}})
        self.assertEqual(brain.context_window(), 1_000_000)

    def test_switching_model_drops_the_old_window(self):
        from vision.cli import _switch_model

        brain = _brain("haiku")
        brain.model, brain._model_seen_for = "claude-haiku-4-5-20251001", "haiku"
        brain.context = (5_000, 0)
        brain._note_window({"claude-haiku-4-5-20251001": {"contextWindow": 200_000}})
        self.assertEqual(brain.context, (5_000, 200_000))
        from types import SimpleNamespace
        brain, _ = _switch_model(SimpleNamespace(brain=brain.cfg), brain, "opus", voice_mode=False)
        self.assertEqual(brain.context_window(), 1_000_000)
        self.assertEqual(brain.context, (5_000, 1_000_000))

    def test_reported_window_is_remembered_for_new_chats(self):
        brain = _brain("sonnet")
        brain.model, brain._model_seen_for = "claude-sonnet-5", "sonnet"
        brain._note_window({"claude-sonnet-5": {"contextWindow": 400_000}})
        fresh = _brain("sonnet")
        with patch.object(Brain, "resolved_model", return_value="claude-sonnet-5"):
            self.assertEqual(fresh.context_window(), 400_000)


if __name__ == "__main__":
    unittest.main()
