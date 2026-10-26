"""`vision sessions grep` / `export` and the chat's /export: searching and saving conversations."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from vision import sessions


def _write(path: str, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _claude(sid, *turns, cwd="/home/u"):
    """A Vision (sdk-cli) Claude session: (role, text) turns."""
    out = [{"type": "user", "entrypoint": "sdk-cli", "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": turns[0][1]}}]
    for role, text in turns[1:]:
        content = text if role == "user" else [{"type": "text", "text": text}]
        rec = {"type": role, "message": {"role": role, "content": content}}
        if role == "user":
            rec["entrypoint"] = "sdk-cli"  # every user record carries it; one without marks an interactive session
        out.append(rec)
    return out


class SessionStoreCase(unittest.TestCase):
    """A temp HOME-like layout for every provider, patched into the readers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude", "-home-u")
        self.codex = os.path.join(self.tmp.name, "codex", "2026", "09", "15")
        self.grok = os.path.join(self.tmp.name, "grok", "%2Fhome%2Fu")
        self.local = Path(self.tmp.name) / "local-sessions"
        for d in (self.claude, self.codex, self.grok, self.local):
            os.makedirs(d)
        self.patches = [
            patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")),
            patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")),
            patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")),
            patch("vision.local.SESSIONS_DIR", self.local),
            patch.object(sessions, "EXPORT_DIR", os.path.join(self.tmp.name, "Documents", "vision")),
        ]
        for p in self.patches:
            p.start()
        sessions._parsed.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def age(self, path: str, seconds: float) -> None:
        t = time.time() - seconds
        os.utime(path, (t, t))


class GrepTests(SessionStoreCase):
    def test_every_word_must_appear_and_newest_session_comes_first(self):
        _write(os.path.join(self.claude, "aaaa.jsonl"), _claude("aaaa",
            ("user", "set up the Tailscale funnel"),
            ("assistant", "Funnel is on.\nIt publishes port 8765."),
            ("user", [{"type": "tool_result", "content": "tailscale funnel: tool output"}]),
        ))
        _write(os.path.join(self.claude, "bbbb.jsonl"), _claude("bbbb", ("user", "what about tailscale alone"), ("assistant", "it's a VPN")))
        _write(os.path.join(self.claude, "cccc.jsonl"), [{"type": "user", "entrypoint": "cli", "cwd": "/x", "message": {"content": "tailscale funnel interactively"}}])
        self.age(os.path.join(self.claude, "aaaa.jsonl"), 3600)
        self.age(os.path.join(self.claude, "bbbb.jsonl"), 60)
        hits = sessions.grep_sessions(["tailscale", "funnel"])
        self.assertEqual([m.info.id for m in hits], ["aaaa"])  # bbbb lacks "funnel"; cccc is not Vision's
        self.assertEqual(hits[0].lines, [("user", "set up the Tailscale funnel"), ("assistant", "Funnel is on.")])
        self.assertEqual(hits[0].total, 2)
        hits = sessions.grep_sessions(["TAILSCALE"])
        self.assertEqual([m.info.id for m in hits], ["bbbb", "aaaa"])  # newest first
        self.assertEqual(sessions.grep_sessions([]), [])
        self.assertEqual(sessions.grep_sessions(["nowhere"]), [])

    def test_best_lines_are_kept_in_conversation_order_unless_all_are_asked_for(self):
        turns = [("user", "lock one")] + [("assistant", f"lock and key {i}" if i == 2 else f"lock {i}") for i in range(5)]
        _write(os.path.join(self.claude, "aaaa.jsonl"), _claude("aaaa", *turns))
        [m] = sessions.grep_sessions(["lock", "key"])
        self.assertEqual(m.total, 6)
        self.assertEqual(len(m.lines), sessions.GREP_LINES)
        self.assertIn(("assistant", "lock and key 2"), m.lines)  # the line with both words is in
        self.assertEqual(m.lines, sorted(m.lines, key=lambda l: turns.index(l)))  # conversation order
        [m] = sessions.grep_sessions(["lock", "key"], per_session=0)
        self.assertEqual(len(m.lines), 6)

    def test_every_provider_is_searched_and_one_can_be_chosen(self):
        _write(os.path.join(self.claude, "aaaa.jsonl"), _claude("aaaa", ("user", "the shared word")))
        sid = "01a0b5b5-c272-75f2-93f0-6d2733f9572f"
        _write(os.path.join(self.codex, f"rollout-2026-09-18T14-09-43-{sid}.jsonl"), [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/w", "originator": "codex_exec"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Vision, a personal AI assistant"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codex has the shared word"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yes"}]}},
        ])
        gsid = "g-vision"
        os.makedirs(os.path.join(self.grok, gsid))
        with open(os.path.join(self.grok, gsid, "system_prompt.txt"), "w") as f:
            f.write("You are Vision, a personal AI assistant running locally.")
        with open(os.path.join(self.grok, gsid, "summary.json"), "w") as f:
            json.dump({"info": {"id": gsid, "cwd": "/w"}, "generated_title": "Grok one", "last_active_at": "2026-09-18T12:00:00Z"}, f)
        _write(os.path.join(self.grok, gsid, "chat_history.jsonl"), [
            {"type": "user", "content": [{"type": "text", "text": "<user_query>\ngrok says the shared word\n</user_query>"}]},
            {"type": "assistant", "content": "indeed"},
        ])
        with open(self.local / "loc1.json", "w") as f:
            json.dump({"at": time.time(), "messages": [{"role": "user", "content": "local shared word"}, {"role": "assistant", "content": "ok"}]}, f)
        hits = sessions.grep_sessions(["shared", "word"])
        self.assertEqual(sorted(m.info.provider for m in hits), ["claude", "codex", "grok", "local"])
        self.assertEqual([m.info.provider for m in sessions.grep_sessions(["shared"], providers=("grok",))], ["grok"])
        self.assertEqual(sessions.grep_sessions(["shared"], providers=("grok",))[0].lines, [("user", "grok says the shared word")])

    def test_raw_prefilter_skips_files_without_the_word_but_not_escaped_words(self):
        path = os.path.join(self.claude, "aaaa.jsonl")
        _write(path, _claude("aaaa", ("user", "crème brûlée \"quoted\"")))
        self.assertTrue(sessions._file_may_contain(path, ["quoted"]))
        self.assertFalse(sessions._file_may_contain(path, ["absent"]))
        self.assertTrue(sessions._file_may_contain(path, ["brûlée"]))  # json escapes it: not pre-checked
        self.assertTrue(sessions._file_may_contain(path, ['"quoted"']))
        self.assertFalse(sessions._file_may_contain(os.path.join(self.claude, "gone.jsonl"), ["x"]))
        [m] = sessions.grep_sessions(["brûlée"])
        self.assertEqual(m.info.id, "aaaa")

    def test_match_window_centres_a_long_line_on_its_first_match(self):
        line = "a" * 300 + " needle " + "b" * 300
        shown = sessions.match_window(line, ["needle"], width=60)
        self.assertEqual(len(shown), 62)
        self.assertTrue(shown.startswith("…") and shown.endswith("…"))
        self.assertIn("needle", shown)
        self.assertEqual(sessions.match_window("short needle", ["needle"], width=60), "short needle")
        self.assertTrue(sessions.match_window("needle" + "x" * 100, ["needle"], width=20).startswith("needle"))


class ExportTests(SessionStoreCase):
    def test_markdown_has_a_title_a_meta_line_and_a_section_per_turn(self):
        text = sessions.export_markdown("Fix the lock", "claude", [
            {"role": "user", "text": "fix the lock"},
            {"role": "assistant", "text": "looking\n\npatched", "agents": [{"id": 1}]},
            {"role": "user", "text": "   "},
        ], when=time.mktime((2026, 10, 21, 9, 5, 0, 0, 0, -1)), model="Opus 5")
        self.assertEqual(text, "# Fix the lock\n\nClaude · Opus 5 · 2026-10-21 09:05\n\n## You\n\nfix the lock\n\n## Vision\n\nlooking\n\npatched\n")
        self.assertTrue(sessions.export_markdown("", "codex", []).startswith("# Vision conversation\n\nCodex · "))

    def test_default_path_is_dated_slugged_and_never_overwrites(self):
        when = time.mktime((2026, 10, 21, 9, 5, 0, 0, 0, -1))
        path = sessions.export_path("Fix the lock, please!", when)
        self.assertEqual(path, os.path.join(self.tmp.name, "Documents", "vision", "2026-10-21-fix-the-lock-please.md"))
        os.makedirs(os.path.dirname(path))
        Path(path).write_text("x")
        self.assertTrue(sessions.export_path("Fix the lock, please!", when).endswith("-fix-the-lock-please-2.md"))
        self.assertTrue(sessions.export_path("???", when).endswith("-conversation.md"))
        self.assertLessEqual(len(os.path.basename(sessions.export_path("w" * 200, when))), len("2026-10-21-") + 48 + len(".md"))

    def test_export_session_writes_the_file_where_asked(self):
        _write(os.path.join(self.claude, "aaaa.jsonl"), _claude("aaaa", ("user", "fix the lock"), ("assistant", "done")))
        info = sessions.find_any_session("aaaa")
        path = sessions.export_session(info)
        self.assertTrue(path.startswith(os.path.join(self.tmp.name, "Documents", "vision")))
        self.assertIn("## You\n\nfix the lock\n\n## Vision\n\ndone\n", Path(path).read_text())
        folder = os.path.join(self.tmp.name, "out") + os.sep
        self.assertTrue(sessions.export_session(info, folder).startswith(folder))
        exact = os.path.join(self.tmp.name, "deep", "notes.md")
        self.assertEqual(sessions.export_session(info, exact, model="Opus 5"), exact)
        self.assertIn("Claude · Opus 5 · ", Path(exact).read_text())
        empty = sessions.SessionInfo("none", "claude", "nothing", time.time())
        with self.assertRaises(ValueError):
            sessions.export_session(empty)


class SessionsCommandTests(SessionStoreCase):
    def setUp(self):
        super().setUp()
        from vision.cli import app

        self.app = app
        self.runner = CliRunner()
        _write(os.path.join(self.claude, "aaaa.jsonl"), _claude("aaaa", ("user", "set up the Tailscale funnel"), ("assistant", "Funnel is on.")))
        _write(os.path.join(self.claude, "bbbb.jsonl"), _claude("bbbb", ("user", "hello there"), ("assistant", "hi")))

    def invoke(self, *args):
        return self.runner.invoke(self.app, ["sessions", *args], env={"COLUMNS": "200"})

    def test_bare_and_list_show_a_table(self):
        for args in ((), ("list",), ("list", "-p", "claude", "-n", "5")):
            r = self.invoke(*args)
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertIn("aaaa", r.output)
            self.assertIn("set up the Tailscale funnel", r.output)
            self.assertIn("/home/u", r.output)
        self.assertEqual(self.invoke("list", "-p", "nope").exit_code, 2)

    def test_grep_prints_a_block_per_conversation(self):
        r = self.invoke("grep", "tailscale", "FUNNEL")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("aaaa · set up the Tailscale funnel · Claude ·", r.output)
        self.assertIn("you › set up the Tailscale funnel", r.output)
        self.assertIn("vision › Funnel is on.", r.output)
        self.assertNotIn("bbbb", r.output)
        self.assertEqual(self.invoke("grep", "nothing-like-this").exit_code, 1)

    def test_export_writes_markdown_and_says_where(self):
        out = os.path.join(self.tmp.name, "saved.md")
        r = self.invoke("export", "aa", "-o", out)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("saved to", r.output)
        self.assertIn("## Vision\n\nFunnel is on.", Path(out).read_text())
        self.assertEqual(self.invoke("export", "zzzz").exit_code, 1)


if __name__ == "__main__":
    unittest.main()
