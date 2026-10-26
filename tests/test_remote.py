"""A terminal joining a chat that `vision serve` runs (vision/remote.py): one conversation, read
from the phone and the terminal alike, with the server the only one driving the agent."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from unittest.mock import patch

from vision import link, remote, server
from tests.test_server import _StubBrain, _Turn


class _Live:
    """A real HTTP/WebSocket server on a free loopback port, so the sync client in remote.py can dial it."""

    def __init__(self, hub):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(server.create_app(hub), host="127.0.0.1", port=0, log_level="error", ws_ping_interval=None))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = 0

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("uvicorn never started")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return {"pid": os.getpid(), "url": f"http://127.0.0.1:{self.port}", "port": self.port, "token": "tok"}

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


class _Phone:
    """A phone: the other client of the same chat."""

    def __init__(self, info):
        from websockets.sync.client import connect

        self.ws = connect(info["url"].replace("http", "ws", 1) + "/ws", additional_headers={"Authorization": "Bearer tok"}, open_timeout=5, legacy=True)
        self.seen = []

    def send(self, frame):
        self.ws.send(json.dumps(frame))

    def until(self, pred, limit=80):
        for _ in range(limit):
            ev = json.loads(self.ws.recv(timeout=5))
            self.seen.append(ev)
            if pred(ev):
                return ev
        raise AssertionError(f"never saw it; got {[e['type'] for e in self.seen]}")

    def close(self):
        self.ws.close()


def _hub(brain):
    from vision.config import Config

    cfg = Config()
    cfg.conversation.model = "haiku"
    return server.Hub(cfg, brain, "tok", log=lambda *_: None)


def _quiet(d):
    return (patch.object(link, "LIVE_DIR", Path(d)),
            patch.object(server.Hub, "warm_up", lambda self: None),
            patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []),
            patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain))


class DescriptorTests(unittest.TestCase):
    def test_serve_descriptor_is_found_while_the_server_lives(self):
        with tempfile.TemporaryDirectory() as d, patch.object(remote, "SERVE_FILE", Path(d, "serve.json")), \
             patch("vision.server.TOKEN_FILE", Path(d, "remote_token")):
            Path(d, "remote_token").write_text("tok\n")
            self.assertIsNone(remote.running_server())
            remote.write_serve_descriptor("0.0.0.0", 8765)
            found = remote.running_server()
            self.assertEqual((found["pid"], found["url"], found["token"]), (os.getpid(), "http://127.0.0.1:8765", "tok"))
            remote.remove_serve_descriptor()
            self.assertIsNone(remote.running_server())
            # a dead server's descriptor is swept
            Path(d, "serve.json").write_text(json.dumps({"pid": 999999999, "url": "http://127.0.0.1:1"}))
            self.assertIsNone(remote.running_server())
            self.assertFalse(Path(d, "serve.json").exists())

    def test_a_descriptor_from_before_the_last_boot_is_swept_even_if_its_pid_is_taken(self):
        # After a reboot the old server's pid can belong to anything; here, this very process.
        with tempfile.TemporaryDirectory() as d, patch.object(remote, "SERVE_FILE", Path(d, "serve.json")), \
             patch("vision.server.TOKEN_FILE", Path(d, "remote_token")), \
             patch.object(remote, "boot_time", return_value=time.time() - 60):
            Path(d, "remote_token").write_text("tok\n")
            Path(d, "serve.json").write_text(json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:1", "started": time.time() - 3600}))
            self.assertIsNone(remote.running_server())
            self.assertFalse(Path(d, "serve.json").exists())
            remote.write_serve_descriptor("0.0.0.0", 8765)  # written since: found
            self.assertEqual(remote.running_server()["pid"], os.getpid())

    @unittest.skipUnless(Path("/proc/self/status").exists(), "needs /proc")
    def test_a_thread_id_is_not_a_live_process(self):
        from vision.link import pid_alive

        ready, done = threading.Event(), threading.Event()
        tid = []
        worker = threading.Thread(target=lambda: (tid.append(threading.get_native_id()), ready.set(), done.wait()))
        worker.start()
        try:
            ready.wait()
            self.assertTrue(pid_alive(os.getpid()))
            self.assertFalse(pid_alive(tid[0]))
        finally:
            done.set()
            worker.join()
        self.assertFalse(pid_alive(0))

    def test_picker_rows_and_keys(self):
        chats = [{"chat": "abc123", "title": "fix the tests", "model": "Opus 5", "updated": time.time() - 120, "source": "server"}]
        rows = remote.remote_rows(chats)
        self.assertEqual(rows[0][:2], ("remote:abc123", "fix the tests"))
        self.assertIn("2 min ago", rows[0][2])
        self.assertIs(remote.remote_from_key("remote:abc123", chats), chats[0])
        self.assertIs(remote.remote_from_key("abc", chats), chats[0])
        self.assertIsNone(remote.remote_from_key("zzz", chats))
        self.assertTrue(remote.is_remote_key("remote:x") and not remote.is_remote_key("claude:x"))


class JoinTests(unittest.TestCase):
    def test_terminal_and_phone_read_one_conversation(self):
        from vision.config import BrainConfig

        brain = _StubBrain(BrainConfig(), delay=0.1)
        hub = _hub(brain)
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    foreign, notes, deltas = [], [], []
                    cfg = BrainConfig()
                    rb = remote.RemoteBrain(info, remote.list_remote_chats(info)[0], cfg,
                                            on_foreign_turn=foreign.append, on_note=lambda t, k: notes.append(t))
                    rb.connect()
                    self.assertEqual(rb.chat_id, chat_id)
                    self.assertEqual(rb.provider, "claude")

                    # typed in the terminal: the server runs it, the phone watches it stream
                    turn = rb.ask("hi", on_text=deltas.append)
                    self.assertEqual((turn.text, turn.is_error), ("echo: hi", False))
                    self.assertEqual(deltas, ["echo: hi"])
                    self.assertEqual(turn.session_id, brain.session_id)
                    phone.until(lambda ev: ev["type"] == "start" and ev["text"] == "hi")
                    phone.until(lambda ev: ev["type"] == "done" and ev["text"] == "echo: hi")

                    # typed on the phone: announced to the terminal, which follows without re-sending
                    phone.send({"type": "message", "chat": chat_id, "text": "yo"})
                    for _ in range(50):
                        if foreign:
                            break
                        time.sleep(0.05)
                    self.assertEqual(foreign, ["yo"])
                    self.assertEqual(rb.pending_turn(), "yo")
                    deltas.clear()
                    turn = rb.ask("yo", on_text=deltas.append)
                    self.assertEqual((turn.text, deltas), ("echo: yo", ["echo: yo"]))
                    self.assertEqual(brain.calls, 2)  # one run per message, not one per client
                    phone.until(lambda ev: ev["type"] == "done" and ev["text"] == "echo: yo")

                    # the phone closes the chat: the terminal is told and its next ask fails cleanly
                    closed = []
                    rb.on_closed = closed.append
                    phone.send({"type": "close", "chat": chat_id})
                    for _ in range(50):
                        if closed:
                            break
                        time.sleep(0.05)
                    self.assertEqual(closed, ["closed on the phone"])
                    turn = rb.ask("anyone?")
                    self.assertTrue(turn.is_error)
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_joining_mid_reply_follows_it_from_the_start(self):
        from vision.config import BrainConfig

        class Slow(_StubBrain):
            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                self.calls += 1
                self.session_id = "s1"
                on_text("first half, ")
                time.sleep(0.6)
                on_text("second half")
                return _Turn("first half, second half", "s1")

        hub = _hub(Slow(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "long one"})
                    phone.until(lambda ev: ev["type"] == "delta")
                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig())
                    rb.connect()
                    self.assertEqual(rb.pending_turn(), "long one")
                    deltas = []
                    turn = rb.ask("long one", on_text=deltas.append)
                    self.assertEqual("".join(deltas), "first half, second half")
                    self.assertEqual(turn.text, "first half, second half")
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_joining_mid_reply_brings_the_tool_calls_already_run(self):
        from vision.brain import ToolCall
        from vision.config import BrainConfig

        go_on = threading.Event()

        class Tools(_StubBrain):
            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                self.session_id = "s1"
                on_text("Looking. ")
                call = ToolCall("t1", "Bash", "ls")
                on_tool(call)
                call.done, call.output = True, "a.py"
                on_tool(call)
                on_text("Found it.")
                go_on.wait(5)
                return _Turn("Looking. Found it.", "s1")

        hub = _hub(Tools(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d) + (patch("vision.agentlog.AGENTS_DIR", Path(d) / "agents"),)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "find it"})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "Found it.")
                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig())
                    rb.connect()
                    order = []
                    result = {}
                    t = threading.Thread(target=lambda: result.update(turn=rb.ask(
                        "find it", on_text=lambda d: order.append(("text", d)),
                        on_tool=lambda c: order.append(("tool", c.id, c.done)))))
                    t.start()
                    for _ in range(50):
                        if len(order) >= 3:
                            break
                        time.sleep(0.05)
                    go_on.set()
                    t.join(5)
                    self.assertEqual(order[:3], [("text", "Looking. "), ("tool", "t1", True), ("text", "Found it.")])
                    self.assertEqual(result["turn"].tools[0].output, "a.py")
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_agents_keep_their_timers_across_a_join_and_a_steered_message_splits_the_reply(self):
        from vision.brain import AgentRun
        from vision.config import BrainConfig

        steered, go_on = [], threading.Event()

        class Agents(_StubBrain):
            def steer(self, text):
                steered.append(text)
                go_on.set()
                return True

            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                self.session_id = "s1"
                on_text("Sending one off.")
                run = AgentRun("a1", "Explore", "Map the tests", started=time.monotonic() - 90)
                on_agent(run)
                go_on.wait(5)
                on_text("Right, cli.py then.")
                run.done, run.duration_ms = True, 95_000
                on_agent(run)
                return _Turn("Sending one off.Right, cli.py then.", "s1")

        hub = _hub(Agents(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d) + (patch("vision.agentlog.AGENTS_DIR", Path(d) / "agents"),)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "map the tests"})
                    phone.until(lambda ev: ev["type"] == "agent")
                    # A terminal joins mid-reply: the row comes with the reply so far, timed from its real start.
                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig())
                    rb.connect()
                    rows = []
                    result = {}
                    t = threading.Thread(target=lambda: result.update(turn=rb.ask("map the tests", on_agent=lambda r: rows.append(r))))
                    t.start()
                    for _ in range(50):
                        if rows:
                            break
                        time.sleep(0.05)
                    self.assertAlmostEqual(time.monotonic() - rows[0].started, 90, delta=3)
                    # Send now from the phone: into the running turn, and everyone is told where it went in.
                    phone.send({"type": "message", "chat": chat_id, "text": "use cli.py", "now": True})
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "use cli.py")
                    phone.until(lambda ev: ev["type"] == "done")
                    t.join(5)
                    self.assertEqual(steered, ["use cli.py"])
                    history = hub.chats[chat_id].history()
                    self.assertEqual([(m["role"], m["text"]) for m in history[-4:]], [
                        ("user", "map the tests"), ("assistant", "Sending one off."), ("user", "use cli.py"), ("assistant", "Right, cli.py then.")])
                    self.assertEqual([a["id"] for a in history[-3]["agents"]], ["a1"])  # the row stays with the part it started in
                    self.assertTrue(history[-3]["agents"][0]["done"])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_phone_send_now_splits_a_queued_message_and_runs_the_remaining_queue(self):
        from vision.config import BrainConfig

        release = threading.Event()
        steered = []
        calls = []

        class Streaming(_StubBrain):
            def steer(self, text):
                steered.append(text)
                release.set()
                return True

            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                calls.append(text)
                self.session_id = "s1"
                if text == "first":
                    on_text("Before.")
                    if not release.wait(5):
                        raise AssertionError("Send now did not reach the running turn")
                    on_text(" After.")
                    return _Turn("Before. After.", "s1")
                on_text("Next reply.")
                return _Turn("Next reply.", "s1")

        hub = _hub(Streaming(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "first"})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "Before.")
                    phone.send({"type": "message", "chat": chat_id, "text": "steer this"})
                    phone.send({"type": "message", "chat": chat_id, "text": "next"})
                    phone.send({"type": "steer", "chat": chat_id, "text": "steer this"})
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "steer this")
                    phone.until(lambda ev: ev["type"] == "done")
                    phone.until(lambda ev: ev["type"] == "start" and ev["text"] == "next")
                    phone.until(lambda ev: ev["type"] == "done")
                    self.assertEqual(steered, ["steer this"])
                    self.assertEqual(calls, ["first", "next"])
                    self.assertEqual([(m["role"], m["text"]) for m in hub.chats[chat_id].history()[-6:]], [
                        ("user", "first"), ("assistant", "Before."), ("user", "steer this"),
                        ("assistant", "After."), ("user", "next"), ("assistant", "Next reply.")])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def _claude_like(self, take: bool):
        """A brain that, like Claude, takes a steered message at its next step and says so then."""
        from vision.config import BrainConfig

        sent, calls = threading.Event(), []

        class Claude(_StubBrain):
            on_steer_taken = None

            def __init__(self, cfg):
                super().__init__(cfg)
                self.waiting = []

            def steer(self, text):
                self.waiting.append(text)
                sent.set()
                return True

            def take_untaken_steers(self):
                left, self.waiting = self.waiting, []
                return left

            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                calls.append(text)
                self.session_id = "s1"
                if text == "first":
                    on_text("Before.")
                    if not sent.wait(5):
                        raise AssertionError("Send now did not reach the running turn")
                    time.sleep(0.2)  # the phone gets the summary listing it as waiting
                    on_text(" Still on it.")
                    if take:
                        self.on_steer_taken(self.waiting.pop(0))
                        on_text(" After.")
                    return _Turn("Before. Still on it." + (" After." if take else ""), "s1")
                on_text("Next reply.")
                return _Turn("Next reply.", "s1")

        return _hub(Claude(BrainConfig())), calls

    def test_a_steered_message_splits_the_reply_when_claude_takes_it(self):
        hub, calls = self._claude_like(take=True)
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "first"})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "Before.")
                    phone.send({"type": "message", "chat": chat_id, "text": "steer this", "now": True})
                    phone.until(lambda ev: ev["type"] == "chat" and ev.get("queued") == ["steer this"])  # still waiting
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == " Still on it.")
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "steer this")
                    phone.until(lambda ev: ev["type"] == "done")
                    self.assertEqual(calls, ["first"])
                    self.assertEqual([(m["role"], m["text"]) for m in hub.chats[chat_id].history()[-4:]], [
                        ("user", "first"), ("assistant", "Before. Still on it."), ("user", "steer this"), ("assistant", "After.")])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_a_message_typed_mid_reply_goes_into_claudes_queue_without_send_now(self):
        hub, calls = self._claude_like(take=True)
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "first"})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "Before.")
                    phone.send({"type": "message", "chat": chat_id, "text": "steer this"})
                    phone.until(lambda ev: ev["type"] == "chat" and ev.get("queued") == ["steer this"])  # still waiting
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == " Still on it.")
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "steer this")
                    phone.until(lambda ev: ev["type"] == "done")
                    self.assertEqual(calls, ["first"])
                    self.assertEqual([(m["role"], m["text"]) for m in hub.chats[chat_id].history()[-4:]], [
                        ("user", "first"), ("assistant", "Before. Still on it."), ("user", "steer this"), ("assistant", "After.")])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_a_steered_message_claude_never_took_runs_next(self):
        hub, calls = self._claude_like(take=False)
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "first"})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "Before.")
                    phone.send({"type": "message", "chat": chat_id, "text": "steer this", "now": True})
                    phone.until(lambda ev: ev["type"] == "start" and ev["text"] == "steer this")
                    phone.until(lambda ev: ev["type"] == "done")
                    self.assertEqual(calls, ["first", "steer this"])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_talking_on_a_call_goes_into_the_running_call_turn_instead_of_queuing(self):
        """An agent launched on a call keeps the call's turn open: what's said next is answered in it
        now, not queued behind the agent. Typed off the call it still queues."""
        from vision.config import BrainConfig

        heard, release = [], threading.Event()

        class Call:  # the conversation model, waiting on an agent it launched
            def steer(self, text):
                heard.append(text)
                if text == "and the other thing":
                    release.set()
                return True

            def cancel(self):
                pass

            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                on_text("On it.")
                if not release.wait(5):
                    raise AssertionError("what was said never reached the call's turn")
                on_text(" Done.")
                return _Turn("On it. Done.", "s1")

        hub = _hub(_StubBrain(BrainConfig(), delay=0))
        hub.cfg.voice.enabled = False  # the spoken replies here must never load a real voice (with the GPU free, they would)
        chat_id = next(iter(hub.chats))
        hub.chats[chat_id].conversation = Call()
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)[:3] + (patch("vision.cli._turn_brain",
                                             lambda brain, conv, voice, text="", talk=False, **kw: conv if voice or talk else brain),)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "launch an agent", "voice": True, "speak": True})
                    phone.until(lambda ev: ev["type"] == "delta" and ev["text"] == "On it.")
                    phone.send({"type": "message", "chat": chat_id, "text": "typed aside"})  # not on the call: queues
                    time.sleep(0.5)  # the agent works a while before anything is said
                    phone.send({"type": "message", "chat": chat_id, "text": "how's it going", "voice": True, "speak": True})
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "how's it going")
                    phone.send({"type": "message", "chat": chat_id, "text": "and the other thing", "talk": True})
                    phone.until(lambda ev: ev["type"] == "steered" and ev["text"] == "and the other thing")
                    done = phone.until(lambda ev: ev["type"] == "done")
                    # Each part of the reply times itself: the reply to what was said last doesn't
                    # carry the clock that has run since the agent went off.
                    # Only the call's turn: the queued "typed aside" starts straight after and its reply can land first.
                    rows = hub.chats[chat_id].history()
                    rows = rows[:next((i for i, m in enumerate(rows) if m["role"] == "user" and m.get("text") == "typed aside"), len(rows))]
                    timed = [m.get("duration_ms") for m in rows if m["role"] == "assistant"]
                    self.assertEqual(len(timed), 3)
                    self.assertGreaterEqual(timed[0], 450)
                    self.assertLess(timed[2], 450)
                    self.assertEqual(done["duration_ms"], timed[2])
                    phone.until(lambda ev: ev["type"] == "start" and ev["text"] == "typed aside")
                    phone.until(lambda ev: ev["type"] == "done")
                    self.assertEqual(heard, ["how's it going", "and the other thing"])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_a_question_answered_on_the_phone_closes_the_terminal_form(self):
        from vision.config import BrainConfig

        got = {}

        class Asks(_StubBrain):
            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                self.session_id = "s1"
                got["answers"] = on_question([{"question": "Which?", "header": "Pick", "options": [{"label": "A"}, {"label": "B"}]}])
                on_text("went with " + (got["answers"] or {}).get("Which?", "nothing"))
                return _Turn("went with " + (got["answers"] or {}).get("Which?", "nothing"), "s1")

        hub = _hub(Asks(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    dismissed = threading.Event()
                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig(), on_answered=dismissed.set)
                    rb.connect()

                    def form(questions):  # the terminal's form: sits open until the phone answers
                        self.assertEqual(questions[0]["question"], "Which?")
                        dismissed.wait(5)
                        return None

                    result = {}
                    t = threading.Thread(target=lambda: result.update(turn=rb.ask("choose", on_question=form)))
                    t.start()
                    phone.until(lambda ev: ev["type"] == "question")
                    phone.send({"type": "answer", "chat": chat_id, "answers": {"Which?": "B"}})
                    t.join(5)
                    self.assertTrue(dismissed.is_set())
                    self.assertEqual(got["answers"], {"Which?": "B"})
                    self.assertEqual(result["turn"].text, "went with B")
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_joining_during_a_question_restores_it_on_phone_and_terminal(self):
        from vision.config import BrainConfig

        questions = [{"question": "Which?", "header": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]
        got = {}

        class Asks(_StubBrain):
            def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
                self.session_id = "s1"
                got["answers"] = on_question(questions)
                return _Turn("done", "s1")

        hub = _hub(Asks(BrainConfig()))
        chat_id = next(iter(hub.chats))
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d)
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    phone.send({"type": "message", "chat": chat_id, "text": "choose"})
                    phone.until(lambda ev: ev["type"] == "question")

                    late_phone = _Phone(info)
                    snapshot = late_phone.until(lambda ev: ev["type"] == "hello")["chats"][0]
                    self.assertTrue(snapshot["waiting"])
                    self.assertEqual(snapshot["questions"], questions)

                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig())
                    rb.connect()
                    self.assertEqual(rb.pending_turn(), "choose")
                    opened = threading.Event()
                    dismissed = threading.Event()
                    rb.on_answered = dismissed.set

                    def form(qs):
                        self.assertEqual(qs, questions)
                        opened.set()
                        dismissed.wait(5)

                    result = {}
                    turn_thread = threading.Thread(target=lambda: result.update(turn=rb.ask("choose", on_question=form)))
                    turn_thread.start()
                    self.assertTrue(opened.wait(5))
                    late_phone.send({"type": "answer", "chat": chat_id, "answers": {"Which?": "B"}})
                    turn_thread.join(5)
                    self.assertFalse(turn_thread.is_alive())
                    self.assertEqual(got["answers"], {"Which?": "B"})
                    self.assertEqual(result["turn"].text, "done")
                    self.assertTrue(dismissed.is_set())
                    self.assertEqual(hub.chats[chat_id].summary()["questions"], [])
                    rb.close()
                    late_phone.close()
                    phone.close()
            finally:
                for p in patches:
                    p.stop()

    def test_open_on_machine_opens_one_window_per_chat(self):
        from vision.config import BrainConfig

        hub = _hub(_StubBrain(BrainConfig()))
        chat_id = next(iter(hub.chats))
        opened, focused = [], []
        with tempfile.TemporaryDirectory() as d:
            patches = _quiet(d) + (
                patch.object(server, "open_terminal", lambda cid, workdir: opened.append(cid) or "kitty"),
                patch.object(server, "focus_terminal", lambda pid: focused.append(pid) or True),
            )
            for p in patches:
                p.start()
            try:
                with _Live(hub) as info:
                    phone = _Phone(info)
                    phone.until(lambda ev: ev["type"] == "hello")
                    toast = lambda: phone.until(lambda ev: ev["type"] == "toast")["text"]

                    # a second tap while the first window is still starting up opens nothing
                    phone.send({"type": "open_terminal", "chat": chat_id})
                    self.assertEqual(toast(), "Opened in a terminal on the computer.")
                    phone.send({"type": "open_terminal", "chat": chat_id})
                    self.assertEqual(toast(), "That chat is already opening on the computer.")
                    self.assertEqual(opened, [chat_id])

                    # the window dials in: a tap brings it forward
                    rb = remote.RemoteBrain(info, {"chat": chat_id}, BrainConfig())
                    rb.connect()
                    self.assertEqual(list(hub.mirrors[chat_id].values()), [os.getpid()])
                    phone.send({"type": "open_terminal", "chat": chat_id})
                    self.assertEqual(toast(), "Already open on the computer; brought it to the front.")
                    self.assertEqual((opened, focused), ([chat_id], [os.getpid()]))

                    # it closes: the next tap opens a window again
                    rb.close()
                    for _ in range(50):
                        if chat_id not in hub.mirrors:
                            break
                        time.sleep(0.05)
                    self.assertNotIn(chat_id, hub.mirrors)
                    phone.send({"type": "open_terminal", "chat": chat_id})
                    self.assertEqual(toast(), "Opened in a terminal on the computer.")
                    self.assertEqual(opened, [chat_id, chat_id])
                    phone.close()
            finally:
                for p in patches:
                    p.stop()



class LiveSessionTests(unittest.TestCase):
    """A phone chat's session is on disk too: its provider-tab row is tagged and keyed to the chat."""

    CHATS = [
        {"chat": "c1", "provider": "claude", "session_id": "abc"},
        {"chat": "c2", "provider": "codex", "session_id": ""},  # no first message yet
    ]

    def test_live_sessions_keys_like_session_key(self):
        self.assertEqual(remote.live_sessions(self.CHATS), {"claude:abc": self.CHATS[0]})

    def test_mark_live_rows_tags_only_the_open_session(self):
        tabs = [
            ("Claude", [("claude:abc", "Weather", "2m · abc"), ("claude:xyz", "Other", "1d · xyz")], ""),
            ("Codex", [("codex:abc", "Same id, other provider", "3d · abc")], ""),
            ("Grok", [], "no conversations"),
        ]
        marked = remote.mark_live_rows(tabs, self.CHATS)
        self.assertEqual(marked[0][1][0], ("claude:abc", "Weather", "2m · abc · live on phone"))
        self.assertEqual(marked[0][1][1], tabs[0][1][1])
        self.assertEqual(marked[1], tabs[1])
        self.assertEqual(marked[2], tabs[2])


if __name__ == "__main__":
    unittest.main()


class QueueEditTests(unittest.TestCase):
    def test_a_queued_message_taken_back_never_runs(self):
        from collections import deque

        from vision.config import BrainConfig

        hub = _hub(_StubBrain(BrainConfig()))
        chat = next(iter(hub.chats.values()))
        chat._pending = deque([("first", False, False, False), ("second", False, False, False)])
        self.assertTrue(chat.unqueue("second"))
        self.assertFalse(chat.unqueue("second"))
        self.assertEqual([p[0] for p in chat._pending], ["first"])
