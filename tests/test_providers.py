from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from vision.brain import Brain, agent_frame, create_brain
from vision.codex import CodexBrain
from vision.models import ModelInfo
from vision.config import BrainConfig, CodexConfig, GrokConfig, load_config, save_brain_defaults, save_input_device, save_wake_enabled
from vision.grok import GrokBrain, fetch_subscription
from vision.models import coerce_effort, effort_choices, model_label, provider_for


class _Input:
    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, value):
        self.value += value

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Process:
    def __init__(self, events, returncode=0):
        self.stdin = _Input()
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _LingeringProcess(_Process):
    def __init__(self, events):
        super().__init__(events, returncode=None)
        self.terminated = False

    def wait(self, timeout=None):
        if self.returncode is None and timeout is not None:
            import subprocess

            raise subprocess.TimeoutExpired("codex", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def _cfg(model="gpt-6-astra", effort="high"):
    cfg = BrainConfig(model=model, effort=effort)
    cfg.codex = CodexConfig()
    cfg.grok = GrokConfig(transport="headless")  # the grok -p stream tests; agent mode is tests/test_grok_acp.py
    return cfg


class ModelTests(unittest.TestCase):
    def test_provider_routing(self):
        self.assertEqual(provider_for("opus"), "claude")
        self.assertEqual(provider_for("gpt-6-astra"), "codex")
        self.assertEqual(provider_for("o9-preview"), "codex")
        self.assertEqual(provider_for("grok-4.6"), "grok")
        self.assertEqual(provider_for("grok"), "grok")
        self.assertEqual(provider_for("grok-experimental"), "grok")

    def test_effort_capabilities_and_coercion(self):
        self.assertEqual([v for v, _, _ in effort_choices("haiku")], [""])
        self.assertEqual(coerce_effort("opus", "ultra")[0], "ultracode")
        self.assertEqual(coerce_effort("gpt-6-astra", "ultracode")[0], "ultra")
        self.assertEqual(coerce_effort("gpt-5.5", "max")[0], "xhigh")
        # An empty effort (inherited from Haiku) must not leave an effort-capable model "off".
        self.assertEqual(coerce_effort("haiku", "")[0], "")
        self.assertEqual(coerce_effort("opus", "")[0], "medium")  # the default a Claude model rests at
        self.assertEqual(coerce_effort("gpt-6-astra", "")[0], "low")
        self.assertEqual(coerce_effort("grok-4.6", "ultra")[0], "xhigh")
        self.assertEqual(coerce_effort("grok-4.5", "xhigh")[0], "high")
        self.assertEqual(coerce_effort("grok-4.6", "")[0], "high")

    def test_local_models_offer_thinking_off(self):
        # The local models' levels are the thinking switch: off (default) and high; low/medium land on off,
        # and "off" reaching a model that always thinks becomes its lowest level.
        self.assertEqual([v for v, _, _ in effort_choices("local-model")], ["off", "high"])
        self.assertEqual(coerce_effort("local-model", "")[0], "off")
        self.assertEqual(coerce_effort("local-model", "low")[0], "off")
        self.assertEqual(coerce_effort("local-model", "medium")[0], "off")
        self.assertEqual(coerce_effort("local-model", "high")[0], "high")
        self.assertEqual(coerce_effort("local-model", "xhigh")[0], "high")
        self.assertEqual(coerce_effort("local-model", "ultracode")[0], "high")
        self.assertEqual(coerce_effort("opus", "off"), ("low", "effort off → low (Opus 5 supports low, medium, high, xhigh, max, ultracode)"))
        self.assertEqual(coerce_effort("haiku", "off")[0], "")

    def test_factory_selects_driver(self):
        with patch("vision.brain.find_claude", return_value="claude"):
            self.assertIsInstance(create_brain(_cfg("opus")), Brain)
        with patch("vision.codex.find_codex", return_value="codex"):
            cfg = _cfg("gpt-5.5", "ultra")
            self.assertIsInstance(create_brain(cfg), CodexBrain)
            self.assertEqual(cfg.effort, "xhigh")
        with patch("vision.grok.find_grok", return_value="grok"):
            self.assertIsInstance(create_brain(_cfg("grok-4.6")), GrokBrain)
            self.assertIsInstance(create_brain(_cfg("grok")), GrokBrain)

    def test_fast_argument_toggle_and_validation(self):
        from vision.cli import _fast_value

        self.assertTrue(_fast_value("", False))
        self.assertFalse(_fast_value("off", True))
        self.assertTrue(_fast_value("ON", False))
        with self.assertRaises(ValueError):
            _fast_value("maybe", False)


class ConfigTests(unittest.TestCase):
    def test_explicit_blank_effort_is_preserved_for_haiku(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[brain]\nmodel = "haiku"\neffort = ""\n')
            with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                cfg = load_config()
        self.assertEqual(cfg.brain.model, "haiku")
        self.assertEqual(cfg.brain.effort, "")
        self.assertIs(cfg.brain.codex, cfg.codex)

    def test_saving_defaults_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[brain]\n# keep me\nmodel = "opus"\neffort = "high"\n\n[voice]\nspeed = 1.2\n')
            with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                save_brain_defaults("gpt-6-astra", "ultra")
                text = path.read_text()
        self.assertIn("# keep me", text)
        self.assertIn('model = "gpt-6-astra"', text)
        self.assertIn("speed = 1.2", text)

    def test_saving_wake_enabled_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[wake]\n# keep me\nenabled = true\n\n[voice]\nvoice = "jarvis"\n')
            with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                save_wake_enabled(False)
                text = path.read_text()
                self.assertFalse(load_config().wake.enabled)
        self.assertIn("# keep me", text)
        self.assertIn("enabled = false", text)

    def test_saving_input_device_edits_listen_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[listen]\n# which mic\ninput_device = ""\nwhisper_model = "base.en"\n\n[wake]\nenabled = false\n')
            with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                save_input_device('Blue "Snowball"')
                cfg = load_config()
                text = path.read_text()
                save_input_device("")
                self.assertEqual(load_config().listen.input_device, "")
        self.assertEqual(cfg.listen.input_device, 'Blue "Snowball"')
        self.assertEqual(cfg.listen.whisper_model, "base.en")
        self.assertIn("# which mic", text)


class CodexProtocolTests(unittest.TestCase):
    """The `codex exec` transport ([codex].transport = "exec"); the app-server one is in test_codex_app."""

    thread_id = "12345678-1234-1234-1234-123456789abc"

    def setUp(self):
        p = patch.object(CodexBrain, "_transport", lambda self: "exec")
        p.start()
        self.addCleanup(p.stop)

    def _events(self, text, tokens):
        return [
            {"type": "thread.started", "thread_id": self.thread_id},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": tokens // 2}},
        ]

    def test_stdin_jsonl_sessions_and_cumulative_usage(self):
        first = _Process(self._events("first", 10))
        second = _Process(self._events("second", 25))
        seen_commands = []

        def popen(command, **kwargs):
            seen_commands.append(command)
            return first if len(seen_commands) == 1 else second

        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.codex.subprocess.Popen", side_effect=popen):
            brain = CodexBrain(_cfg())
            turn1 = brain.ask("--option-looking prompt")
            turn2 = brain.ask("follow up")

        self.assertEqual(turn1.text, "first")
        self.assertEqual(turn2.text, "second")
        self.assertEqual(first.stdin.value, "--option-looking prompt")
        self.assertTrue(first.stdin.closed)
        self.assertNotIn("--option-looking prompt", seen_commands[0])
        self.assertEqual(seen_commands[1][2:4], ["resume", self.thread_id])
        self.assertEqual(brain.last_usage["thread"]["input_tokens"], 25)
        self.assertEqual(brain.last_usage["last"]["input_tokens"], 15)

    def test_usage_hides_token_totals(self):
        from rich.console import Console

        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"):
            brain = CodexBrain(_cfg())
        brain.last_usage = {
            "provider": "codex",
            "model": "gpt-5.4",
            "last": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2, "reasoning_output_tokens": 1},
            "thread": {"input_tokens": 25, "output_tokens": 8, "cached_input_tokens": 2, "reasoning_output_tokens": 1},
            "turns": 2,
            "at": 0,
        }
        with patch.object(brain, "rate_limits", return_value={
            "rate_limits": {
                "plan_type": "plus",
                "primary": {"used_percent": 12, "window_minutes": 300, "resets_at": 0},
                "secondary": {"used_percent": 3, "window_minutes": 10080, "resets_at": 0},
            }
        }):
            buf = io.StringIO()
            Console(file=buf, force_terminal=False, width=120, color_system=None).print(brain.usage_renderable())
            text = buf.getvalue()
        self.assertIn("Current session", text)
        self.assertIn("Current week", text)
        self.assertNotIn("Tokens", text)
        self.assertNotIn("last turn", text)
        self.assertNotIn("thread total", text)
        self.assertNotIn("vision usage --full", text)

    def test_status_says_thinking_only_for_a_reasoning_item(self):
        events = [
            {"type": "thread.started", "thread_id": self.thread_id},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "reasoning"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "..."}},
            {"type": "item.started", "item": {"type": "command_execution", "command": "vision weather"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "vision weather"}},
            {"type": "item.started", "item": {"type": "agent_message"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Cloudy, 65."}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        statuses = []
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.codex.subprocess.Popen", return_value=_Process(events)):
            turn = CodexBrain(_cfg()).ask("what's the weather", on_status=statuses.append)
        self.assertEqual(turn.text, "Cloudy, 65.")
        self.assertEqual(statuses, ["thinking", "Bash", "reading", "writing"])

    def test_narration_before_a_tool_is_dropped(self):
        events = [
            {"type": "thread.started", "thread_id": self.thread_id},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "I'm checking Springfield."}},
            {"type": "item.started", "item": {"type": "command_execution", "command": "vision weather"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "vision weather"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Cloudy, 65."}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "High of 73."}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        streamed = []
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.codex.subprocess.Popen", return_value=_Process(events)):
            turn = CodexBrain(_cfg()).ask("what's the weather", on_text=streamed.append)
        self.assertEqual(turn.text, "Cloudy, 65.\n\nHigh of 73.")
        self.assertEqual("".join(streamed), turn.text)
        self.assertEqual(turn.tools_used, ["Bash"])

    def test_fast_mode_uses_priority_service_tier(self):
        cfg = _cfg()
        cfg.fast = True
        with patch("vision.codex.find_codex", return_value="codex"):
            cmd = CodexBrain(cfg)._command()
        self.assertIn('service_tier="priority"', cmd)

        cfg.fast = False
        with patch("vision.codex.find_codex", return_value="codex"):
            cmd = CodexBrain(cfg)._command()
        self.assertIn('service_tier="default"', cmd)

    def test_completed_turn_does_not_wait_for_lingering_cli(self):
        proc = _LingeringProcess(self._events("done", 10))
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.STATE_DIR", Path(td)), \
             patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.codex.subprocess.Popen", return_value=proc):
            turn = CodexBrain(_cfg()).ask("hello")

        self.assertEqual(turn.text, "done")
        self.assertFalse(turn.is_error)
        self.assertTrue(proc.terminated)

    def test_nonzero_exit_with_partial_text_is_an_error(self):
        proc = _Process(self._events("partial", 5)[:-1], returncode=-15)
        with patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.codex.subprocess.Popen", return_value=proc):
            turn = CodexBrain(_cfg()).ask("stop")
        self.assertEqual(turn.text, "partial")
        self.assertTrue(turn.is_error)
        self.assertEqual(turn.error, "cancelled")

    def test_cancelled_turn_recovers_partial_rollout_usage(self):
        turn_id = "12345678-1234-1234-1234-123456789def"
        events = [
            {"type": "thread.started", "thread_id": self.thread_id},
            {"type": "turn.started", "turn_id": turn_id},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}},
        ]
        proc = _Process(events, returncode=-15)
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            rollout = sessions / f"rollout-test-{self.thread_id}.jsonl"
            rollout.write_text(json.dumps({
                "type": "token_usage_record",
                "payload": {
                    "thread_id": self.thread_id,
                    "turn_id": turn_id,
                    "thread_token_usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 100,
                        "output_tokens": 7,
                    },
                },
            }) + "\n")
            with patch("vision.codex.find_codex", return_value="codex"), \
                 patch("vision.codex.CODEX_SESSIONS", str(sessions)), \
                 patch("vision.codex.STATE_DIR", Path(td)), \
                 patch("vision.codex.LAST_SESSION_FILE", Path(td) / "last"), \
                 patch("vision.codex.USAGE_FILE", Path(td) / "usage"), \
                 patch("vision.codex.subprocess.Popen", return_value=proc):
                turn = CodexBrain(_cfg()).ask("stop")

        self.assertEqual(turn.error, "cancelled")
        self.assertEqual(turn.usage["input_tokens"], 120)
        self.assertEqual(turn.usage["output_tokens"], 7)


class GrokProtocolTests(unittest.TestCase):
    session_id = "01a0b65a-039e-72c1-9ff0-f3e54c08899b"

    def _events(self, text, tokens=10):
        return [
            {"type": "text", "data": text},
            {"type": "end", "sessionId": self.session_id, "usage": {"input_tokens": tokens, "output_tokens": tokens // 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "reasoning_tokens": 2}},
        ]

    def test_prompt_file_resume_and_usage(self):
        first = _Process(self._events("first", 10))
        second = _Process(self._events("second", 10))
        seen_commands, prompts = [], []

        def popen(command, **kwargs):
            seen_commands.append(command)
            path = command[command.index("--prompt-file") + 1]
            prompts.append(Path(path).read_text())
            return first if len(seen_commands) == 1 else second

        with tempfile.TemporaryDirectory() as td, \
             patch("vision.grok.find_grok", return_value="grok"), \
             patch("vision.grok.STATE_DIR", Path(td)), \
             patch("vision.grok.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.grok.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.grok.subprocess.Popen", side_effect=popen):
            brain = GrokBrain(_cfg("grok-4.6"))
            turn1 = brain.ask("hello there")
            turn2 = brain.ask("follow up")

        self.assertEqual(turn1.text, "first")
        self.assertEqual(turn2.text, "second")
        self.assertEqual(prompts[0], "hello there")
        self.assertIn("--resume", seen_commands[1])
        self.assertIn(self.session_id, seen_commands[1])
        self.assertEqual(seen_commands[1][seen_commands[1].index("--resume") + 1], self.session_id)
        self.assertNotIn("--resume", seen_commands[0])
        self.assertIn("--always-approve", seen_commands[0])
        self.assertEqual(seen_commands[0][seen_commands[0].index("--sandbox") + 1], "off")
        self.assertEqual(seen_commands[0][seen_commands[0].index("--model") + 1], "grok-4.6")
        self.assertEqual(brain.last_usage["last"]["input_tokens"], 10)
        self.assertEqual(brain.last_usage["turns"], 2)

    def test_plan_mode_uses_read_only_sandbox(self):
        from vision.grok import sandbox_for

        self.assertEqual(sandbox_for(BrainConfig(mode="plan", grok=GrokConfig())), "read-only")
        self.assertEqual(sandbox_for(BrainConfig(mode="auto", grok=GrokConfig())), "off")
        self.assertEqual(sandbox_for(BrainConfig(mode="auto", grok=GrokConfig(sandbox="workspace"))), "workspace")
        with patch("vision.grok.find_grok", return_value="grok"):
            cmd = GrokBrain(_cfg("grok-4.6"))._command("/tmp/p")
        self.assertIn("--always-approve", cmd)
        self.assertIn("--no-plan", cmd)
        cfg = _cfg("grok-4.6")
        cfg.mode = "plan"
        with patch("vision.grok.find_grok", return_value="grok"):
            cmd = GrokBrain(cfg)._command("/tmp/p")
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_grok_nickname_becomes_grok_4_6(self):
        with patch("vision.grok.find_grok", return_value="grok"):
            cmd = GrokBrain(_cfg("grok"))._command("/tmp/p")
        self.assertEqual(cmd[cmd.index("--model") + 1], "grok-4.6")

    def test_tool_calls_surface_as_status_and_join_text(self):
        events = [
            {"type": "text", "data": "looking"},
            {"type": "tool_call", "toolCallId": "c1", "toolName": "read_file", "title": "Read"},
            {"type": "tool_call_update", "toolCallId": "c1", "status": "completed"},
            {"type": "text", "data": "done"},
            {"type": "end", "sessionId": self.session_id, "usage": {"input_tokens": 3, "output_tokens": 2}},
        ]
        statuses = []
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.grok.find_grok", return_value="grok"), \
             patch("vision.grok.STATE_DIR", Path(td)), \
             patch("vision.grok.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.grok.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.grok.subprocess.Popen", return_value=_Process(events)):
            turn = GrokBrain(_cfg("grok-4.6")).ask("go", on_status=statuses.append)
        self.assertEqual(turn.text, "done")  # "looking" was narration before the tool: dropped
        self.assertEqual(turn.tools_used, ["Read"])
        self.assertEqual(statuses, ["Read", "reading"])

    def test_thought_chunks_report_thinking_once(self):
        events = [
            {"type": "thought", "data": "Weather question."},
            {"type": "thought", "data": " Run the tool."},
            {"type": "tool_call", "toolCallId": "c1", "toolName": "read_file", "title": "Read"},
            {"type": "tool_call_update", "toolCallId": "c1", "status": "completed"},
            {"type": "thought", "data": "Now answer."},
            {"type": "text", "data": "Cloudy, 65."},
            {"type": "end", "sessionId": self.session_id},
        ]
        statuses = []
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.grok.find_grok", return_value="grok"), \
             patch("vision.grok.STATE_DIR", Path(td)), \
             patch("vision.grok.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.grok.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.grok.subprocess.Popen", return_value=_Process(events)):
            turn = GrokBrain(_cfg("grok-4.6")).ask("go", on_status=statuses.append)
        self.assertEqual(turn.text, "Cloudy, 65.")
        self.assertEqual(statuses, ["thinking", "Read", "reading", "thinking"])

    def test_long_text_before_a_tool_is_kept_and_joined(self):
        warning = "Heads up: this wipes the build directory and every cached artefact under it, so anything unsaved there is gone for good. Running it now."
        events = [
            {"type": "text", "data": warning[:80]}, {"type": "text", "data": warning[80:]},
            {"type": "tool_call", "toolCallId": "c1", "toolName": "shell", "title": "Bash"},
            {"type": "tool_call_update", "toolCallId": "c1", "status": "completed"},
            {"type": "text", "data": "Done."},
            {"type": "end", "sessionId": self.session_id},
        ]
        streamed = []
        with tempfile.TemporaryDirectory() as td, \
             patch("vision.grok.find_grok", return_value="grok"), \
             patch("vision.grok.STATE_DIR", Path(td)), \
             patch("vision.grok.LAST_SESSION_FILE", Path(td) / "last"), \
             patch("vision.grok.USAGE_FILE", Path(td) / "usage"), \
             patch("vision.grok.subprocess.Popen", return_value=_Process(events)):
            turn = GrokBrain(_cfg("grok-4.6")).ask("go", on_text=streamed.append)
        self.assertEqual(turn.text, warning + "\n\nDone.")
        self.assertEqual("".join(streamed), turn.text)

    def test_nonzero_exit_without_end_is_an_error(self):
        proc = _Process([{"type": "text", "data": "partial"}], returncode=-15)
        with patch("vision.grok.find_grok", return_value="grok"), \
             patch("vision.grok.subprocess.Popen", return_value=proc):
            turn = GrokBrain(_cfg("grok-4.6")).ask("stop")
        self.assertEqual(turn.text, "partial")
        self.assertTrue(turn.is_error)
        self.assertEqual(turn.error, "cancelled")

    def test_usage_shows_weekly_subscription_limit(self):
        from rich.console import Console

        billing = {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-09-17T22:02:59+00:00",
                    "end": "2026-09-24T22:02:59+00:00",
                },
                "creditUsagePercent": 25.0,
                "productUsage": [
                    {"product": "GrokBuild", "usagePercent": 22.0},
                    {"product": "GrokImagine", "usagePercent": 3.0},
                ],
            }
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(billing).encode()

        with tempfile.TemporaryDirectory() as td:
            auth = Path(td) / "auth.json"
            auth.write_text(json.dumps({"https://auth.x.ai::x": {"key": "tok", "user_id": "u1"}}))
            settings = Path(td) / "settings_cache.json"
            settings.write_text(json.dumps({
                "payload": json.dumps({"settings": {"subscription_tier_display": "SuperGrok"}}),
            }))
            usage = Path(td) / "usage"
            usage.write_text(json.dumps({
                "provider": "grok",
                "model": "grok-4.6",
                "last": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 0,
                    "reasoning_tokens": 1,
                },
                "turns": 3,
                "at": 0,
            }))
            with patch("vision.grok.find_grok", return_value="grok"), \
                 patch("vision.grok.GROK_AUTH_FILE", str(auth)), \
                 patch("vision.grok.GROK_SETTINGS_CACHE", str(settings)), \
                 patch("vision.grok.USAGE_FILE", usage), \
                 patch("vision.grok.urllib.request.urlopen", return_value=_Resp()):
                brain = GrokBrain(_cfg("grok-4.6"))
                buf = io.StringIO()
                Console(file=buf, force_terminal=False, width=120, color_system=None).print(brain.usage_renderable())
                text = buf.getvalue()
                self.assertIn("Current week", text)
                self.assertIn("25%", text)
                self.assertIn("SuperGrok", text)
                self.assertNotIn("Build", text)
                self.assertNotIn("Grok tokens", text)
                self.assertNotIn("last turn", text)
                self.assertNotIn("vision usage --full", text)
                self.assertNotIn("/usage full", text)
                full_buf = io.StringIO()
                Console(file=full_buf, force_terminal=False, width=120, color_system=None).print(
                    brain.usage_renderable(full=True)
                )
                full = full_buf.getvalue()
                self.assertIn("Build", full)
                self.assertIn("Imagine", full)
                self.assertNotIn("Grok tokens", full)
                self.assertNotIn("last turn", full)

    def test_fetch_subscription_parses_unified_billing(self):
        billing = {
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2026-09-24T22:02:59+00:00"},
                "creditUsagePercent": 25.0,
                "productUsage": [{"product": "GrokBuild", "usagePercent": 22.0}],
                "prepaidBalance": {"val": 0},
            }
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(billing).encode()

        with tempfile.TemporaryDirectory() as td:
            auth = Path(td) / "auth.json"
            auth.write_text(json.dumps({"k": {"key": "tok", "user_id": "u1"}}))
            with patch("vision.grok.GROK_AUTH_FILE", str(auth)), \
                 patch("vision.grok.GROK_SETTINGS_CACHE", str(Path(td) / "missing.json")), \
                 patch("vision.grok.urllib.request.urlopen", return_value=_Resp()):
                sub = fetch_subscription()
        self.assertEqual(sub["used_percent"], 25.0)
        self.assertEqual(sub["period"], "weekly")
        self.assertEqual(sub["products"][0]["label"], "Build")
        self.assertEqual(sub["resets_at"], datetime(2026, 9, 24, 22, 2, 59, tzinfo=timezone.utc).timestamp())

    def test_fetch_subscription_missing_login_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("vision.grok.GROK_AUTH_FILE", str(Path(td) / "nope.json")):
                self.assertIsNone(fetch_subscription())


class ModelTranscriptTests(unittest.TestCase):
    """Switching models preserves the transcript without a summary-generation turn."""

    def _claude_events(self, sid="s-new"):
        return [
            {"type": "system", "subtype": "init", "session_id": sid, "model": "claude-x"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Next step"}]}},
            {"type": "result", "session_id": sid, "is_error": False},
        ]

    def test_same_provider_switch_resumes_full_session_without_calling_old_model(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        with patch("vision.brain.subprocess.Popen") as popen:
            brain2, detail = _switch_model(cfg, brain, "sonnet", voice_mode=False)
            popen.assert_not_called()
        self.assertIs(brain2, brain)
        self.assertEqual(brain.session_id, "s-old")
        self.assertIsNone(brain.handoff)
        self.assertIn("carrying the whole transcript", detail)
        proc = _Process(self._claude_events("s-old"))
        with patch("vision.brain.subprocess.Popen", return_value=proc) as popen, \
             patch("vision.brain.Brain._remember_session"):
            brain.ask("what next?")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s-old")
        self.assertIn("sonnet", cmd)
        self.assertEqual(json.loads(proc.stdin.value)["message"]["content"], "what next?")

    def test_full_keeps_transcript_and_unchanged_model_is_noop(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        with patch("vision.brain.subprocess.Popen") as popen:
            _switch_model(cfg, brain, "sonnet", voice_mode=False, full=True)
            _switch_model(cfg, brain, "sonnet", voice_mode=False)  # same model again: nothing to hand over
            popen.assert_not_called()
        self.assertEqual(brain.session_id, "s-old")
        self.assertIsNone(brain.handoff)

    def test_codex_and_grok_keep_their_existing_session(self):
        from vision.cli import _switch_model
        from vision.config import Config

        for cls, finder, old, new in (
            (CodexBrain, "vision.codex.find_codex", "gpt-6-astra", "gpt-5.6-sol"),
            (GrokBrain, "vision.grok.find_grok", "grok-4.6", "grok-4.5"),
        ):
            with self.subTest(provider=cls.provider):
                cfg = Config()
                cfg.brain.model = old
                with patch(finder, return_value="provider-cli"):
                    brain = cls(cfg.brain, session_id="existing-session")
                with patch.object(brain, "ask") as ask, patch.object(brain, "new_session") as new_session:
                    result, detail = _switch_model(cfg, brain, new, voice_mode=False)
                ask.assert_not_called()
                new_session.assert_not_called()
                self.assertIs(result, brain)
                self.assertEqual(result.session_id, "existing-session")
                self.assertIn("carrying the whole transcript", detail)

    def test_cross_provider_switch_carries_local_transcript_without_asking(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        hist = [{"role": "user", "text": "fix the lock"}, {"role": "assistant", "text": "patched talker.py"}]
        with patch("vision.sessions.session_history", return_value=hist) as hist_fn, \
             patch("vision.brain.subprocess.Popen") as popen, \
             patch("vision.codex.find_codex", return_value="codex"):
            brain2, detail = _switch_model(cfg, brain, "gpt-6-astra", voice_mode=False)
            popen.assert_not_called()
            hist_fn.assert_called_once_with("claude", "s-old", limit=0, include_context=True)
        self.assertIsInstance(brain2, CodexBrain)
        self.assertIn("fix the lock", brain2.handoff)
        self.assertIn("patched talker.py", brain2.handoff)
        self.assertIn("new Codex conversation from the previous transcript", detail)

    def test_cross_provider_carries_long_transcript_and_preserves_it_before_first_message(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        hist = [{"role": "user", "text": "first message"}, {"role": "assistant", "text": "x" * 40_000}]
        with patch("vision.sessions.session_history", return_value=hist), \
             patch("vision.brain.subprocess.Popen") as popen, \
             patch("vision.codex.find_codex", return_value="codex"):
            brain2, _ = _switch_model(cfg, brain, "gpt-6-astra", voice_mode=False)
            popen.assert_not_called()
        transcript = brain2.handoff
        self.assertIn("first message", transcript)
        self.assertIn("x" * 40_000, transcript)
        brain3, _ = _switch_model(cfg, brain2, "gpt-5.6-sol", voice_mode=False)
        self.assertEqual(brain3.handoff, transcript)
        with patch("vision.brain.find_claude", return_value="claude"):
            brain4, _ = _switch_model(cfg, brain3, "opus", voice_mode=False)
        self.assertEqual(brain4.handoff, transcript)

    def test_codex_to_claude_carries_local_transcript(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "gpt-6-astra"
        with patch("vision.codex.find_codex", return_value="codex"):
            brain = CodexBrain(cfg.brain, session_id="t-old")
        with patch("vision.sessions.session_history", return_value=[{"role": "user", "text": "keep going"}]), \
             patch("vision.brain.find_claude", return_value="claude"), \
             patch("vision.brain.subprocess.Popen") as popen:
            brain2, detail = _switch_model(cfg, brain, "fable", voice_mode=False)
            popen.assert_not_called()
        self.assertIsInstance(brain2, Brain)
        self.assertIn("keep going", brain2.handoff)
        self.assertIn("new Claude conversation from the previous transcript", detail)

    def test_cross_provider_switch_falls_back_to_the_chats_own_transcript(self):
        """The outgoing session saved nothing (its only turn hit a usage limit, and the reply before it
        came from the conversation model): the chat's transcript is what gets carried."""
        from vision.cli import _switch_model
        from vision.config import Config

        chat = [{"role": "user", "text": "background voice in the dynamic island"},
                {"role": "assistant", "text": "on it, the worker's started"},
                {"role": "user", "text": "Darn"},
                {"role": "assistant", "text": "", "error": "usage limit"}]
        for session_id in ("t-empty", None):
            with self.subTest(session_id=session_id):
                cfg = Config()
                cfg.brain.model = "gpt-6-sol"
                with patch("vision.codex.find_codex", return_value="codex"):
                    brain = CodexBrain(cfg.brain, session_id=session_id)
                with patch("vision.sessions.session_history", return_value=[]), \
                     patch("vision.brain.find_claude", return_value="claude"), \
                     patch("vision.brain.subprocess.Popen") as popen:
                    brain2, detail = _switch_model(cfg, brain, "opus", voice_mode=False, history=chat)
                    popen.assert_not_called()
                self.assertIn("background voice in the dynamic island", brain2.handoff)
                self.assertIn("on it, the worker's started", brain2.handoff)
                self.assertIn("from the previous transcript", detail)

    def test_cross_provider_switch_prefers_the_saved_session_when_it_is_as_full(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        saved = [{"role": "user", "text": "fix the lock"}, {"role": "assistant", "text": "Read talker.py, patched it"}]
        shown = [{"role": "user", "text": "fix the lock"}, {"role": "assistant", "text": "patched"}]
        with patch("vision.sessions.session_history", return_value=saved), \
             patch("vision.codex.find_codex", return_value="codex"):
            brain2, _ = _switch_model(cfg, brain, "gpt-6-astra", voice_mode=False, history=shown)
        self.assertIn("Read talker.py, patched it", brain2.handoff)

    def test_claude_to_grok_carries_local_transcript_without_asking(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "opus"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")
        hist = [{"role": "user", "text": "add grok"}, {"role": "assistant", "text": "on it"}]
        with patch("vision.sessions.session_history", return_value=hist) as hist_fn, \
             patch("vision.brain.subprocess.Popen") as popen, \
             patch("vision.grok.find_grok", return_value="grok"):
            brain2, detail = _switch_model(cfg, brain, "grok-4.6", voice_mode=False)
            popen.assert_not_called()
            hist_fn.assert_called_once_with("claude", "s-old", limit=0, include_context=True)
        self.assertIsInstance(brain2, GrokBrain)
        self.assertIn("add grok", brain2.handoff)
        self.assertIn("new Grok conversation from the previous transcript", detail)

    def test_grok_to_codex_carries_local_transcript(self):
        from vision.cli import _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "grok-4.6"
        with patch("vision.grok.find_grok", return_value="grok"):
            brain = GrokBrain(cfg.brain, session_id="g-old")
        with patch("vision.sessions.session_history", return_value=[{"role": "user", "text": "keep going"}]), \
             patch("vision.codex.find_codex", return_value="codex"), \
             patch("vision.brain.subprocess.Popen") as popen:
            brain2, detail = _switch_model(cfg, brain, "gpt-6-astra", voice_mode=False)
            popen.assert_not_called()
        self.assertIsInstance(brain2, CodexBrain)
        self.assertIn("keep going", brain2.handoff)
        self.assertIn("new Codex conversation from the previous transcript", detail)

    def test_claude_fast_mode_moves_to_opus_and_model_switch_turns_it_off(self):
        from vision.cli import _set_fast, _switch_model
        from vision.config import Config

        cfg = Config()
        cfg.brain.model = "sonnet"
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(cfg.brain, session_id="s-old")

        same_brain, _ = _set_fast(cfg, brain, True, voice_mode=False)
        self.assertIs(same_brain, brain)
        self.assertEqual(cfg.brain.model, "opus")
        self.assertTrue(cfg.brain.fast)

        _, detail = _switch_model(cfg, brain, "sonnet", voice_mode=False, full=True)
        self.assertFalse(cfg.brain.fast)
        self.assertIn("fast mode off", detail)


class ModeTests(unittest.TestCase):
    """Auto mode runs everything unasked; plan mode is read-only until the plan is approved."""

    def _brain(self, mode):
        with patch("vision.brain.find_claude", return_value="claude"):
            return Brain(BrainConfig(model="opus", mode=mode))

    def test_mode_flags_and_codex_sandbox(self):
        from vision.codex import sandbox_for

        self.assertIn("--dangerously-skip-permissions", self._brain("auto")._command())
        cmd = self._brain("plan")._command()
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(sandbox_for(BrainConfig(mode="plan")), "read-only")
        self.assertEqual(sandbox_for(BrainConfig(mode="auto")), "danger-full-access")
        self.assertEqual(sandbox_for(BrainConfig(mode="auto", codex=CodexConfig(sandbox="workspace-write"))), "workspace-write")
        from vision.grok import sandbox_for as grok_sandbox

        self.assertEqual(grok_sandbox(BrainConfig(mode="plan", grok=GrokConfig())), "read-only")
        self.assertEqual(grok_sandbox(BrainConfig(mode="auto", grok=GrokConfig(sandbox="workspace"))), "workspace")

    def test_claude_fast_mode_is_session_scoped(self):
        brain = self._brain("auto")
        brain.cfg.fast = True
        cmd = brain._command()
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings, {"fastMode": True})

    def test_unknown_mode_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            for written, read in (('"yolo"', "auto"), ('"Plan"', "plan"), ('" AUTO "', "auto")):
                path.write_text(f"[brain]\nmode = {written}\n")
                with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                    self.assertEqual(load_config().brain.mode, read)

    def _plan_turn(self, answer):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            {"type": "control_request", "request_id": "r1", "request": {"subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {"plan": "# Plan\n1. do it"}}},
            {"type": "control_request", "request_id": "r2", "request": {"subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": "x"}}},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "done"},
        ]
        brain = self._brain("plan")
        proc = _Process(events)
        asked = []

        def on_question(questions):
            asked.append(questions[0]["question"])
            return {questions[0]["question"]: answer} if answer else None

        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_question=on_question)
        replies = [json.loads(l)["response"]["response"] for l in proc.stdin.value.splitlines() if '"control_response"' in l]
        return brain, turn, asked, replies

    def test_approving_the_plan_runs_the_rest_of_the_turn_in_auto_mode(self):
        brain, turn, asked, replies = self._plan_turn("Yes")
        self.assertEqual(asked, ["Carry out this plan?"])
        self.assertEqual([r["behavior"] for r in replies], ["allow", "allow"])
        self.assertTrue(turn.plan_approved)
        self.assertEqual(brain.cfg.mode, "auto")
        self.assertIn("# Plan", turn.text)  # the plan is shown in the reply

    def test_declining_keeps_plan_mode_and_passes_feedback(self):
        brain, turn, _, replies = self._plan_turn("use tabs")
        self.assertEqual([r["behavior"] for r in replies], ["deny", "deny"])
        self.assertIn("use tabs", replies[0]["message"])
        self.assertIn("plan mode", replies[1]["message"])
        self.assertFalse(turn.plan_approved)
        self.assertEqual(brain.cfg.mode, "plan")


class SubagentTests(unittest.TestCase):
    """The Agent tool's nested Claude is visible in the stream as messages tagged with its
    parent_tool_use_id: Vision shows its tool calls live and keeps its text out of the reply."""

    AGENT = "toolu_agent"

    def _brain(self):
        with patch("vision.brain.find_claude", return_value="claude"):
            return Brain(BrainConfig(model="opus"))

    @staticmethod
    def _tool_start(tid, name):
        return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}}, "parent_tool_use_id": None}

    @staticmethod
    def _tool_result(tid, parent=None, text="ok"):
        return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}, "parent_tool_use_id": parent}

    def _run(self, events):
        brain = self._brain()
        statuses, updates = [], []
        with patch("subprocess.Popen", return_value=_Process(events)), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_status=statuses.append, on_agent=lambda r: updates.append((r.kind, len(r.steps), r.done)))
        return turn, statuses, updates

    def test_status_says_thinking_only_while_a_thinking_block_streams(self):
        def block_start(kind):
            return {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": kind}}, "parent_tool_use_id": None}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            block_start("thinking"),
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}, "parent_tool_use_id": None},
            self._tool_start("toolu_1", "Bash"),
            self._tool_result("toolu_1"),
            block_start("redacted_thinking"),
            block_start("text"),
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}}, "parent_tool_use_id": None},
            {"type": "result", "session_id": "s1", "result": "done"},
        ]
        turn, statuses, _ = self._run(events)
        self.assertEqual(turn.text, "done")
        self.assertEqual(statuses, ["thinking", "Bash", "reading", "thinking"])

    def test_task_tools_build_the_checklist_and_it_lasts_the_session(self):
        from vision.checklist import checklist_of

        def use(tid, name, args):
            return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}, "parent_tool_use_id": None}

        def result(tid, structured, text="ok"):
            return {**self._tool_result(tid, text=text), "tool_use_result": structured}

        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start("t1", "TaskCreate"), use("t1", "TaskCreate", {"subject": "Add backups", "activeForm": "Adding backups"}),
            result("t1", {"task": {"id": "1", "subject": "Add backups"}}),
            self._tool_start("t2", "TaskCreate"), use("t2", "TaskCreate", {"subject": "Write docs"}),
            result("t2", None, text="Task #2 created successfully: Write docs"),
            self._tool_start("t3", "TaskUpdate"), use("t3", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
            result("t3", {"success": True}),
            {"type": "result", "session_id": "s1", "result": "done"},
        ]
        brain = self._brain()
        with patch("subprocess.Popen", return_value=_Process(events)), patch("vision.brain.Brain._remember_session"):
            brain.ask("go")
        tasks = checklist_of(brain).items()
        self.assertEqual([(t.text, t.active, t.status) for t in tasks], [("Add backups", "Adding backups", "in_progress"), ("Write docs", "", "pending")])
        brain.session_id = "s2"  # another conversation: its own list
        self.assertEqual(checklist_of(brain).items(), [])

    def test_subagent_steps_are_tracked_and_its_text_stays_out_of_the_reply(self):
        A = self.AGENT
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "Find the config loader", "subagent_type": "Explore"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Find where config is loaded"}]}, "parent_tool_use_id": A, "subagent_type": "Explore"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_c1", "name": "Grep", "input": {"pattern": "load_config", "path": "/home/x/code/project"}}]}, "parent_tool_use_id": A},
            self._tool_result("toolu_c1", parent=A, text="vision/config.py:12"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_c2", "name": "Read", "input": {"file_path": "/home/x/code/project/vision/config.py"}}]}, "parent_tool_use_id": A},
            self._tool_result("toolu_c2", parent=A, text="..."),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "CHILD REPORT: config.py"}]}, "parent_tool_use_id": A},
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": "CHILD REPORT: config.py", "usage": {"tool_uses": 2, "duration_ms": 2300}},
            self._tool_result(A, text="CHILD REPORT: config.py"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "It is loaded in config.py."}]}, "parent_tool_use_id": None},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "It is loaded in config.py."},
        ]
        turn, statuses, updates = self._run(events)
        self.assertEqual(turn.text, "It is loaded in config.py.")
        self.assertNotIn("CHILD", turn.text)
        self.assertEqual(turn.tools_used, ["Agent"])  # the child's tools are its own
        self.assertEqual(statuses, ["Agent", "reading"])  # the child's results do not end "using Agent…"
        self.assertEqual(len(turn.agents), 1)
        run = turn.agents[0]
        self.assertEqual((run.kind, run.label, run.done, run.failed), ("Explore", "Find the config loader", True, False))
        self.assertEqual(run.steps, [("Grep", "load_config  in /home/x/code/project"), ("Read", "/home/x/code/project/vision/config.py")])
        self.assertEqual(run.status, "2 tools · 2.3s")
        self.assertEqual(run.summary, "CHILD REPORT: config.py")
        self.assertEqual(updates, [("Explore", 0, False), ("Explore", 1, False), ("Explore", 2, False), ("Explore", 2, True)])

    def test_an_agent_woken_from_an_earlier_turn_gets_one_row_that_its_report_closes(self):
        # SendMessage to an agent spawned in an earlier turn: its task_started comes on the
        # SendMessage call's id, its steps under the old Agent call's id (unknown this turn), and its
        # task_notification on SendMessage's again. One row, closed by the report.
        S, OLD = "toolu_send", "toolu_agent_last_turn"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(S, "SendMessage"),
            {"type": "system", "subtype": "task_started", "task_id": "a9", "tool_use_id": S, "description": "brain-failover feature", "subagent_type": "fork"},
            self._tool_result(S, text="Resuming agent a9"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_c1", "name": "Read", "input": {"file_path": "/x/a.py"}}]},
             "parent_tool_use_id": OLD, "task_description": "brain-failover feature", "subagent_type": "fork"},
            self._tool_result("toolu_c1", parent=OLD, text="..."),
            {"type": "system", "subtype": "task_notification", "task_id": "a9", "tool_use_id": S, "status": "completed", "summary": "Done: branch brain-failover", "usage": {"tool_uses": 1, "duration_ms": 900}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Landed."}]}, "parent_tool_use_id": None},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Landed."},
        ]
        turn, _, updates = self._run(events)
        self.assertEqual(len(turn.agents), 1)
        run = turn.agents[0]
        self.assertEqual((run.kind, run.label, run.done, run.failed, run.summary), ("fork", "brain-failover feature", True, False, "Done: branch brain-failover"))
        self.assertEqual(run.steps, [("Read", "/x/a.py")])
        self.assertTrue(updates[-1][2])  # the last update says it is done: nothing left to hold the turn open

    def test_a_woken_agent_that_reports_straight_away_still_gets_its_row(self):
        S = "toolu_send"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(S, "SendMessage"),
            {"type": "system", "subtype": "task_started", "task_id": "a9", "tool_use_id": S, "description": "ask --json", "subagent_type": "fork"},
            self._tool_result(S, text="Resuming agent a9"),
            {"type": "system", "subtype": "task_notification", "task_id": "a9", "tool_use_id": S, "status": "completed", "summary": "Already done.", "usage": {"tool_uses": 0, "duration_ms": 10}},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "ok"},
        ]
        turn, _, _ = self._run(events)
        self.assertEqual([(r.label, r.done, r.summary) for r in turn.agents], [("ask --json", True, "Already done.")])

    def test_subagent_row_shows_the_model_override_or_the_inherited_one_and_our_effort(self):
        A, B = self.AGENT, "toolu_agent2"
        def launch(tid, **model):
            return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Agent", "input": {"description": "d", "prompt": "p", **model}}]}, "parent_tool_use_id": None}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            launch(A, model="sonnet"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "Named model", "subagent_type": "general-purpose"},
            self._tool_start(B, "Agent"),
            launch(B),
            {"type": "system", "subtype": "task_started", "task_id": "t2", "tool_use_id": B, "description": "Inherited model", "subagent_type": "Explore"},
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": "a", "usage": {"tool_uses": 1, "duration_ms": 1000}},
            {"type": "system", "subtype": "task_notification", "task_id": "t2", "tool_use_id": B, "status": "completed", "summary": "b", "usage": {"tool_uses": 1, "duration_ms": 1000}},
            self._tool_result(A, text="a"),
            self._tool_result(B, text="b"),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "done"},
        ]
        turn, _, _ = self._run(events)  # the brain runs opus at BrainConfig's default effort, medium
        self.assertEqual([(r.label, r.model, r.effort) for r in turn.agents], [("Named model", "sonnet", "medium"), ("Inherited model", "opus", "medium")])
        self.assertEqual([r.prompt for r in turn.agents], ["p", "p"])

    def test_workflow_agents_get_rows_from_task_progress(self):
        # Ultracode: a Workflow runs its agents in the background, so none of their messages reach the
        # stream; Claude Code reports them as task_progress events on the Workflow call instead.
        W = "toolu_wf"
        script = "export const meta = {\n  name: 'review',\n  description: 'Review the diff',\n}\n"
        def progress(*agents):
            return {"type": "system", "subtype": "task_progress", "task_id": "w1", "tool_use_id": W, "description": "Review",
                    "workflow_progress": [{"type": "workflow_phase", "index": 1, "title": "Review"}, *agents]}
        a0 = {"type": "workflow_agent", "index": 0, "label": "review:bugs", "phaseTitle": "Review", "state": "start"}
        a1 = {"type": "workflow_agent", "index": 1, "label": "review:perf", "phaseTitle": "Review", "state": "start"}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(W, "Workflow"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": W, "name": "Workflow", "input": {"script": script}}]}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": "w1", "tool_use_id": W, "description": "Review the diff", "task_type": "local_workflow"},
            self._tool_result(W, text="Workflow launched in background."),
            progress(a0, a1),
            progress({**a0, "state": "progress", "toolCalls": 1, "lastToolName": "Read", "lastToolSummary": "vision/brain.py"}, a1),
            progress({**a0, "state": "done", "toolCalls": 2, "startedAt": 1000, "lastProgressAt": 3500, "resultPreview": "2 bugs"}, a1),
            progress({**a0, "state": "done", "toolCalls": 2, "startedAt": 1000, "lastProgressAt": 3500, "resultPreview": "2 bugs"}, a1),  # nothing new
            {"type": "system", "subtype": "task_notification", "task_id": "w1", "tool_use_id": W, "status": "failed", "summary": "Review failed"},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Found two bugs."},
        ]
        turn, _, updates = self._run(events)
        self.assertEqual([c.detail for c in turn.tools], ["Review the diff"])
        self.assertEqual([(r.kind, r.label) for r in turn.agents], [("Review", "review:bugs"), ("Review", "review:perf")])
        bugs, perf = turn.agents
        self.assertEqual((bugs.done, bugs.failed, bugs.status, bugs.summary, bugs.steps), (True, False, "2 tools · 2.5s", "2 bugs", [("Read", "vision/brain.py")]))
        self.assertEqual((perf.done, perf.failed, perf.cut_off), (True, True, True))  # never reported done: the workflow's end closes it
        self.assertEqual(updates, [("Review", 0, False), ("Review", 0, False), ("Review", 1, False), ("Review", 1, True), ("Review", 0, True)])

    @staticmethod
    def _text(text):
        return [
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}, "parent_tool_use_id": None},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}, "parent_tool_use_id": None},
        ]

    def test_a_turn_is_held_open_while_its_workflow_runs(self):
        # The reply ends (a `result`) while the workflow still runs in the background. Closing stdin then
        # would end claude and kill the workflow, so the turn waits for its task_notification and for
        # Claude's answer to it, and only closes stdin after that second `result`.
        W = "toolu_wf"
        now_ms = time.time() * 1000
        a0 = {"type": "workflow_agent", "index": 0, "label": "probe:codex", "phaseTitle": "Probe", "model": "claude-fable-5-1",
              "state": "progress", "startedAt": now_ms - 90_000, "toolCalls": 3, "tokens": 1200}
        progress = lambda a: {"type": "system", "subtype": "task_progress", "task_id": "w1", "tool_use_id": W, "workflow_progress": [a]}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(W, "Workflow"),
            {"type": "system", "subtype": "task_started", "task_id": "w1", "tool_use_id": W, "description": "Probe", "task_type": "local_workflow"},
            self._tool_result(W, text="Workflow launched in background."),
            *self._text("It's running."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "It's running.", "usage": {"output_tokens": 10}, "total_cost_usd": 0.5},
            progress(a0),
            progress({**a0, "state": "done", "toolCalls": 18, "tokens": 70212, "durationMs": 381720}),
            {"type": "system", "subtype": "task_notification", "task_id": "w1", "tool_use_id": W, "status": "completed"},
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            *self._text("All done."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "All done.", "usage": {"output_tokens": 5}, "total_cost_usd": 0.75},  # counts up over the process
        ]
        proc = _Process(events)
        closed_at = []
        proc.stdin.close = lambda: closed_at.append(proc.stdout.tell())
        brain = self._brain()
        brain.cfg.keep_alive_s = 0  # this is about when stdin closes: a process that is not kept
        statuses, frames = [], []
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_status=statuses.append, on_agent=lambda r: frames.append(agent_frame(r)))
        self.assertEqual(closed_at, [len(proc.stdout.getvalue())])  # only after the last result
        self.assertEqual(turn.text, "It's running.\n\nAll done.")
        self.assertIn("1 agent still working…", statuses)
        self.assertEqual((turn.usage, turn.cost_usd), ({"output_tokens": 15}, 0.75))
        run = turn.agents[0]
        self.assertEqual((run.done, run.failed, run.tokens, run.duration_ms, run.status), (True, False, 70212, 381720, "18 tools · 6m 21s · ↓70,212"))
        self.assertAlmostEqual(frames[0]["started"], now_ms / 1000 - 90, delta=2)  # the agent's own start, wall clock

    def _held_shell_run(self, launch, ended):
        """A reply that starts `launch` (a Bash or Monitor tool_use block) and ends while it runs, then
        `ended` (its task_notification) and Claude's answer to it. Returns (streamed when stdin closed, text, statuses)."""
        tid = launch["id"]
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(tid, launch["name"]),
            {"type": "assistant", "message": {"role": "assistant", "content": [launch]}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": tid, "description": "job", "task_type": "local_bash", "is_backgrounded": True},
            self._tool_result(tid, text="Running in the background."),
            *self._text("I'll let you know when it's done."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "I'll let you know when it's done."},
            ended,
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            *self._text("Finished."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Finished."},
        ]
        proc = _Process(events)
        sent, closed_at = [], []
        proc.stdin.close = lambda: closed_at.append("".join(sent))  # what had streamed when stdin closed
        brain = self._brain()
        brain.cfg.keep_alive_s = 0
        statuses = []
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_text=sent.append, on_status=statuses.append)
        return closed_at, turn.text, statuses

    def test_a_background_agent_resumed_by_sendmessage_still_closes_its_row_and_the_turn(self):
        # A resumed subagent can report under the SendMessage call's id; that id must stay associated
        # with the agent so its row closes and the reply can finish.
        A, S = self.AGENT, "toolu_send"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "Build the UI", "subagent_type": "general-purpose"},
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "stopped"},
            self._tool_start(S, "SendMessage"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": S, "name": "SendMessage", "input": {"to": "t1", "message": "carry on"}}]}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": S, "description": "Build the UI"},
            self._tool_result(S, text="Resuming agent"),
            *self._text("It's back at it."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "It's back at it."},
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": S, "status": "completed", "summary": "Built it"},
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            *self._text("Done."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Done."},
        ]
        proc = _Process(events)
        brain = self._brain()
        brain.cfg.keep_alive_s = 0
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go")
        self.assertEqual(len(turn.agents), 1)  # SendMessage's id is not a second agent
        run = turn.agents[0]
        self.assertEqual((run.done, run.failed, run.summary), (True, False, "Built it"))
        self.assertEqual(turn.text, "It's back at it.\n\nDone.")

    def _run_events(self, events):
        proc = _Process(events)
        sent, closed_at = [], []
        proc.stdin.close = lambda: closed_at.append("".join(sent))
        brain = self._brain()
        brain.cfg.keep_alive_s = 0
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_text=sent.append)
        return turn, closed_at

    def test_a_background_command_that_ends_before_the_reply_does_still_gets_its_answer(self):
        # A background task can finish before the reply's first `result`; Claude Code answers its notice
        # in a new sub-turn, so the reply must remain open for that answer.
        bash = {"type": "tool_use", "id": "toolu_bg", "name": "Bash", "input": {"command": "./poll.sh", "run_in_background": True}}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start("toolu_bg", "Bash"),
            {"type": "assistant", "message": {"role": "assistant", "content": [bash]}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "toolu_bg", "description": "poll", "task_type": "local_bash", "is_backgrounded": True},
            self._tool_result("toolu_bg", text="Command running in background."),
            {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "toolu_bg", "status": "completed", "summary": "poll"},
            *self._text("Polling it."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Polling it."},
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            *self._text("It finished: both commits landed."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "It finished: both commits landed."},
        ]
        turn, closed_at = self._run_events(events)
        self.assertEqual(turn.text, "Polling it.\n\nIt finished: both commits landed.")
        self.assertEqual(closed_at, [turn.text])  # stdin stayed open until the answer to the notice

    @staticmethod
    def _agent(tid, n):
        return [
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": tid, "name": "Agent", "input": {}}}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": f"t{n}", "tool_use_id": tid, "description": f"wait {n}", "subagent_type": "general-purpose", "is_backgrounded": True, "task_type": "local_agent"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "Async agent launched successfully."}]}, "parent_tool_use_id": None},
        ]

    @staticmethod
    def _report(tid, n):
        return {"type": "system", "subtype": "task_notification", "task_id": f"t{n}", "tool_use_id": tid, "status": "completed", "summary": f"Agent {n} done.", "usage": {"tool_uses": 1, "duration_ms": 14000}}

    # Taking queued notices in after a reply can emit an `init` and blank `result` before the sub-turn
    # that answers them.
    INIT = {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}
    BLANK = {"type": "result", "subtype": "success", "session_id": "s1", "result": "", "num_turns": 0, "duration_api_ms": 0, "stop_reason": None, "duration_ms": 4}

    def test_a_blank_result_while_notices_are_queued_does_not_end_the_reply(self):
        # Several agents can report while Claude answers the first. Taking the queued notices in may emit
        # an init and blank result before the next answer, which must not end the reply.
        A, B, C = "toolu_a1", "toolu_a2", "toolu_a3"
        events = [
            self.INIT,
            *self._agent(A, 1), *self._agent(B, 2), *self._agent(C, 3),
            *self._text("All three are running."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "All three are running.", "num_turns": 4},
            self._report(A, 1),
            self.INIT,
            *self._text("Agent 1 finished. Two still running."),
            self._report(B, 2),
            self._report(C, 3),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Agent 1 finished. Two still running.", "num_turns": 1},
            self.INIT,
            self.BLANK,
            self.INIT,
            *self._text("Agents 2 and 3 are done too."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Agents 2 and 3 are done too.", "num_turns": 1},
        ]
        started = time.monotonic()
        turn, closed_at = self._run_events(events)
        self.assertEqual(turn.text, "All three are running.\n\nAgent 1 finished. Two still running.\n\nAgents 2 and 3 are done too.")
        self.assertEqual(closed_at, [turn.text])  # stdin stayed open until the answer to the second and third
        self.assertLess(time.monotonic() - started, 5)  # and closed at that answer, not after a grace period
        self.assertTrue(all(r.done and not r.cut_off for r in turn.agents))

    def test_a_blank_result_after_every_agent_reported_at_once_does_not_end_the_reply(self):
        # Both reports land while Claude is idle (nothing owed by the old reckoning); the init and blank
        # result that follow would have ended the turn before the one sub-turn that answers both.
        A, B = "toolu_a1", "toolu_a2"
        events = [
            self.INIT,
            *self._agent(A, 1), *self._agent(B, 2),
            *self._text("Both running."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Both running.", "num_turns": 3},
            self._report(A, 1),
            self._report(B, 2),
            self.INIT,
            self.BLANK,
            self.INIT,
            *self._text("Both done."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Both done.", "num_turns": 1},
        ]
        turn, closed_at = self._run_events(events)
        self.assertEqual(turn.text, "Both running.\n\nBoth done.")
        self.assertEqual(closed_at, [turn.text])

    def test_a_foreground_agents_report_does_not_hold_the_reply(self):
        A = self.AGENT
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "look", "subagent_type": "Explore"},
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": "found it"},
            self._tool_result(A, text="found it"),
            *self._text("Found it."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Found it."},
        ]
        started = time.monotonic()
        turn, _ = self._run_events(events)
        self.assertEqual(turn.text, "Found it.")
        self.assertLess(time.monotonic() - started, 5)  # no notice owed: no grace wait

    def test_a_turn_is_held_open_while_its_background_command_runs(self):
        # "I'll let you know": closing stdin at the first result would end claude and kill the command,
        # so the turn waits for it and for the answer to it.
        bash = {"type": "tool_use", "id": "toolu_bg", "name": "Bash", "input": {"command": "./run.sh", "run_in_background": True}}
        done = {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "toolu_bg", "status": "completed", "summary": "done"}
        closed_at, text, statuses = self._held_shell_run(bash, done)
        self.assertEqual(closed_at, [text])  # only after the answer to it
        self.assertEqual(text, "I'll let you know when it's done.\n\nFinished.")
        self.assertIn("1 background job still running…", statuses)

    def test_a_turn_is_held_open_while_its_monitor_watches(self):
        monitor = {"type": "tool_use", "id": "toolu_mon", "name": "Monitor", "input": {"command": "tail -f log"}}
        killed = {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "toolu_mon", "status": "killed", "summary": "stopped"}
        closed_at, text, _ = self._held_shell_run(monitor, killed)
        self.assertEqual(closed_at, [text])
        self.assertTrue(text.endswith("Finished."))

    def test_a_turn_is_not_held_for_a_background_command_that_failed_to_start(self):
        bash = {"type": "tool_use", "id": "toolu_bg", "name": "Bash", "input": {"command": "x", "run_in_background": True}}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start("toolu_bg", "Bash"),
            {"type": "assistant", "message": {"role": "assistant", "content": [bash]}, "parent_tool_use_id": None},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_bg", "content": "denied", "is_error": True}]}, "parent_tool_use_id": None},
            *self._text("Couldn't start it."),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "Couldn't start it."},
            *self._text("never read"),
        ]
        proc = _Process(events)
        sent, closed_at = [], []
        proc.stdin.close = lambda: closed_at.append("".join(sent))
        brain = self._brain()
        brain.cfg.keep_alive_s = 0
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            brain.ask("go", on_text=sent.append)
        self.assertEqual(closed_at, ["Couldn't start it."])

    def test_a_turn_with_nothing_in_the_background_closes_at_its_result(self):
        events = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}, *self._text("Hi."),
                  {"type": "result", "subtype": "success", "session_id": "s1", "result": "Hi."}, *self._text("never read")]
        proc = _Process(events)
        sent, closed_at = [], []
        proc.stdin.close = lambda: closed_at.append("".join(sent))  # what had streamed when stdin closed
        brain = self._brain()
        brain.cfg.keep_alive_s = 0  # not kept: stdin closes at the result
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            brain.ask("go", on_text=sent.append)
        self.assertEqual(closed_at, ["Hi."])  # at the result, not at the end of the stream

    def test_steer_sends_a_message_into_the_running_turn(self):
        brain = self._brain()
        self.assertFalse(brain.steer("too early"))  # nothing running: the caller queues it instead
        events = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}, *self._text("Working."),
                  {"type": "result", "subtype": "success", "session_id": "s1", "result": "Working."}]
        proc = _Process(events)
        proc.returncode = None  # still running while the reply streams
        proc.wait = lambda timeout=None: 0
        closes = []
        proc.stdin.close = lambda: closes.append(True)
        sent = []
        with patch("subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            brain.ask("go", on_text=lambda d: sent.append(brain.steer("use the other file")) if d == "Working." else None)
        self.assertEqual(sent, [True])
        self.assertIn('"content": "use the other file"', proc.stdin.value)
        self.assertEqual(closes, [])  # a steered reply waits a moment for the queued message instead of closing at once
        self.assertFalse(brain.steer("after the turn"))

    def test_a_task_worker_takes_an_update_and_its_last_result_stands(self):
        brain = self._brain()
        brain.task_mode = True
        first = {"status": "completed", "summary": "Ran the tests."}
        second = {"status": "completed", "summary": "Stopped before the tests, as asked."}
        events = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}, *self._text("x"),
                  {"type": "result", "subtype": "success", "session_id": "s1", "result": "", "structured_output": first},
                  {"type": "result", "subtype": "success", "session_id": "s1", "result": "", "structured_output": second}]
        proc = _Process(events)
        proc.returncode = None
        proc.wait = lambda timeout=None: 0
        sent = []
        with patch("subprocess.Popen", return_value=proc):
            turn = brain.ask("task", on_text=lambda d: sent.append(brain.steer('{"type": "vision_task_update"}')))
        self.assertEqual(sent, [True])
        self.assertIn("vision_task_update", proc.stdin.value)
        self.assertEqual(turn.data, second)

    def test_a_subagents_bash_task_is_not_a_second_agent_row(self):
        # A Bash command that runs a few seconds inside a subagent gets task events of its own
        # (task_type local_bash), which must not open an `agent · <description> · 0 tools · 0.0s` row.
        A = self.AGENT
        bash = {"type": "tool_use", "id": "toolu_c1", "name": "Bash", "input": {"command": "du -sh ~", "description": "Measure home"}}
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "Find largest folder", "subagent_type": "general-purpose", "task_type": "local_agent", "is_backgrounded": True},
            {"type": "assistant", "message": {"role": "assistant", "content": [bash]}, "parent_tool_use_id": A, "subagent_type": "general-purpose", "task_description": "Find largest folder"},
            {"type": "system", "subtype": "task_started", "task_id": "b1", "owned_by_subagent": True, "tool_use_id": "toolu_c1", "description": "Measure home", "is_backgrounded": False, "task_type": "local_bash"},
            {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "toolu_c1", "status": "completed", "output_file": "", "summary": "Measure home"},
            self._tool_result("toolu_c1", parent=A, text="129G"),
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": ".var, 129G", "usage": {"tool_uses": 1, "duration_ms": 4100}},
            self._tool_result(A, text=".var, 129G"),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "It is .var."},
        ]
        turn, _, updates = self._run(events)
        self.assertEqual([(r.kind, r.label, r.status) for r in turn.agents], [("general-purpose", "Find largest folder", "1 tool · 4.1s")])
        self.assertGreater(turn.agents[0].started, 0)  # the row's live timer runs while it works
        self.assertEqual(updates, [("general-purpose", 0, False), ("general-purpose", 1, False), ("general-purpose", 1, True)])

    def test_a_subagents_bash_task_is_recognised_by_its_tool_id_when_untagged(self):
        # Older Claude Code: no task_type on the events. The child's tool_use came first, so its id tells.
        A = self.AGENT
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start(A, "Agent"),
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "Find largest folder", "subagent_type": "Explore"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_c1", "name": "Bash", "input": {"command": "du -sh ~", "description": "Measure home"}}]}, "parent_tool_use_id": A},
            {"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "toolu_c1", "description": "Measure home"},
            {"type": "system", "subtype": "task_notification", "task_id": "b1", "tool_use_id": "toolu_c1", "status": "completed", "summary": "Measure home"},
            self._tool_result("toolu_c1", parent=A, text="129G"),
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": "ok", "usage": {"tool_uses": 1, "duration_ms": 4100}},
            self._tool_result(A, text="ok"),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "done"},
        ]
        turn, _, _ = self._run(events)
        self.assertEqual([(r.kind, r.label) for r in turn.agents], [("Explore", "Find largest folder")])

    def test_parallel_tool_calls_keep_the_status_until_the_last_one_returns(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            self._tool_start("a", "Read"),
            self._tool_start("b", "Grep"),
            self._tool_result("a"),
            self._tool_result("b"),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "done"},
        ]
        _, statuses, _ = self._run(events)
        self.assertEqual(statuses, ["Read", "Grep", "reading"])

    def test_activity_block_renders_running_and_finished_agents(self):
        from rich.console import Console

        from vision.brain import AgentRun
        from vision.ui import agent_activity

        running = AgentRun("x", "Explore", "Map the tests", steps=[("Bash", "ls tests"), ("Read", "tests/t0.py")])
        done = AgentRun("y", "Plan", "Design the change", steps=[("Read", "a.py")], done=True, tool_uses=1, duration_ms=1500)
        console = Console(width=60, force_terminal=False, file=io.StringIO())
        console.print(agent_activity([running, done], "⠋"))
        out = console.file.getvalue()
        lines = [ln.rstrip() for ln in out.splitlines()]
        # a running agent lists its latest tool calls over its status; a done one is a single row
        self.assertEqual(lines[:5], ["⠋ Explore Map the tests", "  ⎿  Bash ls tests", "     Read tests/t0.py", "     running",
                                     "✓ Plan Design the change · 1 tool · 1.5s"])
        import time
        timed = AgentRun("t", "Explore", "Map the tests", model="opus", effort="high", started=time.monotonic() - 12.4)
        console = Console(width=60, force_terminal=False, file=io.StringIO())
        console.print(agent_activity([timed], "⠋"))
        self.assertIn(f"  ⎿  running · {model_label('opus')} high · 12s", console.file.getvalue())  # a live timer from the moment it started
        native = AgentRun("z", "general-purpose", "Find largest folder", done=True, tool_uses=5, duration_ms=19400, model="opus", effort="high")
        failed = AgentRun("f", "Explore", "Nope", done=True, failed=True, tool_uses=2, duration_ms=800)
        console = Console(width=80, force_terminal=False, file=io.StringIO())
        console.print(agent_activity([native, failed]))
        self.assertIn(f"✓ general-purpose Find largest folder · {model_label('opus')} · 5 tools · 19.4s\n", console.file.getvalue())
        self.assertIn("✗ Explore Nope\n  ⎿  failed · 2 tools · 0.8s", console.file.getvalue())
        self.assertNotIn("a.py", out)  # a done agent's calls wait for a click (or ctrl-o)

    def test_agent_rows_show_the_latest_calls_and_expand_to_all(self):
        from rich.console import Console

        from vision.brain import AgentRun
        from vision.ui import agent_activity

        def draw(*args, **kwargs):
            console = Console(width=60, force_terminal=False, file=io.StringIO())
            console.print(agent_activity(*args, **kwargs))
            return [ln.rstrip() for ln in console.file.getvalue().splitlines()]

        steps = [("Read", f"f{i}.py") for i in range(6)]
        busy = AgentRun("x", "Explore", "Read it all", steps=steps)
        self.assertEqual(draw([busy], "⠋"), ["⠋ Explore Read it all", "  ⎿  +3 more tool uses", "     Read f3.py",
                                              "     Read f4.py", "     Read f5.py", "     running"])
        self.assertEqual(len(draw([busy], "⠋", expanded={"x"})), 1 + 6 + 1)  # clicked: every call
        done = AgentRun("y", "Plan", "Design", steps=steps, done=True, tool_uses=6, duration_ms=1000)
        self.assertEqual(draw([done]), ["✓ Plan Design · 6 tools · 1.0s"])
        self.assertIn("  ⎿  Read f0.py", draw([done], full=True))  # the reply unfolded with ctrl-o


class _LiveProcess:
    """A claude that stays up between turns: each user message written to its stdin plays the next
    scripted list of events on its stdout, which blocks in between like a pipe does."""

    def __init__(self, turns):
        import queue as _queue

        self.turns = [list(t) for t in turns]
        self.q = _queue.Queue()
        self.messages: list[str] = []
        self.returncode = None
        self.stdin = _Input()
        self.stdin.write = self._write
        self.stdin.close = self._close
        self.stdout = self
        self.stderr = io.StringIO("")

    def _write(self, value):
        self.stdin.value += value
        for line in value.splitlines():
            msg = json.loads(line)
            if msg.get("type") == "user":
                self.messages.append(msg["message"]["content"])
                for ev in (self.turns.pop(0) if self.turns else []):
                    self.q.put(json.dumps(ev) + "\n")

    def _close(self):
        self.stdin.closed = True
        self.returncode = 0
        self.q.put(None)

    def __iter__(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.q.put(None)

    def kill(self):
        self.returncode = -9
        self.q.put(None)


class KeepAliveTests(unittest.TestCase):
    """The Claude process stays between typed turns (keep_alive_s): the next turn skips its start-up,
    1.1 s warm and 2.2 s cold on claude 2.1.289, plus the transcript load that grows with the session."""

    @staticmethod
    def _events(text, sid="s1", cost=0.0):
        return [
            {"type": "system", "subtype": "init", "session_id": sid, "model": "claude-x"},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}, "parent_tool_use_id": None},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}, "parent_tool_use_id": None},
            {"type": "result", "subtype": "success", "session_id": sid, "result": text, "total_cost_usd": cost},
        ]

    def _brain(self, keep=600):
        with patch("vision.brain.find_claude", return_value="claude"):
            brain = Brain(BrainConfig(model="opus", keep_alive_s=keep))
        self.addCleanup(brain.close)
        return brain

    def test_the_next_turn_reuses_the_process_and_the_cost_is_the_turns_own(self):
        proc = _LiveProcess([self._events("One.", cost=0.5), self._events("Two.", cost=0.8)])
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", return_value=proc) as popen, patch("vision.brain.Brain._remember_session"):
            first = brain.ask("a")
            second = brain.ask("b")
        self.assertEqual((first.text, second.text), ("One.", "Two."))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(proc.messages, ["a", "b"])
        self.assertFalse(proc.stdin.closed)  # kept: its input stays open for the next turn
        self.assertEqual((brain.session_id, second.session_id, second.model), ("s1", "s1", "claude-x"))
        self.assertEqual(first.cost_usd, 0.5)
        self.assertAlmostEqual(second.cost_usd, 0.3)  # total_cost_usd counts up over the process
        self.assertFalse(brain.steer("between turns"))  # nothing running: the caller queues it, as before
        brain.close()
        self.assertTrue(proc.stdin.closed)

    def test_a_changed_command_line_or_session_starts_a_new_process(self):
        first, second, third = (_LiveProcess([self._events("One.")]), _LiveProcess([self._events("Two.")]),
                                _LiveProcess([self._events("Three.", sid="s2")]))
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", side_effect=[first, second, third]) as popen, \
             patch("vision.brain.Brain._remember_session"):
            brain.ask("a")
            brain.cfg.effort = "high"  # the command line changes: a new process, resuming the session
            brain.ask("b")
            self.assertEqual(popen.call_count, 2)
            cmd = popen.call_args.args[0]
            self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")
            self.assertTrue(first.stdin.closed)  # the old one was let go
            brain.new_session()  # /new: the kept process is on the old session
            self.assertTrue(second.stdin.closed)
            brain.ask("c")
            self.assertEqual(popen.call_count, 3)
            self.assertNotIn("--resume", popen.call_args.args[0])
            self.assertEqual(brain.session_id, "s2")

    def test_an_idle_process_is_closed_after_keep_alive_s(self):
        proc = _LiveProcess([self._events("One.")])
        brain = self._brain(keep=0.05)
        with patch("vision.brain.subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            brain.ask("a")
        for _ in range(200):
            if proc.stdin.closed:
                break
            time.sleep(0.01)
        self.assertTrue(proc.stdin.closed)
        self.assertIsNone(brain._live)

    def test_cancel_between_turns_only_closes_the_kept_process(self):
        proc, again = _LiveProcess([self._events("One.")]), _LiveProcess([self._events("Two.")])
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", side_effect=[proc, again]), patch("vision.brain.Brain._remember_session"):
            brain.ask("a")
            brain.cancel()  # Esc with nothing running (a voice turn's cancel reaches the typed brain too)
            self.assertTrue(proc.stdin.closed)
            turn = brain.ask("b")  # not a cancelled turn: a fresh process answers it
        self.assertEqual((turn.is_error, turn.text), (False, "Two."))

    def test_warm_starts_the_process_ahead_of_the_first_turn(self):
        """When a chat opens, the process boots while the first message is being typed; that first
        turn then goes to it like any later one."""
        proc = _LiveProcess([self._events("One.")])
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", return_value=proc) as popen, patch("vision.brain.Brain._remember_session"):
            brain.warm()
            self.assertEqual(popen.call_count, 1)
            brain.warm()  # one is there already
            self.assertEqual(popen.call_count, 1)
            turn = brain.ask("a")
        self.assertEqual((turn.text, popen.call_count, proc.messages), ("One.", 1, ["a"]))

    def test_a_steered_message_shows_when_claude_takes_it_not_when_sent(self):
        """Ctrl-X mid-reply: claude queues the message till its next step and echoes it back
        (--replay-user-messages) when it takes it in; only that echo reports it taken."""
        import threading

        first = self._events("Working.")[:-1]  # still going when the message is sent
        second = [{"type": "user", "isReplay": True, "message": {"role": "user", "content": "use blue"}},
                  {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " Blue it is."}}, "parent_tool_use_id": None},
                  {"type": "result", "subtype": "success", "session_id": "s1", "result": "Working. Blue it is."}]
        proc = _LiveProcess([first, second])
        brain = self._brain()
        log = []
        brain.on_steer_taken = lambda t: log.append(("taken", t))

        def on_text(d):
            log.append(("text", d))
            if d == "Working.":
                threading.Thread(target=lambda: log.append(("sent", brain.steer("use blue")))).start()

        with patch("vision.brain.subprocess.Popen", return_value=proc) as popen, patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("paint it", on_text=on_text)
        self.assertIn("--replay-user-messages", popen.call_args.args[0])
        self.assertEqual(log, [("text", "Working."), ("sent", True), ("taken", "use blue"), ("text", " Blue it is.")])
        self.assertEqual(json.loads(proc.stdin.value.splitlines()[-1]).keys() >= {"uuid"}, True)
        self.assertEqual(turn.text, "Working. Blue it is.")
        self.assertEqual(brain.take_untaken_steers(), [])

    def test_a_steered_message_never_taken_comes_back_and_the_process_is_dropped(self):
        import threading

        first = self._events("Done.")[:-1]
        second = [{"type": "result", "subtype": "success", "session_id": "s1", "result": "Done."}]
        proc = _LiveProcess([first, second])
        brain = self._brain()

        def on_text(d):
            threading.Thread(target=lambda: brain.steer("one more thing")).start()

        with patch("vision.brain.subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"), \
             patch("vision.brain.STEER_GRACE_S", 0.02):
            brain.ask("go", on_text=on_text)
        self.assertEqual(brain.take_untaken_steers(), ["one more thing"])
        self.assertTrue(proc.stdin.closed)  # claude still holds it: kept, it would run it twice

    def test_a_queued_steer_never_cuts_off_a_reply_still_streaming(self):
        """Queued steers wait for Claude to finish its current response; the grace period must not end
        a still-streaming turn or kill its background work."""
        import threading

        def delta(t):
            return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}, "parent_tool_use_id": None}

        first = self._events("Started.")
        proc = _LiveProcess([first, [], []])
        brain = self._brain()
        taken = []
        brain.on_steer_taken = taken.append

        def stream_story():
            for i in range(40):  # 0.4s of story, far past 6 quiet periods of 0.02s
                proc.q.put(json.dumps(delta(f" {i}")) + "\n")
                time.sleep(0.01)
            proc.q.put(json.dumps({"type": "result", "subtype": "success", "session_id": "s1", "result": "story"}) + "\n")
            proc.q.put(json.dumps({"type": "user", "isReplay": True, "message": {"role": "user", "content": "ty"}}) + "\n")
            proc.q.put(json.dumps(delta(" Any time.")) + "\n")
            proc.q.put(json.dumps({"type": "result", "subtype": "success", "session_id": "s1", "result": "Any time."}) + "\n")

        def on_text(d):
            if d == "Started.":
                self.assertTrue(brain.steer("ty"))  # before the first reply's result, as Ctrl-X mid-reply
                threading.Thread(target=stream_story).start()

        with patch("vision.brain.subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"), \
             patch("vision.brain.STEER_GRACE_S", 0.02):
            turn = brain.ask("go", on_text=on_text)
        self.assertEqual(taken, ["ty"])
        self.assertIn("Any time.", turn.text)
        self.assertEqual(brain.take_untaken_steers(), [])

    def test_a_blank_result_that_nothing_follows_still_ends_the_turn_after_the_grace_period(self):
        # The blank result keeps the turn waiting as a notice does, not for ever.
        A = "toolu_a1"
        first = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": A, "name": "Agent", "input": {}}}, "parent_tool_use_id": None},
            {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": A, "description": "wait", "task_type": "local_agent", "is_backgrounded": True},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": A, "content": "Async agent launched successfully."}]}, "parent_tool_use_id": None},
            *self._events("Running.")[1:],
            {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": A, "status": "completed", "summary": "done"},
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "", "num_turns": 0, "duration_api_ms": 0, "stop_reason": None},
        ]
        proc = _LiveProcess([first])
        brain = self._brain()
        started = time.monotonic()
        with patch("vision.brain.subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"), \
             patch("vision.brain.NOTIFICATION_GRACE_S", 0.05):
            turn = brain.ask("go")
        self.assertEqual(turn.text, "Running.")
        self.assertFalse(turn.is_error)
        self.assertLess(time.monotonic() - started, 3)

    def test_warm_does_nothing_when_the_process_is_not_kept_or_the_cli_is_missing(self):
        from vision.brain import BrainError

        with patch("vision.brain.subprocess.Popen") as popen:
            self._brain(keep=0).warm()
            worker = self._brain()
            worker.task_mode = True
            worker.warm()
            with patch("vision.brain.find_claude", side_effect=BrainError("not installed")):
                missing = Brain(BrainConfig(model="opus", keep_alive_s=600))
                missing.warm()  # the first turn says what is missing; warming stays quiet
        popen.assert_not_called()

    def test_an_unused_warm_process_is_closed_after_keep_alive_s(self):
        proc = _LiveProcess([])
        brain = self._brain(keep=0.05)
        with patch("vision.brain.subprocess.Popen", return_value=proc):
            brain.warm()
        for _ in range(200):
            if proc.stdin.closed:
                break
            time.sleep(0.01)
        self.assertTrue(proc.stdin.closed)
        self.assertIsNone(brain._live)

    def test_warm_replaces_an_idle_process_whose_command_line_changed(self):
        """/effort, /mode, /cd, /fast, a memory line: the next turn would restart the process; warm() does
        it now, resuming the session, so that turn finds the new one booted."""
        first, second = _LiveProcess([self._events("One.")]), _LiveProcess([self._events("Two.")])
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", side_effect=[first, second]) as popen, \
             patch("vision.brain.Brain._remember_session"):
            brain.ask("a")
            brain.warm()  # nothing changed: the kept process stays
            self.assertEqual(popen.call_count, 1)
            brain.cfg.effort = "high"
            brain.warm()
            self.assertEqual(popen.call_count, 2)
            cmd = popen.call_args.args[0]
            self.assertEqual((cmd[cmd.index("--effort") + 1], cmd[cmd.index("--resume") + 1]), ("high", "s1"))
            self.assertTrue(first.stdin.closed)  # the stale one was let go
            turn = brain.ask("b")
        self.assertEqual((turn.text, popen.call_count, second.messages), ("Two.", 2, ["b"]))
        self.assertFalse(second.stdin.closed)

    def test_warm_leaves_a_running_turn_alone_and_the_turns_end_replaces_the_process(self):
        """A change made during a reply (/cd says "from the next reply on"): the process answering stays;
        once the turn is over, the replacement for the new settings boots on its own."""
        import threading

        held = self._events("One.")
        result = held.pop()  # the turn stays open until its result arrives
        first, second = _LiveProcess([held]), _LiveProcess([self._events("Two.")])
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", side_effect=[first, second]) as popen, \
             patch("vision.brain.Brain._remember_session"):
            got = []
            t = threading.Thread(target=lambda: got.append(brain.ask("a")), daemon=True)
            t.start()
            for _ in range(500):
                if first.messages:
                    break
                time.sleep(0.01)
            brain.cfg.mode = "plan"
            brain.warm()  # a turn is running: nothing yet
            self.assertEqual(popen.call_count, 1)
            self.assertFalse(first.stdin.closed)
            first.q.put(json.dumps(result) + "\n")  # the reply ends
            t.join(timeout=5)
            self.assertEqual(got[0].text, "One.")
            for _ in range(500):  # the replacement boots on its own thread
                if popen.call_count == 2 and first.stdin.closed:
                    break
                time.sleep(0.01)
            self.assertEqual(popen.call_count, 2)
            self.assertTrue(first.stdin.closed)
            cmd = popen.call_args.args[0]
            self.assertEqual((cmd[cmd.index("--permission-mode") + 1], cmd[cmd.index("--resume") + 1]), ("plan", "s1"))
            turn = brain.ask("b")
        self.assertEqual((turn.text, popen.call_count, second.messages), ("Two.", 2, ["b"]))

    def test_warm_after_new_session_or_resume_starts_the_process_for_that_conversation(self):
        first, second, third = (_LiveProcess([self._events("One.")]), _LiveProcess([self._events("Two.", sid="s2")]),
                                _LiveProcess([]))
        brain = self._brain()
        with patch("vision.brain.subprocess.Popen", side_effect=[first, second, third]) as popen, \
             patch("vision.brain.Brain._remember_session"):
            brain.ask("a")
            brain.new_session()  # /new
            self.assertTrue(first.stdin.closed)
            brain.warm()
            self.assertEqual(popen.call_count, 2)
            self.assertNotIn("--resume", popen.call_args.args[0])
            turn = brain.ask("b")  # the new conversation's first message goes to the warm process
            self.assertEqual((turn.text, popen.call_count, brain.session_id), ("Two.", 2, "s2"))
            brain.resume("s7")  # /session
            self.assertTrue(second.stdin.closed)
            brain.warm()
            cmd = popen.call_args.args[0]
            self.assertEqual((popen.call_count, cmd[cmd.index("--resume") + 1]), (3, "s7"))

    def test_warm_soon_runs_warm_on_its_own_thread_and_skips_a_driver_without_one(self):
        from unittest.mock import Mock

        from vision.brain import warm_soon

        brain = Mock()
        t = warm_soon(brain)
        t.join(timeout=5)
        brain.warm.assert_called_once_with()
        self.assertIsNone(warm_soon(object()))  # Codex, Grok: nothing to start ahead

    def test_a_task_worker_is_never_kept(self):
        proc = _LiveProcess([self._events("One.")])
        brain = self._brain()
        brain.task_mode = True
        with patch("vision.brain.subprocess.Popen", return_value=proc), patch("vision.brain.Brain._remember_session"):
            brain.ask("task")
        self.assertTrue(proc.stdin.closed)  # one task, then gone


class ToolRowTests(unittest.TestCase):
    """The main conversation's tool calls are shown live in the reply, Claude Code style: the call
    when it starts, its detail once the input is whole, the first lines of the result when it is back."""

    def _brain(self):
        with patch("vision.brain.find_claude", return_value="claude"):
            return Brain(BrainConfig(model="opus"))

    def test_tool_calls_are_tracked_from_start_to_result(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"},
            SubagentTests._tool_start("t1", "Bash"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git status --short", "description": "Show working tree status"}}]}, "parent_tool_use_id": None},
            SubagentTests._tool_result("t1", text=" M a.py\n M b.py\n"),
            SubagentTests._tool_start("t2", "Read"),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/nope"}}]}, "parent_tool_use_id": None},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "File does not exist."}], "is_error": True}]}, "parent_tool_use_id": None},
            SubagentTests._tool_start("toolu_agent", "Agent"),
            {"type": "result", "subtype": "success", "session_id": "s1", "result": "done"},
        ]
        brain = self._brain()
        updates = []
        with patch("subprocess.Popen", return_value=_Process(events)), patch("vision.brain.Brain._remember_session"):
            turn = brain.ask("go", on_tool=lambda c: updates.append((c.name, c.detail, c.done, c.is_error)))
        self.assertEqual([c.name for c in turn.tools], ["Bash", "Read"])  # the Agent call is a subagent row, not a tool row
        bash, read = turn.tools
        self.assertEqual((bash.detail, bash.output, bash.done, bash.is_error), ("Show working tree status", " M a.py\n M b.py\n", True, False))
        self.assertEqual((read.detail, read.output, read.done, read.is_error), ("/nope", "File does not exist.", True, True))
        self.assertEqual(updates, [
            ("Bash", "", False, False), ("Bash", "Show working tree status", False, False), ("Bash", "Show working tree status", True, False),
            ("Read", "", False, False), ("Read", "/nope", False, False), ("Read", "/nope", True, True),
        ])
        # The phone's call page lists the input whole, the description first.
        self.assertEqual(bash.input, [["description", "Show working tree status"], ["command", "git status --short"]])
        self.assertEqual(read.input, [["file_path", "/nope"]])

    def test_tool_input_is_whole_capped_and_leaves_an_edit_body_to_its_diff(self):
        from vision.brain import INPUT_CHARS, ToolCall, tool_frame, tool_input

        command = "cd ~/x && \\\n  make " + "a" * (INPUT_CHARS + 50)
        rows = tool_input("Bash", {"command": command, "timeout": 60000, "run_in_background": True, "description": "Build"})
        self.assertEqual([k for k, _ in rows], ["description", "command", "timeout", "run_in_background"])
        self.assertEqual(rows[1][1][:20], command[:20])  # not one_line()'d: the continuation line stays
        self.assertEqual(len(rows[1][1]), INPUT_CHARS)
        self.assertTrue(rows[1][1].endswith("…"))
        self.assertEqual(rows[2:], [["timeout", "60000"], ["run_in_background", "true"]])
        edit = tool_input("Edit", {"file_path": "/a.py", "old_string": "x", "new_string": "y", "replace_all": False})
        self.assertEqual(edit, [["file_path", "/a.py"], ["replace_all", "false"]])
        self.assertEqual(tool_input("Bash", "not a dict"), [])
        # the frame carries it only when there is one
        self.assertNotIn("input", tool_frame(ToolCall("a", "Bash")))
        self.assertEqual(tool_frame(ToolCall("a", "Read", input=[["file_path", "/a"]]))["input"], [["file_path", "/a"]])

    def test_tool_block_renders_running_done_and_failed_calls(self):
        from rich.console import Console

        from vision.brain import ToolCall
        from vision.ui import tool_activity

        running = ToolCall("a", "Grep", "load_config  in ~/code/project")
        done = ToolCall("b", "Bash", "git status --short", done=True, output="\n".join(f" M file{i}.py" for i in range(5)))
        failed = ToolCall("c", "Read", "~/nope", done=True, is_error=True, output="File does not exist.")
        silent = ToolCall("d", "Bash", "true", done=True, output="\n")
        console = Console(width=70, force_terminal=False, file=io.StringIO())
        console.print(tool_activity([running, done, failed, silent], "⠋"))
        out = console.file.getvalue()
        self.assertIn("⠋ Grep load_config  in ~/code/project", out)
        self.assertIn("✓ Bash git status --short", out)
        self.assertNotIn("file0.py", out)  # none of the result until a click
        self.assertIn("✗ Read ~/nope", out)
        self.assertNotIn("File does not exist.", out)  # even a one-line result stays behind the click
        self.assertNotIn("⎿", out)  # one row per call, nothing under it
        self.assertEqual(len(out.strip().splitlines()), 4)
        # expanded: the whole result under its row
        console = Console(width=70, force_terminal=False, file=io.StringIO())
        console.print(tool_activity([done, failed], "⠋", expanded={"b"}))
        out = console.file.getvalue()
        self.assertIn("  ⎿   M file0.py", out)
        self.assertIn("      M file4.py", out)
        self.assertNotIn("File does not exist.", out)  # the other call is still folded


    def test_click_on_a_tool_row_expands_and_collapses_its_result(self):
        from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton, MouseModifier
        from prompt_toolkit.layout.screen import Point

        from vision.brain import ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=True)
        e.place(ToolCall("b", "Bash", "ls", done=True, output="\n".join(f"line{i}" for i in range(6))))
        e.buf, e.finished, e.done = "done.", True, True
        e.unfolded = True  # a done reply folds its calls to one row (see the fold test); this one is about a call's own row
        redraws = []
        e.on_click = lambda: redraws.append(1)

        def plain(rows):
            return ["".join(f[1] for f in r) for r in rows]

        rows = e.lines(80)
        text = plain(rows)
        self.assertTrue(any("Bash ls" in t for t in text))
        self.assertFalse(any("line0" in t for t in text))  # nothing of the result until a click
        # every fragment of the tool rows carries the handler (the gutter's pad too); the reply text's do not
        handlers = list({f[2] for r in rows if "Bash ls" in "".join(f[1] for f in r) for f in r if len(f) == 3})
        self.assertTrue(all(len(f) == 3 for r in rows if "Bash ls" in "".join(f[1] for f in r) for f in r))
        self.assertEqual(len(handlers), 1)
        self.assertTrue(all(len(f) == 2 for r in rows if any("done." in f[1] for f in r) for f in r))
        click = lambda kind: MouseEvent(position=Point(0, 0), event_type=kind, button=MouseButton.LEFT, modifiers=frozenset())
        handler = handlers[0]
        self.assertIs(handler(click(MouseEventType.MOUSE_DOWN)), NotImplemented)  # a press is not a click
        self.assertEqual(redraws, [])
        handler(click(MouseEventType.MOUSE_UP))
        self.assertEqual(redraws, [1])
        text = plain(e.lines(80))
        self.assertTrue(all(any(f"line{i}" in t for t in text) for i in range(6)))
        # the result's rows carry the handler too, so a click on them folds it back
        self.assertTrue(all(len(f) == 3 for r in e.lines(80) if any("line3" in f[1] for f in r) for f in r))
        handler(click(MouseEventType.MOUSE_UP))
        text = plain(e.lines(80))
        self.assertFalse(any("line0" in t for t in text))

    def test_tool_summary_counts_calls_by_kind(self):
        from vision.brain import ToolCall
        from vision.brain import AgentRun
        from vision.ui import tool_summary

        calls = [
            ToolCall("1", "Read", "a.py", done=True), ToolCall("2", "Bash", "ls", done=True), ToolCall("3", "Read", "b.py", done=True),
            ToolCall("4", "Grep", "x", done=True), ToolCall("5", "Edit", "a.py", done=True), ToolCall("6", "Glob", "*.py", done=True),
            ToolCall("7", "Read", "c.py", done=True, is_error=True), ToolCall("8", "WebSearch", "q", done=True),
            ToolCall("9", "Frobnicate", "", done=True), ToolCall("10", "Write", "d.py", done=True),
        ]
        self.assertEqual(  # the desktop's words and order (render.ts stepsSummary), whatever order the calls came in
            tool_summary(calls),
            "Read 3 files, searched twice, edited 2 files, ran a command, looked at a page, used a tool",
        )
        agent = AgentRun("a", "Explore", "look", done=True)
        self.assertEqual(tool_summary([agent, ToolCall("1", "Read", "a.py"), ToolCall("2", "Grep", "x")]), "Read a file, searched once, sent an agent")
        self.assertEqual(tool_summary([ToolCall(str(i), "LS", "") for i in range(3)]), "Searched 3 times")
        self.assertEqual(tool_summary([]), "")

    def test_a_fold_counts_the_lines_its_edits_changed(self):
        from rich.console import Console

        from vision.brain import ToolCall
        from vision.ui import diff_counts, tool_fold

        edit = ToolCall("e", "Edit", "a.py", done=True, diff=[{"old": 1, "new": 1, "lines": [" a", "-b", "+B", "+C"]}])
        write = ToolCall("w", "Write", "n.py", done=True, diff=[{"old": 0, "new": 1, "lines": ["+x"]}])
        self.assertEqual(diff_counts([edit, write, ToolCall("b", "Bash", "ls", done=True)]), (3, 1))
        console = Console(width=80, force_terminal=False, file=io.StringIO())
        console.print(tool_fold([edit, write, ToolCall("b", "Bash", "ls", done=True)]))
        self.assertEqual(console.file.getvalue().rstrip(), "● Edited 2 files, ran a command +3 −1  ›")
        console = Console(width=80, force_terminal=False, file=io.StringIO())
        console.print(tool_fold([ToolCall("r", "Read", "a.py", done=True, is_error=True)]))
        self.assertEqual(console.file.getvalue().rstrip(), "● Read a file, 1 failed  ›")  # no edits: no counts

    def test_a_live_run_of_tool_calls_is_one_row_that_becomes_its_total(self):
        """While a run of calls is going it is one row showing the latest call, not a row per call
        and no count; prose after it folds it to its total."""
        from vision.brain import ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.on_click = lambda: None
        e.buf, e.shown = "Having a look.", 1 << 30

        def plain():
            e.full(80)  # the pacer's refresh: a live entry otherwise draws its last render of the buffer
            e.invalidate()
            return ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]

        e.place(ToolCall("a", "Read", "~/a.py"))
        text = plain()
        self.assertTrue(any(t.endswith("Read ~/a.py") for t in text))
        e.tools["a"].done = True
        e.place(ToolCall("b", "Bash", "ls"))
        e.place(ToolCall("c", "Read", "~/b.py"))
        text = plain()
        self.assertTrue(any(t.endswith("Read ~/b.py") for t in text))  # no count until the total
        self.assertFalse(any("Read ~/a.py" in t or "Bash ls" in t for t in text))
        e.tools["b"].done = e.tools["c"].done = True
        e.buf += "\n\nFound it."
        text = plain()
        self.assertIn("  ● Read 2 files, ran a command  ›", text)
        self.assertFalse(any("Read ~/b.py" in t for t in text))
        e.toggle_fold()  # ctrl-o works mid-reply: every call back on its own row
        text = plain()
        self.assertTrue(all(any(c in t for t in text) for c in ("Read ~/a.py", "Bash ls", "Read ~/b.py")))

    def test_input_diff_matches_lines_not_characters_so_a_big_edit_does_not_stall(self):
        """An Edit's preview diff ran difflib.ndiff on the brain's stream reader; pairing changed lines
        up character by character took 15 s for a 300-line rewrite, during which nothing streamed."""
        from vision.brain import DIFF_LINES, input_diff, line_diff

        self.assertEqual(line_diff(["a", "b", "c"], ["a", "B", "c", "d"]), [" a", "-b", "+B", " c", "+d"])
        self.assertEqual(line_diff([], ["x"]), ["+x"])
        old = [f"    value_{i} = compute({i})" for i in range(300)]
        new = [ln.replace("compute", "calc") for ln in old]
        started = time.monotonic()
        hunks = input_diff("Edit", {"old_string": "\n".join(old), "new_string": "\n".join(new)})
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(hunks[0]["lines"]), DIFF_LINES)
        self.assertTrue(hunks[0]["lines"][0].startswith("-") and hunks[0]["lines"][-1].startswith("+"))

    def test_a_folded_edit_is_counted_and_unfolds_to_its_diff(self):
        """A run with an Edit in it folds like any other, the lines it changed counted on the row (`+2
        −1`) rather than its diff shown; unfolded, the Edit is a block of its own as in Claude Code:
        `Update path`, the count of additions and removals, then the numbered lines."""
        from vision.brain import ToolCall, edit_diff
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.on_click = lambda: None
        e.place(ToolCall("r", "Read", "~/a.py", done=True))
        patch = {"structuredPatch": [{"oldStart": 7, "newStart": 7, "lines": [" x = 1", "-y = 2", "+y = 3", "+z = 4"]}]}
        e.place(ToolCall("e", "Edit", "~/a.py", done=True, diff=edit_diff(patch)))
        e.place(ToolCall("b", "Bash", "pytest", done=True))
        e.buf, e.shown, e.finished, e.done = "Fixed.", 1 << 30, True, True
        e.full(80)
        text = ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]
        self.assertEqual(text[1], "  ● Read a file, edited a file, ran a command +2 −1  ›")
        self.assertFalse(any("y = 3" in t for t in text))
        e.toggle_fold()
        text = ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]
        self.assertEqual(text[1:10], [
            "  ✓ Read ~/a.py",
            "  ✓ Update ~/a.py",
            "    ⎿  Updated ~/a.py with 2 additions and 1 removal",
            "         7   x = 1",
            "         8 - y = 2",
            "         8 + y = 3",
            "         9 + z = 4",
            "  ✓ Bash pytest",
            "    ctrl-o to fold",
        ])

    def test_done_reply_folds_its_tool_calls_to_one_row_until_unfolded(self):
        """As Claude Code: once the reply is done its calls fold
        to `● Read 2 files, ran a command  ›`, and a click on that row
        (or ctrl-o, which calls toggle_fold) brings them back, with a way to fold again."""
        from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
        from prompt_toolkit.layout.screen import Point

        from vision.brain import ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=True)
        e.place(ToolCall("a", "Read", "~/a.py", done=True, output="x = 1"))
        e.place(ToolCall("b", "Bash", "ls", done=True, output="a.py\nb.py\nc.py\nd.py"))
        e.place(ToolCall("c", "Read", "~/b.py", done=True, output="y = 2"))
        e.buf, e.shown = "done.", 1 << 30
        e.on_click = lambda: None

        def plain(rows):
            return ["".join(f[1] for f in r) for r in rows]

        e.finished = e.done = True
        e.invalidate()
        rows = e.lines(80)
        text = plain(rows)
        self.assertTrue(any("● Read 2 files, ran a command  ›" in t for t in text))
        self.assertFalse(any("ctrl-o" in t for t in text))  # the key is in /help, not on every row
        self.assertFalse(any("Bash ls" in t for t in text))
        self.assertFalse(any("a.py" in t and "⎿" in t for t in text))
        handler = next(f[2] for r in rows for f in r if len(f) == 3)
        handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        text = plain(e.lines(80))
        self.assertTrue(any("Read ~/a.py" in t for t in text))
        self.assertTrue(any("Bash ls" in t for t in text))
        self.assertTrue(any("ctrl-o to fold" in t for t in text))
        self.assertFalse(any("Read 2 files" in t for t in text))
        e.toggle_fold()  # ctrl-o
        text = plain(e.lines(80))
        self.assertTrue(any("Read 2 files, ran a command" in t for t in text))
        self.assertFalse(any("Bash ls" in t for t in text))
        # a failed call: red dot, and the count says so
        e.tools["b"].is_error = True
        e.invalidate()
        self.assertTrue(any("Read 2 files, ran a command, 1 failed" in t for t in plain(e.lines(80))))

    def test_agent_and_tool_rows_sit_where_they_happened(self):
        """A model that says "sending an agent off" and then launches one must read that way: the
        rows go in at the point in the text where they arrived, straight under the prose before them,
        and the typewriter reaches the rows only after the text before them is out."""
        from vision.brain import AgentRun, ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.on_click = lambda: None
        e.buf = "Sent the agent off."
        e.place(AgentRun("a", "general-purpose", "Find largest folder", done=True, tool_uses=3, duration_ms=17300, model="opus", effort="high"))
        e.buf += "\n\nSteam is the monster."
        e.place(ToolCall("t", "Bash", "du -sh ~/.steam", done=True, output="121G"))
        e.buf += "\n\nUninstall CS:GO."

        def plain(rows):
            return ["".join(f[1] for f in r).rstrip() for r in rows]

        e.shown = 1 << 30  # prose follows each run: they have settled into their totals, mid-reply too
        self.assertEqual(plain(e.lines(80))[1:-1], [
            "  Sent the agent off.",
            "  ● Sent an agent  ›",
            "",
            "  Steam is the monster.",
            "  ● Ran a command  ›",
            "",
            "  Uninstall CS:GO.",
        ])
        e.shown = 4  # the typewriter is still on the first sentence: nothing after it but the live line
        e.invalidate()
        text = plain(e.lines(80))
        self.assertEqual(text[1], "  Sent")
        self.assertRegex(text[2], r"^  . working… \(0s · esc to stop\)$")
        self.assertNotIn("agent", "".join(text[2:]))
        e.shown = len("Sentthe agentoff.")  # the first piece is out (blanks are free): the agent's rows come into view, not the next prose
        e.invalidate()
        self.assertEqual(plain(e.lines(80))[1:3], ["  Sent the agent off.", "  ● Sent an agent  ›"])
        self.assertNotIn("Steam", "".join(plain(e.lines(80))))
        # done: the agent and the tool call each fold in place, under their own prose, not at the top
        e.shown, e.finished, e.done = 1 << 30, True, True
        e.invalidate()
        text = plain(e.lines(80))
        self.assertLess(text.index("  Sent the agent off."), text.index("  ● Sent an agent  ›"))
        self.assertLess(text.index("  ● Sent an agent  ›"), text.index("  Steam is the monster."))
        self.assertLess(text.index("  Steam is the monster."), text.index("  ● Ran a command  ›"))
        self.assertLess(text.index("  ● Ran a command  ›"), text.index("  Uninstall CS:GO."))
        self.assertNotIn("Find largest folder", "".join(text))
        e.toggle_fold()  # ctrl-o: every row back, the way back under the last run only
        text = plain(e.lines(80))
        self.assertTrue(any(t.startswith("  ✓ general-purpose Find largest folder · ") for t in text))
        self.assertIn("  ✓ Bash du -sh ~/.steam", text)
        self.assertEqual(sum("ctrl-o to fold" in t for t in text), 1)
        self.assertLess(text.index("  ✓ Bash du -sh ~/.steam"), text.index("    ctrl-o to fold"))
        # a second report of the same run is a redraw, not a second row
        e.place(e.agents["a"])
        self.assertEqual(len(e.marks), 2)

    def test_agents_and_tools_launched_together_fold_into_one_line(self):
        """Five agents and two ToolSearch calls with no prose between them are one run: ten rows
        plus two while the reply is live, one summary row once it is done (a red dot if any failed),
        and the live line says the wait is on the agents."""
        from vision.brain import AgentRun, ToolCall
        from vision.ui import WORKING, _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.on_click = lambda: None
        for i in range(5):
            e.place(AgentRun(f"a{i}", "general-purpose", f"Task {i}", model="haiku", effort="high"))
        e.place(ToolCall("t1", "ToolSearch", "select:Foo", done=True, output="ok"))
        e.place(ToolCall("t2", "ToolSearch", "select:Bar", done=True, output="ok"))
        e.status = "using Agent…"
        text = ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]
        self.assertEqual(sum("general-purpose Task" in t for t in text), 5)
        self.assertRegex(text[-1], r"^  . waiting on 5 agents… \(")
        e.agents["a0"].done = True
        e.status = WORKING
        e.invalidate()
        text = ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]
        self.assertRegex([t for t in text if t][-1], r"^  . waiting on 4 agents… \(")  # the done row is one line shorter
        for run in e.agents.values():
            run.done = True
        e.agents["a3"].failed = True
        e.buf, e.shown, e.finished, e.done = "All five landed.", 1 << 30, True, True
        e.invalidate()
        text = ["".join(f[1] for f in r).rstrip() for r in e.lines(80)]
        self.assertIn("  ● Sent 5 agents, used 2 tools, 1 failed  ›", text)
        self.assertNotIn("general-purpose Task 0", "".join(text))
        self.assertLess(text.index("  ● Sent 5 agents, used 2 tools, 1 failed  ›"), text.index("  All five landed."))

    def test_folding_a_tool_result_mid_reply_does_not_leave_blank_rows(self):
        """The never-shrink-mid-reply floor (a cushion against a one-row markdown reflow) must not
        apply to a click: folding a 200-line result would otherwise pad the entry with ~200 blank
        rows, and the bottom-anchored transcript would show nothing else until the turn ended."""
        from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
        from prompt_toolkit.layout.screen import Point

        from vision.brain import ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=True)
        e.buf, e.shown = "so far", 1 << 30  # text revealed, but the brain is still working: not done, no `ended`
        e.place(ToolCall("b", "Bash", "ls", done=True, output="\n".join(f"line{i}" for i in range(200))))  # the live row, nothing after it yet
        e.on_click = lambda: None

        def plain(rows):
            return ["".join(f[1] for f in r) for r in rows]

        rows = e.lines(80)
        folded = len(rows)
        handler = next(f[2] for r in rows for f in r if len(f) == 3)
        click = MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset())
        handler(click)
        self.assertGreater(len(e.lines(80)), 200)
        handler(click)
        rows = e.lines(80)
        self.assertEqual(len(rows), folded)
        self.assertFalse(any("line0" in t or "⎿" in t for t in plain(rows)))
        self.assertTrue(plain(rows)[-1].strip())  # the live `thinking…` line is last, not padding
        self.assertEqual(e._floor[80], folded - 1)  # the floor tracks the new height from here (less the blank line above the reply)


class SelectionTests(unittest.TestCase):
    """Dragging over the transcript highlights cells and copies their text (see ChatScreen._mouse)."""

    def test_slice_row_splits_fragments_at_cell_boundaries_and_keeps_handlers(self):
        from vision.ui import _slice_row

        h = lambda e: None
        row = [("bold", "Vision ›"), ("", "  "), ("dim", "⏺ Bash(ls)", h)]
        out, text = _slice_row(row, 3, 13, "reverse")
        self.assertEqual(text, "ion ›  ⏺ B")
        self.assertEqual(out, [("bold", "Vis"), ("bold reverse", "ion ›"), (" reverse", "  "), ("dim reverse", "⏺ B", h), ("dim", "ash(ls)", h)])
        # a wide char counts two cells
        out, text = _slice_row([("", "a日b")], 1, 3, "reverse")
        self.assertEqual((text, out), ("日", [("", "a"), (" reverse", "日"), ("", "b")]))
        # a span past the end takes the rest; one outside touches nothing
        self.assertEqual(_slice_row([("", "abc")], 1, 1 << 30, "reverse")[1], "bc")
        self.assertEqual(_slice_row([("", "abc")], 5, 9, "reverse"), ([("", "abc")], ""))

    def test_sel_cols_covers_first_last_and_whole_middle_lines(self):
        from vision.ui import _sel_cols

        sel = ((2, 4), (4, 1))
        self.assertIsNone(_sel_cols(sel, 1))
        self.assertEqual(_sel_cols(sel, 2), (4, 1 << 30))
        self.assertEqual(_sel_cols(sel, 3), (0, 1 << 30))
        self.assertEqual(_sel_cols(sel, 4), (0, 2))  # the cell under the pointer is included
        self.assertIsNone(_sel_cols(sel, 5))


    def test_copied_text_loses_the_margin_but_keeps_relative_indentation_and_blank_lines(self):
        from vision.ui import dedent_rows

        rows = [  # (column the sliced text starts at, text) as selected_text collects them
            (16, "Here is the fix:"),  # the press landed on the text of the first row
            (0, ""),
            (0, "                    def foo():"),
            (0, "                        return 1"),
            (0, ""),
            (0, "                Then run it."),
            (0, ""),
        ]
        self.assertEqual(dedent_rows(rows), "Here is the fix:\n\n    def foo():\n        return 1\n\nThen run it.")
        # a press inside the margin: the first row's leading spaces go too
        self.assertEqual(dedent_rows([(4, "            a"), (0, "                b")]), "a\nb")
        # only code selected: it starts at column 0
        self.assertEqual(dedent_rows([(20, "def foo():"), (0, "                        return 1")]), "def foo():\n    return 1")
        self.assertEqual(dedent_rows([(0, "   "), (0, "")]), "")

    def test_release_copies_and_drops_the_highlight(self):
        from prompt_toolkit.layout.screen import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        from vision.ui import ChatScreen

        screen = ChatScreen.__new__(ChatScreen)
        screen._view = (0, 10, 5)
        screen._sel = None
        screen._sel_dragged = False
        screen._sel_copied = ""
        screen._sel_held = False
        screen._last_click = (0.0, -1)
        copied = []
        screen._copy_selection = lambda: copied.append(screen._sel_range())
        screen.app = type("App", (), {"invalidate": lambda self: None})()

        def ev(kind, y, x):
            return MouseEvent(position=Point(x=x, y=y), event_type=kind, button=MouseButton.LEFT, modifiers=frozenset())

        self.assertIsNone(screen._mouse(ev(MouseEventType.MOUSE_DOWN, 0, 2)))
        self.assertIsNone(screen._sel_range())  # a press alone is not a selection
        self.assertIsNone(screen._mouse(ev(MouseEventType.MOUSE_MOVE, 0, 8)))
        self.assertEqual(screen._sel_range(), ((0, 2), (0, 8)))
        self.assertEqual(copied, [])  # nothing is copied while the button is down, however long the drag pauses
        self.assertIsNone(screen._mouse(ev(MouseEventType.MOUSE_UP, 0, 8)))
        self.assertEqual(copied, [((0, 2), (0, 8))])
        self.assertEqual(screen._sel_range(), ((0, 2), (0, 8)))  # the highlight stays up after release

        copied.clear()
        self.assertIsNone(screen._mouse(ev(MouseEventType.MOUSE_DOWN, 1, 0)))  # the next press drops it
        self.assertIsNone(screen._sel_range())
        self.assertIs(screen._mouse(ev(MouseEventType.MOUSE_UP, 1, 0)), NotImplemented)
        self.assertEqual(copied, [])
        self.assertIsNone(screen._sel)

    def test_a_drag_holds_the_mouse_until_the_button_comes_up_anywhere(self):
        from prompt_toolkit.layout.screen import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        from rich.text import Text

        from vision.ui import ChatScreen

        screen = ChatScreen("", lambda: "")
        for i in range(3):
            screen.add(Text(f"line {i}"))
        screen._view = (0, 10, 3)
        elsewhere = []

        def ev(kind, y, x, button=MouseButton.LEFT):
            return MouseEvent(position=Point(x=x, y=y), event_type=kind, button=button, modifiers=frozenset())

        def at(e):  # an event over some other window: its own handler would get it
            return screen._grab(e, lambda e: elsewhere.append(e.event_type) or NotImplemented)

        copied = []
        with patch("vision.ui.copy_to_clipboard", lambda text, out: copied.append(text) or "wl-copy"), \
                patch.object(screen.app, "create_background_task", lambda coro: coro.close()):
            screen._mouse(ev(MouseEventType.MOUSE_DOWN, 0, 0))
            at(ev(MouseEventType.MOUSE_MOVE, 1, 3))
            self.assertEqual(screen._sel_range(), ((0, 0), (1, 3)))
            at(ev(MouseEventType.MOUSE_MOVE, 14, 2))  # below the transcript: held at its last row
            self.assertEqual(screen._sel_edge[0], 1)
            self.assertEqual(copied, [])
            at(ev(MouseEventType.MOUSE_UP, 14, 2))  # let go over the input box
        self.assertEqual(copied, ["line 0\nline 1\nline 2"])  # once, on release, through the last row
        self.assertFalse(screen._sel_held)
        self.assertEqual(elsewhere, [])  # the drag had the mouse throughout
        at(ev(MouseEventType.MOUSE_UP, 14, 2))
        self.assertEqual(elsewhere, [MouseEventType.MOUSE_UP])  # and gave it back

    def test_a_double_click_selects_and_copies_the_line(self):
        from prompt_toolkit.layout.screen import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        from rich.text import Text

        from vision.ui import SEL_STYLE, ChatScreen

        screen = ChatScreen("", lambda: "")
        screen.add(Text("first line"))
        screen.add(Text("second line"))
        screen._view = (0, 10, 2)

        def click(y, x):
            down = MouseEvent(position=Point(x=x, y=y), event_type=MouseEventType.MOUSE_DOWN, button=MouseButton.LEFT, modifiers=frozenset())
            up = MouseEvent(position=Point(x=x, y=y), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset())
            screen._mouse(down)
            return screen._mouse(up)

        copied = []
        with patch("vision.ui.copy_to_clipboard", lambda text, out: copied.append(text) or "wl-copy"):
            self.assertIs(click(1, 3), NotImplemented)  # one click is a click
            self.assertEqual(copied, [])
            self.assertIsNone(click(1, 8))  # a second on the same line soon after selects it
            self.assertEqual(copied, ["second line"])
            self.assertIn(SEL_STYLE, str(screen._transcript_text()))
            self.assertEqual(screen._notice[0], "copied 1 line")

            copied.clear()
            click(0, 1)
            with patch("vision.ui.time.monotonic", return_value=time.monotonic() + 5):
                self.assertIs(click(0, 1), NotImplemented)  # too slow: two clicks
            click(1, 1)
            self.assertIs(click(0, 1), NotImplemented)  # another line: two clicks
        self.assertEqual(copied, [])

    def test_ctrl_c_copies_the_highlight_and_drops_it_without_arming_quit(self):
        from prompt_toolkit.keys import Keys

        from rich.text import Text

        from vision.ui import SEL_STYLE, ChatScreen

        screen = ChatScreen("", lambda: "")
        screen.add(Text("hello there"))
        handler = screen.app.key_bindings.get_bindings_for_keys((Keys.ControlC,))[0].handler
        event = type("Event", (), {"app": screen.app})()
        copied = []
        with patch("vision.ui.copy_to_clipboard", lambda text, out: copied.append(text) or "wl-copy"):
            screen._sel, screen._sel_dragged = [(0, 0), (0, 4)], True
            self.assertIn(SEL_STYLE, str(screen._transcript_text()))
            handler(event)
        self.assertEqual(copied, ["hello"])
        self.assertIsNone(screen._sel)
        self.assertNotIn(SEL_STYLE, str(screen._transcript_text()))
        self.assertEqual(screen._quit_armed, 0.0)  # not a step towards quitting...
        self.assertEqual(screen._notice[0], "copied 1 line")
        handler(event)  # ...the next Ctrl-C, with nothing highlighted, is the usual first warning
        self.assertEqual(screen._notice[0], "Press Ctrl-C again to exit")


class BrainEnvTests(unittest.TestCase):
    """A shell command inside a turn must not be able to start the other provider's CLI."""

    def test_shims_and_other_provider_credentials(self):
        import os
        import subprocess
        from vision import brain as brain_mod
        from vision.brain import brain_env

        with tempfile.TemporaryDirectory() as tmp, patch.object(brain_mod, "DATA_DIR", Path(tmp)), \
                patch.dict(os.environ, {"CLAUDECODE": "1", "PATH": "/usr/bin:/bin"}):
            empty = str(Path(tmp) / "no-credentials")
            homes = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "grok": "GROK_HOME"}
            for provider, kept in homes.items():
                env = brain_env(provider)
                self.assertNotIn("CLAUDECODE", env)
                self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(tmp) / "shims"))
                for other, var in homes.items():
                    if other != provider:
                        self.assertEqual(env[var], empty)
                self.assertNotEqual(env.get(kept), empty)
            self.assertEqual(env["GROK_MEMORY"], "0")  # last env is grok
            for name in ("claude", "codex", "grok"):
                r = subprocess.run([name, "-p", "hi"], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 1)
                self.assertIn("blocked by Vision", r.stderr)
                self.assertIn("/model", r.stderr)

    def test_default_deny_list_and_persona_cover_both_clis(self):
        from vision.persona import system_prompt

        denied = BrainConfig().denied_tools
        self.assertIn("Bash(claude:*)", denied)
        self.assertIn("Bash(codex:*)", denied)
        self.assertIn("Bash(grok:*)", denied)
        for provider in ("claude", "codex", "grok"):
            self.assertIn("/model", system_prompt(False, tools=["Bash"], provider=provider))
            self.assertIn("`grok`", system_prompt(False, tools=["Bash"], provider=provider))


class ClaudeCatalogueTests(unittest.TestCase):
    """Claude Code's `initialize` model list (as 2.1.280 returns it) becomes Vision's Claude models."""

    ROWS = [
        {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True, "supportedEffortLevels": ["low", "high"]},
        {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "description": "Opus 5.5 with 1M context · Best for everyday, complex tasks",
         "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
        {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1", "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high"]},
        {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "description": "Haiku 4.5 · Fastest for quick answers"},
        {"value": "claude-poem-1", "resolvedModel": "claude-poem-1", "description": "Poem 1 · Writes verse.", "supportsEffort": True,
         "supportedEffortLevels": ["low", "high", "turbo"]},
    ]

    def test_catalogue_names_labels_and_efforts(self):
        from vision.models import claude_models_from_catalogue

        got = {m.alias: m for m in claude_models_from_catalogue(self.ROWS)}
        self.assertEqual([m for m in got], ["fable", "opus", "haiku", "claude-poem-1"])  # known order first, "default" dropped
        self.assertEqual((got["opus"].label, got["opus"].model_id, got["opus"].top), ("Opus 5.5", "claude-opus-5-5", "ultracode"))
        self.assertEqual(got["opus"].description, "very capable, good all-rounder")  # Vision's own copy for a known family
        self.assertEqual((got["fable"].efforts, got["fable"].top), (("low", "medium", "high"), None))
        self.assertEqual((got["haiku"].label, got["haiku"].efforts), ("Haiku 4.5", ()))
        poem = got["claude-poem-1"]
        self.assertEqual((poem.label, poem.efforts, poem.description), ("Poem 1", ("low", "high"), "writes verse"))

    def test_an_unusable_catalogue_keeps_the_current_list(self):
        from vision import models

        before = list(models.CLAUDE_MODELS)
        self.assertFalse(models.set_claude_catalogue([{"value": "default"}]))
        self.assertEqual(models.CLAUDE_MODELS, before)

    def test_other_providers_refresh_and_grok_follows_the_newest(self):
        from vision import models

        saved, live = {p: list(models._LISTS[p]) for p in models._LISTS}, set(models.LIVE)
        try:
            self.assertEqual(models.find("grok").alias, "grok-4.6")
            models._swap("grok", [ModelInfo("grok", "grok-4.10", "Grok 4.10", "d", ("low", "high"))])
            self.assertEqual((models.find("grok").alias, models.provider_default("grok")), ("grok-4.10", "grok-4.10"))
            self.assertEqual([r[0] for r in models.MODEL_TABS[2][1]], ["grok-4.10"])
            self.assertTrue(models.set_local_models(["qwen3.6", "gemma-5"]))
            self.assertEqual([m.label for m in models.LOCAL_MODELS], ["Qwen 3.6", "Gemma 5"])
            self.assertEqual(models.provider_for("gemma-5"), "local")
            self.assertIn("gemma-5", [r[0] for r in models.MODEL_TABS[3][1]])
            self.assertTrue(models.set_local_models(["qwen3.6-27b", "qwen3.6-35b-a3b", "/m/Gemma-5-E4B-Q4_K_M.gguf"]))
            self.assertEqual([m.label for m in models.LOCAL_MODELS],
                             ["Qwen 3.6 27B", "Qwen 3.6 35B-A3B", "Gemma 5 E4B Q4_K_M"])
            # a size-less --alias takes the parameter count llama-server reads from the GGUF
            self.assertTrue(models.set_local_models([{"id": "qwen", "n_params": 26_895_998_464}]))
            self.assertEqual(models.model_label("qwen"), "Qwen 27B")
            self.assertTrue(models.set_local_models([{"id": "tiny", "n_params": 3_821_079_552}]))
            self.assertEqual(models.model_label("tiny"), "Tiny 3.8B")
            # a chat saved under the fallback name shows the one model the server has loaded
            self.assertEqual(models.model_label("local-model"), "Tiny 3.8B")
        finally:
            for p, rows in saved.items():
                models._swap(p, rows)
            models.LIVE.clear()
            models.LIVE.update(live)


class RetiredModelTests(unittest.TestCase):
    """A saved model its provider stopped listing runs as that provider's default, config.toml untouched."""

    def setUp(self):
        from vision import models

        saved, live = {p: list(models._LISTS[p]) for p in models._LISTS}, set(models.LIVE)

        def restore():
            for p, rows in saved.items():
                models._swap(p, rows)
            models.LIVE.clear()
            models.LIVE.update(live)
        self.addCleanup(restore)
        models.LIVE.clear()
        models._swap("grok", [ModelInfo("grok", "grok-4.7", "Grok 4.7", "d", ("low", "high"))])

    def test_retired_model_on_a_fetched_list_falls_back(self):
        from vision import models

        models.LIVE.add("grok")
        model, note = models.usable_model("grok-4.6")
        self.assertEqual(model, "grok-4.7")
        self.assertIn("grok-4.6 is no longer offered", note)

    def test_fallback_lists_and_known_names_are_left_alone(self):
        from vision import models

        self.assertEqual(models.usable_model("grok-4.6"), ("grok-4.6", ""))  # grok list not fetched: can't tell
        models.LIVE.update({"grok", "claude"})
        self.assertEqual(models.usable_model("grok-4.7"), ("grok-4.7", ""))
        opus = models.find("opus")
        self.assertEqual(models.usable_model(f"{opus.model_id or 'opus'}[1m]")[1], "")  # a full Claude id still listed
        self.assertEqual(models.usable_model(""), ("", ""))

    def test_replace_retired_models_covers_brain_voice_and_agents(self):
        from vision import models
        from vision.config import Config

        models.LIVE.update({"grok", "local"})
        models._swap("local", [ModelInfo("local", "gemma-5", "gemma-5", "d", ("off", "high"), None, "off")])
        cfg = Config()
        cfg.brain.model, cfg.brain.effort = "grok-4.6", "max"
        cfg.conversation.model = "local-model"
        cfg.router.agents = {"opus": "opus", "grok": "grok-4.5", "codex": ""}
        notes = models.replace_retired_models(cfg)
        self.assertEqual((cfg.brain.model, cfg.brain.effort), ("grok-4.7", "high"))
        self.assertEqual(cfg.conversation.model, "gemma-5")
        self.assertEqual(cfg.router.agents, {"opus": "opus", "grok": "grok-4.7", "codex": ""})
        self.assertEqual(len(notes), 3)

        cfg.brain.model = "grok-4.6"  # a --model on the command line is the user's call, not a saved default
        models.replace_retired_models(cfg, brain=False)
        self.assertEqual(cfg.brain.model, "grok-4.6")
