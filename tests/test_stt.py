"""Live speech-preview safeguards."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from vision import mlxrun, stt as stt_module
from vision.config import MLX_WHISPER, ListenConfig
from vision.stt import LiveTranscript, Transcriber


class LiveTranscriptTests(unittest.TestCase):
    def test_live_preview_is_only_enabled_on_a_gpu(self):
        stt = Transcriber(ListenConfig())
        stt.device = "cpu/small.en"
        self.assertFalse(stt.can_preview_live)
        stt.device = "cuda/large-v3-turbo"
        self.assertTrue(stt.can_preview_live)
        stt.device = "mlx/large-v3-turbo"
        self.assertTrue(stt.can_preview_live)

    def test_stop_times_out_instead_of_freezing_on_stuck_preview(self):
        entered = threading.Event()
        release = threading.Event()

        class StuckTranscriber:
            def transcribe(self, _audio):
                entered.set()
                release.wait()
                return "done"

        live = LiveTranscript(StuckTranscriber(), lambda _text: None)
        live.feed(np.ones(16000, dtype=np.float32))
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.assertFalse(live.stop(timeout=0.05))
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        live._thread.join(timeout=1)


class SpeechEndSnapshotTests(unittest.TestCase):
    def test_the_speech_end_snapshot_makes_the_final_transcription_unnecessary(self):
        seen = []

        class Ears:
            def transcribe(self, audio):
                seen.append(audio.size)
                return f"heard {audio.size}"

        texts = []
        live = LiveTranscript(Ears(), texts.append)
        live.feed(np.ones(1600, dtype=np.float32))
        live.feed_final(np.ones(3200, dtype=np.float32))
        self.assertTrue(live.stop(timeout=5))  # a pending speech-end snapshot is still transcribed on stop
        self.assertEqual(live.final, "heard 3200")
        self.assertEqual(texts[-1], "heard 3200")
        self.assertIn(3200, seen)

    def test_without_a_speech_end_snapshot_the_caller_transcribes_itself(self):
        class Ears:
            def transcribe(self, audio):
                return "a preview"

        live = LiveTranscript(Ears(), lambda _t: None)
        live.feed(np.ones(1600, dtype=np.float32))
        self.assertTrue(live.stop(timeout=5))
        self.assertIsNone(live.final)


class MlxWhisperTests(unittest.TestCase):
    """Apple Silicon: Whisper runs through MLX; the model is faked, so this runs anywhere."""

    def test_which_model_runs_on_mlx(self):
        with mock.patch.object(mlxrun, "mlx_available", return_value=True):
            self.assertEqual(stt_module.mlx_whisper_repo(ListenConfig()), MLX_WHISPER["large-v3-turbo"])
            self.assertEqual(stt_module.mlx_whisper_repo(ListenConfig(whisper_model="org/whisper-x")), "org/whisper-x")
            self.assertIsNone(stt_module.mlx_whisper_repo(ListenConfig(whisper_model="small.en")))  # no MLX build of it
            self.assertIsNone(stt_module.mlx_whisper_repo(ListenConfig(device="cpu")))  # the wake word pins the CPU
        with mock.patch.object(mlxrun, "mlx_available", return_value=False):
            self.assertIsNone(stt_module.mlx_whisper_repo(ListenConfig()))

    def mlx_stt(self, segments, text=""):
        calls = []

        class Model:
            def generate(self, audio, **kw):
                calls.append(kw)
                return SimpleNamespace(text=text, segments=segments)

        stt = Transcriber(ListenConfig())
        stt._model, stt._mlx, stt.device = Model(), mlxrun.MlxWorker("test-whisper"), "mlx/large-v3-turbo"
        return stt, calls

    def test_segments_are_joined_and_silence_and_hallucinations_dropped(self):
        stt, calls = self.mlx_stt([
            {"text": " Hey Vision,", "no_speech_prob": 0.1, "avg_logprob": -0.2},
            {"text": " the weather?", "no_speech_prob": 0.2, "avg_logprob": -0.3},
            {"text": " (music)", "no_speech_prob": 0.95, "avg_logprob": -1.5},
        ])
        self.assertEqual(stt.transcribe(np.ones(16000, dtype=np.float32)), "Hey Vision, the weather?")
        self.assertEqual(calls[-1]["initial_prompt"], Transcriber.MLX_PROMPT)  # keeps the output punctuated
        self.assertEqual(calls[-1]["language"], "en")
        stt, _ = self.mlx_stt([{"text": " Thank you.", "no_speech_prob": 0.1, "avg_logprob": -0.2}])
        self.assertEqual(stt.transcribe(np.ones(16000, dtype=np.float32)), "")

    def fake_modules(self, load_model, whisper_model):
        """mlx_audio and faster-whisper stood in (neither is installed here, nor on Linux CI), so load() runs
        its real path: the MLX attempt, then the faster-whisper fallbacks."""
        import sys
        import types

        modules = {}
        for name in ("mlx_audio", "mlx_audio.stt", "mlx_audio.stt.utils", "transformers", "transformers.logging",
                     "faster_whisper", "mlx", "mlx.core"):
            modules[name] = types.ModuleType(name)
        modules["mlx_audio.stt.utils"].load_model = load_model
        modules["transformers"].logging = modules["transformers.logging"]
        modules["transformers.logging"].set_verbosity_error = lambda: None
        modules["faster_whisper"].WhisperModel = whisper_model
        modules["mlx.core"].clear_cache = lambda: None
        modules["mlx"].core = modules["mlx.core"]
        return mock.patch.dict(sys.modules, modules)

    class FasterWhisper:
        """faster-whisper on a Mac: no CUDA, so the CPU attempt is the one that loads."""
        loads = []

        def __init__(self, name, device, compute_type):
            if device == "cuda":
                raise RuntimeError("CUDA driver version is insufficient")
            self.loads.append((name, device, compute_type))
            self.model = SimpleNamespace(unload_model=lambda: None)  # the CTranslate2 wrapper close() unloads

        def transcribe(self, audio, **kw):
            return iter(()), None

    def test_a_failed_mlx_load_falls_back_to_faster_whisper(self):
        # `device = "mlx"`, and the download fails: faster-whisper takes over as it would for "auto", with the
        # CPU-sized model (large on the CPU is too slow for live use), and the reason is kept for `vision doctor`.
        self.FasterWhisper.loads.clear()
        stt = Transcriber(ListenConfig(device="mlx"))
        with self.fake_modules(load_model=lambda path: self.fail("no model to load"), whisper_model=self.FasterWhisper), \
                mock.patch.object(stt_module, "mlx_whisper_repo", return_value="org/whisper-x"), \
                mock.patch.object(stt_module, "_preload_cuda_libs"), \
                mock.patch.object(mlxrun, "mlx_fetch", side_effect=OSError("offline")), \
                mock.patch("sys.stderr"):
            stt.load()
        self.assertIsNone(stt._mlx)
        self.assertEqual(stt.device, "cpu/small.en")
        self.assertEqual(self.FasterWhisper.loads, [("small.en", "cpu", "int8")])
        self.assertEqual(stt.mlx_error, "OSError: offline")
        stt.close()

    def test_the_mlx_worker_is_kept_between_loads(self):
        class Model:
            def generate(self, audio, **kw):
                return SimpleNamespace(text="", segments=[])

        stt = Transcriber(ListenConfig())
        with self.fake_modules(load_model=lambda path: Model(), whisper_model=self.FasterWhisper), \
                mock.patch.object(stt_module, "mlx_whisper_repo", return_value="org/whisper-x"), \
                mock.patch.object(mlxrun, "mlx_fetch", return_value="/models/whisper-x"):
            stt.load()
            self.assertEqual(stt.device, "mlx/x")  # the label drops the repo's "whisper-"
            self.assertIsNone(stt.mlx_error)
            worker = stt._mlx
            stt.close()  # /talk off: the model goes, the thread stays for the next /talk on
            self.assertIsNone(stt._model)
            self.assertIsNone(stt._mlx)
            self.assertEqual(stt.device, "?")
            stt.load()
            self.assertIs(stt._mlx, worker)
            stt.close()

    def test_closing_does_not_wait_on_a_wedged_worker(self):
        # A preview stuck in generate() (LIVE_STOP_TIMEOUT_S already gave up on it) holds the worker: /talk off
        # must still come back, say so, and give the next load a fresh thread.
        stuck = threading.Event()
        stt = Transcriber(ListenConfig())
        stt._model, stt._mlx, stt.device = object(), mlxrun.MlxWorker("test-whisper"), "mlx/whisper-x"
        stt._worker = stt._mlx
        stt._mlx._jobs.put((stuck.wait, __import__("queue").Queue()))
        with mock.patch.object(Transcriber, "UNLOAD_TIMEOUT_S", 0.05):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "still busy"):
                stt.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(stt._model)
        self.assertIsNone(stt._mlx)
        self.assertIsNone(stt._worker)
        stuck.set()


if __name__ == "__main__":
    unittest.main()
