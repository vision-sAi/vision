"""Microphone.record_utterance: end-pointing, cancellation and a mic that delivers nothing."""
import contextlib
import threading
import time
import unittest
from unittest import mock

from vision import audio
from vision.audio import MicStalled, Microphone
from vision.config import ListenConfig

FRAME = b"\0" * 1024  # 512 int16 samples of silence


def _speech(n: int) -> list[bytes]:
    return [(b"S").ljust(1024, b"\0")] * n


class _FakeVad:
    def reset(self):
        pass

    def mark_started(self):
        pass

    def is_speech(self, pcm: bytes, rms: float) -> bool:
        return pcm[:1] == b"S"


def _mic(frames: list[bytes], repeat: bool = False, **cfg) -> Microphone:
    """A Microphone whose stream hands `frames` to the callback as soon as it opens, then goes quiet
    (`repeat`: keeps cycling them from a thread every millisecond, like a live device)."""
    mic = Microphone.__new__(Microphone)
    mic.cfg = ListenConfig(end_silence_ms=320, max_utterance_s=10, start_timeout_s=0, **cfg)  # 10 quiet frames end it
    mic._vad = _FakeVad()
    mic.frame_len, mic.frame_ms, mic.name = 512, 32.0, "the test microphone"

    @contextlib.contextmanager
    def stream(callback=None):
        closed = threading.Event()

        def feed():
            while not closed.is_set():
                for f in frames:
                    callback(f, None, None, None)
                if not repeat:
                    return
                time.sleep(0.001)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            yield
        finally:
            closed.set()
            feeder.join()

    mic._stream = stream
    return mic


class RecordUtteranceTests(unittest.TestCase):
    def test_level_callback_tracks_speech_and_returns_to_zero_in_quiet(self):
        levels = []
        mic = _mic([FRAME] * 2 + _speech(5) + [FRAME] * 15)
        mic.record_utterance(on_level=levels.append)
        self.assertTrue(all(0 <= level <= 1 for level in levels))
        self.assertTrue(any(level > 0 for level in levels))
        self.assertEqual(levels[:2], [0, 0])
        self.assertEqual(levels[-10:], [0] * 10)

    def test_records_speech_with_preroll_until_silence(self):
        mic = _mic([FRAME] * 20 + _speech(5) + [FRAME] * 30)
        started = []
        audio_out = mic.record_utterance(on_speech_start=lambda: started.append(1))
        self.assertEqual(started, [1])
        # 400 ms of preroll (12 frames, the 3 speech frames that triggered it included) + 2 more speech + 10 quiet
        self.assertEqual(len(audio_out) // 512, 12 + 2 + 10)

    def test_previews_pause_in_the_quiet_and_the_speech_end_snapshot_has_all_the_speech(self):
        """Previews used to keep firing through the end silence, each a full transcription of audio
        that had not changed; the one at the first quiet frame is the whole utterance already."""
        snaps, ends = [], []
        mic = _mic(_speech(8) + [FRAME] * 20)
        audio_out = mic.record_utterance(on_audio=lambda a: snaps.append(a.size), every_ms=64,
                                         on_speech_end=lambda a: ends.append(a.size))
        self.assertEqual(ends, [9 * 512])  # once, at the first quiet frame, with every speech frame in it
        self.assertTrue(snaps)
        self.assertTrue(all(n <= 8 * 512 for n in snaps), snaps)  # none during the end silence
        self.assertEqual(audio_out.size, (8 + 10) * 512)

    def test_cancel_returns_promptly_even_with_no_audio(self):
        mic = _mic([])
        cancel = threading.Event()
        threading.Timer(0.15, cancel.set).start()
        t0 = time.monotonic()
        self.assertIsNone(mic.record_utterance(cancel=cancel))
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_start_timeout_with_only_silence(self):
        mic = _mic([FRAME], repeat=True)
        t0 = time.monotonic()
        self.assertIsNone(mic.record_utterance(start_timeout_s=0.2))
        self.assertAlmostEqual(time.monotonic() - t0, 0.2, delta=0.15)

    def test_silent_stream_raises_mic_stalled(self):
        mic = _mic([])
        with mock.patch.object(audio, "STALL_S", 0.25):
            t0 = time.monotonic()
            with self.assertRaises(MicStalled) as ctx:
                mic.record_utterance()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("the test microphone", str(ctx.exception))

    def test_push_to_talk_stops_on_event(self):
        mic = _mic(_speech(4))
        stop = threading.Event()
        threading.Timer(0.15, stop.set).start()
        self.assertEqual(len(mic.record_until_enter(stop)), 4 * 512)


if __name__ == "__main__":
    unittest.main()
