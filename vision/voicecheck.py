"""`vision voice check`: the three things worth knowing before the first /talk, right after the voice
is installed (welcome.voice_step runs it then). Vision says a line and asks whether you heard it,
listens to one and shows what it made of it, then waits for its name. A check that fails offers the
other speakers or microphones once and tries again; the pick is saved to config.toml.

Only one Vision at a time keeps the voice on the GPU (cuda.GpuClaim), and the background service
(`vision service install`, for the phone) is usually the one that has it. The check says so rather
than blaming the speakers, and offers to pause the service while it runs.

Speaking and listening go through `Hands`, so tests put a fake there (tests/test_voicecheck.py), and
the questions through the same `IO` as the rest of the setup."""
from __future__ import annotations

import re
import threading

from vision import config
from vision.config import load_config

from vision.theme import ACCENT
LINE = "Hello. This is Vision. If you can hear me, the voice is working."
WAKE_WAIT_S = 12  # how long it waits for the name
HEAR_WAIT_S = 10  # how long it waits for you to start speaking


class Hands:
    """The real voice and ears, each loaded the first time it is used (the models take a moment)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._speaker = self._mic = self._stt = None

    def say(self, text: str) -> None:
        """Speak it and come back when it has finished."""
        if self._speaker is None:
            from vision.tts import Speaker

            self._speaker = Speaker(self.cfg.voice)
        self._speaker._load()  # raises when the voice can't load; say() alone would just stay silent
        self._speaker.say(text)

    def reset(self) -> None:
        """Forget the loaded (or failed) voice, so the next say() loads it afresh."""
        self._speaker = None

    def hear(self) -> str:
        """One utterance off the microphone, transcribed; "" when nothing was said."""
        from vision.audio import Microphone
        from vision.stt import Transcriber

        if self._stt is None:
            self._stt = Transcriber(self.cfg.listen)
            self._stt.warm_up()
        if self._mic is None:
            self._mic = Microphone(self.cfg.listen)
        audio = self._mic.record_utterance(start_timeout_s=HEAR_WAIT_S)
        if audio is None or audio.size == 0:
            return ""
        return self._stt.transcribe(audio).strip()

    def wake(self, timeout: float = WAKE_WAIT_S) -> bool:
        """Whether Vision's name was heard within `timeout` seconds."""
        from vision.wake import WakeListener

        self._mic = None  # the wake listener opens the microphone itself
        listener = WakeListener(self.cfg.listen, self.cfg.wake)
        listener.warm_up()
        cancel = threading.Event()
        timer = threading.Timer(timeout, cancel.set)
        timer.start()
        try:
            return listener.wait(cancel) is not None
        finally:
            timer.cancel()

    def use_output(self, spec: str) -> None:
        self.cfg.voice.output_device = spec
        self._speaker = None

    def use_input(self, spec: str) -> None:
        self.cfg.listen.input_device = spec
        self._mic = None


def _heading(io, text: str) -> None:
    io.say(f"\n[bold {ACCENT}]◆[/] [bold]{text}[/bold]")


def _ok(io, text: str) -> None:
    io.say(f"  [green]✓[/green] {text}")


def _warn(io, text: str) -> None:
    io.say(f"  [yellow]![/yellow] {text}")


def _other_device(io, title: str, rows, current: str) -> str | None:
    """Offer the other devices after a failed check: the pick, or None when there is nothing else to try."""
    try:
        choices = rows()
    except Exception:  # noqa: BLE001 - no PortAudio, no PipeWire
        return None
    if len(choices) < 2:
        return None
    return io.pick(title, choices, current=current)


def voice_holder(error: Exception) -> int | None:
    """The pid of the other Vision that has the voice loaded, when that is why it would not load here."""
    m = re.search(r"already loaded in another Vision \(pid (\d+)", str(error))
    return int(m.group(1)) if m else None


def _held_elsewhere(io, hands, holder: int, paused: list) -> bool:
    """The voice is loaded in another Vision. When that is the background service, offer to pause it
    for the check (True: paused, try again); otherwise say whose it is and what to do."""
    from vision import service

    if holder != service.pid():
        _warn(io, f"Another Vision (pid {holder}) has the voice loaded, and only one can at a time. "
                  "Close it, then `vision voice check` again.")
        return False
    io.say("  The background service (`vision serve`, for your phone) has the voice loaded, and only one Vision can at a time.")
    if not io.confirm("Pause the service for this check? It starts again afterwards.", default=True):
        _warn(io, "Left it running, so the voice can't be checked here. Your speakers were not tested.")
        return False
    try:
        with io.working("pausing the background service…"):
            service.stop()
    except service.ServiceError as e:
        _warn(io, f"Couldn't pause it: {e}")
        return False
    paused.append(True)
    getattr(hands, "reset", lambda: None)()
    return True


def speaker_check(io, cfg, hands, paused: list | None = None) -> bool:
    paused = [] if paused is None else paused
    _heading(io, "Can you hear Vision?")
    attempt = 1
    while attempt <= 2:
        try:
            with io.working("loading the voice…" if attempt == 1 else "speaking…"):
                hands.say(LINE)
        except Exception as e:  # noqa: BLE001 - a missing model, no output device, a GPU that is busy
            holder = voice_holder(e)
            if holder is None:
                _warn(io, f"The voice didn't start: {e}")
                return False
            if not paused and _held_elsewhere(io, hands, holder, paused):
                continue  # the service is out of the way: the same attempt again
            return False
        attempt += 1
        if io.confirm("Did you hear that?", default=True):
            _ok(io, "The voice works.")
            return True
        if attempt > 2:
            break
        pick = _other_device(io, "Which speakers should Vision use?", config.output_device_choices, cfg.voice.output_device)
        if pick is None:
            break
        config.save_output_device(pick)
        hands.use_output(pick)
    _warn(io, "No sound yet. `vision doctor` lists the audio devices; `vision voice check` tries again.")
    return False


def mic_check(io, cfg, hands) -> bool:
    _heading(io, "Can Vision hear you?")
    for attempt in (1, 2):
        io.say("  Say something, like “testing, one two three”.")
        try:
            with io.working("listening…"):
                heard = hands.hear()
        except Exception as e:  # noqa: BLE001 - no microphone, a stalled stream, a model that did not load
            _warn(io, f"The microphone didn't open: {e}")
            heard = ""
        if heard:
            _ok(io, f"Heard: “{heard}”")
            return True
        _warn(io, "Didn't hear anything.")
        if attempt == 2:
            break
        pick = _other_device(io, "Which microphone should Vision use?", config.input_device_choices, cfg.listen.input_device)
        if pick is None:
            break
        config.save_input_device(pick)
        hands.use_input(pick)
    _warn(io, "Still nothing. /mic in a chat picks the microphone; `vision voice check` tries again.")
    return False


def wake_check(io, cfg, hands) -> bool:
    """The name is optional (/wake turns it on), so missing it is said gently and fails nothing."""
    name = (cfg.wake.names[0] if cfg.wake.names else "vision").capitalize()
    _heading(io, "The wake word")
    io.say(f"  Say “{name}”.")
    try:
        with io.working("listening for its name…"):
            woke = hands.wake()
    except Exception as e:  # noqa: BLE001
        io.say(f"  [dim]Couldn't listen for it ({e}). /wake in a chat turns it on later.[/dim]")
        return False
    if woke:
        _ok(io, f"“{name}” wakes it. /wake in a chat keeps it listening for its name.")
    else:
        io.say("  [dim]Didn't catch it. It's optional: /wake in a chat turns it on, and saying the name a little louder usually does it.[/dim]")
    return woke


def check(io, cfg=None, hands=None) -> bool:
    """All three, in order. True when Vision can be heard and can hear; the wake word is a bonus, and
    is only tried once the microphone is known to work."""
    cfg = cfg or load_config()
    hands = hands or Hands(cfg)
    paused: list = []  # non-empty once the background service was paused for the check
    try:
        heard = speaker_check(io, cfg, hands, paused)
        hears = mic_check(io, cfg, hands)
        if hears:
            wake_check(io, cfg, hands)
    finally:
        if paused:
            _resume_service(io, hands)
    if heard and hears:
        io.say("\n  [green]✓[/green] Voice is ready. /talk in a chat starts a voice conversation; /speak reads replies aloud.")
    return heard and hears


def _resume_service(io, hands) -> None:
    """Start the background service again after the check. This process lets go of the voice first,
    so the service can load it for the phone."""
    from vision import service

    getattr(hands, "reset", lambda: None)()
    try:
        service.start()
    except service.ServiceError as e:
        _warn(io, f"The background service didn't start again: {e}. `vision service install` brings it back.")
        return
    io.say("  [dim]The background service is running again. While it is, the terminal's /speak and /talk can't load the voice: "
           "`systemctl --user stop vision` frees it.[/dim]" if service.kind() == "systemd"
           else "  [dim]The background service is running again. While it is, the terminal's /speak and /talk can't load the voice.[/dim]")
