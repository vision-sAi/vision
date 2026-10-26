import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MemoryTests(unittest.TestCase):
    def setUp(self):
        from vision import memory

        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "memory"
        self.patches = [mock.patch.object(memory, "MEMORY_DIR", d), mock.patch.object(memory, "MEMORY_FILE", d / "MEMORY.md")]
        for p in self.patches:
            p.start()
        self.memory = memory

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_remember_creates_file_with_dated_lines(self):
        m = self.memory
        self.assertEqual(m.facts(), [])
        line = m.remember("  the user's   machine is a ThinkPad ")
        self.assertRegex(line, r"^- \d{4}-\d{2}-\d{2}: the user's machine is a ThinkPad$")
        m.remember("no sudo for Vision")
        self.assertTrue(m.read().startswith("# Vision's memory"))
        self.assertEqual(len(m.facts()), 2)
        with self.assertRaises(ValueError):
            m.remember("   ")

    def test_forget_drops_matching_lines_only(self):
        m = self.memory
        m.remember("likes tea")
        m.remember("Likes coffee in the morning")
        m.remember("uses Fedora")
        self.assertEqual(len(m.forget("LIKES")), 2)
        self.assertEqual([ln.split(": ", 1)[1] for ln in m.facts()], ["uses Fedora"])
        self.assertEqual(m.forget("nothing like this"), [])
        with self.assertRaises(ValueError):
            m.forget("")

    def test_prompt_section_shows_facts_and_rules(self):
        m = self.memory
        self.assertIn("empty so far", m.prompt_section())
        m.remember("calls the user boss")
        sec = m.prompt_section()
        self.assertIn("calls the user boss", sec)
        self.assertIn(str(m.MEMORY_FILE), sec)
        self.assertIn("/remember", sec)
        self.assertNotIn("# Vision's memory", sec)  # header stays out of the prompt

    def test_topics_are_listed_not_loaded(self):
        m = self.memory
        m.remember("replies stay short")
        m.remember("the composer keeps the keyboard up", "iPhone app", about="The iPhone app: UI decisions")
        m.remember("videos never autoplay", "iphone-app.md")
        self.assertEqual(m.topics(), [("iphone-app", "The iPhone app: UI decisions", 2)])
        self.assertEqual(len(m.facts()), 1)  # the core is untouched
        sec = m.prompt_section()
        self.assertIn("- iphone-app.md (2): The iPhone app: UI decisions", sec)
        self.assertIn("replies stay short", sec)
        self.assertNotIn("keyboard up", sec)
        for bad in ("", "MEMORY", "///"):
            with self.assertRaises(ValueError):
                m.slug(bad)

    def test_search_and_forget_cover_every_file(self):
        m = self.memory
        m.remember("uses Fedora on the machine")
        m.remember("the build box builds the iPhone app", "infra")
        m.remember("the machine binds loopback only", "infra")
        self.assertEqual([name for name, _ in m.search("MACHINE")], ["MEMORY.md", "infra.md"])
        self.assertEqual(len(m.search("box builds")), 1)
        self.assertEqual(m.search("nothing here"), [])
        self.assertEqual(len(m.forget("machine")), 2)
        self.assertEqual((len(m.facts()), len(m.facts("infra"))), (0, 1))
        self.assertTrue(m.read("infra").startswith("# infra"))  # the header survives

    def test_the_phone_lists_adds_searches_and_drops_facts(self):
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from vision import server

        m = self.memory
        m.remember("replies stay short")
        m.remember("videos never autoplay", "iphone-app", about="The iPhone app")
        client = TestClient(server.create_app(SimpleNamespace(authorized=lambda t: t == "t", log=lambda _: None)))
        auth = {"Authorization": "Bearer t"}
        self.assertEqual(client.get("/memory").status_code, 401)
        core = client.get("/memory", headers=auth).json()
        self.assertEqual([f["text"] for f in core["facts"]], ["replies stay short"])
        self.assertRegex(core["facts"][0]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertGreater(core["topics"][0].pop("updated"), 0)
        self.assertGreater(core["updated"], 0)
        self.assertEqual(core["topics"], [{"name": "iphone-app", "about": "The iPhone app", "count": 1}])
        topic = client.get("/memory", params={"topic": "iphone-app"}, headers=auth).json()
        self.assertEqual(([f["text"] for f in topic["facts"]], topic["topics"]), (["videos never autoplay"], []))
        hits = client.get("/memory", params={"q": "never"}, headers=auth).json()["facts"]
        self.assertEqual([(h["topic"], h["text"]) for h in hits], [("iphone-app", "videos never autoplay")])
        added = client.post("/memory", json={"fact": "the gear keeps its swell", "topic": "iphone-app"}, headers=auth).json()
        self.assertEqual(len(m.facts("iphone-app")), 2)
        self.assertEqual(client.post("/memory", json={"fact": " "}, headers=auth).status_code, 400)
        gone = client.post("/memory/forget", json={"line": added["line"], "topic": "iphone-app"}, headers=auth)
        self.assertEqual((gone.status_code, len(m.facts("iphone-app"))), (200, 1))
        self.assertEqual(client.post("/memory/forget", json={"line": added["line"], "topic": "iphone-app"}, headers=auth).status_code, 404)
        self.assertEqual(len(m.facts()), 1)  # the core was never touched

    def test_tidy_keeps_a_copy_and_says_what_good_memory_is(self):
        m = self.memory
        m.remember("uses Fedora")
        m.remember("the gear keeps its swell", "iphone-app")
        saved = m.backup()
        self.assertEqual(sorted(p.name for p in saved.iterdir()), ["MEMORY.md", "iphone-app.md"])
        self.assertEqual(saved.parent.parent, m.MEMORY_DIR.parent)
        prompt = m.tidy_prompt(saved)
        for said in (str(m.MEMORY_DIR), str(saved), "how code works", "# Name: what it covers", "personality.md"):
            self.assertIn(said, prompt)
        rules = m.prompt_section()
        self.assertIn("could not work out for itself", rules)
        self.assertIn("rewrite that instead", rules)

    def test_prompt_section_truncates_huge_files(self):
        m = self.memory
        big = "\n".join(f"- 2026-01-01: fact number {i} " + "x" * 100 for i in range(200))
        sec = m.prompt_section(big)
        self.assertIn("consolidate", sec)
        self.assertLess(len(sec), m.MAX_PROMPT_CHARS + 2500)  # the facts, cut off, plus the rules for saving

    def test_persona_carries_memory_for_both_brains(self):
        from vision.persona import system_prompt

        for provider in ("claude", "codex"):
            p = system_prompt(False, tools=["Bash"], provider=provider, memory="- 2026-09-16: the cat is called Pixel")
            self.assertIn("the cat is called Pixel", p)
            self.assertIn("MEMORY:", p)
            self.assertLess(p.index("Today is"), p.index("MEMORY:"))

    def test_codex_workspace_write_gets_memory_dir_as_writable_root(self):
        from vision.codex import CodexBrain, toml_str
        from vision.config import BrainConfig

        cfg = BrainConfig()
        cfg.codex.sandbox = "workspace-write"
        with mock.patch("vision.codex.find_codex", return_value="/fake/codex"):
            brain = CodexBrain(cfg, voice_mode=False)
        cmd = brain._command()
        self.assertIn(f"sandbox_workspace_write.writable_roots=[{toml_str(str(self.memory.MEMORY_DIR))}]", cmd)
        cfg.codex.sandbox = "read-only"
        self.assertFalse(any("writable_roots" in a for a in brain._command()))


if __name__ == "__main__":
    unittest.main()
