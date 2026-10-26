from __future__ import annotations

import asyncio
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from vision.brain import Turn
from vision.config import Config
from vision.server import Hub


def make_brain(cfg):
    brain = SimpleNamespace(provider="claude", cfg=cfg.brain, workdir="/tmp", session_id=None,
                            voice_mode=False, cancel=Mock(), new_session=Mock(), resume=Mock(), resolved_model=lambda: cfg.brain.model)
    def answer(text, **callbacks):
        if callbacks.get("on_text"):
            callbacks["on_text"]("Typed reply")
        return Turn(text="Typed reply")
    brain.ask = Mock(side_effect=answer)
    return brain


class TerminalTalkTests(unittest.TestCase):
    """`vision talk` is the chat opened with /talk on; push-to-talk rides on Enter in an empty box."""

    def test_talk_opens_the_chat_in_a_voice_conversation(self):
        from typer.testing import CliRunner
        from vision.cli import app

        with patch("vision.cli.chat") as chat:
            result = CliRunner().invoke(app, ["talk", "--ptt", "-c"])
        self.assertEqual(result.exit_code, 0, result.output)
        kwargs = chat.call_args.kwargs
        self.assertTrue(kwargs["talk"])
        self.assertTrue(kwargs["ptt"])
        self.assertTrue(kwargs["cont"])

    def test_enter_on_an_empty_box_goes_to_push_to_talk_not_the_chat(self):
        import os
        import tempfile
        from prompt_toolkit.keys import Keys
        from vision.ui import ChatScreen

        with tempfile.TemporaryDirectory() as directory:
            screen = ChatScreen(os.path.join(directory, "history"), lambda: "")
            sent, pressed = [], []
            screen.on_submit = sent.append
            screen.on_empty_enter = lambda: pressed.append(True) or True
            binding = [b for b in screen.app.key_bindings.get_bindings_for_keys((Keys.ControlM,)) if b.filter()][-1]
            binding.handler(type("Event", (), {"app": screen.app})())
        self.assertEqual(pressed, [True])
        self.assertEqual(sent, [])


class RemoteRoutingTests(unittest.IsolatedAsyncioTestCase):
    """The phone's side: each chat on the hub routes its own turns, like the terminal's talk loop."""

    def setUp(self):
        self.cfg = Config()
        self.brain = make_brain(self.cfg)
        self.hub = Hub(self.cfg, self.brain, "token", log=lambda _: None)
        self.events = []
        self.hub.post = self.events.append
        (self.chat,) = self.hub.chats.values()
        self.voice = Mock()
        def answer(text, **callbacks):
            callbacks["on_text"]("Spoken reply")
            return Turn(text="Spoken reply", model="sonnet")
        self.voice.ask.side_effect = answer
        self.chat.conversation = self.voice
        # A fresh chat has no saved session to seed its transcript from.
        history = patch("vision.sessions.session_history", return_value=[])
        history.start()
        self.addCleanup(history.stop)

    def socket(self):
        ws = Mock()
        ws.send_json = AsyncMock()
        return ws

    async def test_playback_does_not_send_typed_input_to_conversation(self):
        wire = Mock()
        with patch.object(self.hub, "speaker", return_value=Mock()), patch("vision.server.WireSpeaker", return_value=wire):
            await self.chat.run_turn("typed", speak=True, voice=False)
        self.brain.ask.assert_called_once()
        self.voice.ask.assert_not_called()
        wire.feed.assert_called_once_with("Typed reply")
        self.assertFalse(self.brain.voice_mode)

    async def test_voice_routes_to_conversation_even_without_playback(self):
        await self.chat.run_turn("spoken", speak=False, voice=True)
        self.voice.ask.assert_called_once()
        self.brain.ask.assert_not_called()
        history = self.chat.history()
        self.assertIsInstance(history[-1].pop("duration_ms"), int)  # the reply's row also says how long the turn took
        self.assertEqual(history, [{"role": "user", "text": "spoken"}, {"role": "assistant", "text": "Spoken reply"}])
        self.assertIsNotNone(self.hub.hello()["chats"][0]["history_id"])
        self.assertFalse(self.chat.busy)

    async def test_voice_then_text_preserves_mixed_history_on_reconnect(self):
        await self.chat.run_turn("spoken", speak=False, voice=True)
        await self.chat.run_turn("typed", speak=False, voice=False)
        self.assertEqual([row["text"] for row in self.chat.history()], ["spoken", "Spoken reply", "typed", "Typed reply"])
        self.assertIs(self.voice.agent, self.brain)

    async def test_new_and_resume_open_their_own_chats_and_leave_this_one_alone(self):
        from vision.sessions import SessionInfo

        ws = self.socket()
        old = SessionInfo(id="old-typed", provider=self.brain.provider, title="earlier", last_active=0.0)
        with patch("vision.brain.create_brain", side_effect=lambda *a, **k: make_brain(self.cfg)), \
             patch("vision.sessions.find_any_session", return_value=old), \
             patch("vision.cli._apply_session", return_value="resumed old-typed"):
            await self.hub.handle(ws, {"type": "new"})
            await self.hub.handle(ws, {"type": "resume", "session_id": "old-typed"})
        opened = [c.args[0] for c in ws.send_json.call_args_list]
        self.assertEqual([frame.get("opened") for frame in opened], [True, True])
        self.assertEqual(len(self.hub.chats), 3)
        ids = {frame["chat"] for frame in opened} | {self.chat.id}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len({c.history_id for c in self.hub.chats.values()}), 3)
        self.assertEqual(self.hub.chats[opened[1]["chat"]].title, "earlier")
        self.voice.new_session.assert_not_called()

    async def test_resume_of_an_unknown_id_changes_nothing(self):
        ws = self.socket()
        history_id = self.chat.history_id
        with patch("vision.sessions.find_any_session", return_value=None):
            await self.hub.handle(ws, {"type": "resume", "session_id": "nope"})
        self.assertEqual(list(self.hub.chats), [self.chat.id])
        self.assertEqual(self.chat.history_id, history_id)
        self.voice.new_session.assert_not_called()
        self.assertIn("no conversation starts with", ws.send_json.call_args.args[0]["text"])

    async def test_cancel_reaches_both_models_and_audio(self):
        wire = self.chat._wire = Mock()
        self.hub.cancel()
        self.voice.cancel.assert_called_once()
        self.brain.cancel.assert_called_once()
        wire.stop.assert_called_once()

    async def test_messages_received_during_a_turn_run_in_order(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def run_one(text, speak, voice, talk=False):
            calls.append(text)
            if text == "first":
                started.set()
                await release.wait()
            self.events.append({"type": "done", "text": f"reply to {text}", "busy": bool(self.chat._pending)})

        self.chat._run_one_turn = run_one
        ws = self.socket()
        await self.hub.handle(ws, {"type": "message", "text": "first", "chat": self.chat.id})
        await asyncio.wait_for(started.wait(), 1)

        await self.hub.handle(ws, {"type": "message", "text": "second", "chat": self.chat.id})
        self.assertEqual(calls, ["first"])
        self.assertEqual(len(self.chat._pending), 1)
        task = self.chat._turn_task
        self.assertIsNotNone(task)
        release.set()
        await asyncio.wait_for(task, 3)

        self.assertFalse(self.chat.busy)
        self.assertEqual(calls, ["first", "second"])
        done = [event for event in self.events if event["type"] == "done"]
        self.assertEqual([event["busy"] for event in done], [True, False])


if __name__ == "__main__":
    unittest.main()
