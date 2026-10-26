from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from vision import sessions
from vision.ui import MenuRow, SlashCommand, _locate, slash_menu_rows

COMMANDS = [
    SlashCommand("model", "switch model", lambda: [("default", "saved"), ("sonnet", "Sonnet 5"), ("opus", "Opus 5")]),
    SlashCommand("effort", "effort level", lambda: [("high", ""), ("max", "")]),
    SlashCommand("session", "resume", aliases=("resume",)),
    SlashCommand("say", "speak text"),
    SlashCommand("quit", "leave", aliases=("exit", "q")),
]


class SlashMenuTests(unittest.TestCase):
    def rows(self, text: str) -> list[str]:
        return [r.text for r in slash_menu_rows(COMMANDS, text)]

    def test_bare_slash_lists_everything(self):
        self.assertEqual(self.rows("/"), ["/model", "/effort", "/session", "/say", "/quit"])

    def test_filters_as_you_type_prefix_first_then_substring(self):
        self.assertEqual(self.rows("/s"), ["/session", "/say"])
        self.assertEqual(self.rows("/se"), ["/session"])
        self.assertEqual(self.rows("/ess"), ["/session"])  # substring match
        self.assertEqual(self.rows("/SAY"), ["/say"])  # case-insensitive

    def test_aliases_match(self):
        self.assertEqual(self.rows("/res"), ["/session"])
        self.assertEqual(self.rows("/exi"), ["/quit"])

    def test_not_a_command(self):
        self.assertEqual(self.rows("hello"), [])
        self.assertEqual(self.rows("/zzz"), [])
        self.assertEqual(self.rows("/model\nx"), [])

    def test_argument_choices_are_listed_once_while_the_menu_stays_on_a_command(self):
        """`/session` lists and parses every session file for its choices; typing its argument asked
        again on every keystroke. With a cache the list is made once per command the menu is on."""
        asked = []
        cmds = [SlashCommand("session", "resume", lambda: asked.append("s") or [("abcd", "one"), ("abef", "two")]),
                SlashCommand("model", "switch", lambda: asked.append("m") or [("opus", "")])]
        cache: dict = {}
        self.assertEqual([r.label for r in slash_menu_rows(cmds, "/session ", cache)], ["abcd", "abef"])
        self.assertEqual([r.label for r in slash_menu_rows(cmds, "/session ab", cache)], ["abcd", "abef"])
        self.assertEqual([r.label for r in slash_menu_rows(cmds, "/session abe", cache)], ["abef"])
        self.assertEqual(asked, ["s"])
        self.assertEqual([r.label for r in slash_menu_rows(cmds, "/model ", cache)], ["opus"])
        self.assertEqual([r.label for r in slash_menu_rows(cmds, "/session a", cache)], ["abcd", "abef"])
        self.assertEqual(asked, ["s", "m", "s"])  # another command, then back: listed afresh each time
        slash_menu_rows(cmds, "/session a")  # without a cache, as before
        self.assertEqual(asked, ["s", "m", "s", "s"])

    def test_argument_choices_after_the_command(self):
        rows = slash_menu_rows(COMMANDS, "/model ")
        self.assertEqual([r.text for r in rows], ["/model default", "/model sonnet", "/model opus"])
        self.assertTrue(all(r.is_arg for r in rows))
        self.assertEqual(self.rows("/model so"), ["/model sonnet"])
        self.assertEqual(self.rows("/model nn"), ["/model sonnet"])

    def test_no_argument_menu_for_free_text_commands(self):
        self.assertEqual(self.rows("/say hello"), [])
        self.assertEqual(self.rows("/model sonnet more"), [])  # only the first argument is offered

    def test_menu_row_shape(self):
        row = slash_menu_rows(COMMANDS, "/qu")[0]
        self.assertEqual(row, MenuRow("/quit", "/quit", "leave"))


def _write(path: str, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class SessionListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude", "-home-u")
        self.codex = os.path.join(self.tmp.name, "codex", "2026", "09", "15")
        self.grok = os.path.join(self.tmp.name, "grok", "%2Fhome%2Fu")
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        os.makedirs(self.grok)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claude_lists_only_vision_sessions_newest_first_with_titles(self):
        user = lambda sid, text, ep: {"type": "user", "entrypoint": ep, "cwd": "/home/u", "sessionId": sid, "message": {"role": "user", "content": text}}
        _write(os.path.join(self.claude, "aaaa.jsonl"), [{"type": "queue-operation"}, user("aaaa", "first prompt that is rather long " * 3, "sdk-cli")])
        _write(os.path.join(self.claude, "bbbb.jsonl"), [user("bbbb", "hi", "sdk-cli"), {"type": "ai-title", "aiTitle": "Greeting"}])
        _write(os.path.join(self.claude, "cccc.jsonl"), [user("cccc", "interactive claude", "cli")])
        _write(os.path.join(self.claude, "dddd.jsonl"), [user("dddd", [{"type": "tool_result"}], "sdk-cli")])
        now = time.time()
        os.utime(os.path.join(self.claude, "aaaa.jsonl"), (now - 100, now - 100))
        os.utime(os.path.join(self.claude, "bbbb.jsonl"), (now - 5000, now - 5000))
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            got = sessions.list_sessions("claude")
            hit = sessions.find_session("claude", "BB")
        self.assertEqual([s.id for s in got], ["dddd", "aaaa", "bbbb"])
        by_id = {s.id: s for s in got}
        self.assertEqual(by_id["bbbb"].title, "Greeting")
        self.assertTrue(by_id["aaaa"].title.endswith("…"))
        self.assertLessEqual(len(by_id["aaaa"].title), sessions.TITLE_WIDTH)
        self.assertEqual(by_id["dddd"].title, "(no prompt)")
        self.assertEqual(by_id["aaaa"].cwd, "/home/u")
        self.assertEqual(hit.id, "bbbb")
        self.assertEqual(by_id["bbbb"].age(now), "1 h ago")
        self.assertEqual(sessions.session_rows([by_id["aaaa"]], now)[0][2], "1 min ago · aaaa")
        self.assertEqual(sessions.session_rows([by_id["aaaa"]], now)[0][0], "claude:aaaa")

    def test_codex_lists_only_vision_exec_threads(self):
        def rollout(name, originator, developer, prompt):
            _write(os.path.join(self.codex, f"rollout-{name}.jsonl"), [
                {"type": "session_meta", "payload": {"id": name, "cwd": "/w", "originator": originator}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": developer}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>…"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
            ])

        rollout("v1", "codex_exec", "You are Vision, a personal AI assistant", "what time is it")
        rollout("tui", "codex-tui", "", "interactive")
        rollout("other", "codex_exec", "You are Codex", "some script")
        with patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")):
            got = sessions.list_sessions("codex")
        self.assertEqual([(s.id, s.title, s.cwd) for s in got], [("v1", "what time is it", "/w")])

    def test_grok_lists_only_vision_sessions(self):
        def grok_sess(sid, title, persona, cwd="/w"):
            folder = os.path.join(self.grok, sid)
            os.makedirs(folder)
            with open(os.path.join(folder, "system_prompt.txt"), "w") as f:
                f.write(persona)
            with open(os.path.join(folder, "summary.json"), "w") as f:
                json.dump({
                    "info": {"id": sid, "cwd": cwd},
                    "generated_title": title,
                    "last_active_at": "2026-09-18T12:00:00Z",
                }, f)

        grok_sess("g-vision", "Add Grok support", "You are Vision, a personal AI assistant running locally.")
        grok_sess("g-tui", "Interactive grok", "You are Grok 4.6 released by xAI.")
        with patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")):
            got = sessions.list_sessions("grok")
        self.assertEqual([(s.id, s.title, s.cwd) for s in got], [("g-vision", "Add Grok support", "/w")])

    def test_session_tabs_are_claude_codex_grok_and_keep_empty_providers(self):
        _write(os.path.join(self.claude, "aaaa.jsonl"), [
            {"type": "user", "entrypoint": "sdk-cli", "cwd": "/home/u", "sessionId": "aaaa",
             "message": {"role": "user", "content": "claude one"}},
        ])
        _write(os.path.join(self.codex, "rollout-v1.jsonl"), [
            {"type": "session_meta", "payload": {"id": "v1", "cwd": "/w", "originator": "codex_exec"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "You are Vision, a personal AI assistant"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "codex one"}]}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")), \
             patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")), \
             patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")), \
             patch("vision.local.SESSIONS_DIR", Path(self.tmp.name) / "local"):
            tabs = sessions.session_tabs()
            hit = sessions.find_any_session("v1", prefer="grok")
            miss = sessions.find_any_session("nope")
            listed = sessions.list_all_sessions()
        self.assertEqual([name for name, _, _ in tabs], ["Claude", "Codex", "Grok", "Local"])
        self.assertEqual(tabs[0][1][0][0], "claude:aaaa")
        self.assertEqual(tabs[1][1][0][0], "codex:v1")
        self.assertEqual(tabs[2][1], [])
        self.assertEqual(tabs[2][2], "no conversations")
        self.assertEqual(hit.id, "v1")
        self.assertEqual(hit.provider, "codex")
        self.assertIsNone(miss)
        self.assertEqual(sessions.session_from_key("codex:v1", listed).id, "v1")
        self.assertEqual(sessions.session_from_key("v1", listed).id, "v1")

    def test_find_any_session_prefers_the_active_provider(self):
        _write(os.path.join(self.claude, "aa11.jsonl"), [
            {"type": "user", "entrypoint": "sdk-cli", "cwd": "/home/u", "sessionId": "aa11",
             "message": {"role": "user", "content": "claude"}},
        ])
        _write(os.path.join(self.codex, "rollout-aa22.jsonl"), [
            {"type": "session_meta", "payload": {"id": "aa22", "cwd": "/w", "originator": "codex_exec"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "You are Vision, a personal AI assistant"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "codex"}]}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")), \
             patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")), \
             patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")):
            grok_pref = sessions.find_any_session("aa", prefer="codex")
            claude_pref = sessions.find_any_session("aa", prefer="claude")
        self.assertEqual((grok_pref.provider, grok_pref.id), ("codex", "aa22"))
        self.assertEqual((claude_pref.provider, claude_pref.id), ("claude", "aa11"))


class ApplySessionTests(unittest.TestCase):
    def brain(self, provider="grok", session_id="g1"):
        b = type("B", (), {})()
        b.provider = provider
        b.session_id = session_id
        b.handoff = "stale"
        b.resumed = []
        b.resume = lambda sid: (setattr(b, "session_id", sid), setattr(b, "handoff", None), b.resumed.append(sid))
        b.new_session = lambda: (setattr(b, "session_id", None), setattr(b, "handoff", None))
        return b

    def test_same_provider_resumes_the_native_thread(self):
        from vision.cli import _apply_session

        info = sessions.SessionInfo("s2", "grok", "Later chat", 0, "/w")
        brain = self.brain()
        msg = _apply_session(brain, info)
        self.assertEqual(brain.session_id, "s2")
        self.assertIsNone(brain.handoff)
        self.assertEqual(brain.resumed, ["s2"])
        self.assertIn("resumed", msg)
        self.assertIn("Later chat", msg)

    def test_same_session_is_a_noop(self):
        from vision.cli import _apply_session

        info = sessions.SessionInfo("g1", "grok", "Here", 0, "/w")
        brain = self.brain()
        self.assertEqual(_apply_session(brain, info), "already in that conversation")
        self.assertEqual(brain.resumed, [])
        self.assertEqual(brain.session_id, "g1")

    def test_other_provider_keeps_this_brain_and_injects_the_transcript(self):
        from vision.cli import _apply_session

        info = sessions.SessionInfo("c1", "codex", "Pip redesign", 0, "/w")
        brain = self.brain()
        hist = [{"role": "user", "text": "new pip"}, {"role": "assistant", "text": "obsidian"}]
        with patch.object(sessions, "session_history", return_value=hist) as hist_fn:
            msg = _apply_session(brain, info)
            hist_fn.assert_called_once_with("codex", "c1", limit=0, include_context=True)
        self.assertEqual(brain.provider, "grok")
        self.assertIsNone(brain.session_id)
        self.assertIn("new pip", brain.handoff)
        self.assertIn("obsidian", brain.handoff)
        self.assertEqual(brain.resumed, [])
        self.assertIn("from Codex", msg)
        self.assertIn("from that transcript", msg)


class PickerLocateTests(unittest.TestCase):
    def test_locate_finds_the_row_then_falls_back_to_the_named_tab(self):
        tabs = [
            ("Claude", [("claude:a", "A", "")], ""),
            ("Codex", [("codex:b", "B", "")], ""),
            ("Grok", [], "no conversations"),
        ]
        self.assertEqual(_locate(tabs, "codex:b"), (1, 0))
        self.assertEqual(_locate(tabs, "", "Grok"), (2, 0))
        self.assertEqual(_locate(tabs, "missing", "Codex"), (1, 0))
        self.assertEqual(_locate(tabs, ""), (0, 0))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude", "-home-u")
        self.codex = os.path.join(self.tmp.name, "codex", "2026", "09", "18")
        self.grok = os.path.join(self.tmp.name, "grok", "%2Fhome%2Fu")
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        os.makedirs(self.grok)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chat_between_repeated_provider_switches_keeps_all_earlier_context(self):
        from vision.brain import inject_handoff
        from vision.cli import _switch_model
        from vision.config import Config
        from vision.models import provider_for

        cfg = Config()
        cfg.brain.model = "opus"
        local_dir = Path(self.tmp.name) / "local"
        local_dir.mkdir()

        def create_brain(config, **kw):
            return SimpleNamespace(provider=provider_for(config.model), session_id=None,
                                   handoff=None, cancel=lambda: None)

        def save_turn(brain, sid, prompt, reply):
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
            if brain.provider == "claude":
                _write(os.path.join(self.claude, f"{sid}.jsonl"), [
                    {"type": m["role"], "message": m} for m in messages
                ])
            elif brain.provider == "codex":
                _write(os.path.join(self.codex, f"rollout-test-{sid}.jsonl"), [
                    {"type": "response_item", "payload": {"type": "message", **m}} for m in messages
                ])
            elif brain.provider == "grok":
                folder = os.path.join(self.grok, sid)
                os.makedirs(folder)
                _write(os.path.join(folder, "chat_history.jsonl"), [
                    {"type": m["role"], "content": f"<user_query>\n{m['content']}\n</user_query>"
                     if m["role"] == "user" else m["content"]} for m in messages
                ])
            else:
                (local_dir / f"{sid}.json").write_text(json.dumps({"messages": messages}))
            brain.session_id, brain.handoff = sid, None  # a completed provider turn

        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")), \
             patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")), \
             patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")), \
             patch("vision.local.SESSIONS_DIR", local_dir), \
             patch("vision.brain.create_brain", side_effect=create_brain):
            brain = create_brain(cfg.brain)
            earlier = []
            for i, model in enumerate(("opus", "gpt-6-astra", "grok-4.6", "local-model", "opus")):
                if i:
                    brain, _ = _switch_model(cfg, brain, model, voice_mode=False)
                    for text in earlier:
                        self.assertEqual(brain.handoff.count(text), 1)
                user, reply = f"request-{i}-unique", f"answer-{i}-unique"
                prompt = inject_handoff(brain, user)
                save_turn(brain, f"session-{i}", prompt, reply)
                earlier.extend((user, reply))
                # Visible history unfolds every nested provider transfer into the earlier turns, so
                # the chat reads as one conversation after each switch.
                self.assertEqual(sessions.session_history(brain.provider, brain.session_id), [
                    {"role": "user" if n % 2 == 0 else "assistant", "text": text} for n, text in enumerate(earlier)
                ])
                restored = sessions.format_transcript(sessions.session_history(
                    brain.provider, brain.session_id, limit=0, include_context=True,
                ))
                for text in earlier:
                    self.assertEqual(restored.count(text), 1)

    def test_transfers_preserve_legacy_summary_context(self):
        sid = "legacy-context"
        prompt = (
            "You are taking over an ongoing conversation from another model. Its handoff note:\n\n"
            "The user chose SQLite.\n\n---\n\nContinue naturally from here; do not mention the handoff unless asked. "
            "The user's next message:\n\ncontinue"
        )
        _write(os.path.join(self.claude, f"{sid}.jsonl"), [
            {"type": "user", "message": {"content": prompt}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            history = sessions.session_history("claude", sid, limit=0, include_context=True)
        self.assertEqual(history, [{"role": "user", "text": prompt}])

    def test_claude_history_parses_only_what_the_file_gained(self):
        """The typed session is read on every voice turn and three times when a chat opens on it: parsed
        once, then only for the records Claude Code appended; a rewritten file is read from the start."""
        sid = "grows"
        path = os.path.join(self.claude, f"{sid}.jsonl")
        _write(path, [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"}]}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            self.assertEqual(sessions.claude_history(sid), [{"role": "user", "text": "first"}, {"role": "assistant", "text": "looking"}])
            offset = sessions._parsed[(path, False)].offset
            self.assertEqual(offset, os.path.getsize(path))
            with patch.object(sessions, "_fold_claude_lines", side_effect=AssertionError("parsed an unchanged file again")):
                self.assertEqual(len(sessions.claude_history(sid)), 2)
            with open(path, "a") as f:  # an assistant fragment that merges into the last turn, then a new turn
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "patched"}]}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": "thanks"}}))  # no newline yet: still complete JSON
            self.assertEqual(sessions.claude_history(sid), [
                {"role": "user", "text": "first"}, {"role": "assistant", "text": "looking\n\npatched"}, {"role": "user", "text": "thanks"},
            ])
            self.assertEqual(sessions._parsed[(path, False)].offset, os.path.getsize(path))
            with open(path, "a") as f:
                f.write('\n{"type": "user", "message": {"content": "torn')  # a record still being written
            self.assertEqual(sessions.claude_history(sid)[-1], {"role": "user", "text": "thanks"})
            self.assertLess(sessions._parsed[(path, False)].offset, os.path.getsize(path))
            _write(path, [{"type": "user", "message": {"content": "fresh"}}])  # replaced by a shorter file
            self.assertEqual(sessions.claude_history(sid), [{"role": "user", "text": "fresh"}])
            self.assertEqual(sessions.claude_history(sid, limit=1, include_context=True), [{"role": "user", "text": "fresh"}])

    def test_claude_history_skips_tools_and_unwraps_handoff(self):
        sid = "sess-1"
        _write(os.path.join(self.claude, f"{sid}.jsonl"), [
            {"type": "user", "message": {"content": "fix the lock"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"}, {"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "patched"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "<local-command-stdout>x</local-command-stdout>"}},
            {"type": "user", "message": {"content": "This conversation is being handed over to a different model that will not see this transcript. Write a note."}},
            {"type": "user", "message": {"content": "You are taking over an ongoing conversation from another model. What happened so far:\n\nnote\n\n---\n\nContinue naturally from here; do not mention the handoff unless asked. The user's next message:\n\nwhat next?"}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            hist = sessions.claude_history(sid)
        self.assertEqual(hist, [
            {"role": "user", "text": "fix the lock"},
            {"role": "assistant", "text": "looking\n\npatched"},
            {"role": "user", "text": "what next?"},
        ])

    def test_codex_history_keeps_user_and_assistant_text(self):
        sid = "01a0b5b5-c272-75f2-93f0-6d2733f9572f"
        _write(os.path.join(self.codex, f"rollout-2026-09-18T14-09-43-{sid}.jsonl"), [
            {"type": "session_meta", "payload": {"id": sid, "originator": "codex_exec"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Vision"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the lock"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "content": [{"type": "text", "text": "hmm"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "on it"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "Bash"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "patched talker.py"}]}},
        ])
        with patch("vision.codex.CODEX_SESSIONS", os.path.join(self.tmp.name, "codex")):
            hist = sessions.codex_history(sid)
        self.assertEqual(hist, [
            {"role": "user", "text": "fix the lock"},
            {"role": "assistant", "text": "on it\n\npatched talker.py"},
        ])

    def test_grok_history_unwraps_user_query_and_skips_wrappers(self):
        sid = "01a0b65a-039e-72c1-9ff0-f3e54c08899b"
        folder = os.path.join(self.grok, sid)
        os.makedirs(folder)
        _write(os.path.join(folder, "chat_history.jsonl"), [
            {"type": "system", "content": "You are Grok"},
            {"type": "user", "content": [{"type": "text", "text": "<user_info>\nOS Version: linux\n</user_info>"}]},
            {"type": "user", "content": [{"type": "text", "text": "<user_query>\nfix the lock\n</user_query>"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "hmm"}]},
            {"type": "assistant", "content": "on it", "tool_calls": [{"name": "read_file"}]},
            {"type": "assistant", "content": ""},
            {"type": "assistant", "content": "patched talker.py"},
            {"type": "user", "content": "You are taking over an ongoing conversation from another model. What happened so far:\n\nnote\n\n---\n\nContinue naturally from here; do not mention the handoff unless asked. The user's next message:\n\nwhat next?"},
        ])
        with patch("vision.grok.GROK_SESSIONS", os.path.join(self.tmp.name, "grok")):
            hist = sessions.grok_history(sid)
        self.assertEqual(hist, [
            {"role": "user", "text": "fix the lock"},
            {"role": "assistant", "text": "on it\n\npatched talker.py"},
            {"role": "user", "text": "what next?"},
        ])

    def test_format_transcript_keeps_the_tail(self):
        messages = [
            {"role": "user", "text": "aaaa"},
            {"role": "assistant", "text": "bbbb"},
            {"role": "user", "text": "cccc"},
        ]
        text = sessions.format_transcript(messages, max_chars=20)
        self.assertIn("cccc", text)
        self.assertIn("[earlier turns omitted]", text)
        self.assertTrue(text.startswith("[earlier turns omitted]"))
        self.assertEqual(sessions.format_transcript([]), "")
        self.assertEqual(sessions.session_history("claude", ""), [])
        self.assertEqual(sessions.user_turns("claude", ""), [])

    def test_full_transcript_has_no_message_or_character_cap(self):
        sid = "long-session"
        events = [
            {"type": "user", "message": {"content": f"message {i}: " + "x" * 1000}}
            for i in range(sessions.HISTORY_LIMIT + 1)
        ]
        _write(os.path.join(self.claude, f"{sid}.jsonl"), events)
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            history = sessions.session_history("claude", sid, limit=0)
        self.assertEqual(len(history), len(events))
        transcript = sessions.format_transcript(history)
        self.assertGreater(len(transcript), 32_000)
        self.assertTrue(transcript.startswith("User: message 0: "))
        self.assertIn(f"message {sessions.HISTORY_LIMIT}: ", transcript)
        self.assertNotIn("[earlier turns omitted]", transcript)

    def test_user_turns_are_oldest_first(self):
        sid = "sess-2"
        _write(os.path.join(self.claude, f"{sid}.jsonl"), [
            {"type": "user", "message": {"content": "first"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "user", "message": {"content": "second"}},
        ])
        with patch.object(sessions, "CLAUDE_PROJECTS", os.path.join(self.tmp.name, "claude")):
            self.assertEqual(sessions.user_turns("claude", sid), ["first", "second"])


if __name__ == "__main__":
    unittest.main()
