from __future__ import annotations

import unittest

from vision.ui import _as_tabs, _locate, _picker_height, _picker_lines


class SingleSelectorTests(unittest.TestCase):
    def test_flat_options_render_as_single_selector(self):
        options = [
            ("codex", "Codex", "local coding agent"),
            ("claude", "Claude", "external assistant"),
            ("none", "None", "skip for now"),
        ]

        tabs = _as_tabs(options)
        tab, idx = _locate(tabs, "claude")
        rendered = "".join(text for _, text in _picker_lines("Choose provider", tabs, tab, idx, "claude"))

        self.assertEqual(tabs, [("", options, "")])
        self.assertEqual((tab, idx), (0, 1))
        self.assertEqual(_picker_height(tabs), 5)
        self.assertIn("Choose provider", rendered)
        self.assertIn("❯ 2 Claude", rendered)
        self.assertIn("✓ current", rendered)
        self.assertNotIn("provider ·", rendered)
        self.assertNotIn("◉", rendered)
        self.assertNotIn("○", rendered)


    def test_a_tabs_note_sits_dim_under_its_rows(self):
        tabs = _as_tabs([("Claude", [("opus", "Opus 5", "all-rounder"), ("sonnet", "Sonnet 5", "fast")], "via Claude Code"),
                         ("Ollama", [("q", "qwen3", "")], "via Ollama")])
        lines = _picker_lines("Pick", tabs, 1, 0, "")
        text = "".join(t for _, t in lines).splitlines()
        self.assertEqual(text[1].strip(), "Claude   Ollama")  # nothing after the tabs
        self.assertEqual(text[-2].strip(), "via Ollama")  # under the rows, the active tab's
        self.assertIn(("class:pick.note", "     via Ollama\n"), lines)
        self.assertEqual(_picker_height(tabs), len(text))


if __name__ == "__main__":
    unittest.main()
