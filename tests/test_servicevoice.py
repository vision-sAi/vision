from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import numpy as np

from vision import servicevoice as sv
from vision.config import Config
from vision.tts import SAMPLE_RATE, TTSError

SERVER = {"url": "http://127.0.0.1:8765", "token": "tok"}


def http_error(code: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1:8765/x", code, "no", {}, io.BytesIO(json.dumps({"detail": detail}).encode()))


class FakeService:
    """Stands in for `_call`: records each request and answers it from `answers` (path → bytes or exception)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, str, bytes | None, str]] = []

    def __call__(self, server, method, path, body=None, content_type="", timeout=30):
        self.calls.append((method, path, body, content_type))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FindTheServiceTests(unittest.TestCase):
    def find(self, running, answers=None):
        fake = FakeService(answers or {"/voice/rate": b'{"rate": 1.0}'})
        with patch("vision.remote.running_server", return_value=running), patch.object(sv, "_call", fake):
            return sv.service(), fake

    def test_a_running_service_that_answers_is_borrowed_from_over_loopback(self):
        found, fake = self.find({"pid": 1, "url": "http://0.0.0.0:8765", "port": 8765, "token": "tok"})
        self.assertEqual(found, {"url": "http://127.0.0.1:8765", "token": "tok"})
        self.assertEqual(fake.calls[0][:2], ("GET", "/voice/rate"))

    def test_no_service_this_process_or_one_that_does_not_answer_is_none(self):
        self.assertIsNone(self.find(None)[0])
        self.assertIsNone(self.find({"pid": os.getpid(), "url": "u", "port": 1, "token": "t"})[0])
        down = urllib.error.URLError("refused")
        self.assertIsNone(self.find({"pid": 1, "url": "u", "port": 1, "token": "t"}, {"/voice/rate": down})[0])

    def test_without_a_service_the_terminal_keeps_its_own_voice_and_ears(self):
        from vision.stt import Transcriber
        from vision.tts import Speaker

        cfg = Config()
        with patch.object(sv, "service", return_value=None):
            self.assertIs(type(sv.speaker(cfg.voice)), Speaker)
            self.assertIs(type(sv.transcriber(cfg.listen)), Transcriber)
        with patch.object(sv, "service", return_value=SERVER):
            self.assertIsInstance(sv.speaker(cfg.voice), sv.ServiceSpeaker)
            self.assertIsInstance(sv.transcriber(cfg.listen), sv.ServiceTranscriber)


class ServiceSpeakerTests(unittest.TestCase):
    def setUp(self):
        self.speaker = sv.ServiceSpeaker(Config().voice, SERVER)
        tone = np.sin(np.linspace(0, 200, SAMPLE_RATE // 2)).astype(np.float32) * 0.5
        self.wav = sv._wav(tone, SAMPLE_RATE)

    def test_a_sentence_is_spoken_by_the_service_and_comes_back_as_audio_to_play_here(self):
        fake = FakeService({"/speak": self.wav})
        with patch.object(sv, "_call", fake):
            pieces = list(self.speaker.synth_stream("  Right, that's done.  "))
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].dtype, np.float32)
        self.assertEqual(len(pieces[0]), SAMPLE_RATE // 2)
        method, path, body, kind = fake.calls[0]
        self.assertEqual((method, path, kind), ("POST", "/speak", "application/json"))
        # already speechified by StreamingSpeaker: the service must not do it again
        self.assertEqual(json.loads(body), {"text": "Right, that's done.", "spoken": True})

    def test_nothing_to_say_asks_for_nothing(self):
        fake = FakeService({})
        with patch.object(sv, "_call", fake):
            self.assertEqual(list(self.speaker.synth_stream("   ")), [])
        self.assertEqual(fake.calls, [])

    def test_trouble_is_said_in_the_services_words(self):
        with patch.object(sv, "_call", FakeService({"/speak": http_error(500, "voice not downloaded")})):
            with self.assertRaisesRegex(TTSError, "voice not downloaded"):
                list(self.speaker.synth_stream("Hello."))
        with patch.object(sv, "_call", FakeService({"/voice": urllib.error.URLError("refused")})):
            with self.assertRaisesRegex(TTSError, "isn't answering"):
                self.speaker._load()

    def test_switching_the_voice_switches_the_services(self):
        fake = FakeService({"/voice": b"{}"})
        with patch.object(sv, "_call", fake):
            self.speaker.set_voice("jarvis")
        self.assertEqual((fake.calls[0][:2], json.loads(fake.calls[0][2])), (("POST", "/voice"), {"voice": "jarvis"}))
        with patch.object(sv, "_call", FakeService({"/voice": http_error(400, "unknown voice 'nope'")})):
            with self.assertRaisesRegex(TTSError, "unknown voice"):
                self.speaker.set_voice("nope")

    def test_it_says_where_the_voice_is(self):
        self.assertEqual((self.speaker.device, self.speaker.device_note), (sv.WHERE, sv.WHERE))


class ServiceTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.ears = sv.ServiceTranscriber(Config().listen, SERVER)
        self.speech = np.zeros(SAMPLE_RATE, dtype=np.float32)

    def test_what_was_heard_goes_as_a_wav_and_its_words_come_back(self):
        fake = FakeService({"/transcribe": b'{"text": " Hello there. "}'})
        with patch.object(sv, "_call", fake):
            self.assertEqual(self.ears.transcribe(self.speech), "Hello there.")
        method, path, body, kind = fake.calls[0]
        self.assertEqual((method, path), ("POST", "/transcribe"))
        self.assertTrue(kind.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="speech.wav"', body)
        self.assertIn(b"RIFF", body)

    def test_a_preview_is_asked_for_as_one_and_a_scrap_of_sound_is_not_sent(self):
        fake = FakeService({"/transcribe?preview=1": b'{"text": "Hello"}'})
        with patch.object(sv, "_call", fake):
            self.assertEqual(self.ears.preview(self.speech), "Hello")
            self.assertEqual(self.ears.transcribe(np.zeros(100, dtype=np.float32)), "")
        self.assertEqual(len(fake.calls), 1)

    def test_warming_up_finds_out_whether_live_previews_are_quick_enough(self):
        with patch.object(sv, "_call", FakeService({"/transcribe?preview=1": b'{"text": ""}'})):
            self.ears.warm_up()
        self.assertTrue(self.ears.can_preview_live)
        self.assertEqual(self.ears.device, sv.WHERE)

        slow = sv.ServiceTranscriber(Config().listen, SERVER)
        with patch.object(sv, "_call", FakeService({"/transcribe?preview=1": http_error(409, "live preview needs Whisper on the GPU")})):
            slow.warm_up()
        self.assertFalse(slow.can_preview_live)

        down = sv.ServiceTranscriber(Config().listen, SERVER)
        with patch.object(sv, "_call", FakeService({"/transcribe?preview=1": urllib.error.URLError("refused")})):
            with self.assertRaisesRegex(RuntimeError, "isn't answering"):
                down.warm_up()


class LivePreviewTests(unittest.TestCase):
    def test_live_words_use_the_ears_preview_when_they_have_one(self):
        import threading

        from vision.stt import LiveTranscript

        asked, heard = [], threading.Event()

        class Ears:
            def transcribe(self, audio):
                asked.append("transcribe")
                return "final"

            def preview(self, audio):
                asked.append("preview")
                return "so far"

        live = LiveTranscript(Ears(), lambda text: heard.set())
        live.feed(np.zeros(SAMPLE_RATE, dtype=np.float32))
        self.assertTrue(heard.wait(5))
        live.stop()
        self.assertEqual(asked, ["preview"])


class SpeakEndpointTests(unittest.TestCase):
    def test_text_said_to_be_ready_is_not_speechified_again(self):
        from vision import server

        said = []

        class Voice:
            def synth(self, text):
                said.append(text)
                return np.zeros(10, dtype=np.float32)

        hub = server.Hub.__new__(server.Hub)
        hub.speaker = lambda: Voice()
        with patch("vision.tts.speechify", lambda t: f"speechified {t}"):
            hub.speak_bytes("**bold** 50%")
            hub.speak_bytes("ready as it is", spoken=True)
        self.assertEqual(said, ["speechified **bold** 50%", "ready as it is"])


if __name__ == "__main__":
    unittest.main()
