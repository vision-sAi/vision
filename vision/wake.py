"""Wake word: hear Vision's name while the chat is idle, then hand the utterance to the voice loop.

No trained keyword model: a tiny Whisper on the CPU transcribes the first couple of seconds of every
utterance the VAD end-points and looks for the name in it (loosely, since Whisper spells a lone
"Vision" in several ways). Utterances without the name are dropped as they are heard; the one with
it is returned whole, so "Vision, what's the weather?" needs no second prompt.
"""
from __future__ import annotations

import collections
import dataclasses
import difflib
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from vision.audio import STALL_S, MicStalled, Microphone
from vision.config import ListenConfig, WakeConfig
from vision.stt import Transcriber

# How much of an utterance the name is looked for in. Anything said after the name is the command.
PROBE_S = 2.4
# While Vision speaks, the last PROBE_S seconds are probed this often (overlapping, so the name is never split).
HOP_S = 1.2
# A probe while Vision speaks needs at least this much voice in its window (the speakers are quiet in headphones).
MIN_VOICED_S = 0.3
# With `barge_in = "speech"`, this much continuous voice cuts a reply: longer than a cough, shorter than a word.
CUT_IN_S = 0.25
# difflib ratio a word needs against a configured name: "vision's"/"envision" pass, "version"/"mission" don't.
FUZZ = 0.85
# Words that may precede the name and are dropped from the command: "hey Vision", "okay Vision".
_LEAD_INS = {"hey", "hi", "ok", "okay", "yo", "oi", "so", "um", "uh", "right"}

_PUNCT = re.compile(r"^[^\w']+|[^\w']+$")


def match_wake(text: str, names: list[str] | tuple[str, ...], fuzz: float = FUZZ, anywhere: bool = False) -> str | None:
    """If the name is in `text`, return what was said besides it (the command; '' if only the name).

    None when the name is not there. The name must open the sentence (one of the first three words,
    so "okay, Vision" counts) or close it ("what time is it, Vision?"): said in the middle it is just
    the word ("my vision is blurry"). A lead-in word right before it ("hey") goes with it, and a stray
    comma after it is dropped.

    `anywhere` is for cutting into a reply: the mic then also hears Vision's own voice before the
    name, so the name counts wherever it is and only what follows it is the command.
    """
    tokens = text.split()
    hit = None
    for i, tok in enumerate(tokens):
        if not anywhere and i >= 3 and i != len(tokens) - 1:
            continue
        word = _PUNCT.sub("", tok).lower()
        if not word:
            continue
        if word in names or any(difflib.SequenceMatcher(None, word, n).ratio() >= fuzz for n in names):
            hit = i
            break
    if hit is None:
        return None
    start = hit
    if hit > 0 and _PUNCT.sub("", tokens[hit - 1]).lower() in _LEAD_INS:
        start = hit - 1
    rest = ([] if anywhere else tokens[:start]) + tokens[hit + 1 :]
    command = " ".join(rest).strip()
    command = re.sub(r"^[\s,.;:!?-]+", "", command)
    command = re.sub(r"[\s,;:-]+$", "", command)
    if command and command[0].islower():
        command = command[0].upper() + command[1:]
    return command


class WakeListener:
    """Waits for the name on its own mic stream; the caller opens the real ears afterwards."""

    def __init__(self, listen: ListenConfig, wake: WakeConfig):
        self.names = tuple(wake.names)
        self.mic = Microphone(listen)
        # Its own tiny Whisper on the CPU: the GPU one may not be loaded yet, and this runs all day.
        self.stt = Transcriber(dataclasses.replace(listen, whisper_model=wake.model, device="cpu"))
        self.listen = listen

    def warm_up(self) -> None:
        self.stt.warm_up()

    @property
    def model(self) -> str:
        return self.stt.device

    def _probe(self, frames: list[bytes], anywhere: bool = False) -> str | None:
        audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
        text = self.stt.transcribe(audio)
        return match_wake(text, self.names, anywhere=anywhere) if text else None

    def wait(
        self,
        cancel: threading.Event,
        paused: Callable[[], bool] | None = None,
        on_heard: Callable[[str], None] | None = None,
        replying: Callable[[], bool] | None = None,
        on_spotted: Callable[[], None] | None = None,
        speech_cuts_in: bool = False,
    ) -> tuple[np.ndarray, str] | None:
        """Block until an utterance starts with the name. Returns (the whole utterance, the command
        heard by the small model, '' if only the name) or None once `cancel` is set.

        `paused` is polled per frame: while it is true (Vision is speaking) audio is dropped, so the
        speakers cannot wake it. `on_heard` gets the small model's transcript of a wake, for the UI.

        `replying` is the alternative to `paused` that lets you cut in: while it is true the mic may be
        hearing the speakers too, so nothing can be end-pointed; instead the last PROBE_S seconds are
        probed every HOP_S for the name anywhere in them (with `speech_cuts_in`, CUT_IN_S of any voice
        counts instead). A hit calls `on_spotted` at once, so the caller can stop the reply, and what is
        said until the next silence is returned as the utterance. It starts at the probed window, so it
        may open with the tail of the reply: transcribe it with `match_wake(..., anywhere=True)`.
        """
        mic = self.mic
        frame_ms = mic.frame_ms
        end_silence_frames = max(1, int(self.listen.end_silence_ms / frame_ms))
        max_frames = int(self.listen.max_utterance_s * 1000 / frame_ms)
        probe_frames = int(PROBE_S * 1000 / frame_ms)
        hop_frames = max(1, int(HOP_S * 1000 / frame_ms))
        min_voiced = max(1, int(MIN_VOICED_S * 1000 / frame_ms))
        cut_in_frames = max(1, int(CUT_IN_S * 1000 / frame_ms))
        # A callback stream feeds a queue: the probe transcription takes a few hundred milliseconds and
        # a blocking read would overflow PortAudio's buffer meanwhile.
        frames_q: queue.Queue[bytes] = queue.Queue()

        def on_audio(indata, _frames, _time, _status):
            frames_q.put(bytes(indata))

        preroll: collections.deque[bytes] = collections.deque(maxlen=int(400 / frame_ms))
        # While a reply is on: the last PROBE_S seconds, each frame with whether it was voiced.
        window: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=probe_frames)
        frames: list[bytes] = []
        started = probed = ignoring = False
        voiced_run = silence_run = since_probe = 0
        command: str | None = None
        mic.vad_reset()

        def reset():
            nonlocal frames, started, probed, ignoring, voiced_run, silence_run, since_probe, command
            frames, started, probed, ignoring, command = [], False, False, False, None
            voiced_run = silence_run = since_probe = 0
            preroll.clear()
            window.clear()
            mic.vad_reset()

        with mic._stream(callback=on_audio):
            last = time.monotonic()
            while True:
                if cancel.is_set():
                    return None
                try:
                    pcm = frames_q.get(timeout=0.1)
                except queue.Empty:
                    if time.monotonic() - last > STALL_S:  # the device went quiet: say so, don't sit here all day
                        raise MicStalled(
                            f"no audio is arriving from {mic.name}: another program may be holding it, or it was unplugged"
                        )
                    continue
                last = time.monotonic()
                if paused is not None and paused():
                    if started or frames or preroll:
                        reset()
                    continue
                is_speech = mic.is_speech(pcm)
                if not started:
                    if replying is not None and replying():
                        window.append((pcm, is_speech))
                        voiced_run = voiced_run + 1 if is_speech else 0
                        since_probe += 1
                        if speech_cuts_in:
                            if voiced_run < cut_in_frames:
                                continue
                            command = ""
                        else:
                            if since_probe < hop_frames or sum(v for _, v in window) < min_voiced:
                                continue
                            since_probe = 0
                            command = self._probe([f for f, _ in window], anywhere=True)
                            if command is None:
                                continue
                        # Heard: stop the reply, then keep the window and record on to the next silence.
                        started = probed = True
                        mic.vad_started()
                        frames = [f for f, _ in window]
                        window.clear()
                        if on_spotted:
                            on_spotted()
                        continue
                    if window:  # the reply ended without a cut-in: back to end-pointing utterances
                        window.clear()
                        voiced_run = since_probe = 0
                    preroll.append(pcm)
                    if is_speech:
                        voiced_run += 1
                        if voiced_run >= 3:  # ~100 ms of speech
                            started = True
                            mic.vad_started()
                            frames.extend(preroll)
                    else:
                        voiced_run = 0
                    continue
                if not ignoring:
                    frames.append(pcm)
                if is_speech:
                    silence_run = 0
                else:
                    silence_run += 1
                ended = silence_run >= end_silence_frames or len(frames) >= max_frames
                if not probed and not ignoring and (len(frames) >= probe_frames or ended):
                    probed = True
                    command = self._probe(frames)
                    if command is None:
                        ignoring = True  # not for us: let the utterance run out without keeping it
                        frames = []
                if ended:
                    if command is not None:
                        audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
                        if on_heard:
                            on_heard(command)
                        return audio, command
                    reset()


class BargeIn:
    """Runs the listener on a thread while one reply is spoken, so you can cut in without the keyboard.

    `mode` is `barge_in` from the config: "wake" (say the name) or "speech" (just talk; needs headphones
    or echo cancellation, or Vision hears itself). `on_cut` runs on the listener's thread the moment you
    are heard and should stop the reply. Once the reply is over, stop() lets go of the mic and returns
    what you said, (audio, the small model's command), or None if you did not cut in.
    """

    def __init__(self, listener: WakeListener, mode: str, on_cut: Callable[[], None]):
        self.listener = listener
        self.mode = mode
        self.on_cut = on_cut
        self.cut = threading.Event()
        self.error: Exception | None = None
        self._cancel = threading.Event()
        self._result: tuple[np.ndarray, str] | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> BargeIn:
        self._thread.start()
        return self

    def _spotted(self) -> None:
        self.cut.set()
        self.on_cut()

    def _run(self) -> None:
        try:
            self._result = self.listener.wait(
                self._cancel, replying=lambda: True, on_spotted=self._spotted, speech_cuts_in=self.mode == "speech"
            )
        except Exception as e:  # noqa: BLE001  e.g. MicStalled: the caller reports it
            self.error = e

    def stop(self) -> tuple[np.ndarray, str] | None:
        """The reply has ended: if you cut in, wait for the rest of what you said; otherwise let go now."""
        if not self.cut.is_set():
            self._cancel.set()
        self._thread.join(timeout=self.listener.listen.max_utterance_s + 5)
        if self._thread.is_alive():
            self._cancel.set()
            self._thread.join(timeout=3)
        return self._result
