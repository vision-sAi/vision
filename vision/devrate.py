"""Open audio devices at their own sample rate and resample in Vision, not in PortAudio.

The voice is made at 24 kHz and Whisper wants 16 kHz, but most devices run at 44.1 or 48 kHz. Opening a
stream at Vision's rate makes PortAudio convert on the fly, and on macOS that conversion is audibly gritty
(AirPods at 48 kHz: static over every word, while the same clip played by `afplay` was clean). So streams
are opened at the device's default rate and soxr converts: streaming and stateful, so a reply fed in
pieces resamples as one continuous signal, with no seam at the joins.

Without soxr (an install from before it was a dependency) everything opens at Vision's rate as before.
"""
from __future__ import annotations

import numpy as np

from vision.config import AudioDevice


def _soxr():
    try:
        import soxr

        return soxr
    except ImportError:
        return None


def device_rate(device: AudioDevice, kind: str, fallback: int) -> int:
    """The rate `device` runs at on its own (`kind`: "input" or "output"); `fallback` when unknown or
    when there is no resampler to bridge the difference."""
    if _soxr() is None:
        return fallback
    import sounddevice as sd

    try:
        info = sd.query_devices(device.index, kind) if device.index is not None else sd.query_devices(kind=kind)
        rate = int(round(float(info["default_samplerate"])))
    except Exception:  # noqa: BLE001
        return fallback
    return rate if rate > 0 else fallback


class Resampler:
    """Streaming float32 mono resampler from `src` to `dst` Hz; a pass-through when they match."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst = src, dst
        self._rs = None
        if src != dst:
            self._rs = _soxr().ResampleStream(src, dst, 1, dtype="float32", quality="HQ")

    def feed(self, audio: np.ndarray, last: bool = False) -> np.ndarray:
        audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if self._rs is None:
            return audio
        return self._rs.resample_chunk(audio, last=last)


class OutputStream:
    """sounddevice's OutputStream at the device's own rate, taking `rate` Hz audio from write().

    Only what Speaker uses is passed through: start, stop, abort, close, latency and write."""

    def __init__(self, device: AudioDevice, rate: int, latency: float | None = None):
        import sounddevice as sd

        self.rate = rate
        dev_rate = device_rate(device, "output", rate)
        with device.opening():
            try:
                self._stream = sd.OutputStream(samplerate=dev_rate, channels=1, dtype="float32",
                                               device=device.index, latency=latency)
            except Exception:  # noqa: BLE001
                if dev_rate == rate:
                    raise
                dev_rate = rate  # the device refused its own default: let PortAudio convert as before
                self._stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                               device=device.index, latency=latency)
        self._resampler = Resampler(rate, dev_rate)
        self.underflows = 0  # writes that found the device had run dry: each is a gap the listener heard

    @property
    def latency(self) -> float:
        return self._stream.latency

    def write(self, audio: np.ndarray) -> None:
        out = self._resampler.feed(audio)
        if out.size and self._stream.write(out.reshape(-1, 1)):
            self.underflows += 1

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        try:
            # After abort() (a reply cut short) the stream takes no more writes: skip the resampler's tail.
            if self._stream.active:
                tail = self._resampler.feed(np.zeros(0, dtype=np.float32), last=True)
                if tail.size:
                    self._stream.write(tail.reshape(-1, 1))
        finally:
            self._stream.stop()

    def abort(self) -> None:
        self._stream.abort()

    def close(self) -> None:
        self._stream.close()


def play(audio: np.ndarray, rate: int, device: AudioDevice) -> None:
    """sd.play for a whole clip, at the device's own rate. Returns at once, like sd.play: sd.wait() for it."""
    import sounddevice as sd

    dev_rate = device_rate(device, "output", rate)
    if dev_rate != rate:
        audio = Resampler(rate, dev_rate).feed(audio, last=True)
    with device.opening():
        sd.play(audio, dev_rate, device=device.index)


class InputResampler:
    """Wraps a sounddevice int16 input callback so the device can run at `dev_rate` while the callback
    still gets `rate` Hz audio in `frame_len`-sample frames (the VAD needs exact frame sizes)."""

    def __init__(self, callback, rate: int, dev_rate: int, frame_len: int):
        self.callback = callback
        self.frame_len = frame_len
        self._resampler = Resampler(dev_rate, rate)
        self._pending = np.zeros(0, dtype=np.float32)

    def __call__(self, indata, frames, time, status) -> None:
        x = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float32) / 32768.0
        self._pending = np.concatenate([self._pending, self._resampler.feed(x)])
        while len(self._pending) >= self.frame_len:
            frame, self._pending = self._pending[: self.frame_len], self._pending[self.frame_len :]
            pcm = (np.clip(frame, -1.0, 32767 / 32768) * 32768.0).astype(np.int16)
            self.callback(pcm.tobytes(), self.frame_len, time, status)
