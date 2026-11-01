"""Renaming a chat, and editing a sent message into a new chat branched from that point."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import _nopush  # noqa: F401  (keeps the suite off the real phones)

from vision import server


class _Turn:
    def __init__(self, text, sid):
        self.text, self.session_id, self.model, self.error, self.is_error = text, sid, "stub", "", False


class _Brain:
    """Answers "echo: <text>", and keeps the handoff it was given when its first turn ran."""

    provider = "claude"
    workdir = "/tmp"

    def __init__(self, cfg, session_id=None):
        self.cfg, self.session_id, self.handoff = cfg, session_id, None
        self.handed: list = []

    def resolved_model(self):
        return self.cfg.model

    def cancel(self):
        pass

    def ask(self, text, on_text=None, **kw):
        self.handed.append(self.handoff)
        if not self.session_id:
            self.handoff = None  # delivered with the first turn, as every brain does
        self.session_id = self.session_id or f"sess-{id(self) % 1000}"
        time.sleep(0.05)
        reply = f"echo: {text}"
        if on_text:
            on_text(reply)
        return _Turn(reply, self.session_id)


def _quiet(stack: ExitStack, tmp: str) -> None:
    """No real provider, port or journal folder: the brains are _Brain, the journals in `tmp`."""
    stack.enter_context(patch("vision.turnjournal.JOURNAL_DIR", Path(tmp) / "journals"))
    stack.enter_context(patch.object(server, "PINNED", Path(tmp) / "pinned.json"))
    stack.enter_context(patch("vision.brain.create_brain", side_effect=lambda c, **kw: _Brain(c, kw.get("session_id"))))
    stack.enter_context(patch("vision.providers.settle", lambda cfg: ""))
    stack.enter_context(patch("vision.providers._server_answers", lambda *a, **k: False))
    stack.enter_context(patch("vision.providers._is_model_server", lambda *a, **k: False))
    stack.enter_context(patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []))


def _hub(journal: bool = True):
    from vision.config import Config

    cfg = Config()
    cfg.conversation.model = "haiku"
    cfg.remote.suggestions = False
    return server.Hub(cfg, _Brain(cfg.brain), "tok", log=lambda *_: None, open_initial=False, journal_enabled=journal)


ROWS = [
    {"role": "user", "text": "one"},
    {"role": "assistant", "text": "echo: one", "duration_ms": 10},
    {"role": "user", "text": "two"},
    {"role": "assistant", "text": "echo: two", "duration_ms": 12},
]


class RenameTests(unittest.TestCase):
    def test_a_rename_is_shared_kept_over_a_restart_and_never_replaced_by_an_automatic_title(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            _quiet(stack, tmp)
            hub = _hub()
            chat = hub.open_chat()
            posted = []
            hub.post = posted.append
            ws = Mock(send_json=AsyncMock())
            asyncio.run(hub.handle(ws, {"type": "rename", "chat": chat.id, "title": "  Upload   race  "}))
            self.assertEqual(chat.title, "Upload race")
            self.assertEqual((posted[-1]["type"], posted[-1]["title"]), ("chat", "Upload race"), "every app hears of it")
            asyncio.run(hub.handle(ws, {"type": "rename", "chat": chat.id, "title": "   "}))
            self.assertEqual(chat.title, "Upload race", "an empty title changes nothing")
            chat.history()  # where a title is worked out from the first message, when there is none
            self.assertEqual(chat.title, "Upload race")
            updated = chat.updated
            chat.journal.release()  # the old process is gone

            restarted = _hub()
            again = restarted.chats[chat.id]
            self.assertEqual(again.title, "Upload race")
            self.assertAlmostEqual(again.updated, updated, delta=1, msg="a rename doesn't move the chat up the list")

    def test_a_terminal_chat_keeps_the_name_an_app_gave_it_until_its_conversation_changes(self):
        hub = Mock(chats={})
        chat = server.LinkedChat(hub, {"pid": 4242})
        state = {"type": "chat", "provider": "claude", "session_id": "s1", "title": "fix the tests"}
        chat.on_event(dict(state))
        self.assertEqual(chat.title, "fix the tests")
        chat.rename("Flaky tests")
        chat.on_event(dict(state))  # the terminal says its own title with every snapshot
        self.assertEqual(chat.title, "Flaky tests")
        chat.on_event({**state, "session_id": "s2", "title": "something else"})  # /new in the terminal
        self.assertEqual(chat.title, "something else")


class BranchRowTests(unittest.TestCase):
    def test_the_row_is_found_by_index_and_checked_by_its_text(self):
        self.assertEqual(server._branch_row(ROWS, 2, "two"), 2)
        self.assertEqual(server._branch_row(ROWS, 2), 2)
        self.assertEqual(server._branch_row(ROWS, 3, "two"), 2, "an app's rows one behind still find it")
        self.assertEqual(server._branch_row(ROWS, 9, "one"), 0)
        self.assertIsNone(server._branch_row(ROWS, 1), "a reply is not something to edit")
        self.assertIsNone(server._branch_row(ROWS, 2, "three"))
        self.assertIsNone(server._branch_row(ROWS, True))

    def test_a_branch_of_a_branch_is_marked_once(self):
        self.assertEqual(server._branch_title("Upload race"), "Upload race (edit)")
        self.assertEqual(server._branch_title("Upload race (edit)"), "Upload race (edit)")
        self.assertEqual(server._branch_title(""), "")
        self.assertLessEqual(len(server._branch_title("x" * 300)), server.TITLE_CHARS)


class BranchTests(unittest.TestCase):
    def test_a_branch_holds_the_earlier_rows_hands_them_over_and_comes_back_after_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            _quiet(stack, tmp)
            hub = _hub()
            chat = hub.open_chat()
            chat._transcript = [dict(r) for r in ROWS]
            chat.title = "Counting"
            branch = asyncio.run(hub.branch_chat(chat, 2, "two"))
            self.assertNotEqual(branch.id, chat.id)
            self.assertEqual(branch.title, "Counting (edit)")
            self.assertEqual([r["text"] for r in branch.history()], ["one", "echo: one"])
            self.assertEqual(branch.history()[-1]["duration_ms"], 10, "the earlier replies keep their footers")
            self.assertIsNone(branch.session_id, "a fresh session on any brain")
            self.assertEqual(branch.brain.handoff, "User: one\n\nVision: echo: one")
            self.assertEqual(len(chat.history()), 4, "the chat it came from is left as it was")
            with self.assertRaises(ValueError):
                asyncio.run(hub.branch_chat(chat, 1, "echo: one"))
            branch.journal.release()
            chat.journal.release()

            restarted = _hub()
            again = restarted.chats[branch.id]
            self.assertEqual([r["text"] for r in again.history()], ["one", "echo: one"])
            self.assertEqual(again.title, "Counting (edit)")
            self.assertIn("User: one", again.brain.handoff or "", "a restart before its first reply hands it over again")

    def test_editing_a_message_opens_a_new_chat_that_runs_it_after_the_earlier_turns(self):
        from fastapi.testclient import TestClient

        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            _quiet(stack, tmp)
            stack.enter_context(patch("vision.link.LIVE_DIR", Path(tmp)))
            stack.enter_context(patch.object(server.Hub, "warm_up", lambda self: None))
            stack.enter_context(patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain))
            hub = _hub(journal=False)
            chat = hub.open_chat()
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                def drain(until, limit=80):
                    for _ in range(limit):
                        ev = ws.receive_json()
                        if until(ev):
                            return ev
                    raise AssertionError("never saw the frame")

                ws.receive_json()  # hello
                for text in ("one", "two"):
                    ws.send_json({"type": "message", "chat": chat.id, "text": text})
                    drain(lambda ev: ev["type"] == "done")
                rows = tc.get("/history", params={"chat": chat.id, "limit": 200}, headers=auth).json()
                self.assertEqual([r["role"] for r in rows], ["user", "assistant", "user", "assistant"])

                ws.send_json({"type": "branch", "chat": chat.id, "at": 2, "was": rows[2]["text"], "text": "two, but shorter"})
                opened = drain(lambda ev: ev["type"] == "chat" and ev.get("opened"))
                new = opened["chat"]
                self.assertNotEqual(new, chat.id)
                self.assertTrue(opened["title"].endswith("(edit)"))
                done = drain(lambda ev: ev["type"] == "done" and ev["chat"] == new)
                self.assertEqual(done["text"], "echo: two, but shorter")

                branched = tc.get("/history", params={"chat": new, "limit": 200}, headers=auth).json()
                self.assertEqual([r["text"] for r in branched], ["one", "echo: one", "two, but shorter", "echo: two, but shorter"])
                self.assertEqual(branched[-1]["duration_ms"], done["duration_ms"], "the footer survives a refetch")
                handed = hub.chats[new].brain.handed[0]
                self.assertIn("User: one", handed)
                self.assertNotIn("two", handed, "nothing from the edited message on")
                again = tc.get("/history", params={"chat": chat.id, "limit": 200}, headers=auth).json()
                self.assertEqual(again, rows, "the original chat is untouched")

                ws.send_json({"type": "branch", "chat": chat.id, "at": 1, "was": "echo: one", "text": "x"})
                self.assertIn("no longer in this chat", drain(lambda ev: ev["type"] == "error")["text"])


if __name__ == "__main__":
    unittest.main()
