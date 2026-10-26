import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vision import memory, memreview, suggest
from vision.config import Config

CONVO = [
    {"role": "user", "text": "I'm on Fedora, no sudo for you. Fix the loop in main.py"},
    {"role": "assistant", "text": "Done: the return was inside the loop."},
    {"role": "user", "text": "thanks. by the way, I prefer concise replies, and never bring back the old cache"},
    {"role": "assistant", "text": "Noted."},
]


def _claude_said(result: str):
    out = json.dumps({"type": "result", "is_error": False, "result": result})
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")


class MemReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "memory"
        for p in (mock.patch.object(memory, "MEMORY_DIR", d), mock.patch.object(memory, "MEMORY_FILE", d / "MEMORY.md")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        suggest._failed.clear()
        self.addCleanup(suggest._failed.clear)

    # -- parsing
    def test_parse_takes_the_json_array_out_of_prose_and_drops_bad_entries(self):
        answer = 'Here you go:\n```json\n[{"fact": "the user runs Fedora", "topic": "", "about": ""}, ' \
                 '{"fact": "SampleApp uses Swift", "topic": "SampleApp", "about": "the sample app"}, ' \
                 '{"fact": "The user runs Fedora."}]\n```'
        props = memreview.parse(answer)
        self.assertEqual([(p.fact, p.topic, p.about) for p in props],
                         [("the user runs Fedora", "", ""), ("SampleApp uses Swift", "sampleapp", "the sample app")])
        self.assertEqual(props[1].file, "sampleapp.md")

    def test_parse_gives_nothing_for_no_array_or_broken_json(self):
        self.assertEqual(memreview.parse(None), [])
        self.assertEqual(memreview.parse("nothing worth keeping"), [])
        self.assertEqual(memreview.parse("[{'fact': 1}]"), [])
        self.assertEqual(memreview.parse("[]"), [])

    def test_parse_keeps_a_bad_topic_name_in_the_core_and_caps_the_list(self):
        rows = [{"fact": f"fact {i}", "topic": "memory" if i == 0 else ""} for i in range(12)]
        props = memreview.parse(json.dumps(rows))
        self.assertEqual(len(props), memreview.MAX_PROPOSALS)
        self.assertEqual(props[0].topic, "")

    def test_fresh_drops_what_memory_already_says(self):
        existing = ["- 2026-01-02: The user runs Fedora", "- 2026-01-03: avoid privileged commands"]
        props = [memreview.Proposal("the user runs fedora."), memreview.Proposal("avoid privileged commands"),
                 memreview.Proposal("the user prefers concise replies")]
        self.assertEqual([p.fact for p in memreview.fresh(props, existing)], ["the user prefers concise replies"])

    # -- the form
    def test_question_is_a_multi_select_with_everything_ticked_and_new_topics_marked(self):
        memory.remember("SampleApp uses Swift", "sampleapp", "the sample app")
        props = [memreview.Proposal("the user prefers concise replies"), memreview.Proposal("SampleApp syncs nightly", "sampleapp"),
                 memreview.Proposal("SampleProject uses a worker", "sampleproject", "the sample project")]
        q = memreview.question(props)
        self.assertTrue(q["multiSelect"])
        self.assertIs(q["other"], False)
        self.assertEqual(q["preselected"], [0, 1, 2])
        self.assertEqual([o["label"] for o in q["options"]], [p.fact for p in props])
        self.assertEqual(q["options"][0]["description"], "→ MEMORY.md")
        self.assertEqual(q["options"][1]["description"], "→ sampleapp.md")
        self.assertEqual(q["options"][2]["description"], "→ sampleproject.md (new topic: the sample project)")

    def test_chosen_maps_the_joined_answer_back_even_with_commas_and_nested_facts(self):
        props = [memreview.Proposal("likes tea, not coffee"), memreview.Proposal("likes tea"),
                 memreview.Proposal("uses Fedora")]
        q = memreview.question(props)
        answer = {q["question"]: "likes tea, not coffee, uses Fedora"}
        self.assertEqual([p.fact for p in memreview.chosen(props, answer)], ["likes tea, not coffee", "uses Fedora"])
        self.assertEqual(memreview.chosen(props, None), [])
        self.assertEqual(memreview.chosen(props, {}), [])

    def test_save_writes_each_fact_to_its_file_and_summary_counts_them(self):
        saved = memreview.save([memreview.Proposal("the user prefers concise replies"),
                                memreview.Proposal("SampleApp syncs nightly", "sampleapp", "the sample app")])
        self.assertEqual([name for name, _ in saved], ["MEMORY.md", "sampleapp.md"])
        self.assertEqual(len(memory.facts()), 1)
        self.assertEqual(memory.facts("sampleapp"), [saved[1][1]])
        self.assertTrue(memory.read("sampleapp").startswith("# the sample app\n"))
        self.assertEqual(memreview.summary(saved), "remembered 2 (1 in MEMORY.md, 1 in sampleapp.md)")
        self.assertEqual(memreview.summary([]), "nothing added to memory")

    # -- the call
    def test_proposals_ask_a_toolless_sonnet_call_with_memory_and_the_transcript(self):
        memory.remember("the user runs Fedora")
        cfg = Config()
        reply = json.dumps([{"fact": "the user runs Fedora"}, {"fact": "the user prefers concise replies"},
                            {"fact": "never bring the old cache back", "topic": "", "about": ""}])
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return _claude_said(reply)(cmd, **kw)

        with mock.patch("vision.clis.find_cli", side_effect=lambda n: n), mock.patch("subprocess.run", side_effect=run):
            props = memreview.proposals(cfg, CONVO)
        self.assertEqual([p.fact for p in props], ["the user prefers concise replies", "never bring the old cache back"])
        cmd, kw = calls[0]
        self.assertEqual(cmd[0], "claude")
        self.assertIn("sonnet", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], memreview.PROMPT)
        self.assertIn("<memory>\n- ", kw["input"])
        self.assertIn("the user runs Fedora", kw["input"])
        self.assertIn("User: I'm on Fedora", kw["input"])
        self.assertEqual(kw["timeout"], cfg.memory.review_timeout_s)

    def test_proposals_are_empty_when_the_call_fails_or_nothing_was_answered(self):
        cfg = Config()
        boom = mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1))
        with mock.patch("vision.clis.find_cli", side_effect=lambda n: n), boom:
            self.assertEqual(memreview.proposals(cfg, CONVO), [])
        suggest._failed.clear()
        with mock.patch("vision.clis.find_cli", side_effect=lambda n: n), mock.patch("subprocess.run", side_effect=_claude_said("[]")):
            self.assertEqual(memreview.proposals(cfg, CONVO), [])
        self.assertEqual(memreview.proposals(cfg, [{"role": "user", "text": "hi"}]), [])

    def test_the_chats_own_provider_is_asked_first(self):
        cfg = Config()
        cfg.brain.model = "gpt-6"
        asked = []

        def run(cmd, **kw):
            asked.append(cmd[0])
            with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
                f.write("[]")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("vision.clis.find_cli", side_effect=lambda n: n), mock.patch("subprocess.run", side_effect=run):
            memreview.proposals(cfg, CONVO)
        self.assertEqual(asked, ["codex"])

    # -- when it runs
    def test_due_needs_the_setting_on_and_enough_turns(self):
        cfg = Config()
        self.assertFalse(memreview.due(cfg, 3))
        self.assertTrue(memreview.due(cfg, 4))
        cfg.memory.review = "off"
        self.assertFalse(memreview.due(cfg, 40))


if __name__ == "__main__":
    unittest.main()
