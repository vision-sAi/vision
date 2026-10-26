"""Shell mode, Claude Code's `!`: a command the user runs themselves in the chat's folder, from the
terminal's input box or the phone's composer. What it printed goes to the brain with the next message
(brain.shell_note); this runs it."""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

TIMEOUT_S = 600  # a command still going after this long is stopped
DRAIN_S = 5  # after the command exits, how long to wait for output from what it left running (`cmd &`)
LIVE_S = 0.25  # what a running command printed is shown at most this often


def run(command: str, cwd: str | None, started: Callable[[subprocess.Popen], None] = lambda proc: None,
        on_output: Callable[[str], None] | None = None) -> tuple[str, int | None]:
    """Run `command` with the user's shell in `cwd`, stdin closed, in a process group of its own (stop()
    ends it and whatever it started). Returns (what it printed, stdout and stderr together; its exit code,
    or None when it was stopped or timed out). `started` gets the process as soon as it runs; `on_output`
    gets what it prints as it prints it (from a reader thread), so a command that prints and then waits
    (a login's one-time code) shows that straight away rather than once it ends."""
    try:
        proc = subprocess.Popen([os.environ.get("SHELL") or "/bin/sh", "-c", command], cwd=cwd or None,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"},  # a Python script's prints as they happen
                                start_new_session=True)
    except OSError as e:
        return str(e), 127
    started(proc)
    parts: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def read() -> None:
        fd = proc.stdout.fileno()
        while chunk := os.read(fd, 65536):
            if text := decoder.decode(chunk):
                parts.append(text)
                if on_output:
                    on_output(text)
        if text := decoder.decode(b"", final=True):
            parts.append(text)
            if on_output:
                on_output(text)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        stop(proc)
        proc.wait()
    reader.join(timeout=DRAIN_S)  # something it started may hold the pipe open: not waited on for ever
    if not reader.is_alive():
        proc.stdout.close()  # else the reader still holds it, and lets go when what's left running does
    return "".join(parts), proc.returncode if proc.returncode >= 0 else None


class Live:
    """What a running command has printed so far, handed to `show` (the whole of it) at most every `every`
    seconds, and once more shortly after the last chunk: a burst doesn't flood the phone, and the line a
    command prints just before it waits (a login's URL) isn't held back until the next one."""

    def __init__(self, show: Callable[[str], None], every: float = LIVE_S):
        self.show, self.every = show, every
        self.text = ""
        self._lock = threading.Lock()
        self._shown = 0.0  # when `show` last ran (monotonic)
        self._timer: threading.Timer | None = None
        self._closed = False

    def add(self, chunk: str) -> None:
        with self._lock:
            self.text += chunk
            if self._closed or self._timer is not None:
                return  # a showing is already due and will take this in
            wait = self.every - (time.monotonic() - self._shown)
            if wait > 0:
                self._timer = threading.Timer(wait, self._flush)
                self._timer.daemon = True
                self._timer.start()
                return
            self._shown = time.monotonic()
            text = self.text
        self.show(text)

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            if self._closed:
                return
            self._shown = time.monotonic()
            text = self.text
        self.show(text)

    def close(self) -> None:
        """The command is over: nothing more is shown (its final output is the caller's to show)."""
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def stop(proc: subprocess.Popen | None) -> bool:
    """Stop a running command and what it started. False when there was none to stop."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def tail(output: str, lines: int = 200) -> str:
    """The end of what a command printed, as the phone is sent it (the brain gets more: brain.shell_note)."""
    rows = output.rstrip("\n").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return f"… {len(rows) - lines} earlier lines\n" + "\n".join(rows[-lines:])
