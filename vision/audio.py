"""Microphone capture with voice-activity end-pointing (hands-free) or push-to-talk."""
from __future__ import annotations

import collections
import contextlib
import os
import queue
import sys
import threading
import time

import numpy as np

from vision.config import ListenConfig, resolve_device

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000  # samples per webrtcvad frame

# Silero's speech probability needed to count a frame as speech, per vad_aggressiveness 0-3.
SILERO_THRESHOLDS = (0.3, 0.4, 0.5, 0.6)
# Seconds an open stream may go without delivering a frame before the mic counts as dead.
STALL_S = 3.0


class MicStalled(RuntimeError):
    """The input stream opened but no audio arrives (another program holds the device, or it is gone)."""


class SileroVad:
    """Streaming Silero VAD (the ONNX model shipped with faster-whisper).

    Neural VAD holds up against steady background noise such as a fan, where
    webrtcvad plus an energy gate does not: with a loud fan the noise floor is
    so high that normal speech never clears the relative threshold.
    """

    frame_len = 512  # 32 ms at 16 kHz, the chunk size the model was trained on
    context_len = 64

    def __init__(self, threshold: float):
        import faster_whisper
        import onnxruntime

        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.context_len, dtype=np.float32)

    def mark_started(self) -> None:
        pass

    def probability(self, pcm: bytes) -> float:
        chunk = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        x = np.concatenate([self._context, chunk])[None, :]
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = chunk[-self.context_len :]
        return float(np.asarray(out).reshape(-1)[0])

    def is_speech(self, pcm: bytes, rms: float) -> bool:
        return self.probability(pcm) >= self.threshold


class WebrtcVad:
    """webrtcvad with an adaptive energy gate so a quiet room's hiss doesn't trigger it."""

    frame_len = FRAME_LEN

    def __init__(self, aggressiveness: int):
        import webrtcvad

        self._vad = webrtcvad.Vad(aggressiveness)
        self.reset()

    def reset(self) -> None:
        self._noise = 0.0
        self._started = False

    def mark_started(self) -> None:
        self._started = True

    def is_speech(self, pcm: bytes, rms: float) -> bool:
        if not self._started:
            self._noise = rms if self._noise == 0 else 0.95 * self._noise + 0.05 * rms
        loud = rms > max(0.006, self._noise * 2.5) if not self._started else rms > max(0.004, self._noise * 1.5)
        return loud and self._vad.is_speech(pcm, SAMPLE_RATE)


def _to_float(frames: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0


class Microphone:
    def __init__(self, cfg: ListenConfig):
        self.cfg = cfg
        self.device = resolve_device(cfg.input_device, "input")
        level = min(3, max(0, int(cfg.vad_aggressiveness)))
        try:
            self._vad = SileroVad(SILERO_THRESHOLDS[level])
        except Exception:
            self._vad = WebrtcVad(level)
        self.frame_len = self._vad.frame_len
        self.frame_ms = self.frame_len * 1000 / SAMPLE_RATE
        self.name = self._describe()

    def _describe(self) -> str:
        spec = self.cfg.input_device
        return f"the {spec!r} microphone" if spec not in (None, "") else "the default microphone"

    def reconnect(self) -> None:
        """Look the device up again (after a stall, a replug or PipeWire restart, or a /mic change)."""
        self.name = self._describe()
        try:
            self.device = resolve_device(self.cfg.input_device, "input", fresh=True)
        except SystemExit:
            pass  # not back yet: keep the old handle and let the next open report it

    @property
    def vad_name(self) -> str:
        return "silero" if isinstance(self._vad, SileroVad) else "webrtc"

    def _stream(self, callback=None):
        """The input stream, at the device's own rate: callbacks still get SAMPLE_RATE frames of frame_len
        (see vision.devrate; PortAudio's own conversion is gritty on macOS)."""
        import sounddevice as sd

        from vision.devrate import InputResampler, device_rate

        rate = device_rate(self.device, "input", SAMPLE_RATE)
        if rate != SAMPLE_RATE and callback is not None:
            try:
                with self.device.opening():
                    return sd.InputStream(
                        samplerate=rate, channels=1, dtype="int16", device=self.device.index,
                        callback=InputResampler(callback, SAMPLE_RATE, rate, self.frame_len),
                    )
            except Exception:  # noqa: BLE001
                pass  # the device refused its own default: let PortAudio convert as before
        with self.device.opening():
            return sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=self.frame_len, device=self.device.index,
                callback=callback,
            )

    def _frames(self, stop: threading.Event | None = None):
        """Yield int16 frames as they arrive, until `stop` is set.

        Audio comes through a callback and a queue rather than a blocking read, so the caller gets
        control back within 0.1 s whatever the device does; STALL_S without a frame raises MicStalled
        instead of waiting forever (PortAudio's blocking read never returns when PipeWire has nothing
        to deliver, e.g. while a raw ALSA client holds the device). Close the generator to release the mic.
        """
        frames_q: queue.Queue[bytes] = queue.Queue()

        def on_audio(indata, _frames, _time, _status):
            frames_q.put(bytes(indata))

        with self._stream(callback=on_audio):
            last = time.monotonic()
            while stop is None or not stop.is_set():
                try:
                    pcm = frames_q.get(timeout=0.1)
                except queue.Empty:
                    if time.monotonic() - last > STALL_S:
                        raise MicStalled(
                            f"no audio is arriving from {self.name}: another program may be holding it, or it was unplugged"
                        )
                    continue
                last = time.monotonic()
                yield pcm

    # The VAD, for callers that run their own capture loop (the wake-word listener).
    def vad_reset(self) -> None:
        self._vad.reset()

    def vad_started(self) -> None:
        self._vad.mark_started()

    def is_speech(self, pcm: bytes) -> bool:
        samples = np.frombuffer(pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
        return self._vad.is_speech(pcm, rms)

    def record_utterance(
        self,
        on_speech_start=None,
        cancel: threading.Event | None = None,
        start_timeout_s: float | None = None,
        on_audio=None,
        every_ms: int = 0,
        on_level=None,
        timing=None,
        on_speech_end=None,
    ) -> np.ndarray | None:
        """Wait for speech, record until end_silence_ms of quiet. Returns float32 16 kHz or None
        (nothing said within the timeout, or `cancel` set). Raises MicStalled when no audio arrives.
        With `on_audio`, every `every_ms` of speech it gets the float32 audio so far (for a live
        transcript); it must return quickly, the capture loop waits on it. `on_speech_end` gets the
        audio so far at the first quiet frame after speech (at every pause; the last call before the
        utterance ends has all of it), so a live transcript can finish during the end silence rather
        than after it. Neither is called while the quiet runs on.
        `on_level` gets a normalized level for each speech frame (zero during silence)."""
        end_silence_frames = max(1, int(self.cfg.end_silence_ms / self.frame_ms))
        live_every = max(1, int(every_ms / self.frame_ms)) if on_audio and every_ms > 0 else 0
        max_frames = int(self.cfg.max_utterance_s * 1000 / self.frame_ms)
        timeout = self.cfg.start_timeout_s if start_timeout_s is None else start_timeout_s
        preroll = collections.deque(maxlen=int(400 / self.frame_ms))  # keep 400 ms before speech
        frames: list[bytes] = []
        voiced_run = 0
        silence_run = 0
        started = False
        t0 = time.monotonic()
        self._vad.reset()
        with contextlib.closing(self._frames(cancel)) as stream:
            for pcm in stream:
                is_speech = self.is_speech(pcm)
                if timing and is_speech:
                    timing.event("speech_end", replace=True)
                if on_level is not None:
                    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(samples ** 2))) if is_speech else 0.0
                    on_level(min(1.0, rms * 12))
                if not started:
                    preroll.append(pcm)
                    if is_speech:
                        voiced_run += 1
                        if voiced_run >= 3:  # ~90 ms of speech
                            started = True
                            self._vad.mark_started()
                            frames.extend(preroll)
                            if on_speech_start:
                                on_speech_start()
                    else:
                        voiced_run = 0
                    if timeout and time.monotonic() - t0 > timeout:
                        return None
                    continue
                frames.append(pcm)
                if is_speech:
                    silence_run = 0
                    if live_every and len(frames) % live_every == 0:
                        on_audio(_to_float(frames))
                else:
                    silence_run += 1
                    if silence_run == 1 and on_speech_end is not None:
                        on_speech_end(_to_float(frames))
                    if silence_run >= end_silence_frames:
                        break
                if len(frames) >= max_frames:
                    break
            else:
                return None  # cancelled
        if timing:
            timing.event("endpoint")
        return _to_float(frames)

    def record_until_enter(self, stop: threading.Event | None = None) -> np.ndarray:
        """Push-to-talk: record until `stop` is set (or, if none given, until Enter is pressed)."""
        if stop is None:
            stop = threading.Event()

            def waiter():
                try:
                    sys.stdin.readline()
                finally:
                    stop.set()

            threading.Thread(target=waiter, daemon=True).start()
        with contextlib.closing(self._frames(stop)) as stream:
            frames = list(stream)
        return np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
