"""vision.devrate: streams open at the device's own rate and Vision resamples, seamlessly, in pieces."""
import importlib.util
import sys
import types
import unittest
from unittest import mock

import numpy as np

from vision import devrate
from vision.config import AudioDevice

HAS_SOXR = importlib.util.find_spec("soxr") is not None
needs_soxr = unittest.skipUnless(HAS_SOXR, "needs soxr (the voice extras)")


def _sine(freq: float, rate: int, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _dominant(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[spectrum.argmax()])


@needs_soxr
class ResamplerTests(unittest.TestCase):
    def test_same_rate_passes_audio_through(self):
        x = _sine(440, 24000, 0.1)
        np.testing.assert_array_equal(devrate.Resampler(24000, 24000).feed(x), x)

    def test_pieces_resample_as_one_signal(self):
        # A reply arrives in ~0.3 s pieces; resampled one by one they must join with no seam.
        x = _sine(440, 24000, 2.0)
        whole = devrate.Resampler(24000, 48000).feed(x, last=True)
        rs = devrate.Resampler(24000, 48000)
        pieces = [rs.feed(x[i : i + 7680]) for i in range(0, len(x), 7680)]
        pieces.append(rs.feed(np.zeros(0, dtype=np.float32), last=True))
        streamed = np.concatenate(pieces)
        self.assertEqual(len(streamed), len(whole))
        self.assertLess(np.abs(streamed - whole).max(), 1e-4)
        # No clicks: a 440 Hz sine of amplitude 0.5 at 48 kHz never steps more than ~0.029 per sample.
        self.assertLess(np.abs(np.diff(streamed[1000:-1000])).max(), 0.035)

    def test_pitch_survives_awkward_ratios(self):
        for dst in (44100, 48000):
            y = devrate.Resampler(24000, dst).feed(_sine(1000, 24000, 1.0), last=True)
            self.assertAlmostEqual(len(y) / dst, 1.0, delta=0.01)
            self.assertAlmostEqual(_dominant(y, dst), 1000, delta=3)


@needs_soxr
class InputResamplerTests(unittest.TestCase):
    def test_device_blocks_become_exact_vad_frames_at_16k(self):
        got = []
        wrap = devrate.InputResampler(lambda pcm, n, t, s: got.append(pcm), 16000, 48000, 512)
        x = (_sine(300, 48000, 1.0) * 32767).astype(np.int16)
        for i in range(0, len(x), 1234):  # a block size that divides nothing
            wrap(x[i : i + 1234].tobytes(), None, None, None)
        self.assertTrue(got)
        self.assertTrue(all(len(f) == 512 * 2 for f in got))
        self.assertGreaterEqual(len(got), 16000 // 512 - 2)  # one second of audio, less the filter's delay
        y = np.frombuffer(b"".join(got), dtype=np.int16).astype(np.float32)
        self.assertAlmostEqual(_dominant(y, 16000), 300, delta=3)


class _FakeStream:
    opened = []

    def __init__(self, samplerate, **kw):
        if samplerate in _FakeStream.refuse:
            raise RuntimeError("Invalid sample rate")
        self.samplerate = samplerate
        self.latency = 0.2
        self.written = []
        self.active = self.stopped = False
        _FakeStream.opened.append(self)

    def write(self, a):
        if not self.active:
            raise RuntimeError("Stream is stopped")  # as PortAudio does
        self.written.append(np.asarray(a).copy())

    def start(self):
        self.active = True

    def stop(self):
        self.active, self.stopped = False, True

    def abort(self):
        self.active = False

    def close(self):
        pass


class OutputStreamTests(unittest.TestCase):
    def setUp(self):
        _FakeStream.opened, _FakeStream.refuse = [], set()
        fake_sd = types.SimpleNamespace(OutputStream=_FakeStream, query_devices=lambda *a, **k: {"default_samplerate": 48000.0})
        patcher = mock.patch.dict(sys.modules, {"sounddevice": fake_sd})
        patcher.start()
        self.addCleanup(patcher.stop)

    @needs_soxr
    def test_opens_at_the_device_rate_and_resamples_writes(self):
        out = devrate.OutputStream(AudioDevice(), 24000, latency=0.2)
        out.start()
        x = _sine(440, 24000, 1.0)
        for i in range(0, len(x), 2400):
            out.write(x[i : i + 2400].reshape(-1, 1))
        out.stop()
        stream = _FakeStream.opened[-1]
        self.assertEqual(stream.samplerate, 48000)
        self.assertTrue(all(w.ndim == 2 and w.shape[1] == 1 for w in stream.written))
        self.assertAlmostEqual(sum(len(w) for w in stream.written), 48000, delta=48)

    @needs_soxr
    def test_stop_after_abort_still_stops_the_stream(self):
        # A reply cut short: the stream is aborted, and the resampler's tail must not be written to it
        # (PortAudio raises, and the caller would then never close the stream).
        out = devrate.OutputStream(AudioDevice(), 24000)
        out.start()
        out.write(_sine(440, 24000, 0.1))
        out.abort()
        out.stop()
        self.assertTrue(_FakeStream.opened[-1].stopped)

    def test_device_refusing_its_own_rate_falls_back_to_vision_rate(self):
        _FakeStream.refuse = {48000}
        out = devrate.OutputStream(AudioDevice(), 24000)
        out.start()
        out.write(np.ones(2400, dtype=np.float32))
        stream = _FakeStream.opened[-1]
        self.assertEqual(stream.samplerate, 24000)
        self.assertEqual(sum(len(w) for w in stream.written), 2400)

    def test_without_soxr_everything_stays_at_vision_rate(self):
        with mock.patch.object(devrate, "_soxr", lambda: None):
            devrate.OutputStream(AudioDevice(), 24000)
        self.assertEqual(_FakeStream.opened[-1].samplerate, 24000)


if __name__ == "__main__":
    unittest.main()
