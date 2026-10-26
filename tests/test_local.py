"""The Local brain against a stub llama-server: streaming, thinking switch, grammar mode, voice packets."""
from __future__ import annotations

import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vision.brain import create_brain
from vision.config import Config
from vision import localtools
from vision.local import LocalConversation, _trim
from vision.models import provider_for


class StubServer(http.server.BaseHTTPRequestHandler):
    """Answers every chat completion with `reply` (or `voice_reply` in JSON-schema mode), as SSE."""

    requests: list[dict] = []
    reply = "Sorted."
    voice_reply = json.dumps({"speech": "Hello there, Sam.", "task": None})
    # Scripted replies consumed one per request, before the defaults above: a string is text, a list is a
    # batch of tool calls [(name, args), ...] streamed as indexed deltas the way llama-server sends them.
    script: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        type(self).post_headers.append(dict(self.headers))
        scripted = type(self).script.pop(0) if type(self).script else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if body["chat_template_kwargs"]["enable_thinking"]:
            self.wfile.write(b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n')
        finish = "stop"
        if isinstance(scripted, list):
            finish = "tool_calls"
            self._sse({"choices": [{"delta": {"content": "Let me look."}}]})  # narration before the call
            for i, (name, args) in enumerate(scripted):
                arguments = json.dumps(args)
                self._sse({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call{i}", "type": "function",
                                                                  "function": {"name": name, "arguments": arguments[:4]}}]}}]})
                self._sse({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": arguments[4:]}}]}}]})
        else:
            text = scripted if scripted is not None else (self.voice_reply if "response_format" in body else self.reply)
            for i in range(0, len(text), 5):
                self._sse({"choices": [{"delta": {"content": text[i:i + 5]}}]})
        self._sse({"choices": [{"delta": {}, "finish_reason": finish}], "usage": {"completion_tokens": 3, "prompt_tokens": 10, "total_tokens": 13},
                   "timings": {"predicted_per_second": 40.0, "prompt_ms": 120.0}})
        self.wfile.write(b"data: [DONE]\n\n")

    def _sse(self, obj: dict) -> None:
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())

    headers_seen: list[dict] = []
    post_headers: list[dict] = []

    def do_GET(self):
        type(self).headers_seen.append(dict(self.headers))
        self.send_response(200)
        self.end_headers()
        if self.path.endswith("/models"):
            self.wfile.write(b'{"data":[{"id":"qwen3"},{"id":"llama4"}]}')
        else:
            self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *a):
        pass


class LocalBrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubServer)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        StubServer.requests = []
        StubServer.script = []
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        self._patches = [patch("vision.local.SESSIONS_DIR", state / "sessions"), patch("vision.local.LAST_SESSION_FILE", state / "last")]
        for p in self._patches:
            p.start()
        self.cfg = Config()
        self.cfg.brain.model, self.cfg.brain.effort = "local-model", "off"
        self.cfg.local.base_url = self.base
        self.cfg.conversation.model = "local-model"
        self.cfg.brain.workdir = self.tmp.name

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_catalogue_routes_to_local(self):
        self.assertEqual(provider_for("local-model"), "local")
        self.assertEqual(type(create_brain(self.cfg.brain)).__name__, "LocalBrain")

    def test_streams_text_and_keeps_the_transcript(self):
        brain = create_brain(self.cfg.brain)
        pieces = []
        turn = brain.ask("hi", on_text=pieces.append)
        self.assertFalse(turn.is_error)
        self.assertEqual("".join(pieces), "Sorted.")
        self.assertEqual(turn.text, "Sorted.")
        self.assertEqual(turn.usage["completion_tokens"], 3)
        self.assertFalse(StubServer.requests[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(StubServer.requests[0]["messages"][0]["role"], "system")
        brain.ask("again")
        roles = [m["role"] for m in StubServer.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        # the session is on disk and resumes into a fresh brain
        resumed = create_brain(self.cfg.brain, session_id=turn.session_id)
        self.assertEqual(len(resumed._messages), 4)
        self.assertEqual(type(brain).last_session_id(), turn.session_id)

    def test_high_effort_turns_thinking_on_and_reports_it(self):
        self.cfg.brain.effort = "high"
        statuses = []
        create_brain(self.cfg.brain).ask("hi", on_status=statuses.append)
        self.assertTrue(StubServer.requests[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("thinking", statuses)

    def test_task_mode_ends_with_the_result_grammar(self):
        # A worker turn: tools on and no grammar first (they cannot share a request); once the model stops
        # calling tools and its text is not a result object, one more call asks for it under the grammar.
        StubServer.voice_reply = json.dumps({"status": "completed", "summary": "done", "changes": [], "checks": [], "findings": [], "question": None})
        brain = create_brain(self.cfg.brain)
        brain.task_mode = True
        turn = brain.ask("do the thing")
        self.assertEqual(turn.data["status"], "completed")
        first, last = StubServer.requests[0], StubServer.requests[-1]
        self.assertIn("tools", first)
        self.assertNotIn("response_format", first)
        self.assertNotIn("tools", last)
        self.assertEqual(last["response_format"]["type"], "json_schema")
        self.assertFalse(last["chat_template_kwargs"]["enable_thinking"])
        self.assertIn("silent task worker", first["messages"][0]["content"])

    def test_task_mode_takes_a_valid_result_without_a_second_call(self):
        result = {"status": "completed", "summary": "done", "changes": ["a.txt"], "checks": [], "findings": [], "question": None}
        StubServer.script = [[("Read", {"file_path": "a.txt"})], "```json\n" + json.dumps(result) + "\n```"]
        Path(self.tmp.name, "a.txt").write_text("x")
        brain = create_brain(self.cfg.brain)
        brain.task_mode = True
        turn = brain.ask("do the thing")
        self.assertEqual(turn.data, result)
        self.assertEqual(len(StubServer.requests), 2)  # the tool round and the answer; no grammar call needed
        self.assertEqual(turn.tools_used, ["Read"])
        self.assertEqual(StubServer.requests[0]["tool_choice"], "required")  # a worker's first move is a real look
        self.assertNotIn("tool_choice", StubServer.requests[1])
        self.assertIn("Work in two steps", StubServer.requests[0]["messages"][0]["content"])

    def test_typed_turns_never_force_a_tool_call(self):
        create_brain(self.cfg.brain).ask("hi")
        self.assertNotIn("tool_choice", StubServer.requests[0])

    def test_runs_tool_calls_and_feeds_the_results_back(self):
        Path(self.tmp.name, "note.txt").write_text("hello from the stub\n")
        StubServer.script = [[("Bash", {"command": "cat note.txt", "description": "show the note"})], "It says hello."]
        brain = create_brain(self.cfg.brain)
        rows, statuses, pieces = [], [], []
        turn = brain.ask("what does note.txt say?", on_text=pieces.append, on_status=statuses.append,
                         on_tool=lambda c: rows.append((c.name, c.detail, c.done, c.is_error, c.output)))
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(turn.text, "It says hello.")
        self.assertEqual("".join(pieces), "It says hello.")  # the narration before the call was held and dropped
        self.assertEqual(turn.tools_used, ["Bash"])
        self.assertEqual(rows[0], ("Bash", "", False, False, ""))
        self.assertEqual(rows[-1], ("Bash", "show the note", True, False, "hello from the stub"))
        self.assertEqual(statuses, ["Bash", "reading"])
        first, second = StubServer.requests
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"])
        roles = [m["role"] for m in second["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"][:-1])
        self.assertEqual(second["messages"][2]["tool_calls"][0]["function"]["name"], "Bash")
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "call0", "content": "hello from the stub"})
        # the transcript keeps the tool exchange and resumes with it
        self.assertEqual([m["role"] for m in brain._messages], ["user", "assistant", "tool", "assistant"])
        self.assertIn("Vision refuses them", first["messages"][0]["content"])

    def test_a_steered_message_joins_after_the_tool_results(self):
        StubServer.script = [[("Bash", {"command": "sleep 1"})], "Done, and noted."]
        brain = create_brain(self.cfg.brain)
        self.assertFalse(brain.steer("too early"))  # no turn running: the caller queues it
        steered = []
        threading.Timer(0.4, lambda: steered.append(brain.steer("use the other folder"))).start()
        turn = brain.ask("do the thing")
        self.assertEqual(steered, [True])
        self.assertEqual(turn.text, "Done, and noted.")
        roles = [m["role"] for m in StubServer.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(StubServer.requests[1]["messages"][-1]["content"], "use the other folder")
        self.assertFalse(brain.steer("after"))  # the turn is over

    def test_a_message_steered_while_the_reply_streams_is_answered_in_the_same_turn(self):
        StubServer.script = ["First answer. It keeps going for a bit.", "Second answer."]
        brain = create_brain(self.cfg.brain)
        pieces = []
        brain.on_steer_taken = lambda t: pieces.append(("taken", t))

        def on_text(chunk):
            if not pieces:
                self.assertTrue(brain.steer("and another thing"))
            pieces.append(chunk)

        turn = brain.ask("hi", on_text=on_text)
        # Told when the model takes it in: after the first answer has streamed, before the second.
        at = pieces.index(("taken", "and another thing"))
        self.assertEqual("".join(p for p in pieces[:at] if isinstance(p, str)).strip(), "First answer. It keeps going for a bit.")
        self.assertEqual(brain.take_untaken_steers(), [])
        self.assertEqual(len(StubServer.requests), 2)
        self.assertEqual([m["role"] for m in StubServer.requests[1]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(turn.text, "First answer. It keeps going for a bit.\n\nSecond answer.")
        self.assertEqual([m["role"] for m in brain._messages], ["user", "assistant", "user", "assistant"])

    def test_denied_command_comes_back_as_an_error_for_the_model(self):
        StubServer.script = [[("Bash", {"command": "sudo systemctl restart foo"})], "I can't run sudo; run it yourself."]
        brain = create_brain(self.cfg.brain)
        rows = []
        turn = brain.ask("restart foo", on_tool=lambda c: rows.append((c.done, c.is_error, c.output)))
        self.assertFalse(turn.is_error)
        self.assertTrue(rows[-1][1])
        self.assertIn("Bash(sudo:*)", rows[-1][2])
        self.assertIn("forbidden", StubServer.requests[1]["messages"][3]["content"])

    def test_plan_mode_offers_read_only(self):
        self.cfg.brain.mode = "plan"
        StubServer.script = [[("Bash", {"command": "ls"})], "Here is the plan."]
        brain = create_brain(self.cfg.brain)
        turn = brain.ask("plan something")
        self.assertEqual([t["function"]["name"] for t in StubServer.requests[0]["tools"]], ["Read", "WebSearch", "WebFetch"])
        self.assertIn("not available", StubServer.requests[1]["messages"][3]["content"])  # the model tried Bash anyway
        self.assertEqual(turn.text, "Here is the plan.")

    def test_no_tools_configured_means_the_old_tool_free_turn(self):
        self.cfg.brain.allowed_tools = []
        brain = create_brain(self.cfg.brain)
        brain.ask("hi")
        self.assertNotIn("tools", StubServer.requests[0])
        self.assertIn("You have no tools in this session", StubServer.requests[0]["messages"][0]["content"])

    def test_tool_rounds_are_capped(self):
        from vision.local import MAX_TOOL_ROUNDS

        StubServer.script = [[("Bash", {"command": "true"})]] * (MAX_TOOL_ROUNDS + 3)
        turn = create_brain(self.cfg.brain).ask("loop forever")
        self.assertEqual(len(turn.tools_used), MAX_TOOL_ROUNDS)
        self.assertEqual(len(StubServer.requests), MAX_TOOL_ROUNDS + 1)
        self.assertNotIn("tools", StubServer.requests[-1])  # the last request makes the model answer
        self.assertEqual(turn.text, "Let me look.")  # what it said is the reply; a call it made anyway is ignored

    def test_cancel_kills_a_running_command(self):
        StubServer.script = [[("Bash", {"command": "sleep 30"})], "never"]
        brain = create_brain(self.cfg.brain)
        started = time.monotonic()
        threading.Timer(0.5, brain.cancel).start()
        turn = brain.ask("wait")
        self.assertTrue(turn.is_error)
        self.assertEqual(turn.error, "cancelled")
        self.assertLess(time.monotonic() - started, 10)

    def test_unreachable_server_is_a_turn_error_not_a_crash(self):
        self.cfg.local.base_url = "http://127.0.0.1:9/v1"
        turn = create_brain(self.cfg.brain).ask("hi")
        self.assertTrue(turn.is_error)
        self.assertIn("unreachable", turn.error)

    def test_voice_conversation_streams_speech_from_the_json(self):
        StubServer.voice_reply = json.dumps({"speech": "Hello there, Sam.", "task": None})
        conv = LocalConversation(self.cfg)
        conv.warm_up()
        spoken = []
        packet = {"history": [], "turn": {"id": "1", "user": "hi", "events": []}, "coding_context": "", "memory": "", "worker_mode": "auto"}
        response = conv.complete(packet, threading.Event(), on_speech=spoken.append)
        self.assertEqual(response["speech"], "Hello there, Sam.")
        self.assertEqual("".join(spoken), "Hello there, Sam.")
        self.assertEqual(StubServer.requests[0]["response_format"]["json_schema"]["name"], "response")
        self.assertFalse(StubServer.requests[0]["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("tools", StubServer.requests[0])  # the front end has no tools: no shell, files or browser
        self.assertNotIn("tool_choice", StubServer.requests[0])
        self.assertIn("search", StubServer.requests[0]["response_format"]["json_schema"]["schema"]["properties"])
        # the next call is incremental: the earlier exchange is in the transcript, the packet is the delta
        conv.complete({**packet, "turn": {"id": "2", "user": "again", "events": []}}, threading.Event())
        roles = [m["role"] for m in StubServer.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertNotIn("history", json.loads(StubServer.requests[1]["messages"][-1]["content"]))


class TrimTests(unittest.TestCase):
    def test_drops_oldest_exchanges_and_keeps_the_system_prompt(self):
        msgs = [{"role": "system", "content": "S" * 10}] + [{"role": r, "content": "x" * 100} for _ in range(5) for r in ("user", "assistant")]
        out = _trim(msgs, 350)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(len(out), 3)  # system + the newest exchange
        self.assertEqual(_trim(msgs, 10_000), msgs)

    def test_an_exchange_carries_its_tool_calls_and_results(self):
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "a" * 50}, {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
                {"role": "tool", "tool_call_id": "1", "content": "r" * 50}, {"role": "assistant", "content": "b" * 50},
                {"role": "user", "content": "c" * 50}, {"role": "assistant", "content": "d" * 50}]
        out = _trim(msgs, 150)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant"])
        self.assertEqual(out[1]["content"], "c" * 50)


class LocalToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_deny_rules_match_prefixes_in_every_simple_command(self):
        rules = ["Bash(sudo:*)", "Bash(rm -rf /:*)", "Bash(dd:*)", "Bash(claude:*)"]
        self.assertEqual(localtools.denied("sudo ls", rules), "Bash(sudo:*)")
        self.assertEqual(localtools.denied("ls; sudo reboot", rules), "Bash(sudo:*)")
        self.assertEqual(localtools.denied("echo x | FOO=1 sudo tee /etc/x", rules), "Bash(sudo:*)")
        self.assertEqual(localtools.denied("rm -rf /home/x", rules), "Bash(rm -rf /:*)")
        self.assertEqual(localtools.denied("echo $(claude -p hi)", rules), "Bash(claude:*)")
        self.assertIsNone(localtools.denied("sudoku", rules))
        self.assertIsNone(localtools.denied("ls -la && git status", rules))
        self.assertIsNone(localtools.denied("dd-tool", rules))
        self.assertIsNone(localtools.denied("rm -rf ./build", rules))

    def test_bash_reports_output_exit_code_and_timeouts(self):
        out, err = localtools.run("Bash", json.dumps({"command": "echo hi; echo oops >&2; exit 3"}), self.dir, [])
        self.assertTrue(err)
        self.assertIn("hi", out)
        self.assertIn("oops", out)
        self.assertIn("[exit code 3]", out)
        out, err = localtools.run("Bash", {"command": "true"}, self.dir, [])
        self.assertEqual((out, err), ("(no output)", False))
        out, err = localtools.run("Bash", {"command": "sleep 5", "timeout_s": 1}, self.dir, [])
        self.assertTrue(err)
        self.assertIn("killed", out)
        out, err = localtools.run("Bash", {"command": "pwd"}, self.dir, [])
        self.assertEqual(Path(out.strip()).resolve(), Path(self.dir).resolve())

    def test_shell_cannot_start_another_brain(self):
        out, err = localtools.run("Bash", {"command": "claude -p hi"}, self.dir, [])
        self.assertTrue(err)
        self.assertIn("blocked by Vision", out)

    def test_read_write_edit(self):
        f = Path(self.dir, "sub", "f.txt")
        out, err = localtools.run("Write", {"file_path": "sub/f.txt", "content": "one\ntwo\nthree\n"}, self.dir, [])
        self.assertFalse(err)
        self.assertEqual(f.read_text(), "one\ntwo\nthree\n")
        out, err = localtools.run("Read", {"file_path": str(f)}, self.dir, [])
        self.assertEqual(out, "     1\tone\n     2\ttwo\n     3\tthree")
        out, err = localtools.run("Read", {"file_path": "sub/f.txt", "offset": 2, "limit": 1}, self.dir, [])
        self.assertEqual(out, "     2\ttwo\n… 1 more lines (offset=3)")
        out, err = localtools.run("Read", {"file_path": "nope.txt"}, self.dir, [])
        self.assertTrue(err)
        out, err = localtools.run("Edit", {"file_path": "sub/f.txt", "old_string": "two", "new_string": "2"}, self.dir, [])
        self.assertFalse(err, out)
        self.assertEqual(f.read_text(), "one\n2\nthree\n")
        out, err = localtools.run("Edit", {"file_path": "sub/f.txt", "old_string": "e", "new_string": "E"}, self.dir, [])
        self.assertTrue(err)
        self.assertIn("appears 3 times", out)
        out, err = localtools.run("Edit", {"file_path": "sub/f.txt", "old_string": "e", "new_string": "E", "replace_all": True}, self.dir, [])
        self.assertEqual(f.read_text(), "onE\n2\nthrEE\n")
        out, err = localtools.run("Edit", {"file_path": "sub/f.txt", "old_string": "zzz", "new_string": ""}, self.dir, [])
        self.assertTrue(err)

    def test_bad_arguments_and_unknown_tools_are_errors_not_exceptions(self):
        self.assertTrue(localtools.run("Bash", "{not json", self.dir, [])[1])
        self.assertTrue(localtools.run("Glob", {"pattern": "*"}, self.dir, [])[1])

    def test_long_output_is_clipped_head_and_tail(self):
        out, err = localtools.run("Bash", {"command": "seq 1 20000"}, self.dir, [])
        self.assertFalse(err)
        self.assertLessEqual(len(out), localtools.MAX_OUTPUT + 60)
        self.assertTrue(out.startswith("1\n2\n"))
        self.assertTrue(out.endswith("19999\n20000"))
        self.assertIn("characters omitted", out)

    def test_available_tools_follow_the_config_and_plan_mode(self):
        self.assertEqual(localtools.available(["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]),
                         ["Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"])
        self.assertEqual(localtools.available(["Read", "Bash", "WebSearch"], "plan"), ["Read", "WebSearch"])
        self.assertEqual(localtools.available([]), [])

    DDG = """<html><body><div class="results">
      <div class="result"><h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc">First <b>hit</b></a></h2>
        <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">A snippet with   <b>bold</b>
        words.</a></div>
      <div class="result"><h2><a class="result__a" href="https://example.org/b">Second</a></h2>
        <a class="result__snippet" href="https://example.org/b">Another.</a></div>
    </div></body></html>"""

    def test_web_search_parses_duckduckgo(self):
        with patch("vision.localtools._http_get", return_value=("text/html; charset=utf-8", self.DDG.encode())) as get:
            out, err = localtools.run("WebSearch", {"query": "example things", "count": 5}, self.dir, [])
        self.assertFalse(err, out)
        self.assertIn("html.duckduckgo.com/html/?q=example+things", get.call_args[0][0])
        self.assertIn("1. First hit\n   https://example.com/a\n   A snippet with bold words.", out)
        self.assertIn("2. Second\n   https://example.org/b\n   Another.", out)

    def test_web_search_uses_brave_with_a_key(self):
        # The title's whitespace is collapsed like the snippet's (newlines and runs of spaces to one space).
        body = json.dumps({"web": {"results": [{"title": "T\n  two", "url": "https://t.example", "description": "D"}]}}).encode()
        with patch("vision.localtools._http_get", return_value=("application/json", body)) as get:
            out, err = localtools.run("WebSearch", {"query": "q"}, self.dir, [], brave_key="k")
        self.assertFalse(err)
        self.assertIn("api.search.brave.com", get.call_args[0][0])
        self.assertEqual(get.call_args[0][1]["X-Subscription-Token"], "k")
        self.assertIn("1. T two\n   https://t.example\n   D", out)

    def test_web_search_failures_are_text(self):
        import urllib.error

        with patch("vision.localtools._http_get", side_effect=urllib.error.URLError("no route")):
            out, err = localtools.run("WebSearch", {"query": "q"}, self.dir, [])
        self.assertTrue(err)
        self.assertIn("could not be reached", out)
        with patch("vision.localtools._http_get", return_value=("text/html", b"<html><body>nothing here</body></html>")):
            out, err = localtools.run("WebSearch", {"query": "q"}, self.dir, [])
        self.assertFalse(err)
        self.assertIn("No results", out)
        with patch("vision.localtools._http_get", return_value=("text/html", b"<html><body><div class='anomaly-modal'>bot check</div></body></html>")):
            out, err = localtools.run("WebSearch", {"query": "q"}, self.dir, [])
        self.assertTrue(err)
        self.assertIn("bot check", out)
        self.assertIn("brave_api_key", out)
        self.assertTrue(localtools.run("WebSearch", {"query": ""}, self.dir, [])[1])

    def test_web_fetch_strips_a_page_to_text(self):
        html = b"""<html><head><title>Page Title</title><style>p{color:red}</style><script>alert(1)</script></head>
        <body><nav><a href="/">Home</a></nav><header>Top</header><h1>Heading</h1><p>First   paragraph.</p><p>Second<br>line</p>
        <script>var x = 1;</script><ul><li>one</li><li>two</li></ul></body></html>"""
        with patch("vision.localtools._http_get", return_value=("text/html; charset=utf-8", html)):
            out, err = localtools.run("WebFetch", {"url": "https://example.com/p"}, self.dir, [])
        self.assertFalse(err, out)
        self.assertTrue(out.startswith("https://example.com/p — Page Title (text/html,"))
        self.assertNotIn("alert", out)
        self.assertNotIn("color:red", out)
        self.assertNotIn("Home", out)  # navigation and headers are noise
        self.assertNotIn("Top", out)
        self.assertIn("Heading\nFirst paragraph.\nSecond\nline", out)
        self.assertIn("one\ntwo", out)
        with patch("vision.localtools._http_get", return_value=("text/plain", b"x" * 20_000)):
            out, err = localtools.run("WebFetch", {"url": "https://example.com/t", "max_chars": 1000}, self.dir, [])
        self.assertIn("characters omitted", out)
        self.assertLess(len(out), 1200)
        self.assertTrue(localtools.run("WebFetch", {"url": "ftp://x"}, self.dir, [])[1])
        with patch("vision.localtools._http_get", return_value=("text/plain", b"# raw")) as get:
            localtools.run("WebFetch", {"url": "https://github.com/o/r/blob/main/docs/a.md"}, self.dir, [])
        self.assertEqual(get.call_args[0][0], "https://raw.githubusercontent.com/o/r/main/docs/a.md")
        self.assertTrue(localtools.run("WebFetch", {"url": "file:///etc/passwd"}, self.dir, [])[1])


if __name__ == "__main__":
    unittest.main()


class ConfigProviderTests(unittest.TestCase):
    """A `[providers.<name>]` server: registered from config, driven by LocalBrain, its models prefixed."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubServer)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        from vision import config, providers

        StubServer.requests, StubServer.headers_seen = [], []
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        (state / "config.toml").write_text(
            "[providers.ollama]\n"
            f'base_url = "{self.base}"\n'
            'label = "Ollama"\n'
            'api_key_env = "OLLAMA_TEST_KEY"\n'
            "context = 4096\n"
            "\n[providers.hosted]\n"
            'base_url = "https://api.example.test/v1"\n'
            'models = ["big-1", "small-1"]\n'
            "\n[providers.claude]\n"
            'base_url = "http://nope/v1"\n'
            "\n[providers.odd]\n"
            'type = "acp"\n'
            'base_url = "http://nope/v1"\n',
            encoding="utf-8")
        self._patches = [patch("vision.local.SESSIONS_DIR", state / "sessions"), patch("vision.local.LAST_SESSION_FILE", state / "last"),
                         patch.object(config, "CONFIG_PATH", state / "config.toml"), patch.object(config, "ensure_dirs"),
                         patch.dict("os.environ", {"OLLAMA_TEST_KEY": "sekrit"})]
        for p in self._patches:
            p.start()
        self.cfg = config.load_config()
        self.addCleanup(lambda: providers.register_endpoints({}))
        self.addCleanup(self.tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def test_registered_from_config_with_notes_for_bad_tables(self):
        from vision import providers

        self.assertEqual([n for n in providers.REGISTRY if not providers.REGISTRY[n].builtin], ["ollama", "hosted"])
        self.assertEqual(self.cfg.providers.enabled, ["claude", "codex", "grok", "local", "ollama", "hosted"])
        self.assertEqual(len(self.cfg.providers.notes), 2)
        self.assertIn("claude", self.cfg.providers.notes[0])
        self.assertIn("acp", self.cfg.providers.notes[1])
        p = providers.REGISTRY["ollama"]
        self.assertEqual((p.label, p.endpoint.base_url, p.endpoint.context, p.endpoint.key()), ("Ollama", self.base, 4096, "sekrit"))
        self.assertTrue(providers.ready("ollama", self.cfg))
        self.assertFalse(providers.ready("hosted", self.cfg))  # nothing listens at api.example.test

    def test_models_come_from_the_server_prefixed(self):
        from vision import clis, models

        self.assertTrue(clis.refresh_local_models("ollama"))
        self.assertEqual([m.alias for m in models._LISTS["ollama"]], ["ollama/qwen3", "ollama/llama4"])
        self.assertEqual(models.provider_for("ollama/qwen3"), "ollama")
        self.assertEqual(models.model_label("ollama/qwen3"), "qwen3")
        self.assertEqual(models.provider_default("ollama"), "ollama/qwen3")
        self.assertEqual(StubServer.headers_seen[-1].get("Authorization"), "Bearer sekrit")
        self.assertIn(("Ollama", [("ollama/qwen3", "qwen3", "shell, files and web search"),
                                  ("ollama/llama4", "llama4", "shell, files and web search")], f"via {self.base.split('://')[-1].removesuffix('/v1')} · an API, by key"),
                      models.MODEL_TABS)  # (this one has a key)
        # a fixed list in the table is taken as it is, no request made
        self.assertTrue(clis.refresh_local_models("hosted"))
        self.assertEqual([m.alias for m in models._LISTS["hosted"]], ["hosted/big-1", "hosted/small-1"])

    def test_a_turn_goes_to_that_server_with_its_key_and_bare_model_id(self):
        from vision import clis, models, providers, sessions

        clis.refresh_local_models("ollama")
        self.cfg.brain.model, self.cfg.brain.effort, self.cfg.brain.mode = "ollama/qwen3", "off", "auto"
        self.cfg.brain.workdir = self.tmp.name
        brain = create_brain(self.cfg.brain)
        self.assertEqual((type(brain).__name__, brain.provider, brain.context_window()), ("LocalBrain", "ollama", 4096))
        turn = brain.ask("hi")
        self.assertEqual(turn.text, "Sorted.")
        self.assertEqual(StubServer.requests[-1]["model"], "qwen3")
        self.assertEqual(StubServer.post_headers[-1].get("Authorization"), "Bearer sekrit")
        self.assertEqual(brain.last_usage["provider"], "ollama")
        self.assertIn("served by Ollama", StubServer.requests[-1]["messages"][0]["content"])
        # its session lists under its own tab, not Local's
        with patch("vision.local.SESSIONS_DIR", Path(self.tmp.name) / "sessions"):
            self.assertEqual([s.id for s in sessions.list_sessions("ollama")], [turn.session_id])
            self.assertEqual(sessions.list_sessions("local"), [])
        self.assertEqual(sessions.session_history("ollama", turn.session_id)[0]["text"], "hi")
        self.assertIn("ollama", providers.conversation_names())
        self.assertEqual(providers.endpoint_for("local", self.cfg).base_url, self.cfg.local.base_url)

    def test_the_voice_model_can_be_that_server(self):
        from vision import clis

        clis.refresh_local_models("ollama")
        self.cfg.conversation.model = "ollama/qwen3"
        conv = LocalConversation(self.cfg)
        self.assertEqual((conv.provider, conv._ep.base_url), ("ollama", self.base))
        conv.warm_up()
