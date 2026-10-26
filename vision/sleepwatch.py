"""Noticing that this computer slept, so the apps can say why agents and replies paused.

A suspended machine stops the monotonic clock (CLOCK_MONOTONIC on Linux, mach_absolute_time on macOS)
while the wall clock carries on, so a step in wall-clock time that the monotonic clock didn't see is
time spent asleep. `vision serve` ticks this every few seconds (Hub._watch_sleep).
"""
from __future__ import annotations

import time
from typing import Callable


class SleepWatch:
    GAP = 60.0          # a smaller step is the clock being set or a slow tick, not a suspend
    KEEP = 24 * 3600.0  # how far back the apps are told about

    def __init__(self, wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        self._wall, self._mono = wall, mono
        self._last = (wall(), mono())
        self.gaps: list[tuple[float, float]] = []

    def tick(self) -> tuple[float, float] | None:
        """(asleep from, woke at) as wall-clock times when the computer slept since the last tick, else None."""
        wall, mono = self._wall(), self._mono()
        last_wall, last_mono = self._last
        self._last = (wall, mono)
        awake = mono - last_mono
        if (wall - last_wall) - awake < self.GAP:
            return None
        gap = (last_wall + awake, wall)
        self.gaps = [g for g in self.gaps if g[1] > wall - self.KEEP] + [gap]
        return gap

    def recent(self) -> list[dict]:
        """The sleeps of the last day, oldest first, as the hello frame carries them."""
        now = self._wall()
        return [{"from": round(a, 1), "to": round(b, 1)} for a, b in self.gaps if b > now - self.KEEP]
