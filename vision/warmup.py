"""Warmup readiness, measured by completed components rather than elapsed time."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class WarmupStatus:
    components: tuple[str, ...]
    ready: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def percent(self) -> int:
        return len(self.ready) * 100 // len(self.components)

    @property
    def fraction(self) -> float:
        return len(self.ready) / len(self.components)


class WarmupProgress:
    """One load attempt. Concurrent completions publish ordered, immutable snapshots."""

    def __init__(self, components: tuple[str, ...], on_update: Callable[[WarmupStatus], None] | None = None):
        if not components or len(set(components)) != len(components):
            raise ValueError("Warmup needs a nonempty set of distinct components")
        self._status = WarmupStatus(components)
        self._lock = threading.Lock()
        self._on_update = on_update
        if on_update:
            on_update(self._status)

    @property
    def status(self) -> WarmupStatus:
        return self._status

    def run(self, component: str, operation: Callable[[], T]) -> T:
        if component not in self._status.components:
            raise ValueError(f"Unknown warmup component: {component}")
        try:
            result = operation()
        except BaseException:
            self._report(component, False)
            raise
        self._report(component, True)
        return result

    def _report(self, component: str, success: bool) -> None:
        with self._lock:
            current = self._status
            ready, failed = set(current.ready), set(current.failed)
            if success:
                ready.add(component)
                failed.discard(component)
            elif component not in ready:
                failed.add(component)
            self._status = WarmupStatus(
                current.components,
                tuple(name for name in current.components if name in ready),
                tuple(name for name in current.components if name in failed),
            )
            if self._on_update and self._status != current:
                # Deliver callbacks under the same lock so fast parallel loads cannot
                # publish 100% before a slower callback publishes an older percentage.
                self._on_update(self._status)


def warm_voice(
    progress: WarmupProgress,
    ears: Callable[[], object],
    voice: Callable[[], object],
    listener: Callable[[], object] | None = None,
) -> BaseException | None:
    """Load concurrently, joining every worker before returning or raising.

    Ears and voice are required. A listener failure is returned so the caller can
    disable interruption and continue with a warning, as it did before progress.
    """
    errors: dict[str, BaseException] = {}

    def warm(component, operation):
        try:
            progress.run(component, operation)
        except BaseException as error:
            errors[component] = error

    loads = [threading.Thread(target=warm, args=("voice", voice), daemon=True)]
    if listener is not None:
        loads.append(threading.Thread(target=warm, args=("listener", listener), daemon=True))
    for thread in loads:
        thread.start()
    try:
        progress.run("ears", ears)
    finally:
        for thread in loads:
            thread.join()
    if "voice" in errors:
        raise errors["voice"]
    return errors.get("listener")
