import os
import tempfile
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from unittest.mock import patch


class VoiceEnabledTests(unittest.TestCase):
    """[voice] enabled as config.toml says it now, for terminals that read their config before it changed."""

    def test_it_follows_the_file(self):
        from vision import config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "_voice_seen", (-1.0, True)):
                self.assertTrue(config.voice_enabled(), "no file: on")
                path.write_text("[voice]\nenabled = false\n")
                self.assertFalse(config.voice_enabled())
                time.sleep(0.01)
                path.write_text("[voice]\nengine = \"qwen3\"\n")
                os.utime(path, (time.time() + 5, time.time() + 5))
                self.assertTrue(config.voice_enabled(), "no key: on")

    def test_save_keeps_the_rest(self):
        from vision import config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[voice]\n# the voice\nvoice = \"jarvis\"\n")
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "ensure_dirs", lambda: None):
                config.save_voice_enabled(False)
            text = path.read_text()
            self.assertIn("enabled = false", text)
            self.assertIn('voice = "jarvis"', text)
            self.assertIn("# the voice", text)


class HubVoicePowerTests(unittest.TestCase):
    def _hub(self):
        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider, workdir = "claude", "/tmp"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        creating = patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id")))
        creating.start()
        self.addCleanup(creating.stop)
        hub = Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=False)
        self.posted = []
        hub.post = self.posted.append
        return hub

    def test_off_unloads_both_and_refuses_to_load_them_again(self):
        from vision.server import VoiceOff

        hub = self._hub()
        closed = []

        class Loaded:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        hub._speaker, hub._stt = Loaded("voice"), Loaded("ears")
        hub.voice_power(False)
        self.assertEqual(closed, ["ears", "voice"], "Whisper first, then the voice")
        self.assertIsNone(hub._speaker)
        self.assertIsNone(hub._stt)
        self.assertIn({"type": "voice_power", "on": False}, self.posted)
        with self.assertRaises(VoiceOff):
            hub.speaker()
        with self.assertRaises(VoiceOff):
            hub.stt()
        self.assertFalse(hub.hello()["voice_on"])
        self.assertTrue(hub.hello()["read_aloud"], "a reply can still be read aloud: the phone says it in its own voice")

    def test_on_loads_them_in_the_background_and_saves(self):
        hub = self._hub()
        hub.cfg.voice.enabled = False
        warmed, saved = [], []
        hub.warm_up = lambda titles=True: warmed.append(titles)
        with patch("vision.config.save_voice_enabled", saved.append):
            hub.voice_power(True, save=True)
        self.assertEqual(saved, [True])
        self.assertEqual(warmed, [False], "the voice and ears, not the chat titles again")
        self.assertTrue(hub.cfg.voice.enabled)
        self.assertIn({"type": "voice_power", "on": True}, self.posted)

    def test_starting_with_the_voice_off_loads_nothing(self):
        hub = self._hub()
        hub.cfg.voice.enabled = False
        with patch.object(hub, "speaker") as speaker, patch.object(hub, "stt") as stt:
            hub.warm_up(titles=False)
            time.sleep(0.1)
        speaker.assert_not_called()
        stt.assert_not_called()

    def test_the_endpoint_switches_it_and_speech_answers_503_while_off(self):
        from fastapi.testclient import TestClient

        from vision.server import create_app

        hub = self._hub()
        hub.warm_up = lambda titles=True: None
        app = create_app(hub)
        with patch("vision.config.save_voice_enabled", lambda on: None):
            client = TestClient(app)
            auth = {"Authorization": "Bearer token"}
            self.assertEqual(client.post("/voice/power", json={"on": False}, headers=auth).json(), {"on": False})
            r = client.post("/speak", json={"text": "hello"}, headers=auth)
            self.assertEqual(r.status_code, 503)
            self.assertIn("voice is off", r.json()["detail"])
            self.assertFalse(client.get("/voice", headers=auth).json()["on"])
            self.assertEqual(client.post("/voice/power", json={"on": "yes"}, headers=auth).status_code, 400)


if __name__ == "__main__":
    unittest.main()
