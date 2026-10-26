"""voiceid: the Kaldi fbank in numpy, the enrolment on disk, and the only_me check around an utterance."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from vision import voiceid
from vision.config import ListenConfig

SR = voiceid.SAMPLE_RATE


def _speechy(seconds: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.1).astype(np.float32)


class FbankTests(unittest.TestCase):
    def test_frames_and_bins_follow_kaldi_snip_edges(self):
        feats = voiceid.fbank(_speechy(1.0))
        self.assertEqual(feats.shape, (1 + (SR - voiceid.FRAME) // voiceid.SHIFT, voiceid.MEL_BINS))
        self.assertEqual(voiceid.fbank(np.zeros(voiceid.FRAME - 1, dtype=np.float32)).shape, (0, voiceid.MEL_BINS))
        self.assertEqual(voiceid.fbank(np.zeros(voiceid.FRAME, dtype=np.float32)).shape, (1, voiceid.MEL_BINS))

    def test_mean_over_time_is_removed(self):
        feats = voiceid.fbank(_speechy(2.0))
        np.testing.assert_allclose(feats.mean(axis=0), 0, atol=1e-4)

    def test_silence_floors_at_the_epsilon(self):
        feats = voiceid.fbank(np.zeros(SR, dtype=np.float32))
        self.assertTrue(np.all(np.isfinite(feats)))

    def test_matches_torchaudio_when_it_is_installed(self):
        try:
            import torch
            import torchaudio.compliance.kaldi as kaldi
        except ImportError:  # the text-only and voice-mac installs
            self.skipTest("torchaudio not installed")
        audio = _speechy(1.5, seed=3)
        ref = kaldi.fbank(torch.from_numpy(audio * 32768)[None], num_mel_bins=80, frame_length=25, frame_shift=10,
                          sample_frequency=SR, window_type="hamming", dither=0.0)
        ref = (ref - ref.mean(0, keepdim=True)).numpy()
        np.testing.assert_allclose(voiceid.fbank(audio), ref, atol=2e-3)


class CosineTests(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(voiceid.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(voiceid.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(voiceid.cosine([0, 0], [1, 1]), 0.0)


class EnrolmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name) / "speakers"
        self.patches = [mock.patch.object(voiceid, "SPEAKERS_DIR", d), mock.patch.object(voiceid, "ENROLMENT", d / "me.npy")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_save_keeps_the_unit_mean_and_forget_removes_it(self):
        self.assertFalse(voiceid.enrolled())
        self.assertIsNone(voiceid.load_enrolment())
        voiceid.save_enrolment([np.array([2.0, 0.0]), np.array([0.0, 2.0])])
        self.assertTrue(voiceid.enrolled())
        np.testing.assert_allclose(voiceid.load_enrolment(), [np.sqrt(0.5), np.sqrt(0.5)], atol=1e-6)
        self.assertTrue(voiceid.forget_enrolment())
        self.assertFalse(voiceid.enrolled())
        self.assertFalse(voiceid.forget_enrolment())


class _StubId:
    """Scores by the first sample's sign: positive audio is "me", negative is somebody else."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls = 0

    def score(self, audio):
        self.calls += 1
        if self.fail:
            raise self.fail
        return 0.8 if audio[0] > 0 else 0.1


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.stub = _StubId()
        self.notes = []
        self.patches = [
            mock.patch.object(voiceid, "shared", lambda: self.stub),
            mock.patch.object(voiceid, "enrolled", lambda: True),
            mock.patch.object(voiceid, "_noted", False),
        ]
        for p in self.patches:
            p.start()
        self.me = np.full(SR, 0.5, dtype=np.float32)
        self.other = np.full(SR, -0.5, dtype=np.float32)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_off_keeps_everything_without_scoring(self):
        self.assertEqual(voiceid.check(ListenConfig(only_me=False), self.other, self.notes.append), (True, None))
        self.assertEqual(self.stub.calls, 0)

    def test_on_keeps_me_and_drops_others_at_the_threshold(self):
        cfg = ListenConfig(only_me=True, only_me_threshold=0.45)
        self.assertEqual(voiceid.check(cfg, self.me, self.notes.append), (True, 0.8))
        self.assertEqual(voiceid.check(cfg, self.other, self.notes.append), (False, 0.1))
        self.assertEqual(voiceid.check(ListenConfig(only_me=True, only_me_threshold=0.05), self.other), (True, 0.1))
        self.assertEqual(self.notes, [])

    def test_too_short_to_judge_is_let_through(self):
        short = np.full(int(voiceid.MIN_SPEECH_S * SR) - 1, -0.5, dtype=np.float32)
        self.assertEqual(voiceid.check(ListenConfig(only_me=True), short, self.notes.append), (True, None))
        self.assertEqual(self.stub.calls, 0)

    def test_nothing_enrolled_keeps_everything_and_says_so_once(self):
        with mock.patch.object(voiceid, "enrolled", lambda: False):
            cfg = ListenConfig(only_me=True)
            self.assertEqual(voiceid.check(cfg, self.other, self.notes.append), (True, None))
            self.assertEqual(voiceid.check(cfg, self.other, self.notes.append), (True, None))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("vision voice enrol", self.notes[0])
        self.assertEqual(self.stub.calls, 0)

    def test_a_model_that_cannot_load_keeps_everything_and_says_so_once(self):
        self.stub.fail = RuntimeError("no model")
        cfg = ListenConfig(only_me=True)
        self.assertEqual(voiceid.check(cfg, self.other, self.notes.append), (True, None))
        self.assertEqual(voiceid.check(cfg, self.other, self.notes.append), (True, None))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("no model", self.notes[0])

    def test_rejected_note_carries_the_score(self):
        self.assertEqual(voiceid.rejected_note(0.123), "not your voice (0.12)")


class VoiceIdTests(unittest.TestCase):
    def test_embed_runs_the_session_on_the_features_and_normalises(self):
        vid = voiceid.VoiceId()
        session = mock.Mock()
        session.run.return_value = [np.array([[3.0, 4.0]], dtype=np.float32)]
        vid._session = session
        vid._enrolment = np.array([0.0, 1.0], dtype=np.float32)
        emb = vid.embed(_speechy(1.0))
        np.testing.assert_allclose(emb, [0.6, 0.8], atol=1e-6)
        feats = session.run.call_args[0][1]["feats"]
        self.assertEqual(feats.shape[0], 1)
        self.assertEqual(feats.shape[2], voiceid.MEL_BINS)
        self.assertAlmostEqual(vid.score(_speechy(1.0)), 0.8, places=5)

    def test_score_is_none_without_an_enrolment(self):
        vid = voiceid.VoiceId()
        vid._session = mock.Mock()
        with mock.patch.object(voiceid, "load_enrolment", lambda: None):
            self.assertIsNone(vid.score(_speechy(1.0)))
            vid._session.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
