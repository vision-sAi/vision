from __future__ import annotations

import contextlib
import threading
import unittest

import numpy as np

from vision.config import ListenConfig
from vision.wake import CUT_IN_S, HOP_S, PROBE_S, BargeIn, WakeListener, match_wake

NAMES = ["vision"]


class MatchWakeTests(unittest.TestCase):
    def test_name_alone_is_an_empty_command(self):
        for text in ("Vision", "Vision.", "vision!", "Hey Vision.", "Okay, Vision"):
            self.assertEqual(match_wake(text, NAMES), "", text)

    def test_request_after_the_name_is_the_command(self):
        self.assertEqual(match_wake("Vision, what's the time?", NAMES), "What's the time?")
        self.assertEqual(match_wake("Hey Vision, what's the weather like today?", NAMES), "What's the weather like today?")
        self.assertEqual(match_wake("Okay, Vision. Set a timer.", NAMES), "Set a timer.")

    def test_name_at_the_end_keeps_the_request(self):
        self.assertEqual(match_wake("What time is it, Vision?", NAMES), "What time is it")

    def test_whisper_spellings_of_the_name_count(self):
        self.assertEqual(match_wake("Envision, turn the lights off", NAMES), "Turn the lights off")
        self.assertEqual(match_wake("Vision's here.", NAMES), "Here.")

    def test_similar_words_do_not_wake(self):
        for text in ("The new version is out.", "Mission accomplished", "Thank you.", "", "Fishing trip"):
            self.assertIsNone(match_wake(text, NAMES), text)

    def test_the_word_in_mid_sentence_is_not_the_name(self):
        self.assertIsNone(match_wake("I can't see anything, my vision is blurry.", NAMES))
        self.assertEqual(match_wake("Right, so, Vision, lights please", NAMES), "Right, lights please")  # "so" is a lead-in

    def test_custom_names(self):
        self.assertEqual(match_wake("Jarvis, lights", ["jarvis"]), "Lights")
        self.assertIsNone(match_wake("Vision, lights", ["jarvis"]))

    def test_anywhere_keeps_only_what_follows_the_name(self):
        # Cutting into a reply: the mic heard Vision's own voice first.
        self.assertEqual(match_wake("and that is why the sky is blue. Vision, stop.", NAMES, anywhere=True), "Stop.")
        self.assertEqual(match_wake("the sky is blue. Hey Vision, what about Mars?", NAMES, anywhere=True), "What about Mars?")
        self.assertEqual(match_wake("blue. Vision.", NAMES, anywhere=True), "")
        self.assertEqual(match_wake("Vision, stop", NAMES, anywhere=True), "Stop")
        self.assertIsNone(match_wake("the sky is blue", NAMES, anywhere=True))


class _FakeMic:
    """Feeds scripted frames through the callback stream; a frame starting with b"S" is speech."""

    frame_ms = 32.0

    def __init__(self, frames: list[bytes]):
        self.frames = frames
        self.callback = None

    @contextlib.contextmanager
    def _stream(self, callback=None):
        for f in self.frames:
            callback(f, None, None, None)
        yield

    def vad_reset(self):
        pass

    def vad_started(self):
        pass

    def is_speech(self, pcm: bytes) -> bool:
        return pcm[:1] == b"S"


def _utterance(kind: str, speech_frames: int, quiet_frames: int) -> list[bytes]:
    frame = 1024  # 512 int16 samples
    return [(b"S" + kind.encode()).ljust(frame, b"\0")] * speech_frames + [b"\0" * frame] * quiet_frames


class _ListenerCase(unittest.TestCase):
    def setUp(self):
        self.listener = WakeListener.__new__(WakeListener)
        self.listener.names = ("vision",)
        self.listener.listen = ListenConfig(end_silence_ms=320, max_utterance_s=10)  # 10 quiet frames end an utterance
        self.probes: list[int] = []

        def probe(frames: list[bytes], anywhere: bool = False):
            self.probes.append(len(frames))
            voiced = [f for f in frames if f[:1] == b"S"]
            if not voiced:
                return None
            kind = frames[-1][1:2] if frames[-1][:1] == b"S" else voiced[0][1:2]
            return {b"n": "", b"c": "What's the time?"}.get(kind)

        self.listener._probe = probe


class WakeListenerTests(_ListenerCase):
    def wait(self, frames: list[bytes]):
        self.listener.mic = _FakeMic(frames)
        return self.listener.wait(threading.Event())

    def test_returns_the_whole_utterance_that_carries_the_name(self):
        frames = _utterance("x", 20, 12) + _utterance("c", 120, 12)
        audio, command = self.wait(frames)
        self.assertEqual(command, "What's the time?")
        # preroll frames (the ~400 ms before speech, quiet here) + 120 speech frames, nothing from the ignored one
        self.assertGreaterEqual(audio.size // 512, 120)
        self.assertLess(audio.size // 512, 120 + 13)

    def test_probes_once_early_and_drops_utterances_without_the_name(self):
        probe_frames = int(PROBE_S * 1000 / _FakeMic.frame_ms)
        frames = _utterance("x", 300, 12) + _utterance("n", 20, 12)
        audio, command = self.wait(frames)
        self.assertEqual(command, "")
        self.assertEqual(len(self.probes), 2)
        self.assertLessEqual(self.probes[0], probe_frames)  # long chatter: decided after PROBE_S, not at its end
        self.assertLess(self.probes[1], probe_frames)  # a short "Vision": decided when it ended

    def test_cancel_stops_waiting(self):
        cancel = threading.Event()
        cancel.set()
        self.listener.mic = _FakeMic(_utterance("c", 30, 12))
        self.assertIsNone(self.listener.wait(cancel))

    def test_paused_audio_never_wakes(self):
        self.listener.mic = _FakeMic(_utterance("c", 30, 12) + _utterance("c", 30, 12))
        cancel = threading.Event()
        calls = {"n": 0}

        def paused():
            calls["n"] += 1
            if calls["n"] > 60:  # unpause once the scripted audio has run out
                cancel.set()
            return True

        self.assertIsNone(self.listener.wait(cancel, paused=paused))
        self.assertEqual(self.probes, [])


class CutInTests(_ListenerCase):
    """While a reply is on (`replying`), the name is probed for in a sliding window and stops the reply."""

    def wait(self, frames: list[bytes], **kw):
        self.listener.mic = _FakeMic(frames)
        self.spotted: list[int] = []
        kw.setdefault("replying", lambda: True)
        kw.setdefault("on_spotted", lambda: self.spotted.append(len(self.probes)))
        return self.listener.wait(threading.Event(), **kw)

    def test_the_name_over_the_reply_is_spotted_before_the_utterance_ends(self):
        # Vision talks for a while ("x": no name), then you say the name ("c"), all without a pause.
        frames = _utterance("x", 200, 0) + _utterance("c", 60, 12)
        hop = int(HOP_S * 1000 / _FakeMic.frame_ms)
        audio, command = self.wait(frames)
        self.assertEqual(command, "What's the time?")
        self.assertEqual(len(self.spotted), 1)
        self.assertGreaterEqual(len(self.probes), 200 // hop)  # probed every hop over the reply, not once
        # The utterance handed back starts at the probed window (≤ PROBE_S), so it runs through to the end.
        n = audio.size // 512
        self.assertLessEqual(n, int(PROBE_S * 1000 / _FakeMic.frame_ms) + 60 + 12)
        self.assertGreaterEqual(n, 60)

    def test_quiet_windows_are_not_probed(self):
        # Headphones: nothing on the mic while Vision speaks. Spotting is by voice, so no model calls.
        cancel = threading.Event()
        quiet = [b"\0" * 1024] * 200
        self.listener.mic = _FakeMic(quiet)
        calls = {"n": 0}

        def replying():
            calls["n"] += 1
            if calls["n"] >= len(quiet):
                cancel.set()
            return True

        self.assertIsNone(self.listener.wait(cancel, replying=replying))
        self.assertEqual(self.probes, [])

    def test_any_speech_cuts_in_without_a_probe(self):
        cut = int(CUT_IN_S * 1000 / _FakeMic.frame_ms)
        audio, command = self.wait(_utterance("x", cut + 20, 12), speech_cuts_in=True)
        self.assertEqual(command, "")
        self.assertEqual(self.probes, [])
        self.assertEqual(len(self.spotted), 1)

    def test_a_cough_does_not_cut_in(self):
        cut = int(CUT_IN_S * 1000 / _FakeMic.frame_ms)
        cancel = threading.Event()
        frames = _utterance("x", max(1, cut - 2), 40)
        self.listener.mic = _FakeMic(frames)
        seen = {"n": 0}

        def replying():
            seen["n"] += 1
            if seen["n"] >= len(frames):
                cancel.set()
            return True

        self.assertIsNone(self.listener.wait(cancel, replying=replying, speech_cuts_in=True))

    def test_after_the_reply_it_end_points_again(self):
        # `replying` turns false: the reply is over and the name must open an utterance as usual.
        frames = _utterance("x", 30, 0) + _utterance("c", 40, 12)
        state = {"i": 0}

        def replying():
            state["i"] += 1
            return state["i"] <= 30

        audio, command = self.wait(frames, replying=replying)
        self.assertEqual(command, "What's the time?")
        self.assertEqual(self.spotted, [])  # a plain wake, not a cut-in
        self.assertEqual(len(self.probes), 1)


class BargeInTests(unittest.TestCase):
    def setUp(self):
        self.listener = WakeListener.__new__(WakeListener)
        self.listener.names = ("vision",)
        self.listener.listen = ListenConfig(end_silence_ms=320, max_utterance_s=10)
        self.listener._probe = lambda frames, anywhere=False: "Stop" if any(f[:2] == b"Sc" for f in frames) else None
        self.cuts = 0

    def cut(self):
        self.cuts += 1

    def test_returns_what_was_said_after_cutting_in(self):
        self.listener.mic = _FakeMic(_utterance("x", 100, 0) + _utterance("c", 50, 12))
        barge = BargeIn(self.listener, "wake", on_cut=self.cut).start()
        hit = barge.stop()
        self.assertEqual(self.cuts, 1)
        self.assertTrue(barge.cut.is_set())
        self.assertEqual(hit[1], "Stop")
        self.assertIsNone(barge.error)

    def test_nothing_said_is_none(self):
        self.listener.mic = _FakeMic([])  # the stream ends at once; the loop then waits on cancel
        barge = BargeIn(self.listener, "wake", on_cut=self.cut).start()
        self.assertIsNone(barge.stop())
        self.assertEqual(self.cuts, 0)

    def test_a_dead_mic_is_reported_not_raised(self):
        class BadMic(_FakeMic):
            @contextlib.contextmanager
            def _stream(self, callback=None):
                raise RuntimeError("no such input device")
                yield

        self.listener.mic = BadMic([])
        barge = BargeIn(self.listener, "speech", on_cut=self.cut).start()
        self.assertIsNone(barge.stop())
        self.assertIsInstance(barge.error, RuntimeError)


if __name__ == "__main__":
    unittest.main()
