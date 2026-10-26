from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console
from rich.text import Text

from vision.cli import (_apply_session, _cd_choices, _cd_target, _failed_turn_delta, _leave_conversation, _session_stays,
                        _split_usage_arg, _usage_renderable, _usage_selection)


class FailedTurnDeltaTests(unittest.TestCase):
    def test_empty_reply_gets_the_wrapper(self):
        err = "You've hit your session limit · resets 6:40pm (America/New_York)"
        self.assertEqual(_failed_turn_delta("", err), f"**Vision could not answer:** {err}")
        self.assertEqual(_failed_turn_delta(None, err, markdown=False), f"Vision could not answer: {err}")

    def test_same_text_is_not_repeated(self):
        err = "You've hit your session limit · resets 6:40pm (America/New_York)"
        self.assertEqual(_failed_turn_delta(err, err), "")
        self.assertEqual(_failed_turn_delta(err + "\n", err), "")
        self.assertEqual(_failed_turn_delta(f"Vision could not answer: {err}", err, markdown=False), "")
        self.assertEqual(_failed_turn_delta(f"**Vision could not answer:** {err}", err), "")

    def test_real_reply_then_error_is_appended(self):
        self.assertEqual(
            _failed_turn_delta("half an answer", "boom"),
            "\n\n**Vision could not answer:** boom",
        )


class UsageRenderableTests(unittest.TestCase):
    report = (
        "Current session: 40% used · resets 6:40pm (America/New_York)\n"
        "Current week: 12% used · resets Sep 24\n"
        "\n"
        "Sonnet 4.6: 80% of usage\n"
    )

    def _print(self, full=False):
        buf = io.StringIO()
        with patch("vision.usage.claude_banked", return_value=None):  # no network in tests
            r = _usage_renderable(self.report, full=full)
        Console(file=buf, force_terminal=False, width=120, color_system=None).print(r)
        return buf.getvalue()

    def test_hides_token_hint_and_contribution_until_full(self):
        text = self._print()
        self.assertIn("Current session", text)
        self.assertIn("40%", text)
        self.assertNotIn("vision usage --full", text)
        self.assertNotIn("/usage full", text)
        self.assertNotIn("Sonnet 4.6", text)

    def test_full_shows_contribution_without_the_hint(self):
        text = self._print(full=True)
        self.assertIn("Sonnet 4.6", text)
        self.assertNotIn("vision usage --full", text)
        self.assertNotIn("/usage full", text)

    def test_all_selects_every_provider_and_keeps_going_if_one_is_unavailable(self):
        self.assertEqual(_split_usage_arg("all full"), ("all", True))
        requested = []

        def select(_cfg, _current, provider):
            requested.append(provider)
            return SimpleNamespace(provider=provider)

        def render(brain, full):
            self.assertTrue(full)
            if brain.provider == "codex":
                raise RuntimeError("not logged in")
            return Text(f"{brain.provider} usage")

        with patch("vision.cli._usage_brain", side_effect=select), patch("vision.cli._usage_for", side_effect=render):
            result = _usage_selection(SimpleNamespace(), SimpleNamespace(provider="claude"), "all", full=True)

        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=120, color_system=None).print(result)
        text = buf.getvalue()
        self.assertEqual(requested, ["claude", "codex", "grok"])
        self.assertIn("claude usage", text)
        self.assertIn("Codex usage unavailable: not logged in", text)
        self.assertIn("grok usage", text)


if __name__ == "__main__":
    unittest.main()


class ActiveSummaryTests(unittest.TestCase):
    """The status row and open card name what answers the next turn: while /talk is on, spoken input
    goes to the [conversation] model and typed input to the brain, so both are shown."""

    def _cfg(self, voice_model: str = "local-model", voice_effort: str = "off"):
        from vision.config import Config

        cfg = Config()
        cfg.brain.model, cfg.brain.effort = "sonnet", "high"
        cfg.conversation.model, cfg.conversation.effort = voice_model, voice_effort
        return cfg

    def _brain(self, cfg, resolved: str = "claude-sonnet-5"):
        return SimpleNamespace(cfg=cfg.brain, resolved_model=lambda: resolved)

    def test_talk_names_only_the_conversation_model(self):
        from vision.cli import _active_summary

        cfg = self._cfg()
        self.assertEqual(_active_summary(cfg, self._brain(cfg), True, " "), "Local model thinking off")

    def test_off_talk_is_just_the_brain(self):
        from vision.cli import _active_summary

        cfg = self._cfg()
        self.assertEqual(_active_summary(cfg, self._brain(cfg), False, " "), "Sonnet 5 high")

    def test_same_model_for_both_is_named_once(self):
        from vision.cli import _active_summary

        cfg = self._cfg("sonnet", "high")
        self.assertEqual(_active_summary(cfg, self._brain(cfg), True, " "), "Sonnet 5 high")


class CornerLabelTests(unittest.TestCase):
    """The input box's bottom-right border names the typed brain the way the Grok CLI does:
    `Sonnet 5 (high)`, `· fast` when the speed tier is on, no parenthesis with effort off."""

    def _cfg(self, effort: str = "high", fast: bool = False):
        from vision.config import Config

        cfg = Config()
        cfg.brain.model, cfg.brain.effort, cfg.brain.fast = "sonnet", effort, fast
        return cfg

    def _brain(self, cfg, resolved: str = "claude-sonnet-5"):
        return SimpleNamespace(cfg=cfg.brain, resolved_model=lambda: resolved)

    def test_model_and_effort_in_grok_form(self):
        from vision.cli import _corner_label

        cfg = self._cfg()
        self.assertEqual(_corner_label(cfg, self._brain(cfg)), "Sonnet 5 (high)")

    def test_fast_tier_is_appended(self):
        from vision.cli import _corner_label

        cfg = self._cfg(fast=True)
        self.assertEqual(_corner_label(cfg, self._brain(cfg)), "Sonnet 5 (high) · fast")

    def test_effort_off_has_no_parenthesis(self):
        from vision.cli import _corner_label

        cfg = self._cfg(effort="")
        self.assertEqual(_corner_label(cfg, self._brain(cfg)), "Sonnet 5")

    def test_corner_frame_builds_with_and_without_a_label(self):
        from prompt_toolkit.layout import to_container
        from prompt_toolkit.widgets import TextArea

        from vision.ui import CornerFrame

        for text in ("Sonnet 5 (high)", ""):
            frame = CornerFrame(TextArea(), lambda text=text: text)
            self.assertIsNotNone(to_container(frame))


class CdTargetTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "sub"))
        os.mkdir(os.path.join(self.root, "sub", "deeper"))
        with open(os.path.join(self.root, "file.txt"), "w") as f:
            f.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_absolute_and_dotdot(self):
        sub = os.path.join(self.root, "sub")
        self.assertEqual(_cd_target("sub", self.root, None), sub)
        self.assertEqual(_cd_target("sub/deeper", self.root, None), os.path.join(sub, "deeper"))
        self.assertEqual(_cd_target(sub, "/", None), sub)
        self.assertEqual(_cd_target("..", os.path.join(sub, "deeper"), None), sub)
        self.assertEqual(_cd_target(" sub ", self.root, None), sub)

    def test_home_and_previous(self):
        self.assertEqual(_cd_target("~", self.root, None), os.path.expanduser("~"))
        self.assertEqual(_cd_target("-", self.root, "/"), "/")
        with self.assertRaisesRegex(ValueError, "no previous directory"):
            _cd_target("-", self.root, None)

    def test_missing_and_files_are_refused(self):
        with self.assertRaisesRegex(ValueError, "no such directory"):
            _cd_target("nope", self.root, None)
        with self.assertRaisesRegex(ValueError, "not a directory"):
            _cd_target("file.txt", self.root, None)

    def test_menu_completes_paths_and_carries_on(self):
        from vision.ui import SlashCommand, slash_menu_rows

        os.mkdir(os.path.join(self.root, ".hidden"))
        cmds = [SlashCommand("cd", "", typed_args=lambda arg: _cd_choices(arg, self.root))]
        rows = lambda text: [r.text for r in slash_menu_rows(cmds, text)]
        self.assertEqual(rows("/cd "), ["/cd ~", "/cd -", "/cd ..", "/cd sub/"])
        self.assertEqual(rows("/cd su"), ["/cd sub/"])
        self.assertEqual(rows("/cd sub/"), ["/cd sub/", "/cd sub/deeper/"])  # itself first, so Enter stays
        self.assertEqual(rows("/cd sub/de"), ["/cd sub/deeper/"])
        self.assertEqual(rows("/cd .hi"), ["/cd .hidden/"])
        self.assertEqual(rows(f"/cd {self.root}/s"), [f"/cd {self.root}/sub/"])
        self.assertEqual(rows("/cd nope/"), [])
        with patch.dict(os.environ, {"HOME": self.root}):
            self.assertEqual(rows("/cd ~"), ["/cd ~/", "/cd ~/sub/"])


class DirectoryBoundSessionTests(unittest.TestCase):
    """Claude Code keeps a conversation under ~/.claude/projects/<directory>/ and `--resume` finds it only
    from there (2.1.289: "No conversation found with session ID" anywhere else). /cd away from one starts
    a new conversation from its transcript; /session on one from another directory moves there."""

    class _Brain:
        provider = "claude"

        def __init__(self, workdir, session_id="s1"):
            self.workdir, self.session_id, self.handoff = workdir, session_id, None
            self.calls = []

        def new_session(self):
            self.calls.append("new")
            self.session_id = self.handoff = None

        def resume(self, sid):
            self.calls.append(("resume", sid))
            self.session_id = sid

    def test_a_conversation_stays_only_when_it_is_claudes_and_exists(self):
        self.assertTrue(_session_stays(self._Brain("/a")))
        self.assertFalse(_session_stays(self._Brain("/a", session_id=None)))
        codex = self._Brain("/a")
        codex.provider = "codex"
        self.assertFalse(_session_stays(codex))

    def test_cd_away_starts_a_new_conversation_from_the_transcript(self):
        brain = self._Brain("/a")
        hist = [{"role": "user", "text": "remember pelican"}, {"role": "assistant", "text": "OK"}]
        with patch("vision.sessions.session_history", return_value=hist) as saved:
            note = _leave_conversation(brain, None)
        saved.assert_called_once_with("claude", "s1", limit=0, include_context=True)
        self.assertEqual((brain.calls, brain.session_id), (["new"], None))
        self.assertIn("remember pelican", brain.handoff)
        self.assertIn("a new conversation from the transcript so far", note)
        self.assertIn("Claude Code keeps a conversation in its directory", note)

    def test_cd_away_from_an_empty_conversation_carries_nothing(self):
        brain = self._Brain("/a")
        with patch("vision.sessions.session_history", return_value=[]):
            note = _leave_conversation(brain, None)
        self.assertEqual((brain.calls, brain.handoff), (["new"], None))
        self.assertNotIn("transcript", note)

    def test_resuming_a_claude_conversation_moves_to_its_directory(self):
        import tempfile

        from vision.config import BrainConfig
        from vision.sessions import SessionInfo

        with tempfile.TemporaryDirectory() as there:
            there = os.path.abspath(there)
            brain, cfg = self._Brain("/a", session_id=None), BrainConfig(workdir="/a")
            info = SessionInfo("abc12345", "claude", "the title", 0.0, cwd=there)
            note = _apply_session(brain, info, cfg)
            self.assertEqual((brain.workdir, cfg.workdir, brain.calls), (there, there, [("resume", "abc12345")]))
            self.assertIn("working in", note)
            already = self._Brain(there, session_id=None)  # already there: nothing to say about it
            self.assertNotIn("working in", _apply_session(already, info, BrainConfig(workdir=there)))
            self.assertEqual(already.workdir, there)

    def test_resuming_a_claude_conversation_whose_directory_is_gone_carries_its_transcript(self):
        from vision.sessions import SessionInfo

        brain = self._Brain("/a", session_id=None)
        info = SessionInfo("abc12345", "claude", "the title", 0.0, cwd="/nowhere/at/all")
        with patch("vision.sessions.session_history", return_value=[{"role": "user", "text": "keep going"}]):
            note = _apply_session(brain, info)
        self.assertEqual((brain.workdir, brain.calls), ("/a", ["new"]))
        self.assertIn("keep going", brain.handoff)
        self.assertIn("is gone", note)

    def test_other_providers_resume_where_they_are(self):
        from vision.sessions import SessionInfo

        brain = self._Brain("/a", session_id=None)
        brain.provider = "codex"
        info = SessionInfo("abc12345", "codex", "the title", 0.0, cwd="/elsewhere")
        note = _apply_session(brain, info)
        self.assertEqual((brain.workdir, brain.calls), ("/a", [("resume", "abc12345")]))
        self.assertNotIn("working in", note)


class FinalTranscriptTests(unittest.TestCase):
    def test_a_finished_preview_spares_the_second_transcription(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from vision.cli import _final_transcript

        stt = Mock(transcribe=Mock(return_value=" fresh words "))
        self.assertEqual(_final_transcript(SimpleNamespace(final=" preview words "), stt, "audio"), "preview words")
        stt.transcribe.assert_not_called()
        self.assertEqual(_final_transcript(SimpleNamespace(final=None), stt, "audio"), "fresh words")
        self.assertEqual(_final_transcript(None, stt, "audio"), "fresh words")
        self.assertEqual(stt.transcribe.call_count, 2)


class DeviceLookupTests(unittest.TestCase):
    def test_a_named_device_is_looked_up_once_a_minute_not_every_turn(self):
        import sys
        from unittest.mock import MagicMock, patch

        from vision import config

        fake_sd = MagicMock()
        fake_sd.query_devices.return_value = [{"name": "pipewire", "max_input_channels": 2, "max_output_channels": 2, "hostapi": 0}]
        fake_sd.query_hostapis.return_value = {"name": "ALSA"}
        dumps = []
        nodes = lambda kind: dumps.append(kind) or [("alsa_output.usb", "USB Speakers")]
        with patch.dict(sys.modules, {"sounddevice": fake_sd}), patch.object(config, "pipewire_nodes", side_effect=nodes), \
             patch.dict(config._devices, clear=True):
            first = config.resolve_device("usb speakers", "output")
            self.assertEqual(first, config.AudioDevice(0, "alsa_output.usb"))
            self.assertEqual(config.resolve_device("usb speakers", "output"), first)
            self.assertEqual(dumps, ["output"])  # one pw-dump, not one per call
            config.resolve_device("usb speakers", "output", fresh=True)  # a replug or a /mic change looks again
            self.assertEqual(len(dumps), 2)
            with patch.object(config, "_DEVICE_TTL_S", 0.0):
                config.resolve_device("usb speakers", "output")
            self.assertEqual(len(dumps), 3)
            self.assertEqual(config.resolve_device("", "output"), config.AudioDevice())  # the default is never looked up
            self.assertEqual(len(dumps), 3)


class DoctorWithoutVoiceTests(unittest.TestCase):
    def test_a_text_only_install_is_reported_not_fatal(self):
        import importlib.util

        from typer.testing import CliRunner

        from vision import cli, clis

        real = importlib.util.find_spec
        missing = lambda name, *a, **k: None if name in ("numpy", "sounddevice", "soundfile", "faster_whisper") else real(name, *a, **k)
        not_installed = [clis.CliInfo(p, error="not installed") for p in clis.PROVIDERS]
        with patch("vision.clis.cli_versions", return_value=not_installed), patch("importlib.util.find_spec", missing):
            result = CliRunner().invoke(cli.app, ["doctor", "--no-usage"])
        self.assertEqual(result.exit_code, 0, result.output)
        import re

        text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())  # a real console: colours and wrapping
        self.assertIn("voice not installed (optional", text)
        self.assertIn(cli.setup_hint(), text)


class VoiceBackendTests(unittest.TestCase):
    """The installed voice build is read from torch's metadata, so it never imports torch."""

    CUDA_TORCH = ['cuda-toolkit[cublas]==13.0.3; platform_system == "Linux"', "filelock"]

    def backend(self, torch_version, torch_requires=(), platform="linux", leftovers=("nvidia-cublas-cu12",)):
        from importlib.metadata import PackageNotFoundError

        from vision import install

        def version(dist):
            if dist == "torch" and torch_version:
                return torch_version
            if dist in leftovers:
                return "12.9"
            raise PackageNotFoundError(dist)

        with patch("importlib.metadata.version", version), \
                patch("importlib.metadata.requires", lambda dist: list(torch_requires)), \
                patch.object(install.sys, "platform", platform):
            return install.voice_backend()

    def test_cuda_torch_is_the_nvidia_build(self):
        self.assertEqual(self.backend("2.14.0", self.CUDA_TORCH), "nvidia")

    def test_cpu_torch_is_the_cpu_build_even_with_cuda_libraries_left_behind(self):
        # A switch to voice-cpu by hand keeps nvidia-cublas-cu12 around; torch is what counts.
        self.assertEqual(self.backend("2.14.0+cpu"), "cpu")
        self.assertEqual(self.backend("2.14.0", ["filelock"]), "cpu")

    def test_macos_torch_is_the_cpu_build(self):
        self.assertEqual(self.backend("2.14.0", self.CUDA_TORCH, platform="darwin"), "cpu")

    def test_no_torch_is_no_voice(self):
        self.assertIsNone(self.backend(None))

    def test_mlx_audio_on_a_mac_is_the_mac_build_even_with_torch_left_behind(self):
        self.assertEqual(self.backend("2.14.0", platform="darwin", leftovers=("mlx-audio",)), "mac")
        self.assertEqual(self.backend(None, platform="darwin", leftovers=("mlx-audio",)), "mac")


class InstallHintTests(unittest.TestCase):
    """Hints are pasted from wherever `vision` runs, so they carry the checkout's path, not a relative one."""

    def test_the_hint_names_the_setup_script_by_full_path_and_the_build(self):
        from vision import install

        with patch.object(install.Path, "home", return_value=install.ROOT.parent):
            self.assertEqual(install.setup_hint("cpu"), f"~/{install.ROOT.name}/scripts/setup.sh --voice --cpu")
        with patch.object(install.Path, "home", return_value=Path("/nowhere")):
            self.assertEqual(install.setup_hint("nvidia"), f"{install.SETUP} --voice --nvidia")
            self.assertEqual(install.setup_hint("mac"), f"{install.SETUP} --voice --mac")

    def test_without_a_checkout_it_falls_back_to_uv(self):
        from vision import install

        with patch.object(install, "SETUP", Path("/nowhere/setup.sh")):
            self.assertEqual(install.setup_hint("cpu"), "uv pip install 'vision[voice-cpu]'")
            self.assertIn("voice-nvidia", install.setup_hint())
            self.assertEqual(install.extra_hint("weather"), "pip install 'vision[weather]'")

    def test_extras_never_suggest_the_cuda_build_by_accident(self):
        from vision import install

        for extra in ("serve", "weather"):
            self.assertNotIn("all", install.extra_hint(extra))
        self.assertTrue(install.extra_hint("serve").endswith("setup.sh --serve"))
        self.assertIn("--extra weather", install.extra_hint("weather"))


class ServiceStatusTests(unittest.TestCase):
    """`vision service status` tells a service that is serving from one that only waits behind a
    `vision serve` in a terminal (it takes the port when that one stops)."""

    SERVER = {"pid": 4242, "url": "http://127.0.0.1:8765", "port": 8765, "token": "t"}

    def status(self, service_pid, server):
        from typer.testing import CliRunner

        from vision import cli, service

        with patch.object(service, "installed", return_value=True), patch.object(service, "running", return_value=True), \
                patch.object(service, "pid", return_value=service_pid), patch.object(service, "logs_hint", return_value="the logs"), \
                patch("vision.remote.running_server", return_value=server):
            return CliRunner().invoke(cli.app, ["service", "status"])

    def test_the_service_that_holds_the_port_is_running(self):
        result = self.status(4242, self.SERVER)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("running", result.output)
        self.assertNotIn("waiting", result.output)

    def test_a_service_behind_a_server_in_a_terminal_is_waiting(self):
        result = self.status(999, self.SERVER)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("waiting", result.output)
        self.assertIn("4242", result.output)
        self.assertNotIn("✓", result.output)

    def test_a_service_whose_pid_is_unknown_is_taken_as_the_one_running(self):
        self.assertIn("✓", self.status(None, self.SERVER).output)

    def test_a_service_with_no_server_answering_is_not_called_waiting(self):
        result = self.status(999, None)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not answering", result.output)

