"""The background service's voice and ears, borrowed by a terminal on the same machine.

Only one Vision can have the voice on the GPU (cuda.GpuClaim), and the background service (`vision serve`, for
the phone) usually has it, with Whisper beside it. A terminal's /talk used to stop there. Now, when a `vision
serve` is running here, the terminal hands it the work instead of loading its own: what the mic heard goes to
POST /transcribe, and each sentence of a reply to POST /speak, whose audio comes back to play through this
terminal's own speakers. The mic, the wake word, cutting in and playback all stay here; only the models are
the service's. With no service running, a terminal loads its own voice and ears as it always did.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import urllib.error
import urllib.request

import numpy as np

from vision.config import ListenConfig, VoiceConfig
from vision.tts import SAMPLE_RATE, Speaker, TTSError

WHERE = "the background service"


def service() -> dict | None:
    """{"url", "token"} of the `vision serve` on this machine to borrow from: running, not this process, and
    answering. None otherwise."""
    try:
        from vision.remote import running_server

        info = running_server()
    except Exception:  # noqa: BLE001 - no serve extra, an unreadable descriptor: nothing to borrow from
        return None
    if not info or info.get("pid") == os.getpid():
        return None
    url = f"http://127.0.0.1:{info['port']}" if info.get("port") else str(info.get("url") or "").rstrip("/")
    server = {"url": url, "token": info["token"]}
    try:
        _call(server, "GET", "/voice/rate", timeout=3)
    except Exception:  # noqa: BLE001 - starting up, stopping, or not this machine's after all
        return None
    return server


def _call(server: dict, method: str, path: str, body: bytes | None = None, content_type: str = "",
          timeout: float = 30) -> bytes:
    """One request to the service; HTTPError and URLError are left to the caller."""
    req = urllib.request.Request(server["url"] + path, data=body, method=method)
    req.add_header("Authorization", f"Bearer {server['token']}")
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _why(e: Exception) -> str:
    """The service's own words for a failed request, when it gave any."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        return str(detail or f"HTTP {e.code}")
    if isinstance(e, urllib.error.URLError):
        return f"it isn't answering ({e.reason})"
    return str(e)


def _wav(audio: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    out = io.BytesIO()
    sf.write(out, np.asarray(audio, dtype=np.float32), sample_rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def _samples(wav: bytes) -> np.ndarray:
    """Mono float32 at SAMPLE_RATE from a WAV the service sent."""
    import soundfile as sf

    audio, sr = sf.read(io.BytesIO(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE and audio.size:
        idx = np.arange(0, len(audio), sr / SAMPLE_RATE)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32)


class ServiceSpeaker(Speaker):
    """A Speaker whose sentences are spoken by the service and played here. Playback, stopping, fillers and
    the reply's pacing are Speaker's and StreamingSpeaker's own; only synthesis goes over. The speed and the
    per-voice tweaks are the service's (it reads the same config), so nothing is stretched again here."""

    def __init__(self, cfg: VoiceConfig, server: dict):
        super().__init__(cfg)
        self.server = server

    @property
    def device(self) -> str:
        return WHERE

    @property
    def device_note(self) -> str:
        return WHERE

    def _load(self):
        if self._loaded:
            return
        try:
            _call(self.server, "GET", "/voice", timeout=10)
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"{WHERE} has the voice, but {_why(e)}") from None
        self._loaded = True

    def close(self):
        self._loaded = False
        self._stop.clear()

    def set_voice(self, spec: str) -> None:
        """Change the service's voice: it is the one voice, for the phone as well."""
        try:
            _call(self.server, "POST", "/voice", json.dumps({"voice": spec}).encode(), "application/json")
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"{WHERE} could not switch to {spec!r}: {_why(e)}") from None
        try:
            self.engine.set_voice(spec)  # the name the fillers are filed under
        except Exception:  # noqa: BLE001
            pass

    def synth_stream(self, text: str, cont: bool = False):
        """The whole of `text` in one piece, as the service speaks it. `text` has been through speechify
        already (StreamingSpeaker, prepare_fillers), so the service is told not to do it again."""
        text = text.strip()
        if not text:
            return
        try:
            wav = _call(self.server, "POST", "/speak", json.dumps({"text": text, "spoken": True}).encode(),
                        "application/json", timeout=180)  # the first sentence may wait for the service to load its voice
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"{WHERE} could not speak: {_why(e)}") from None
        audio = _samples(wav)
        if audio.size:
            yield audio


class ServiceTranscriber:
    """Transcriber's part a talk session uses, answered by the service's Whisper."""

    def __init__(self, cfg: ListenConfig, server: dict):
        self.cfg = cfg
        self.server = server
        self.device = "?"
        self.mlx_error: str | None = None
        self._live = False

    def load(self, quiet: bool = False) -> None:
        """Ask once, with a moment of silence, whether the service's Whisper is quick enough for live previews
        (it says no, 409, when it isn't on a GPU); that also has it load Whisper now, not at the first turn."""
        if self.device != "?":
            return
        try:
            self._post(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), preview=True, timeout=180)
            self._live = True
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise RuntimeError(f"{WHERE} has the ears, but {_why(e)}") from None
            self._live = False
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{WHERE} has the ears, but {_why(e)}") from None
        self.device = WHERE

    def warm_up(self) -> None:
        self.load(quiet=True)

    def close(self) -> None:
        self.device = "?"

    @property
    def can_preview_live(self) -> bool:
        return self._live

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        return self._text(audio, sample_rate, preview=False)

    def preview(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        """The utterance so far, for LiveTranscript: the service neither logs it nor keeps it."""
        return self._text(audio, sample_rate, preview=True)

    def _text(self, audio: np.ndarray, sample_rate: int, preview: bool) -> str:
        if audio.size < sample_rate * 0.25:
            return ""
        try:
            return self._post(audio, preview=preview, sample_rate=sample_rate)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{WHERE} could not transcribe: {_why(e)}") from None

    def _post(self, audio: np.ndarray, preview: bool, sample_rate: int = SAMPLE_RATE, timeout: float = 60) -> str:
        boundary = f"vision-{secrets.token_hex(8)}"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + _wav(audio, sample_rate) + f"\r\n--{boundary}--\r\n".encode()
        path = "/transcribe?preview=1" if preview else "/transcribe"
        raw = _call(self.server, "POST", path, body, f"multipart/form-data; boundary={boundary}", timeout=timeout)
        return str(json.loads(raw.decode("utf-8")).get("text") or "").strip()


def speaker(cfg: VoiceConfig) -> Speaker:
    """The service's voice when one is running here, else this terminal's own."""
    server = service()
    return ServiceSpeaker(cfg, server) if server else Speaker(cfg)


def transcriber(cfg: ListenConfig):
    """The service's ears when one is running here, else this terminal's own Whisper."""
    from vision.stt import Transcriber

    server = service()
    return ServiceTranscriber(cfg, server) if server else Transcriber(cfg)


def set_service_voice(on: bool) -> bool | None:
    """Switch the service's voice on or off (POST /voice/power): off unloads its voice and ears. True when it
    did, False when it didn't answer, None when no service is running here."""
    server = service()
    if server is None:
        return None
    try:
        _call(server, "POST", "/voice/power", json.dumps({"on": on}).encode(), "application/json", timeout=60)
    except Exception:  # noqa: BLE001
        return False
    return True
