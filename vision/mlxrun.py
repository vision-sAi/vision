"""Running MLX work (Apple Silicon): shared by the voice (vision.tts) and speech recognition (vision.stt)."""
from __future__ import annotations

import queue
import sys
import threading


def mlx_available() -> bool:
    """True on Apple Silicon with the voice-mac build (mlx-audio) installed; imports nothing heavy."""
    import importlib.util
    import platform

    return sys.platform == "darwin" and platform.machine() == "arm64" and importlib.util.find_spec("mlx_audio") is not None


def mlx_fetch(repo: str) -> str:
    """Download an MLX model the way mlx-audio does (its files only) and return its local path."""
    from mlx_audio.utils import get_model_path

    from vision.stt import thread_only_tqdm

    thread_only_tqdm()
    return str(get_model_path(repo))


class MlxWorker:
    """One thread that does all of a model's MLX work.

    MLX keeps its GPU streams per thread, and Vision loads the voice on one thread (a warm-up) and speaks
    on another (the synthesis loop), so every MLX call goes through here instead."""

    def __init__(self, name: str):
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name=name).start()

    def _run(self) -> None:
        while True:
            fn, done = self._jobs.get()
            try:
                done.put((True, fn()))
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                done.put((False, e))

    def call(self, fn, timeout: float | None = None):
        """Run fn() on the worker and return its result (or raise its exception). With `timeout`, stop
        waiting after that many seconds (TimeoutError): the job stays queued and still runs once the
        worker gets to it, so a caller can move on from a worker wedged in an earlier job."""
        done: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((fn, done))
        try:
            ok, value = done.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"the MLX worker did not get to it in {timeout:g} s") from None
        if not ok:
            raise value
        return value

    def stream(self, make_iter, cancel: threading.Event):
        """Iterate make_iter() on the worker, yielding its items here as they come; setting `cancel` (or
        abandoning this generator) stops the worker at the next item."""
        items: queue.Queue = queue.Queue()
        end = object()

        def run():
            try:
                for item in make_iter():
                    items.put(item)
                    if cancel.is_set():
                        break
            except BaseException as e:  # noqa: BLE001
                items.put(e)
            items.put(end)

        self._jobs.put((run, queue.Queue()))
        try:
            while True:
                item = items.get()
                if item is end:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            cancel.set()
