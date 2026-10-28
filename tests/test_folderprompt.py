import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vision import folderprompt
from vision.config import BrainConfig


class FolderPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home"  # macOS: /var/folders is a link to /private/var
        self.repo = self.home / "Repos" / "app"
        self.sub = self.repo / "pkg" / "deep"
        self.sub.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_each_brain_gets_the_other_clis_instruction_file(self):
        (self.repo / "CLAUDE.md").write_text("Commit subjects are plain sentences.\n")
        (self.repo / "AGENTS.md").write_text("Run the tests before a PR.\n")
        (self.repo / ".vision.md").write_text("Speak plainly.\n")
        codex = folderprompt.for_brain(str(self.sub), BrainConfig(model="gpt-5.5"))
        self.assertIn("Commit subjects are plain sentences.", codex, "Codex reads AGENTS.md itself, so it's handed CLAUDE.md")
        self.assertNotIn("Run the tests before a PR.", codex)
        self.assertIn("Speak plainly.", codex)
        claude = folderprompt.for_brain(str(self.sub), BrainConfig(model="opus"))
        self.assertIn("Run the tests before a PR.", claude)
        self.assertNotIn("Commit subjects are plain sentences.", claude)
        other = folderprompt.for_brain(str(self.sub), BrainConfig(model="grok-4"))
        self.assertIn("Commit subjects", other)
        self.assertIn("Run the tests", other, "a brain reading neither gets both")
        self.assertEqual(folderprompt.summary(str(self.sub), BrainConfig(model="gpt-5.5")), "instructions: .vision.md + CLAUDE.md")

    def test_a_linked_file_is_handed_over_once_and_not_at_all_when_read_under_its_own_name(self):
        (self.repo / "CLAUDE.md").write_text("Same rules for everyone.\n")
        (self.repo / "AGENTS.md").symlink_to(self.repo / "CLAUDE.md")
        grok = folderprompt.for_brain(str(self.repo), BrainConfig(model="grok-4"))
        self.assertEqual(grok.count("Same rules for everyone."), 1)
        self.assertEqual(folderprompt.for_brain(str(self.repo), BrainConfig(model="gpt-5.5")), "", "Codex reads it as AGENTS.md")

    def test_switching_the_feature_off_drops_the_cli_files_too(self):
        (self.repo / "CLAUDE.md").write_text("rules\n")
        self.assertEqual(folderprompt.for_brain(str(self.repo), BrainConfig(model="gpt-5.5", folder_instructions="")), "")

    def test_nearest_first_and_stops_at_the_repository_root(self):
        (self.home / ".vision.md").write_text("home-wide\n")  # above the repo root: not seen from inside it
        (self.repo / ".vision.md").write_text("repo-wide\n")
        (self.sub / ".vision.md").write_text("deep\n")
        found = folderprompt.find(str(self.sub))
        self.assertEqual([p.parent for p in found], [self.sub, self.repo])

    def test_stops_at_home_when_there_is_no_repository(self):
        plain = self.home / "notes" / "x"
        plain.mkdir(parents=True)
        (self.home / ".vision.md").write_text("home-wide\n")
        (self.home.parent / ".vision.md").write_text("above home\n")
        self.assertEqual([p.parent for p in folderprompt.find(str(plain))], [self.home])

    def test_section_puts_the_nearest_file_last_and_names_each(self):
        (self.repo / ".vision.md").write_text("Use British spelling.\n")
        (self.sub / ".vision.md").write_text("Tests live next to the code.\n")
        text = folderprompt.section(str(self.sub))
        self.assertLess(text.index("British"), text.index("Tests live"))
        self.assertIn(f"Instructions from {self.repo / '.vision.md'}", text)
        self.assertIn(f"Instructions from {self.sub / '.vision.md'}", text)

    def test_empty_name_switches_it_off_and_empty_files_are_skipped(self):
        (self.sub / ".vision.md").write_text("   \n")
        self.assertEqual(folderprompt.section(str(self.sub)), "")
        (self.sub / ".vision.md").write_text("real\n")
        self.assertEqual(folderprompt.find(str(self.sub), ""), [])
        self.assertEqual(folderprompt.for_brain(str(self.sub), BrainConfig(folder_instructions="")), "")
        self.assertIn("real", folderprompt.for_brain(str(self.sub), BrainConfig()))

    def test_a_long_file_is_cut_and_the_prompt_says_so(self):
        (self.sub / ".vision.md").write_text("x" * 50 + "\n")
        text = folderprompt.section(str(self.sub), cap=20)
        self.assertIn("x" * 20, text)
        self.assertNotIn("x" * 21, text)
        self.assertIn("cut at 20 characters", text)
        self.assertTrue(folderprompt.load(str(self.sub), cap=20)[0].cut)

    def test_summary_counts_the_files(self):
        self.assertEqual(folderprompt.summary(str(self.sub), BrainConfig()), "")
        (self.repo / ".vision.md").write_text("a\n")
        self.assertEqual(folderprompt.summary(str(self.sub), BrainConfig()), "instructions: .vision.md")
        (self.sub / ".vision.md").write_text("b\n")
        self.assertEqual(folderprompt.summary(str(self.sub), BrainConfig()), "instructions: .vision.md ×2")

    def test_configured_name(self):
        (self.repo / "AGENTS.md").write_text("agents rules\n")
        cfg = BrainConfig(folder_instructions="AGENTS.md")
        self.assertEqual(folderprompt.for_brain(str(self.sub), cfg).count("agents rules"), 1, "read once, not handed over again")
        self.assertEqual(folderprompt.for_brain(str(self.sub), BrainConfig(model="gpt-5.5")), "", "Codex reads AGENTS.md itself")

    def test_system_prompt_and_worker_prompt_carry_the_section(self):
        from vision.delegation import worker_prompt
        from vision.persona import system_prompt

        (self.repo / ".vision.md").write_text("Never touch the migrations folder.\n")
        folder = folderprompt.section(str(self.sub))
        prompt = system_prompt(False, memory="", project="Goal: ship it", folder=folder)
        self.assertIn("Never touch the migrations folder.", prompt)
        self.assertLess(prompt.index("Goal: ship it"), prompt.index("Never touch"))  # after the project's section
        self.assertNotIn("Never touch", system_prompt(False, memory=""))
        with mock.patch("vision.memory.prompt_section", return_value=""):
            self.assertIn("Never touch the migrations folder.", worker_prompt(BrainConfig(), str(self.sub), "claude"))
            self.assertNotIn("Never touch", worker_prompt(BrainConfig(folder_instructions=""), str(self.sub), "claude"))


if __name__ == "__main__":
    unittest.main()
