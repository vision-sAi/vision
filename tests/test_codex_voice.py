"""The Codex voice driver against a fake `codex app-server` that speaks the JSON-RPC protocol."""
import json
import os
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from fake_exe import python_command
from vision.brain import BrainError
from vision.codex_voice import CodexConversation, strict_schema
from vision.config import Config
from vision.conversation import RESPONSE_SCHEMA, validate_response

FAKE = textwrap.dedent('''\
    import json, os, sys
    log = open(os.environ["FAKE_LOG"], "a")
    reply = json.loads(os.environ["FAKE_REPLY"])
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        log.write(line); log.flush()
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"id": rid, "result": {}})
        elif method == "thread/start":
            send({"id": rid, "result": {"thread": {"id": "t1"}}})
        elif method == "turn/start":
            send({"id": rid, "result": {"turn": {"id": "u1"}}})
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "pre", "text": "Checking."}}})
            text = json.dumps(reply)
            for i in range(0, len(text), 7):
                send({"method": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": text[i:i + 7]}})
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "m1", "text": text}}})
            send({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {"inputTokens": 900, "outputTokens": 30}}}})
            send({"method": "turn/completed", "params": {"turn": {"id": "u1", "status": "completed"}}})
''')


class CodexVoiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "log.jsonl")
        self.exe = python_command(os.path.join(self.dir.name, "codex"), FAKE)
        self.cfg = Config()
        self.cfg.conversation.model = "gpt-5.6-luna"
        self.cfg.conversation.effort = "low"
        self.reply = {"speech": "Mostly cloudy, sixty-five. Rain later.", "task": None, "search": None}

    def tearDown(self):
        self.dir.cleanup()

    def run_turns(self, *packets):
        env = {**os.environ, "FAKE_LOG": self.log, "FAKE_REPLY": json.dumps(self.reply)}
        voice = CodexConversation(self.cfg)
        spoken, out = [], []
        with patch("vision.codex.find_codex", return_value=self.exe), patch("vision.brain.brain_env", return_value=env):
            try:
                for packet in packets:
                    out.append(voice.complete(packet, threading.Event(), on_speech=spoken.append))
            finally:
                voice.close()
        with open(self.log) as f:
            sent = [json.loads(line) for line in f]
        return out, "".join(spoken), sent, voice

    def test_a_turn_streams_its_speech_and_returns_the_reply(self):
        out, spoken, sent, voice = self.run_turns({"user": "weather?", "history": []})
        self.assertEqual(out[0], self.reply)
        self.assertEqual(spoken, self.reply["speech"])  # the stray "Checking." message is never spoken
        self.assertEqual(validate_response(out[0])["speech"], self.reply["speech"])

    def test_the_thread_has_no_tools_and_the_turn_a_strict_schema(self):
        _, _, sent, _ = self.run_turns({"user": "hi", "history": []})
        start = next(m["params"] for m in sent if m.get("method") == "thread/start")
        self.assertEqual((start["model"], start["sandbox"], start["approvalPolicy"], start["ephemeral"]),
                         ("gpt-5.6-luna", "read-only", "never", True))
        config = start["config"]
        for key in ("features.shell_tool", "features.unified_exec", "features.multi_agent", "features.apply_patch_freeform"):
            self.assertIs(config[key], False, key)
        self.assertEqual(config["web_search"], "live")  # [conversation].web defaults on
        self.assertIn("Your only tool is web search", start["baseInstructions"])
        turn = next(m["params"] for m in sent if m.get("method") == "turn/start")
        self.assertEqual((turn["threadId"], turn["effort"]), ("t1", "low"))
        self.assertEqual(turn["outputSchema"], strict_schema(RESPONSE_SCHEMA))

    def test_web_off_disables_codex_search(self):
        self.cfg.conversation.web = False
        _, _, sent, _ = self.run_turns({"user": "hi", "history": []})
        start = next(m["params"] for m in sent if m.get("method") == "thread/start")
        self.assertEqual(start["config"]["web_search"], "disabled")

    def test_later_turns_reuse_the_thread_and_send_only_what_changed(self):
        history = [{"user": "earlier", "speech": "yes"}]
        _, _, sent, _ = self.run_turns({"user": "one", "history": history, "memory": "m"},
                                       {"user": "two", "history": history, "memory": "m"})
        self.assertEqual(sum(m.get("method") == "thread/start" for m in sent), 1)
        turns = [json.loads(m["params"]["input"][0]["text"]) for m in sent if m.get("method") == "turn/start"]
        self.assertIn("history", turns[0])
        self.assertNotIn("history", turns[1])
        self.assertNotIn("memory", turns[1])

    def test_usage_comes_from_the_last_turn(self):
        _, _, _, voice = self.run_turns({"user": "hi", "history": []})
        self.assertEqual(voice.usage, {"input_tokens": 900, "output_tokens": 30})

    def test_a_reply_that_is_not_json_fails(self):
        self.reply = "not an object"
        out, _, _, _ = self.run_turns({"user": "hi", "history": []})
        with self.assertRaises(BrainError):
            validate_response(out[0])


class StrictSchemaTests(unittest.TestCase):
    def test_every_property_is_required_and_min_length_goes(self):
        schema = strict_schema(RESPONSE_SCHEMA)
        self.assertEqual(schema["required"], ["speech", "task", "search", "weather", "close"])
        self.assertEqual(schema["properties"]["weather"]["anyOf"][1]["required"], ["places", "when"])
        task = schema["properties"]["task"]["anyOf"][1]
        self.assertEqual(set(task["required"]), set(task["properties"]))
        self.assertNotIn("minLength", json.dumps(schema))
        self.assertIn("minLength", json.dumps(RESPONSE_SCHEMA))  # the original is untouched


if __name__ == "__main__":
    unittest.main()
