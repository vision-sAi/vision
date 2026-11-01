from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import _nopush  # noqa: F401  (keeps the suite off the real phones)

from vision import server
from vision.brain import COMPACTED, inject_handoff
from vision.turnjournal import ChatJournal, recover


class _Turn:
    def __init__(self, text, sid, error=""):
        self.text, self.session_id, self.model, self.error, self.is_error = text, sid, "stub", error, bool(error)


class _Brain:
    """A brain mid-conversation: a session, a context figure, and a summary when asked for one."""

    provider = "claude"
    workdir = "/tmp"

    def __init__(self, cfg, session_id="s1", summary="The user is fixing the upload test in tests/test_upload.py.", fail=False):
        self.cfg, self.session_id, self.handoff = cfg, session_id, None
        self.context = (150_000, 200_000) if session_id else None
        self.summary, self.fail = summary, fail
        self.asked: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def resolved_model(self):
        return self.cfg.model

    def cancel(self):
        self.release.set()

    def new_session(self):
        self.session_id, self.handoff, self.context = None, None, None

    def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
        self.asked.append(inject_handoff(self, text))
        self.release.wait(5)
        if self.fail:
            raise RuntimeError("the provider is down")
        reply = self.summary if text == server.Chat.COMPACT_PROMPT else f"echo: {text}"
        self.session_id = self.session_id or "s2"
        return _Turn(reply, self.session_id)


ROWS = [{"role": "user", "text": "why does test_upload fail?"},
        {"role": "assistant", "text": "drop_uploads runs on a timer; I made it skip young files.", "duration_ms": 4000}]


class CompactTests(unittest.TestCase):
    """`compact` from an app: the brain summarises, the chat carries on in a fresh session seeded with the summary,
    and a note marks the place, for any brain."""

    def _chat(self, brain=None, journal_dir=None):
        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, _Brain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False,
                         journal_enabled=journal_dir is not None)
        events: list[dict] = []
        hub.post = events.append
        chat = hub.open_chat(brain or _Brain(cfg.brain), cfg)
        chat._transcript = [dict(r) for r in ROWS]
        return hub, chat, events

    def _compact(self, hub, chat, then=None):
        ws = Mock(send_json=AsyncMock())

        async def go():
            await hub.handle(ws, {"type": "compact", "chat": chat.id})
            if then:
                await then()
            while chat._turn_task:
                await chat._turn_task

        asyncio.run(go())
        return [c.args[0] for c in ws.send_json.call_args_list]

    def test_the_chat_carries_on_from_a_summary_in_a_fresh_session(self):
        hub, chat, events = self._chat()
        brain = chat.brain
        self.assertEqual(self._compact(hub, chat), [])
        self.assertEqual(brain.asked, [server.Chat.COMPACT_PROMPT])  # asked in the old session, which has it all
        self.assertIsNone(brain.session_id)
        self.assertTrue(brain.handoff.startswith(COMPACTED))
        self.assertIn("tests/test_upload.py", brain.handoff)
        self.assertIn("drop_uploads runs on a timer", brain.handoff)  # the latest exchanges as they were said
        # Shown like a turn, so every app says it is under way, and done says it was a compaction.
        kinds = [e["type"] for e in events if e.get("chat") == chat.id and e["type"] in ("start", "status", "done")]
        self.assertEqual(kinds, ["start", "status", "done"])
        start = next(e for e in events if e["type"] == "start")
        self.assertEqual(start["note"], "Compacting the conversation")
        done = next(e for e in events if e["type"] == "done")
        self.assertTrue(done["compacted"])
        self.assertEqual(done["error"], "")
        self.assertFalse(chat.busy)
        # The transcript stays, with a note that survives a refetch of the history.
        rows = chat.history()
        self.assertEqual(rows[:2], ROWS)
        self.assertEqual(rows[-1]["role"], "note")
        self.assertTrue(rows[-1]["compacted"])
        self.assertIn("150k tokens", rows[-1]["text"])
        self.assertIn("duration_ms", rows[-1])
        # The context figure drops at once (a guess until the next turn measures it).
        figure = chat.summary()["context"]
        self.assertEqual(figure["window"], 200_000)
        self.assertLess(figure["tokens"], 5_000)
        # The next message goes to a fresh session that starts from the summary, not as a handover between models.
        async def finish():
            chat.queue("and now commit it", False, False)
            await chat._turn_task

        with patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            asyncio.run(finish())
        first = brain.asked[-1]
        self.assertTrue(first.startswith(COMPACTED))
        self.assertNotIn("taking over", first)
        self.assertTrue(first.endswith("and now commit it"))

    def test_a_busy_chat_or_one_with_nothing_said_is_refused(self):
        hub, chat, events = self._chat()
        chat.busy = True
        said = self._compact(hub, chat)
        self.assertEqual(said, [{"type": "toast", "text": "Vision is still replying; compact once it has finished."}])
        self.assertEqual(chat.brain.asked, [])
        self.assertEqual(chat.brain.session_id, "s1")

        from vision.config import Config

        hub, chat, _ = self._chat(brain=_Brain(Config().brain, session_id=None))
        self.assertEqual(self._compact(hub, chat), [{"type": "toast", "text": "Nothing to compact yet."}])
        self.assertFalse(chat.busy)

    def test_without_a_summary_the_latest_exchanges_carry_on(self):
        from vision.config import Config

        hub, chat, events = self._chat(brain=_Brain(Config().brain, fail=True))
        self._compact(hub, chat)
        self.assertIsNone(chat.brain.session_id)
        self.assertTrue(chat.brain.handoff.startswith(COMPACTED))
        self.assertNotIn("A summary of it", chat.brain.handoff)
        self.assertIn("why does test_upload fail?", chat.brain.handoff)
        self.assertIn("latest exchanges", chat.history()[-1]["text"])

    def test_stop_keeps_the_session_it_had(self):
        hub, chat, events = self._chat()
        brain = chat.brain
        brain.release.clear()

        async def stop():
            await asyncio.sleep(0.05)
            hub_ws = Mock(send_json=AsyncMock())
            await hub.handle(hub_ws, {"type": "cancel", "chat": chat.id})

        self._compact(hub, chat, then=stop)
        self.assertEqual(brain.session_id, "s1")
        self.assertIsNone(brain.handoff)
        done = next(e for e in events if e["type"] == "done")
        self.assertFalse(done["compacted"])
        self.assertIn("Stopped", done["error"])
        self.assertIn({"type": "toast", "text": done["error"], "chat": chat.id}, events)  # the failed row goes with the refetch
        self.assertEqual(chat.history(), ROWS)  # no note: nothing happened

    def test_a_message_sent_meanwhile_waits_for_the_fresh_session(self):
        hub, chat, events = self._chat()
        brain = chat.brain
        brain.release.clear()

        async def send():
            await asyncio.sleep(0.05)
            chat.queue("next thing", False, False)
            self.assertEqual([p[0] for p in chat._pending], ["next thing"])
            brain.release.set()

        with patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            self._compact(hub, chat, then=send)
        self.assertEqual(len(brain.asked), 2)
        self.assertTrue(brain.asked[1].startswith(COMPACTED) and brain.asked[1].endswith("next thing"))
        self.assertEqual(brain.session_id, "s2")

    def test_a_restart_before_the_next_turn_carries_on_from_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp, patch("vision.turnjournal.JOURNAL_DIR", Path(tmp)):
            hub, chat, _ = self._chat(journal_dir=tmp)
            self._compact(hub, chat)
            state = recover(ChatJournal(chat.id, Path(tmp)).read())
            self.assertIsNone(state["session_id"])
            self.assertTrue(state["handoff"].startswith(COMPACTED))
            self.assertTrue(state["history"][-1]["compacted"])
            # Once a turn has run on the fresh session, it has the summary: none is carried again.
            chat.journal.append("start", text="hi")
            chat.journal.append("done", text="hello", session_id="s9")
            state = recover(ChatJournal(chat.id, Path(tmp)).read())
            self.assertEqual((state["session_id"], state["handoff"]), ("s9", ""))


if __name__ == "__main__":
    unittest.main()
