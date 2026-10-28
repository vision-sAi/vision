import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vision import memimport, memory, memreview
from vision.config import Config


class MemImportTests(unittest.TestCase):
    """`vision memory import`: Claude Code's notes about the user, offered as a tick list for Vision's memory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        d = self.tmp / "memory"
        for p in (mock.patch.object(memory, "MEMORY_DIR", d), mock.patch.object(memory, "MEMORY_FILE", d / "MEMORY.md")):
            p.start()
            self.addCleanup(p.stop)
        self.claude = self.tmp / "claude"
        (self.claude / "projects" / "-nowhere-code-app" / "memory").mkdir(parents=True)
        (self.claude / "projects" / "-nowhere-code-app" / "memory" / "MEMORY.md").write_text("- [Build](build.md) — the index\n")
        (self.claude / "projects" / "-nowhere-code-app" / "memory" / "build.md").write_text("Builds run on the Mac called haru.\n")
        (self.claude / "projects" / "-nowhere-other").mkdir(parents=True)  # no memory folder: skipped
        (self.claude / "CLAUDE.md").write_text("Keep answers short.\n")

    def test_claude_codes_instructions_and_project_notes_are_read_but_not_its_index(self):
        found = memimport.claude_sources(self.claude)
        self.assertEqual([s.label for s in found],
                         ["Claude Code: your instructions (~/.claude/CLAUDE.md)", "Claude Code: -nowhere-code-app (build.md)"])
        self.assertEqual(found[1].text, "Builds run on the Mac called haru.\n")

    def test_notes_are_batched_whole(self):
        notes = [memimport.Source("a", "x" * 50), memimport.Source("b", "y" * 50), memimport.Source("c", "z" * 50)]
        parts = memimport.batches(notes, limit=150)
        self.assertEqual(len(parts), 2, "two notes fit the first call, the third starts another")
        self.assertTrue(all(p.count("<note ") == p.count("</note>") for p in parts))
        self.assertEqual(sum(p.count("<note ") for p in parts), 3)

    def test_what_memory_already_holds_is_not_offered_again_and_ticks_are_saved_across_pages(self):
        memory.remember("prefers short answers")
        reply = json.dumps([{"fact": "Prefers short answers", "topic": "", "about": ""}]
                           + [{"fact": f"fact number {i}", "topic": "app" if i == 0 else "", "about": "the app" if i == 0 else ""} for i in range(10)])
        with mock.patch("vision.suggest.one_shot", return_value=reply) as call:
            props = memimport.proposals(Config(), memimport.claude_sources(self.claude))
        self.assertEqual(call.call_args.kwargs["prefer"], "claude")
        self.assertIn("Builds run on the Mac called haru.", call.call_args.args[0])
        self.assertEqual(len(props), 10, "the line memory had is left out")
        qs = memimport.questions(props)
        self.assertEqual([len(q["options"]) for q in qs], [8, 2], "a long import is paged")
        answers = {qs[0]["question"]: "fact number 0, fact number 3", qs[1]["question"]: "fact number 9"}
        saved = memreview.save(memimport.chosen(props, answers))
        self.assertEqual([name for name, _ in saved], ["app.md", "MEMORY.md", "MEMORY.md"])
        self.assertIn("fact number 9", memory.read())

    def test_a_skipped_page_saves_nothing_from_it(self):
        props = [memreview.Proposal(f"fact {i}") for i in range(10)]
        qs = memimport.questions(props)
        self.assertEqual([p.fact for p in memimport.chosen(props, {qs[1]["question"]: "fact 8"})], ["fact 8"])
        self.assertEqual(memimport.chosen(props, None), [])


if __name__ == "__main__":
    unittest.main()
