"""Whose voice it is: `[listen] only_me` makes the voice modes act only on the enrolled voice.

Each utterance the mic hands over is turned into a speaker embedding and scored against the enrolment
(`vision voice enrol`) by cosine similarity; below `only_me_threshold` it is dropped before it is
transcribed, so somebody else in the room (or the television) cannot talk to Vision. The model is
WeSpeaker's ResNet34 r-vector (ONNX, 26 MB, fetched from Hugging Face on first use or by `vision setup`),
run on the CPU through onnxruntime: 256-d embeddings in about 30 ms for a few seconds of speech, so it
costs nothing you can hear and leaves the GPU to Whisper and the voice. Its features are Kaldi-style
log mel filterbanks computed here in numpy, so the voice-mac build (no torch) runs it as well.
"""
from __future__ import annotations

import threading
from pathlib import Path

from vision.config import DATA_DIR, ListenConfig

MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
SPEAKERS_DIR = DATA_DIR / "speakers"
ENROLMENT = SPEAKERS_DIR / "me.npy"  # the mean embedding of the enrolment phrases, unit length
SAMPLE_RATE = 16_000
MIN_SPEECH_S = 0.5  # shorter than this the embedding says little; such an utterance is let through

# Kaldi fbank as WeSpeaker computes it: 25 ms Hamming frames every 10 ms, 80 mel bins from 20 Hz to
# Nyquist on a 512-point FFT, pre-emphasis 0.97, no dither, then the mean over time taken off.
FRAME = 400
SHIFT = 160
NFFT = 512
MEL_BINS = 80
LOW_HZ = 20.0
PREEMPH = 0.97
EPS = 1.1920928955078125e-07  # torchaudio's float epsilon, so logs match its fbank exactly

_lock = threading.Lock()
_banks = None


def _mel(hz):
    import numpy as np

    return 1127.0 * np.log1p(hz / 700.0)


def mel_banks():
    """The (MEL_BINS, NFFT // 2) triangular filterbank, built once."""
    global _banks
    if _banks is None:
        import numpy as np

        high = SAMPLE_RATE / 2
        lo, hi = _mel(LOW_HZ), _mel(high)
        delta = (hi - lo) / (MEL_BINS + 1)
        bins = _mel(np.arange(NFFT // 2) * SAMPLE_RATE / NFFT)  # Kaldi leaves the Nyquist bin out
        left = lo + np.arange(MEL_BINS)[:, None] * delta
        centre, right = left + delta, left + 2 * delta
        up = (bins - left) / delta
        down = (right - bins) / delta
        _banks = np.where(bins <= centre, up, down).clip(0, None).astype(np.float32)
    return _banks


def fbank(audio) -> "np.ndarray":
    """(frames, 80) log mel filterbank with the mean over time removed, from float32 16 kHz audio in
    [-1, 1]. Frames that would run past the end are dropped (Kaldi's snip_edges)."""
    import numpy as np

    x = np.asarray(audio, dtype=np.float32) * 32768.0  # WeSpeaker feeds the int16 scale
    n = 1 + (len(x) - FRAME) // SHIFT if len(x) >= FRAME else 0
    if n <= 0:
        return np.zeros((0, MEL_BINS), dtype=np.float32)
    idx = np.arange(FRAME)[None, :] + SHIFT * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)  # remove_dc_offset
    frames = np.concatenate([frames[:, :1] * (1 - PREEMPH), frames[:, 1:] - PREEMPH * frames[:, :-1]], axis=1)
    frames *= np.hamming(FRAME).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=NFFT, axis=1)[:, : NFFT // 2]) ** 2
    feats = np.log(np.maximum(power @ mel_banks().T, EPS)).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def cosine(a, b) -> float:
    import numpy as np

    a, b = np.asarray(a, dtype=np.float32).ravel(), np.asarray(b, dtype=np.float32).ravel()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denom) if denom else 0.0


def model_path(download: bool = True) -> Path | None:
    """The ONNX model in the Hugging Face cache; fetched when `download` and it is not there yet."""
    from huggingface_hub import hf_hub_download

    from vision.stt import thread_only_tqdm

    thread_only_tqdm()
    try:
        return Path(hf_hub_download(MODEL_REPO, MODEL_FILE, local_files_only=not download))
    except Exception:  # noqa: BLE001  offline and not cached, or the hub is down
        return None


def enrolled() -> bool:
    return ENROLMENT.is_file()


def load_enrolment():
    import numpy as np

    return np.load(ENROLMENT) if enrolled() else None


def save_enrolment(embeddings) -> Path:
    """Keep the unit-length mean of the enrolment phrases' embeddings as the voice to match."""
    import numpy as np

    stack = np.stack([np.asarray(e, dtype=np.float32).ravel() for e in embeddings])
    mean = stack.mean(axis=0)
    mean /= np.linalg.norm(mean) or 1.0
    SPEAKERS_DIR.mkdir(parents=True, exist_ok=True)
    np.save(ENROLMENT, mean.astype(np.float32))
    return ENROLMENT


def forget_enrolment() -> bool:
    if not enrolled():
        return False
    ENROLMENT.unlink()
    return True


class VoiceId:
    """The embedding model (loaded on first use) plus the enrolment, scoring utterances."""

    def __init__(self):
        self._session = None
        self._enrolment = None

    def load(self) -> None:
        with _lock:
            if self._session is None:
                import onnxruntime as ort

                path = model_path()
                if path is None:
                    raise RuntimeError(f"the speaker model ({MODEL_REPO}) is not downloaded and could not be fetched")
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2  # a few ms either way; leave the cores to Whisper
                self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
            if self._enrolment is None:
                self._enrolment = load_enrolment()

    def embed(self, audio):
        """A unit-length 256-d speaker embedding of float32 16 kHz audio."""
        import numpy as np

        self.load()
        feats = fbank(audio)
        if len(feats) == 0:
            return np.zeros(256, dtype=np.float32)
        out = self._session.run(None, {"feats": feats[None]})[0][0]
        return out / (np.linalg.norm(out) or 1.0)

    def score(self, audio) -> float | None:
        """Cosine similarity of the utterance to the enrolled voice; None when nothing is enrolled."""
        self.load()
        if self._enrolment is None:
            return None
        return cosine(self.embed(audio), self._enrolment)


_shared: VoiceId | None = None
_noted = False


def shared() -> VoiceId:
    global _shared
    if _shared is None:
        _shared = VoiceId()
    return _shared


def check(cfg: ListenConfig, audio, note=None) -> tuple[bool, float | None]:
    """Whether an utterance may go on to the brain under `only_me`: (keep it, the score).

    Off, or the audio too short to judge, keeps it with no score. On with nothing enrolled keeps it and
    says so once through `note`, so a switched-on check that is not set up cannot silently drop every turn.
    A model that cannot load is reported the same way (once) and the utterance kept: a missing download
    must not break a voice conversation.
    """
    global _noted
    if not cfg.only_me:
        return True, None
    if audio is None or len(audio) < MIN_SPEECH_S * SAMPLE_RATE:
        return True, None
    if not enrolled():
        if not _noted and note:
            _noted = True
            note("[warn]only_me is on but no voice is enrolled: `vision voice enrol`[/warn]")
        return True, None
    try:
        score = shared().score(audio)
    except Exception as e:  # noqa: BLE001
        if not _noted and note:
            _noted = True
            note(f"[warn]the speaker check is off: {e}[/warn]")
        return True, None
    if score is None:
        return True, None
    return score >= cfg.only_me_threshold, score


def rejected_note(score: float) -> str:
    return f"not your voice ({score:.2f})"
