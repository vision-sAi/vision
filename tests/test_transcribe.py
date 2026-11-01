"""POST /transcribe and hello `transcribe`: what the desktop app's mic button goes by."""
from __future__ import annotations

import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from unittest.mock import patch

from vision import server

AUTH = {"Authorization": "Bearer tok"}


class WhisperInstalledTests(unittest.TestCase):
    def _found(self, *present):
        return lambda name, *a, **kw: object() if name in present else None

    def test_faster_whisper_and_soundfile_are_enough(self):
        with patch("importlib.util.find_spec", self._found("soundfile", "faster_whisper")):
            self.assertTrue(server.whisper_installed())

    def test_no_voice_extra_means_no_transcribing(self):
        with patch("importlib.util.find_spec", self._found("soundfile")):
            self.assertFalse(server.whisper_installed())
        with patch("importlib.util.find_spec", self._found("faster_whisper")):
            self.assertFalse(server.whisper_installed(), "the WAV can't be read without soundfile")

    def test_a_mac_with_mlx_whisper_counts(self):
        with patch("importlib.util.find_spec", self._found("soundfile")), patch("vision.mlxrun.mlx_available", lambda: True):
            self.assertTrue(server.whisper_installed())


class TranscribeRouteTests(unittest.TestCase):
    def _client(self, transcribe_bytes):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "tok", log=lambda _: None, transcribe_bytes=transcribe_bytes)
        return TestClient(server.create_app(hub))

    def _post(self, client):
        return client.post("/transcribe", files={"file": ("speech.wav", b"RIFF....WAVE", "audio/wav")}, headers=AUTH)

    def test_the_words_come_back(self):
        r = self._post(self._client(lambda wav: "hello there"))
        self.assertEqual((r.status_code, r.json()), (200, {"text": "hello there"}))

    def test_the_voice_switched_off_is_503_not_a_bad_recording(self):
        def off(wav):
            raise server.VoiceOff()

        r = self._post(self._client(off))
        self.assertEqual((r.status_code, r.json()["detail"]), (503, server.VOICE_OFF))

    def test_no_whisper_installed_is_503(self):
        def missing(wav):
            raise ModuleNotFoundError("No module named 'faster_whisper'")

        r = self._post(self._client(missing))
        self.assertEqual(r.status_code, 503)
        self.assertIn("isn't installed", r.json()["detail"])

    def test_audio_it_can_not_read_is_still_422(self):
        def garbled(wav):
            raise RuntimeError("Format not recognised.")

        self.assertEqual(self._post(self._client(garbled)).status_code, 422)


class HelloTests(unittest.TestCase):
    def test_hello_says_whether_it_can_transcribe(self):
        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider, workdir = "claude", "/tmp"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        with patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id"))):
            hub = Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=False)
            hub.post = lambda frame: None
            for installed in (True, False):
                with patch.object(server, "whisper_installed", lambda: installed):
                    self.assertIs(hub.hello()["transcribe"], installed)


if __name__ == "__main__":
    unittest.main()
