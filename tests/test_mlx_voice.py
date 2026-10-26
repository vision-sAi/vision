"""The MLX voice (Apple Silicon): engine choice, the one-thread worker, generation and setup's model timing.

No MLX needed: the model is a fake, so this runs anywhere."""
import dataclasses
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from vision import tts
from vision.config import MLX_TTS_TIERS, VoiceConfig


class EngineChoiceTests(unittest.TestCase):
    def test_qwen3_runs_on_mlx_where_it_can_unless_the_config_pins_torch(self):
        with mock.patch.object(tts, "mlx_available", return_value=True):
            self.assertTrue(tts.uses_mlx(VoiceConfig()))
            self.assertTrue(tts.uses_mlx(VoiceConfig(device="mlx")))
            self.assertFalse(tts.uses_mlx(VoiceConfig(device="cpu")))
            self.assertFalse(tts.uses_mlx(VoiceConfig(engine="orpheus")))
        with mock.patch.object(tts, "mlx_available", return_value=False):
            self.assertFalse(tts.uses_mlx(VoiceConfig()))

    def test_the_model_is_the_configs_then_setups_pick_then_the_best_tier(self):
        with mock.patch.object(tts, "mlx_choice", return_value={}):
            self.assertEqual(tts.mlx_model(VoiceConfig()), MLX_TTS_TIERS[0])
        with mock.patch.object(tts, "mlx_choice", return_value={"model": MLX_TTS_TIERS[2]}):
            self.assertEqual(tts.mlx_model(VoiceConfig()), MLX_TTS_TIERS[2])
            self.assertEqual(tts.mlx_model(VoiceConfig(mlx_model="some/model")), "some/model")


class WorkerTests(unittest.TestCase):
    def test_every_call_and_stream_runs_on_the_same_thread(self):
        w = tts._MlxWorker("test-mlx")
        threads = {w.call(lambda: threading.get_ident())}

        def items():
            for i in range(3):
                threads.add(threading.get_ident())
                yield i

        self.assertEqual(list(w.stream(items, threading.Event())), [0, 1, 2])
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)

    def test_errors_come_back_to_the_caller(self):
        w = tts._MlxWorker("test-mlx")
        with self.assertRaises(ZeroDivisionError):
            w.call(lambda: 1 / 0)

        def broken():
            yield 1
            raise ValueError("decoder")

        with self.assertRaises(ValueError):
            list(w.stream(broken, threading.Event()))
        self.assertEqual(w.call(lambda: "still alive"), "still alive")

    def test_abandoning_a_stream_stops_the_worker_and_frees_it(self):
        w = tts._MlxWorker("test-mlx")
        made = []

        def endless():  # a sentence that never ends, one piece every 10 ms
            i = 0
            while True:
                made.append(i)
                yield i
                i += 1
                time.sleep(0.01)

        gen = w.stream(endless, threading.Event())
        self.assertEqual(next(gen), 0)
        gen.close()
        self.assertEqual(w.call(lambda: "free"), "free")  # the endless job ended rather than holding the thread
        stopped_at = len(made)
        time.sleep(0.05)
        self.assertEqual(len(made), stopped_at)


class _FakeDecoder:
    """Like mlx-audio's codec decoder: decodes codes piece by piece and keeps what it has seen."""

    def __init__(self):
        self.seen = []  # codes decoded since the last reset
        self.resets = 0

    def reset_streaming_state(self):
        self.resets += 1
        self.seen = []

    def streaming_step(self, codes):
        self.seen.append(codes)
        return np.full((1, 1, codes.shape[2] * 1920), 0.1, dtype=np.float32)


class _FakeModel:
    """mlx-audio's Qwen3-TTS as Vision drives it: generate() resets the decoder, decodes 4-frame pieces
    through tokenizer.decoder (looked up each time, as the library does), and resets it again at the end."""

    sample_rate = tts.SAMPLE_RATE

    def __init__(self, pieces=4):
        self.pieces = pieces
        self.calls = []
        self.decoder = _FakeDecoder()
        self.speech_tokenizer = SimpleNamespace(decoder=self.decoder)
        self._icl_cache = {}
        self.primed = []  # what the decoder had already seen when each chunk's first piece arrived

    def generate(self, **kw):
        self.calls.append(dict(kw, icl=dict(self._icl_cache)))
        self.speech_tokenizer.decoder.reset_streaming_state()
        for i in range(self.pieces):
            if i == 0:
                self.primed.append(list(self.decoder.seen))
            codes = np.full((1, 16, 4), len(self.calls) * 10 + i)
            wav = self.speech_tokenizer.decoder.streaming_step(codes)
            yield SimpleNamespace(audio=wav[0, 0])
        self.speech_tokenizer.decoder.reset_streaming_state()


_NUMPY_MX = SimpleNamespace(concatenate=np.concatenate, eval=lambda *a: None)


class EngineTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.voices = Path(tmp.name)
        voice = self.voices / "phoenix"
        voice.mkdir()
        (voice / "ref.wav").write_bytes(b"")
        (voice / "ref.txt").write_text("Remember, winning takes sacrifice.")
        for patcher in (mock.patch("vision.config.VOICES_DIR", self.voices), mock.patch.object(tts, "_mx", lambda: _NUMPY_MX)):
            patcher.start()
            self.addCleanup(patcher.stop)

    clip = np.linspace(-0.5, 0.5, 24000, dtype=np.float32)

    def engine(self, **cfg):
        stop = threading.Event()
        e = tts.Qwen3MlxEngine(VoiceConfig(voice="phoenix", **cfg), stop)
        e._model, e._worker = _FakeModel(), tts._MlxWorker("test-mlx")
        e._clips[self.voices / "phoenix" / "ref.wav"] = self.clip  # skip mlx_audio.load_audio
        return e, stop

    def test_streams_float32_pieces_with_the_voice_and_language(self):
        e, _ = self.engine(language="English")
        pieces = list(e.generate("Hello there."))
        self.assertEqual(len(pieces), 4)
        self.assertTrue(all(p.dtype == np.float32 for p in pieces))
        call = e._model.calls[-1]
        self.assertIs(call["ref_audio"], self.clip)
        self.assertEqual((call["lang_code"], call["stream"]), ("english", True))

    def test_clone_mode_decides_whether_the_transcript_goes_in(self):
        e, _ = self.engine(clone_mode="embedding")
        list(e.generate("Hi."))
        self.assertIsNone(e._model.calls[-1]["ref_text"])
        e, _ = self.engine(clone_mode="context")
        list(e.generate("Hi."))
        self.assertEqual(e._model.calls[-1]["ref_text"], "Remember, winning takes sacrifice.")
        (self.voices / "phoenix" / "clone_mode.txt").write_text("embedding")  # the voice's own setting wins
        list(e.generate("Hi."))
        self.assertIsNone(e._model.calls[-1]["ref_text"])

    def test_stop_ends_the_sentence_at_the_next_piece(self):
        e, stop = self.engine()
        gen = e.generate("A long sentence.")
        next(gen)
        stop.set()
        self.assertLessEqual(len(list(gen)), 1)

    def test_a_reply_continues_from_what_was_just_said(self):
        e, _ = self.engine(continuity=True)
        list(e.generate("Good evening."))
        list(e.generate("Tomorrow is quiet.", cont=True))
        first, second = e._model.calls[-2:]
        self.assertEqual(first["icl"], {})  # the first sentence starts from the voice's own clip
        # The second gets the reply so far as its in-context reference, under the key mlx-audio looks up...
        self.assertEqual(second["ref_text"], "Good evening.")
        (key, (codes, ids)), = second["icl"].items()
        self.assertEqual((key[0], ids), ("Good evening.", None))
        self.assertEqual(codes.shape, (1, 16, 16))  # all four pieces of the first sentence
        self.assertEqual(e._model._icl_cache, {})  # ...and it is gone again afterwards
        # The decoder was primed with the end of the first sentence before the second one's first piece.
        self.assertEqual(len(e._model.primed[1]), 1)
        np.testing.assert_array_equal(e._model.primed[1][0], codes[:, :, -tts.Qwen3MlxEngine.CONTEXT_FRAMES :])
        self.assertIsInstance(e._model.speech_tokenizer.decoder, _FakeDecoder)  # the real decoder is back
        self.assertEqual([t for t, _ in e._spoken], ["Good evening.", "Tomorrow is quiet."])

    def test_a_new_reply_a_stop_or_continuity_off_start_cold(self):
        e, stop = self.engine(continuity=True)
        list(e.generate("One."))
        list(e.generate("Two.", cont=False))  # a new reply, or a new style of voice
        self.assertEqual(e._model.calls[-1]["icl"], {})
        self.assertEqual([t for t, _ in e._spoken], ["Two."])
        gen = e.generate("Three, cut short.", cont=True)
        next(gen)
        stop.set()
        list(gen)
        self.assertEqual(e._spoken, [])  # the audio no longer matches the text: the next one starts cold
        e, _ = self.engine(continuity=False)
        list(e.generate("One."))
        list(e.generate("Two.", cont=True))
        self.assertEqual((e._model.calls[-1]["icl"], e._spoken), ({}, []))

    def test_only_whole_recent_chunks_within_the_carry_limit_are_kept(self):
        e, _ = self.engine(continuity=True)
        with mock.patch.object(tts.Qwen3MlxEngine, "CARRY_FRAMES", 40):
            for i in range(4):
                list(e.generate(f"Sentence {i}.", cont=i > 0))  # 16 frames each
        self.assertEqual([t for t, _ in e._spoken], ["Sentence 2.", "Sentence 3."])

    def test_a_missing_model_says_to_run_setup(self):
        e = tts.Qwen3MlxEngine(VoiceConfig(voice="phoenix"), threading.Event())
        with mock.patch.object(tts, "mlx_present", return_value=False):
            with self.assertRaisesRegex(tts.TTSError, "vision setup"):
                e.load()


class SetupTimingTests(unittest.TestCase):
    """`vision setup` on a Mac times the tiers best-first and keeps the first that beats MLX_TARGET_RTF."""

    def pick(self, speeds, choice=None, chip="Apple Silicon", retime=False):
        from vision import cli

        saved, timed = {}, []

        def time_mlx(cfg, repo):
            timed.append(repo)
            return speeds[repo]

        cfg = SimpleNamespace(voice=VoiceConfig(voice="phoenix"))
        with mock.patch.object(tts, "time_mlx", time_mlx), \
                mock.patch.object(tts, "mlx_present", return_value=True), \
                mock.patch.object(tts, "mlx_choice", return_value=choice or {}), \
                mock.patch.object(tts, "mac_chip", return_value=chip), \
                mock.patch.object(tts, "save_mlx_choice", lambda repo, rtf: saved.update(model=repo, rtf=rtf)), \
                mock.patch.object(cli, "saved_voices", return_value=["phoenix"]):
            cli._setup_mlx(cfg, retime)
        return saved, timed

    def test_a_fast_mac_keeps_the_best_model_and_times_nothing_else(self):
        saved, timed = self.pick({MLX_TTS_TIERS[0]: 0.62})
        self.assertEqual(saved["model"], MLX_TTS_TIERS[0])
        self.assertEqual(timed, [MLX_TTS_TIERS[0]])

    def test_a_slower_mac_steps_down_to_the_first_tier_fast_enough(self):
        saved, timed = self.pick({MLX_TTS_TIERS[0]: 1.05, MLX_TTS_TIERS[1]: 0.7, MLX_TTS_TIERS[2]: 0.6})
        self.assertEqual(saved["model"], MLX_TTS_TIERS[1])
        self.assertEqual(timed, list(MLX_TTS_TIERS[:2]))

    def test_when_nothing_is_fast_enough_the_fastest_wins(self):
        saved, _ = self.pick({MLX_TTS_TIERS[0]: 2.0, MLX_TTS_TIERS[1]: 1.2, MLX_TTS_TIERS[2]: 1.4})
        self.assertEqual(saved["model"], MLX_TTS_TIERS[1])

    def test_a_timing_from_this_chip_is_reused_and_another_chips_is_not(self):
        mine = {"model": MLX_TTS_TIERS[1], "rtf": 0.5, "chip": "Apple Silicon"}
        saved, timed = self.pick({MLX_TTS_TIERS[0]: 0.6}, choice=mine)
        self.assertEqual((saved, timed), ({}, []))
        saved, timed = self.pick({MLX_TTS_TIERS[0]: 0.6}, choice=dict(mine, chip="Apple M1"))
        self.assertEqual(timed, [MLX_TTS_TIERS[0]])
        saved, timed = self.pick({MLX_TTS_TIERS[0]: 0.6}, choice=mine, retime=True)
        self.assertEqual(timed, [MLX_TTS_TIERS[0]])


if __name__ == "__main__":
    unittest.main()
