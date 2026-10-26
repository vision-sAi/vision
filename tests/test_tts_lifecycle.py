import sys
import threading
import types
import unittest
from unittest.mock import patch

from vision.stt import Transcriber
from vision.tts import Qwen3Engine, Speaker


class _FlakyEngine:
    def __init__(self):
        self.loads = 0
        self.closes = 0

    def load(self):
        self.loads += 1
        if self.loads == 1:
            raise RuntimeError("GPU busy")

    def close(self):
        self.closes += 1


class SpeakerLifecycleTests(unittest.TestCase):
    def test_transcriber_close_drops_the_gpu_model(self):
        transcriber = Transcriber.__new__(Transcriber)
        transcriber._lock = threading.Lock()
        transcriber._mlx = None  # faster-whisper, not the MLX build
        unloaded = []
        transcriber._model = types.SimpleNamespace(
            model=types.SimpleNamespace(unload_model=lambda: unloaded.append(True))
        )
        transcriber.device = "cuda/large-v3-turbo"

        transcriber.close()

        self.assertIsNone(transcriber._model)
        self.assertEqual(transcriber.device, "?")
        self.assertEqual(unloaded, [True])

    def test_close_clears_failed_load_so_the_next_talk_retries_immediately(self):
        speaker = Speaker.__new__(Speaker)
        speaker.engine = _FlakyEngine()
        speaker._loaded = False
        speaker._lock = threading.Lock()
        speaker._stop = threading.Event()
        speaker._load_error = None

        with self.assertRaisesRegex(RuntimeError, "GPU busy"):
            speaker._load()
        speaker.close()
        speaker._load()

        self.assertTrue(speaker._loaded)
        self.assertEqual(speaker.engine.loads, 2)
        self.assertEqual(speaker.engine.closes, 1)

    def test_qwen_close_drops_borrowed_cuda_modules_before_releasing_claim(self):
        released = []
        claim = types.SimpleNamespace(release=lambda: released.append(True))
        engine = Qwen3Engine.__new__(Qwen3Engine)
        engine._model = object()
        engine._prompt = engine._ref_codes = object()
        engine._prompts = {("voice", "mode"): object()}
        engine._spoken = [("hello", object())]
        engine._fast = object()
        engine._talker = object()
        engine._decoder = object()
        engine._claim = claim
        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(empty_cache=lambda: None))

        with patch.dict(sys.modules, {"torch": fake_torch}):
            engine.close()

        self.assertIsNone(engine._model)
        self.assertIsNone(engine._talker)
        self.assertIsNone(engine._decoder)
        self.assertIsNone(engine._fast)
        self.assertEqual(engine._prompts, {})
        self.assertEqual(engine._spoken, [])
        self.assertEqual(released, [True])


if __name__ == "__main__":
    unittest.main()
