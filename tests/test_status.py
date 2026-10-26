"""vision.status: the speech rows and problems /health hands the phone. Models and the Mac are faked."""
import unittest
from types import SimpleNamespace
from unittest import mock

from vision import status, stt, tts
from vision.config import MLX_TTS_TIERS, ListenConfig, VoiceConfig


class StatusTests(unittest.TestCase):
    def setUp(self):
        # A Mac with the voice-mac build, the voice saved, its model downloaded and timed on this chip.
        self.env = {
            "uses_mlx": True, "present": True, "voices": ["phoenix"],
            "choice": {"model": MLX_TTS_TIERS[0], "rtf": 0.65, "chip": "Apple Silicon"}, "chip": "Apple Silicon",
        }
        patches = [
            mock.patch.object(tts, "uses_mlx", lambda cfg: self.env["uses_mlx"]),
            mock.patch.object(tts, "models_present", lambda cfg: self.env["present"]),
            mock.patch("vision.config.saved_voices", lambda: self.env["voices"]),
            mock.patch.object(tts, "mlx_choice", lambda: self.env["choice"]),
            mock.patch.object(tts, "mac_chip", lambda: self.env["chip"]),
            mock.patch.object(stt, "mlx_whisper_repo", lambda cfg: "mlx-community/whisper-large-v3-turbo-asr-8bit"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def check(self, speaker=None, transcriber=None, stt_error=""):
        return status.server_status(VoiceConfig(voice="phoenix"), ListenConfig(), speaker, transcriber, stt_error)

    def test_a_healthy_mac_reads_plainly_and_has_no_problems(self):
        speaker = SimpleNamespace(_loaded=True, _load_error=None, device="mlx", device_note="mlx 8bit")
        result = self.check(speaker, SimpleNamespace(device="mlx/large-v3-turbo"))
        self.assertEqual(result["rows"], [
            {"label": "Voice", "value": "phoenix · 1.7B 8-bit · 0.65 s per second of speech", "state": "ready"},
            {"label": "Speech recognition", "value": "large-v3-turbo", "state": "ready"},
        ])
        self.assertEqual(result["problems"], [])

    def test_states_follow_what_the_hub_has_loaded(self):
        self.assertEqual([r["state"] for r in self.check()["rows"]], ["off", "off"])
        loading = self.check(SimpleNamespace(_loaded=False, _load_error=None), SimpleNamespace(device="?"))
        self.assertEqual([r["state"] for r in loading["rows"]], ["loading", "loading"])
        failed = self.check(SimpleNamespace(_loaded=False, _load_error=(RuntimeError("GPU busy"), 0.0)),
                            SimpleNamespace(device="?"), stt_error="no model")
        self.assertEqual([r["state"] for r in failed["rows"]], ["failed", "failed"])
        self.assertIn("The voice did not load: GPU busy", failed["problems"])
        self.assertIn("Speech recognition did not load: no model", failed["problems"])

    def test_setup_problems_say_what_to_run(self):
        self.env.update(voices=[], present=False)
        problems = self.check()["problems"]
        self.assertTrue(any("vision voice design phoenix" in p for p in problems))
        self.assertTrue(any("vision setup" in p for p in problems))

    def test_a_slow_or_mistimed_voice_is_a_problem(self):
        self.env["choice"] = dict(self.env["choice"], rtf=1.3)
        self.assertTrue(any("break up" in p for p in self.check()["problems"]))
        self.env["choice"] = dict(self.env["choice"], rtf=0.6, chip="Apple M1")
        self.assertTrue(any("--retime" in p for p in self.check()["problems"]))

    def test_a_torch_voice_on_the_cpu_is_a_problem(self):
        self.env["uses_mlx"] = False
        speaker = SimpleNamespace(_loaded=True, _load_error=None, device="cpu", device_note="cpu")
        result = self.check(speaker, SimpleNamespace(device="cpu/small.en"))
        self.assertEqual(result["rows"][0]["value"], "phoenix")
        self.assertEqual(result["rows"][1]["value"], "small.en")
        self.assertTrue(any("on the CPU" in p for p in result["problems"]))

    def test_a_broken_check_is_a_problem_not_an_error(self):
        with mock.patch.object(status, "voice_status", side_effect=ModuleNotFoundError("numpy")):
            result = self.check()
        self.assertEqual([r["label"] for r in result["rows"]], ["Speech recognition"])
        self.assertIn("Could not check: numpy", result["problems"])


if __name__ == "__main__":
    unittest.main()
