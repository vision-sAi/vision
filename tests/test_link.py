from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from unittest.mock import patch

from vision import link, server


class _Terminal:
    """A stand-in for a `vision` chat: announces itself and echoes messages it is sent."""

    def __init__(self):
        self.frames = []
        self.busy = False
        self.waiting = False
        self.questions = []
        self.session = "term-sess-1"
        self.host = link.LinkHost(self.summary, self.on_frame)

    def summary(self):
        return {"pid": os.getpid(), "title": "typed here", "provider": "claude", "model": "Opus 5", "model_id": "opus",
                "effort": "high", "session_id": self.session, "workdir": "/tmp", "busy": self.busy, "waiting": self.waiting,
                "questions": self.questions if self.waiting else []}

    def on_frame(self, frame):
        self.frames.append(frame)
        if frame["type"] == "message":
            threading.Thread(target=self.turn, args=(frame["text"], frame.get("speak", False)), daemon=True).start()

    def turn(self, text, speak):
        self.busy = True
        self.host.post({"type": "start", "text": text, "speak": speak})
        self.host.post({"type": "delta", "text": "echo: "})
        self.host.post({"type": "delta", "text": text})
        self.host.post({"type": "done", "text": f"echo: {text}", "error": "", "busy": False, "session_id": self.session, "model": "opus"})
        self.busy = False


class LinkTests(unittest.TestCase):
    def _hub(self):
        from vision.config import Config

        from tests.test_server import _StubBrain

        cfg = Config()
        brain = _StubBrain(cfg.brain)
        return server.Hub(cfg, brain, "tok", log=lambda *_: None)

    def _drain(self, ws, until, limit=60):
        seen = []
        for _ in range(limit):
            ev = ws.receive_json()
            seen.append(ev)
            if until(ev, seen):
                return seen
        raise AssertionError(f"never saw the frame; got {[e['type'] for e in seen]}")

    def test_descriptor_sweeps_dead_pids(self):
        with tempfile.TemporaryDirectory() as d, patch.object(link, "LIVE_DIR", Path(d)):
            Path(d, "999999999.json").write_text('{"pid": 999999999}')
            Path(d, "junk.json").write_text("not json")
            self.assertEqual(link.list_links(), [])
            self.assertEqual(os.listdir(d), [])

    def test_server_connecting_mid_reply_gets_the_reply_so_far(self):
        # `vision serve` restarted during a workflow: the rows that had already finished never post
        # again, so the terminal replays the running reply (text, steers, every row as it stands).
        with tempfile.TemporaryDirectory() as d, patch.object(link, "LIVE_DIR", Path(d)):
            term = _Terminal()
            term.host.start()
            try:
                term.busy = True
                post = term.host.post
                post({"type": "start", "text": "port it", "speak": True})
                post({"type": "delta", "text": "Starting "})
                post({"type": "delta", "text": "the run."})
                post({"type": "agent", "id": "wf#0", "label": "critic", "done": False, "at": 17})
                post({"type": "agent", "id": "wf#0", "label": "critic", "done": True, "at": 17})
                post({"type": "agent", "id": "wf#1", "label": "fill", "done": False, "at": 17})
                post({"type": "steered", "text": "hurry up"})
                post({"type": "delta", "text": " On it."})
                post({"type": "tool", "id": "t1", "name": "Bash", "done": True, "at": 24})
                info = link.list_links()[0]
                c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                c.connect(info["sock"])
                c.sendall((json.dumps({"type": "hello"}) + "\n").encode())
                buf = b""
                c.settimeout(2)
                while buf.count(b"\n") < 8:
                    buf += c.recv(65536)
                c.close()
                frames = [json.loads(line) for line in buf.decode().splitlines()]
                self.assertEqual([f["type"] for f in frames], ["chat", "start", "delta", "steered", "delta", "agent", "agent", "tool"])
                self.assertFalse(frames[1]["speak"])
                self.assertEqual(frames[2]["text"], "Starting the run.")
                self.assertEqual({f["id"]: f["done"] for f in frames if f["type"] == "agent"}, {"wf#0": True, "wf#1": False})
                post({"type": "done", "text": "", "error": "", "busy": False})
                self.assertEqual(term.host._replay(), [])  # a finished reply is not replayed
            finally:
                term.host.stop()

    def test_late_phone_gets_terminal_question_in_hello(self):
        from fastapi.testclient import TestClient

        questions = [{"question": "Which?", "header": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]
        with tempfile.TemporaryDirectory() as d, patch.object(link, "LIVE_DIR", Path(d)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch.object(server.Hub, "LINK_POLL", 0.1):
            term = _Terminal()
            term.host.start()
            hub = self._hub()
            try:
                with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as first:
                    first.receive_json()
                    self._drain(first, lambda ev, _: ev["type"] == "chat" and ev.get("source") == "terminal")
                    term.busy = term.waiting = True
                    term.questions = questions
                    term.host.post({"type": "question", "questions": questions})
                    term.host.post_summary(force=True)
                    self._drain(first, lambda ev, _: ev["type"] == "chat" and ev.get("waiting"))
                    with tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as late:
                        snapshot = next(c for c in late.receive_json()["chats"] if c["source"] == "terminal")
                        self.assertTrue(snapshot["waiting"])
                        self.assertEqual(snapshot["questions"], questions)
            finally:
                term.host.stop()

    def test_terminal_chat_is_listed_driven_and_leaves_when_it_quits(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as d, patch.object(link, "LIVE_DIR", Path(d)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch.object(server.Hub, "LINK_POLL", 0.1), \
             patch("vision.sessions.session_history", lambda provider, sid: [{"role": "user", "text": "earlier"}, {"role": "assistant", "text": "ok"}]):
            term = _Terminal()
            term.host.start()
            hub = self._hub()
            app = server.create_app(hub)
            with TestClient(app) as tc, tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                # the terminal shows up as a chat once the sweep has connected to it
                seen = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("source") == "terminal" and ev.get("title") == "typed here")
                tid = seen[-1]["chat"]
                self.assertEqual(seen[-1]["model"], "Opus 5")
                self.assertEqual(seen[-1]["session_id"], "term-sess-1")

                # /sessions marks that conversation as open in this chat
                with patch("vision.sessions.list_all_sessions", lambda: []):
                    r = tc.get("/sessions", headers={"Authorization": "Bearer tok"})
                    self.assertEqual(r.status_code, 200)

                # a message from the phone runs in the terminal; the reply streams back with the chat id
                ws.send_json({"type": "message", "chat": tid, "text": "hi there"})
                seen = self._drain(ws, lambda ev, _: ev["type"] == "done" and ev["chat"] == tid)
                kinds = [e["type"] for e in seen if e.get("chat") == tid]
                self.assertIn("start", kinds)
                self.assertIn("delta", kinds)
                done = seen[-1]
                self.assertEqual(done["text"], "echo: hi there")
                self.assertEqual(done["session_id"], "term-sess-1")
                self.assertIn("history_id", done)
                self.assertEqual(term.frames[0]["type"], "message")

                # the transcript: what was on disk, then the new turn
                r = tc.get("/history", params={"chat": tid}, headers={"Authorization": "Bearer tok"})
                self.assertEqual([m["text"] for m in r.json()], ["earlier", "ok", "hi there", "echo: hi there"])

                # answers, cancel and model changes are relayed
                ws.send_json({"type": "answer", "chat": tid, "answers": {"q": "a"}})
                ws.send_json({"type": "cancel", "chat": tid})
                ws.send_json({"type": "model", "chat": tid, "model": "sonnet", "effort": "low"})
                deadline = time.time() + 3
                while len(term.frames) < 4 and time.time() < deadline:
                    time.sleep(0.02)
                self.assertEqual([f["type"] for f in term.frames[1:4]], ["answer", "cancel", "model"])
                self.assertEqual(term.frames[3]["model"], "sonnet")

                # closing it from the phone asks the terminal to quit (as /quit typed there would)...
                ws.send_json({"type": "close", "chat": tid})
                deadline = time.time() + 3
                while len(term.frames) < 5 and time.time() < deadline:
                    time.sleep(0.02)
                self.assertEqual(term.frames[4]["type"], "quit")
                self.assertIn(tid, hub.chats)  # ...and it is the terminal going that removes the chat

                term.host.stop()
                self._drain(ws, lambda ev, _: ev["type"] == "chat_closed" and ev["chat"] == tid)
                self.assertNotIn(tid, hub.chats)
                self.assertEqual(os.listdir(d), [])

    def test_server_copy_of_a_terminal_conversation_closes(self):
        """`vision serve` continued the session a terminal has open: the terminal keeps it, the copy goes."""
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as d, patch.object(link, "LIVE_DIR", Path(d)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch.object(server.Hub, "LINK_POLL", 0.1), \
             patch("vision.sessions.session_history", lambda provider, sid: []):
            term = _Terminal()
            hub = self._hub()
            copy = next(iter(hub.chats.values()))
            copy.brain.session_id = term.session
            try:
                with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as ws:
                    ws.receive_json()
                    # Only now: the first sweep can find the terminal and close the copy before this socket
                    # connects, and a frame posted to no one is never seen here.
                    term.host.start()
                    seen = self._drain(ws, lambda ev, _: ev["type"] == "chat_closed")
                    tid = next(e["chat"] for e in seen if e["type"] == "chat" and e.get("source") == "terminal")
                    self.assertEqual(seen[-1], {"type": "chat_closed", "chat": copy.id, "moved_to": tid})
                    with tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as late:
                        self.assertEqual([c["chat"] for c in late.receive_json()["chats"]], [tid])
            finally:
                term.host.stop()


if __name__ == "__main__":
    unittest.main()
