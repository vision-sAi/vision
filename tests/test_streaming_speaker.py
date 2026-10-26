import threading
import time
import types
import unittest

import numpy as np

from vision.tts import SAMPLE_RATE, StreamingSpeaker, speechify


class FakeSpeaker:
    """A voice that 'speaks' at a fixed pace: synthesis at twice realtime, playback in real time."""

    CPS = 15.0  # chars of text per second of speech
    PIECE_S = 0.4  # audio comes out in pieces this long, like the real decoder's frame groups

    def __init__(self):
        self._stop = threading.Event()
        self._playing = threading.Event()
        self.cfg = types.SimpleNamespace(voice="narrator", rate=1.0)
        self.voice = "narrator"
        self.played = 0.0  # seconds handed to the fake device

    def _load(self):
        pass

    def _voice_rate(self):
        return 1.0

    def available_voices(self):
        return ["narrator"]

    def set_voice(self, spec):
        self.voice = spec

    def is_playing(self):
        return self._playing.is_set()

    def open_stream(self):
        return types.SimpleNamespace(start=lambda: None, stop=lambda: None, close=lambda: None)

    def close_stream(self, stream):
        pass

    def synth_stream(self, text, cont=False):
        total = len(text) / self.CPS
        made = 0.0
        while made < total:
            d = min(self.PIECE_S, total - made)
            time.sleep(d / 2)
            made += d
            yield np.zeros(int(SAMPLE_RATE * d), dtype=np.float32)

    def play(self, audio, stream=None):
        self._playing.set()
        step = 2400
        for i in range(0, len(audio), step):
            if self._stop.is_set():
                break
            n = len(audio[i : i + step])
            time.sleep(n / SAMPLE_RATE)
            self.played += n / SAMPLE_RATE
        self._playing.clear()

    def stop(self):
        self._stop.set()


class OutputStreamTests(unittest.TestCase):
    def test_the_device_is_opened_while_the_first_sentence_is_still_being_made(self):
        """Opened at the first piece, the device's set-up sat inside the time to first sound."""
        marks = {}

        class Speaker(FakeSpeaker):
            def open_stream(self):
                marks.setdefault("opened", time.monotonic())
                return super().open_stream()

            def synth_stream(self, text, cont=False):
                for piece in super().synth_stream(text, cont):
                    marks.setdefault("first_piece", time.monotonic())
                    yield piece

        ss = StreamingSpeaker(Speaker())
        ss.feed("Hello there, this is a sentence. ")
        ss.finish()
        self.assertLess(marks["opened"], marks["first_piece"])


REPLY = (
    "Right, that one is sorted now. The stream was jumping ahead in steps because the gate only moved "
    "once per piece of audio, and it glides along with the voice now.\n\n"
    "Give it a go and tell me how it reads."
)


class PhoenixSpeaker(FakeSpeaker):
    def __init__(self):
        super().__init__()
        self.cfg.voice = self.voice = "phoenix-conversational"
        self.chosen: list[str] = []
        self.spoken: list[tuple[str, str, bool]] = []  # (voice, text, continued the reply?) per chunk

    def available_voices(self):
        return ["phoenix-conversational", "phoenix-expressive", "phoenix-reassuring"]

    def set_voice(self, spec):
        self.voice = spec
        self.chosen.append(spec)

    def synth_stream(self, text, cont=False):
        self.synth_started = getattr(self, "synth_started", time.monotonic())
        self.spoken.append((self.voice, text, cont))
        yield from super().synth_stream(text, cont)


class ReplyVoiceChoiceTests(unittest.TestCase):
    def test_each_chunk_picks_its_own_style(self):
        # A flat opening, then excitement: the voice changes at the sentence where the mood does.
        sp = PhoenixSpeaker()
        ss = StreamingSpeaker(sp)
        ss.feed("Right, had a look at the logs. ")
        time.sleep(0.05)
        ss.feed("Nothing odd in there. It was the cache all along! Cleared it and we're flying now! ")
        ss.finish()
        self.assertEqual(sp.chosen, ["phoenix-expressive"])
        voices = [v for v, _, _ in sp.spoken]
        self.assertEqual(voices, ["phoenix-conversational", "phoenix-conversational", "phoenix-expressive"])
        self.assertTrue(sp.spoken[2][1].startswith("It was the cache"), "the expressive sentences make their own chunk")
        self.assertIn("flying now!", sp.spoken[2][1], "sentences in the same style still merge")
        conts = [c for _, _, c in sp.spoken]
        self.assertEqual(conts, [False, True, False], "same style continues the reply; a new style starts cold")

    def test_style_settles_back_after_the_excitement(self):
        sp = PhoenixSpeaker()
        ss = StreamingSpeaker(sp)
        ss.feed("Get in, that is massive! ")
        time.sleep(0.05)
        ss.feed("Sorry, one thing I should mention. The build still needs a rerun before you ship. ")
        ss.finish()
        self.assertEqual(sp.chosen, ["phoenix-expressive", "phoenix-reassuring", "phoenix-conversational"])
        self.assertEqual([c for _, _, c in sp.spoken], [False, False, False])

    def test_plain_reply_stays_conversational_and_starts_promptly(self):
        sp = PhoenixSpeaker()
        ss = StreamingSpeaker(sp)
        t0 = time.monotonic()
        ss.feed("Right, had a look at the logs. Nothing odd in there. ")
        ss.finish()
        self.assertLess(sp.synth_started - t0, 0.2, "nothing to wait for")
        self.assertEqual(sp.chosen, [], "no switch needed: already conversational")
        self.assertEqual(sp.voice, "phoenix-conversational")

    def test_other_voices_never_wait_or_switch(self):
        sp = FakeSpeaker()
        ss = StreamingSpeaker(sp)
        ss.feed("Perfect! ")
        ss.finish()
        self.assertEqual(sp.voice, "narrator")


class StreamingSpeakerRevealTests(unittest.TestCase):
    def test_reveal_glides_with_the_voice(self):
        sp = FakeSpeaker()
        ss = StreamingSpeaker(sp)
        # The whole reply lands at once, as it does from Codex: the reveal is then paced by the voice alone.
        ss.feed(REPLY)
        threading.Thread(target=ss.finish, daemon=True).start()

        samples: list[tuple[float, int]] = []
        t0 = time.monotonic()
        while ss._play_thread.is_alive():
            samples.append((time.monotonic() - t0, ss.spoken))
            time.sleep(1 / 60)
        self.assertGreaterEqual(ss.spoken, len(REPLY))

        values = [v for _, v in samples]
        self.assertEqual(values, sorted(values), "the gate must never move backwards")
        moving = [(t, v) for t, v in samples if 0 < v < len(REPLY)]
        self.assertGreater(len(moving), 60, "expected several seconds of gated reveal")
        steps = [b - a for (_, a), (_, b) in zip(moving, moving[1:])]
        self.assertLessEqual(max(steps), 4, f"the gate lurched by {max(steps)} chars in one frame")
        # While the voice is talking the gate never sits still for long (15 cps = a char every 67 ms).
        stalls = [t2 - t1 for (t1, a), (t2, b) in zip(moving, moving[1:]) if b == a]
        gaps, run = [], 0.0
        for (t1, a), (t2, b) in zip(moving, moving[1:]):
            run = run + (t2 - t1) if b == a else 0.0
            gaps.append(run)
        self.assertLess(max(gaps), 0.3, f"the gate stalled for {max(gaps):.2f}s mid-speech")
        # And it tracks the voice: chars said ~ 15/s of audio actually played, within a word or two.
        drift = [v - sp.CPS * min(t, len(REPLY) / sp.CPS) for t, v in moving]
        self.assertLess(max(abs(d) for d in drift), 12, f"the gate drifted {max(abs(d) for d in drift):.0f} chars from the voice")

    def test_silent_chunk_and_stop(self):
        table = "| a | b | c |\n"
        self.assertEqual(speechify(table), "", "a lone table row is nothing to say aloud")
        sp = FakeSpeaker()
        ss = StreamingSpeaker(sp)
        ss.feed(table)
        time.sleep(0.2)
        self.assertEqual(ss.spoken, len(table), "a chunk with nothing to say still counts as shown")
        ss.feed("And this one is spoken. ")
        time.sleep(0.5)
        self.assertGreater(ss.spoken, len(table))
        ss.stop()
        ss.finish()
        self.assertGreaterEqual(ss.spoken, 1 << 30)


class RecordingSpeaker(StreamingSpeaker):
    """Records what goes to the voice, and when, without the voice being fast enough to matter."""

    def __init__(self, *a, **k):
        self.emitted: list[tuple[str, str]] = []
        super().__init__(*a, **k)

    def _emit(self, text, kind="sentence"):
        self.emitted.append((text, kind))
        super()._emit(text, kind)


class TailFlushTests(unittest.TestCase):
    """A reply's last sentence has no space after it; it must not wait for the turn to end."""

    def test_finished_last_sentence_is_spoken_once_the_text_goes_quiet(self):
        ss = RecordingSpeaker(FakeSpeaker())
        ss.feed("Right, that is sorted now. ")
        ss.feed("Give it a go")
        ss.feed(" and tell me how it reads.")
        self.assertEqual([t for t, _ in ss.emitted], ["Right, that is sorted now. "])
        time.sleep(ss.TAIL_IDLE_S + 0.2)  # no finish(), no tool call: the brain is still wrapping up
        self.assertEqual([t for t, _ in ss.emitted], ["Right, that is sorted now. ", "Give it a go and tell me how it reads."])
        ss.flush()  # a tool call or the turn's end afterwards adds nothing
        ss.finish()
        self.assertEqual(len(ss.emitted), 2, "no sentence twice")

    def test_a_pause_inside_a_number_does_not_split_it(self):
        ss = RecordingSpeaker(FakeSpeaker())
        ss.feed("It costs about 3.")
        time.sleep(ss.TAIL_IDLE_S / 3)
        ss.feed("5 dollars a month.")
        time.sleep(ss.TAIL_IDLE_S + 0.2)
        self.assertEqual([t for t, _ in ss.emitted], ["It costs about 3.5 dollars a month."])
        ss.finish()
        self.assertEqual(len(ss.emitted), 1)

    def test_unfinished_tail_waits_longer_but_not_for_the_turn(self):
        ss = RecordingSpeaker(FakeSpeaker())
        ss.feed("- the last item on the list")
        time.sleep(ss.TAIL_IDLE_S + 0.2)
        self.assertEqual(ss.emitted, [], "a tail with no full stop gets longer to carry on")
        time.sleep(ss.IDLE_S)
        self.assertEqual([t for t, _ in ss.emitted], ["- the last item on the list"])
        ss.finish()
        self.assertEqual(len(ss.emitted), 1)

    def test_abbreviation_at_the_end_is_not_a_sentence_end(self):
        ss = RecordingSpeaker(FakeSpeaker())
        ss.feed("Ask Dr.")
        time.sleep(ss.TAIL_IDLE_S + 0.2)
        self.assertEqual(ss.emitted, [])
        ss.feed(" Smith about it. ")
        self.assertEqual([t for t, _ in ss.emitted], ["Ask Dr. Smith about it. "])
        ss.finish()

    def test_stop_cancels_a_pending_tail(self):
        ss = RecordingSpeaker(FakeSpeaker())
        ss.feed("This one is cut off.")
        ss.stop()
        time.sleep(ss.TAIL_IDLE_S + 0.2)
        self.assertEqual(ss.emitted, [])
        ss.finish()


if __name__ == "__main__":
    unittest.main()
