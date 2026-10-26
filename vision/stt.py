"""Speech-to-text with faster-whisper (CTranslate2), CUDA when available."""
from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np

from vision.config import MLX_WHISPER, ListenConfig

SAMPLE_RATE = 16000
# Whisper's well-known hallucinations on silence / noise.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching.", "thank you for watching.", "you", "you.", "bye.", "thank you", ".", "",
    "subtitles by the amara.org community", "thanks for watching!",
}

# A preview is expendable. If its inference gets wedged, the voice loop must get control back instead
# of sitting forever between hearing the user and running the final transcription.
LIVE_STOP_TIMEOUT_S = 5.0


from vision.cuda import preload as _preload_cuda_libs


def thread_only_tqdm() -> None:
    """Give tqdm a threading lock before a Hugging Face download builds its default one.

    tqdm's default lock includes a multiprocessing RLock, a named semaphore kept for the life of the process,
    so every exit after a download warned about a leaked semaphore. Vision's downloads only use threads.
    """
    try:
        from tqdm.std import tqdm
    except ImportError:
        return
    if getattr(tqdm, "_lock", None) is None:
        tqdm.set_lock(threading.RLock())


def mlx_whisper_repo(cfg: ListenConfig) -> str | None:
    """The MLX Whisper model for cfg, or None when Whisper stays on faster-whisper here."""
    from vision.mlxrun import mlx_available

    if cfg.device not in ("auto", "mlx") or not mlx_available():
        return None
    name = cfg.whisper_model.strip()
    return name if "/" in name else MLX_WHISPER.get(name)


class Transcriber:
    # How long close() waits for the MLX worker to let the model go. It is quick unless the worker is wedged in
    # a preview (what LIVE_STOP_TIMEOUT_S guards against); then /talk off must not hang on it.
    UNLOAD_TIMEOUT_S = 3.0

    def __init__(self, cfg: ListenConfig):
        self.cfg = cfg
        self._model = None
        self._mlx = None  # vision.mlxrun.MlxWorker running the model while Whisper is loaded through MLX (Apple Silicon)
        self._worker = None  # that worker between loads: one thread for the life of the process, not one per /talk on
        self.mlx_error: str | None = None  # why the MLX build did not load, when it did not (`vision doctor` says)
        self.device = "?"
        self._lock = threading.Lock()

    def _load_mlx(self, repo: str) -> bool:
        """Whisper on the Mac's GPU. False (and faster-whisper takes over) if it will not load."""
        from vision.mlxrun import MlxWorker, mlx_fetch

        os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")  # "PyTorch was not found": none on this build
        worker = self._worker = self._worker or MlxWorker("whisper-mlx")

        def load():
            import transformers
            from mlx_audio.stt.utils import load_model

            transformers.logging.set_verbosity_error()  # a tokenizer notice on every load
            model = load_model(mlx_fetch(repo))
            model.generate(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")  # compile the kernels now
            return model

        try:
            model = worker.call(load)
        except Exception as e:  # noqa: BLE001
            self.mlx_error = f"{type(e).__name__}: {e}"
            print(f"[Whisper did not load on MLX: {self.mlx_error}; faster-whisper instead]", file=sys.stderr)
            return False
        self.mlx_error = None
        # The worker and device first: transcribe() reads `_model` without the lock and picks its path off `_mlx`.
        self._mlx = worker
        self.device = f"mlx/{repo.rsplit('/', 1)[-1].removeprefix('whisper-').removesuffix('-asr-8bit')}"
        self._model = model
        return True

    def load(self, quiet: bool = False) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1" if quiet else "0")
            thread_only_tqdm()
            repo = mlx_whisper_repo(self.cfg)
            if repo and self._load_mlx(repo):
                return
            from faster_whisper import WhisperModel

            want = self.cfg.device
            if want == "mlx":  # no MLX here, or its model would not load: the same fallbacks as "auto"
                want = "auto"
            attempts = []
            if want in ("auto", "cuda"):
                # int8 weights + fp16 activations: ~0.7 GB less VRAM than fp16 for large-v3-turbo, same speed,
                # no measurable accuracy loss — and the voice model needs the room.
                attempts.append(("cuda", "int8_float16", self.cfg.whisper_model))
            if want in ("auto", "cpu"):
                cpu_model = self.cfg.whisper_model
                if want == "auto" and "large" in cpu_model:
                    cpu_model = "small.en"  # large models are too slow on CPU for live use
                attempts.append(("cpu", "int8", cpu_model))
            last = None
            for dev, ct, name in attempts:
                try:
                    if dev == "cuda":
                        _preload_cuda_libs()
                    self._model = WhisperModel(name, device=dev, compute_type=ct)
                    # Touch the encoder once so missing CUDA libs fail here, not mid-conversation.
                    self._model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1, language="en")
                    self.device = f"{dev}/{name}"
                    return
                except Exception as e:  # noqa: BLE001
                    last = e
                    self._model = None
            raise RuntimeError(f"Could not load Whisper model: {last}")

    def warm_up(self) -> None:
        self.load(quiet=True)

    def close(self) -> None:
        """Unload Whisper so an idle chat does not keep GPU memory from another Vision."""
        stuck = None
        with self._lock:
            model, self._model = self._model, None
            self.device = "?"
            worker, self._mlx = self._mlx, None
            if worker is not None:
                if model is not None:
                    def free():
                        import mlx.core as mx

                        mx.clear_cache()

                    # On the worker: it holds the last call's closure (and so the model) until it takes the next
                    # job, and MLX's streams are per thread. Not for long, though: a preview wedged in generate()
                    # would otherwise hold /talk off for ever, at the one moment the memory has to come back.
                    del model
                    try:
                        worker.call(free, timeout=self.UNLOAD_TIMEOUT_S)
                    except TimeoutError:
                        self._worker = None  # the next load gets a fresh thread; this one lets go when its job ends
                        stuck = "Whisper is still busy on the GPU (a stuck preview?); its memory comes back when that ends"
            elif model is not None:
                # faster-whisper wraps CTranslate2, whose explicit unload releases its CUDA weights
                # immediately instead of waiting for Python's cyclic collector to reach the wrapper.
                model.model.unload_model()
        import gc

        gc.collect()
        if stuck:
            raise RuntimeError(stuck)

    @property
    def can_preview_live(self) -> bool:
        """Whether repeated partial transcriptions are fast enough not to hold up the real turn."""
        return self.device.startswith(("cuda/", "mlx/"))

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        self.load(quiet=True)
        if sample_rate != SAMPLE_RATE:
            idx = np.arange(0, len(audio), sample_rate / SAMPLE_RATE)
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        if audio.size < SAMPLE_RATE * 0.25:
            return ""
        if self._mlx is not None:
            return self._transcribe_mlx(audio.astype(np.float32))
        segments, info = self._model.transcribe(
            audio.astype(np.float32),
            beam_size=2,
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,
        )
        pieces = []
        for s in segments:
            if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:
                continue
            pieces.append(s.text.strip())
        text = " ".join(p for p in pieces if p).strip()
        if text.lower() in _HALLUCINATIONS:
            return ""
        return text

    # large-v3-turbo slips into lowercase without punctuation on ~30 % of utterances unless the prompt shows
    # the style it should keep (LibriSpeech: 22 of 73 clips without it, 1 with; the error rate also fell).
    MLX_PROMPT = "Hello. Sure, here's what I said, with punctuation."

    def _transcribe_mlx(self, audio: np.ndarray) -> str:
        model = self._model
        out = self._mlx.call(lambda: model.generate(audio, language="en", temperature=0.0, initial_prompt=self.MLX_PROMPT,
                                                    condition_on_previous_text=False, verbose=None))
        pieces = []
        for s in out.segments or []:
            if s.get("no_speech_prob", 0) > 0.8 and s.get("avg_logprob", 0) < -1.0:
                continue
            pieces.append(str(s.get("text", "")).strip())
        text = " ".join(p for p in pieces if p).strip() if out.segments else str(out.text).strip()
        return "" if text.lower() in _HALLUCINATIONS else text

    def transcribe_file(self, path: str) -> str:
        import soundfile as sf

        audio, sr = sf.read(path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return self.transcribe(audio, sr)


class LiveTranscript:
    """Words forming while someone is still talking. `feed` gets the audio so far from the mic; a
    worker re-transcribes the newest snapshot whenever it is free (older ones are skipped, so slow
    ears just update less often) and hands each text to `on_text`. `feed_final` gets the audio at the
    first quiet frame after speech: once its text is in, `final` holds the utterance's words and the
    caller skips its own transcription of the same audio (the wait between the end of speech and the
    reply used to be two transcriptions long, the preview's and then the final's). `stop` waits
    briefly for the worker, so the final transcription does not run on the model at the same time; a
    wedged preview is reported to the caller instead of freezing the voice loop forever."""

    def __init__(self, stt: Transcriber, on_text):
        self._stt = stt
        self._on_text = on_text
        self._latest: np.ndarray | None = None
        self._final_len: int | None = None  # samples in the speech-end snapshot (feed_final)
        self._done: tuple[int, str] | None = None  # (samples, text) of the last snapshot transcribed
        self._cond = threading.Condition()
        self._stop = False
        self.text = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, audio: np.ndarray) -> None:
        with self._cond:
            self._latest = audio
            self._cond.notify()

    def feed_final(self, audio: np.ndarray) -> None:
        """The audio up to the first quiet frame after speech: all of it, so its text is the final one."""
        with self._cond:
            self._latest = audio
            self._final_len = len(audio)
            self._cond.notify()

    @property
    def final(self) -> str | None:
        """The utterance's text, when the last snapshot transcribed was the speech-end one; None when
        the caller still has to transcribe (no such snapshot, or its text is not in yet)."""
        with self._cond:
            if self._final_len is not None and self._done is not None and self._done[0] == self._final_len:
                return self._done[1]
            return None

    def stop(self, timeout: float = LIVE_STOP_TIMEOUT_S) -> bool:
        """Stop accepting previews; return False rather than freezing if inference is wedged."""
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._latest is None and not self._stop:
                    self._cond.wait()
                # Stopping: a pending speech-end snapshot is still worth the one transcription it
                # saves the caller; any other preview is not.
                if self._stop and (self._latest is None or len(self._latest) != self._final_len):
                    return
                audio, self._latest = self._latest, None
            try:
                # Ears that tell a preview from the real thing (the service's, borrowed) are asked for one.
                text = getattr(self._stt, "preview", self._stt.transcribe)(audio).strip()
            except Exception:  # noqa: BLE001  a preview is not worth breaking the turn over
                continue
            with self._cond:
                self._done = (len(audio), text)
            if text and text != self.text:
                self.text = text
                self._on_text(text)
