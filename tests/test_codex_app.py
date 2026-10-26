"""Codex over `codex app-server` (vision/codex_app.py), against a scripted stand-in for the server."""
from __future__ import annotations

import io
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vision import subagents
from vision.codex import CodexBrain
from vision.config import BrainConfig

TID = "12345678-1234-1234-1234-123456789abc"


class _Stdin:
    def __init__(self, server):
        self.server = server

    def write(self, text):
        for line in text.splitlines():
            if line.strip():
                self.server.received(json.loads(line))

    def flush(self):
        pass

    def close(self):
        self.server.out.put("")


class _Stdout:
    def __init__(self, out):
        self.out = out

    def readline(self):
        try:
            return self.out.get(timeout=5)
        except queue.Empty:
            return ""


class FakeAppServer:
    """Answers each request with `handlers[method](params)` → (result or error dict, follow-up
    notifications). Notifications are (method, params); a callable in the list runs at that point."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.requests: list[dict] = []
        self.out: queue.Queue = queue.Queue()
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(self.out)
        self.stderr = io.StringIO("")
        self.returncode = None

    def emit(self, obj):
        self.out.put(json.dumps(obj) + "\n")

    def received(self, msg):
        self.requests.append(msg)
        if "id" not in msg:
            return
        handler = self.handlers.get(msg["method"])
        if handler is None:
            self.emit({"id": msg["id"], "error": {"message": f"no {msg['method']}"}})
            return
        answer, after = handler(msg.get("params") or {})
        self.emit({"id": msg["id"], **({"error": answer["error"]} if "error" in answer else {"result": answer})})
        for n in after:
            if callable(n):
                n()
            else:
                self.emit({"method": n[0], "params": n[1]})

    def sent(self, method):
        return [r for r in self.requests if r.get("method") == method]

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15
        self.out.put("")

    kill = terminate


def _setup(**extra):
    base = {
        "initialize": lambda p: ({"userAgent": "codex"}, []),
        "thread/start": lambda p: ({"thread": {"id": TID}}, []),
    }
    base.update(extra)
    return base


def _delta(text, item="m1"):
    return ("item/agentMessage/delta", {"threadId": TID, "turnId": "turn-1", "itemId": item, "delta": text})


DONE = ("turn/completed", {"threadId": TID, "turn": {"id": "turn-1", "status": "completed"}})


class AppServerTurnTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        for p in (patch("vision.codex.find_codex", return_value="codex"), patch.object(subagents, "TRACE_FILE", root / "trace.jsonl"),
                  patch("vision.codex.STATE_DIR", root), patch("vision.codex.USAGE_FILE", root / "usage.json"),
                  patch("vision.codex.LAST_SESSION_FILE", root / "last")):
            p.start()
            self.addCleanup(p.stop)

    def _ask(self, server, brain=None, **kw):
        brain = brain or CodexBrain(BrainConfig(model="gpt-5.5", effort="high"))
        with patch("subprocess.Popen", return_value=server):
            return brain, brain.ask("how many files?", **kw)

    def test_a_turn_streams_text_tools_and_usage(self):
        item = {"id": "c1", "type": "commandExecution", "command": "ls", "status": "inProgress"}
        server = FakeAppServer(_setup(**{"turn/start": lambda p: ({"turn": {"id": "turn-1", "status": "inProgress"}}, [
            ("item/started", {"item": {"id": "r1", "type": "reasoning"}}),
            ("item/started", {"item": item}),
            ("item/completed", {"item": {**item, "status": "completed", "aggregatedOutput": "a\nb\n", "exitCode": 0}}),
            _delta("There are two files, "), _delta("a and b."),
            ("item/completed", {"item": {"id": "m1", "type": "agentMessage", "text": "There are two files, a and b."}}),
            ("thread/tokenUsage/updated", {"threadId": TID, "turnId": "turn-1", "tokenUsage": {
                "total": {"inputTokens": 1200, "cachedInputTokens": 800, "outputTokens": 90, "totalTokens": 1290},
                "last": {"inputTokens": 1200, "outputTokens": 90}, "modelContextWindow": 400000}}),
            DONE])}))
        texts, statuses, tools = [], [], []
        brain, turn = self._ask(server, on_text=texts.append, on_status=statuses.append, on_tool=lambda c: tools.append((c.name, c.detail, c.done)))
        self.assertEqual((turn.text, turn.is_error, turn.session_id), ("There are two files, a and b.", False, TID))
        self.assertEqual("".join(texts), "There are two files, a and b.")
        self.assertIn("thinking", statuses)
        self.assertEqual(tools, [("Bash", "ls", False), ("Bash", "ls", True)])
        self.assertEqual(turn.tools[0].output, "a\nb\n")
        self.assertEqual(brain.context, (1200, 400000))
        self.assertEqual(brain.session_id, TID)
        self.assertEqual(turn.usage["input_tokens"], 1200)  # a fresh thread: the whole total is this turn
        start = server.sent("thread/start")[0]["params"]
        self.assertEqual((start["sandbox"], start["approvalPolicy"], start["model"], start["config"]["model_reasoning_effort"]),
                         ("danger-full-access", "never", "gpt-5.5", "high"))
        self.assertTrue(start["developerInstructions"])
        self.assertEqual(server.sent("turn/start")[0]["params"]["input"][0]["text"], "how many files?")

    def test_a_message_steered_into_the_running_turn(self):
        results = []
        go = threading.Event()

        def steer(p):
            go.set()
            return {"turnId": "turn-1"}, [_delta(" Right, cli.py.", "m2"), DONE]

        server = FakeAppServer(_setup(**{"turn/start": lambda p: ({"turn": {"id": "turn-1"}}, [_delta("Looking at brain.py now. It has the loop.")]),
                                         "turn/steer": steer}))
        brain = CodexBrain(BrainConfig())

        def first_text(d):
            if not results:
                results.append(None)
                t = threading.Thread(target=lambda: results.append(brain.steer("use cli.py instead")))
                t.start()

        _, turn = self._ask(server, brain, on_text=first_text)
        for _ in range(50):
            if len(results) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(results[1], True)
        steer_req = server.sent("turn/steer")[0]["params"]
        self.assertEqual((steer_req["threadId"], steer_req["expectedTurnId"], steer_req["input"][0]["text"]), (TID, "turn-1", "use cli.py instead"))
        self.assertIn("Right, cli.py.", turn.text)
        self.assertFalse(brain.steer("after the turn"))  # nothing running: the caller queues it

    def test_a_refused_steer_leaves_the_message_queued(self):
        from vision.codex_app import AppServerTurn

        server = FakeAppServer({"turn/steer": lambda p: ({"error": {"message": "ActiveTurnNotSteerable"}}, [])})
        with patch("subprocess.Popen", return_value=server):
            rpc = AppServerTurn("codex", ".", {})
        rpc.thread_id, rpc.turn_id = TID, "turn-1"
        pump = threading.Thread(target=lambda: rpc.pump(lambda m, p: None, lambda: False), daemon=True)
        pump.start()
        self.assertFalse(rpc.steer("now please"))

    def test_an_unknown_thread_starts_a_new_one(self):
        server = FakeAppServer(_setup(**{"thread/resume": lambda p: ({"error": {"message": "no rollout found"}}, []),
                                         "turn/start": lambda p: ({"turn": {"id": "turn-1"}}, [_delta("Hi."), DONE])}))
        brain = CodexBrain(BrainConfig(), session_id="87654321-4321-4321-4321-cba987654321")
        _, turn = self._ask(server, brain)
        self.assertEqual((turn.text, turn.is_error, brain.session_id), ("Hi.", False, TID))
        self.assertEqual(len(server.sent("thread/resume")), 1)

    def test_sub_agents_get_rows(self):
        spawn = {"id": "i3", "type": "collabAgentToolCall", "tool": "spawnAgent", "status": "inProgress", "senderThreadId": TID,
                 "receiverThreadIds": ["child-1"], "prompt": "Probe the TTS queue", "agentsStates": {}}
        server = FakeAppServer(_setup(**{"turn/start": lambda p: ({"turn": {"id": "turn-1"}}, [
            ("item/started", {"item": spawn}),
            ("item/completed", {"item": {**spawn, "status": "completed", "agentsStates": {"child-1": {"status": "running"}}}}),
            ("item/completed", {"item": {"id": "i4", "type": "collabAgentToolCall", "tool": "wait", "status": "completed", "receiverThreadIds": ["child-1"],
                                         "agentsStates": {"child-1": {"status": "completed", "message": "40 ms"}}}}),
            _delta("Done."), DONE])}))
        rows = []
        _, turn = self._ask(server, on_agent=lambda r: rows.append((r.label, r.done)))
        self.assertEqual([(r.label, r.done, r.failed, r.summary) for r in turn.agents], [("Probe the TTS queue", True, False, "40 ms")])
        self.assertEqual(rows[-1], ("Probe the TTS queue", True))

    def test_sub_agent_threads_stay_out_of_the_reply(self):
        # Child-thread items carry their own threadId and must stay out of the parent reply.
        child = "child-A"
        act = {"id": "call_1", "type": "subAgentActivity", "kind": "started", "agentThreadId": child, "agentPath": "/root/brain_count"}
        cmd = {"id": "exec-9", "type": "commandExecution", "command": "wc -l vision/brain.py", "status": "inProgress"}
        server = FakeAppServer(_setup(**{"turn/start": lambda p: ({"turn": {"id": "turn-1"}}, [
            ("item/completed", {"threadId": TID, "item": act}),
            ("item/started", {"threadId": child, "turnId": "t-child", "item": cmd}),
            ("item/completed", {"threadId": child, "turnId": "t-child", "item": {**cmd, "status": "completed", "aggregatedOutput": "1358"}}),
            ("item/agentMessage/delta", {"threadId": child, "turnId": "t-child", "itemId": "cm", "delta": "vision/brain.py: 1,358 lines."}),
            ("item/completed", {"threadId": child, "item": {"id": "cm", "type": "agentMessage", "text": "vision/brain.py: 1,358 lines."}}),
            ("thread/tokenUsage/updated", {"threadId": child, "tokenUsage": {"last": {"inputTokens": 5}, "modelContextWindow": 9}}),
            ("turn/completed", {"threadId": child, "turn": {"id": "t-child", "status": "completed"}}),
            ("item/completed", {"threadId": TID, "item": {**act, "id": "done-1", "kind": "completed"}}),
            _delta("Done."), DONE])}))
        brain, turn = self._ask(server)
        self.assertEqual((turn.text, turn.tools), ("Done.", []))
        self.assertEqual([(r.label, r.done, r.failed, r.tool_uses, r.summary) for r in turn.agents],
                         [("brain_count", True, False, 1, "vision/brain.py: 1,358 lines.")])
        self.assertNotEqual(getattr(brain, "context", None), (5, 9))

    def test_cancel_interrupts_the_turn(self):
        brain = CodexBrain(BrainConfig())

        def interrupt(p):
            return {}, [("turn/completed", {"threadId": TID, "turn": {"id": "turn-1", "status": "interrupted"}})]

        server = FakeAppServer(_setup(**{"turn/start": lambda p: ({"turn": {"id": "turn-1"}}, [_delta("Working on it. This takes a while.")]),
                                         "turn/interrupt": interrupt}))
        _, turn = self._ask(server, brain, on_text=lambda d: threading.Thread(target=brain.cancel).start())
        self.assertEqual((turn.is_error, turn.error), (True, "cancelled"))
        self.assertEqual(server.sent("turn/interrupt")[0]["params"], {"threadId": TID, "turnId": "turn-1"})


if __name__ == "__main__":
    unittest.main()
