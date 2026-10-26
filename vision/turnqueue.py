"""The terminal's queue of messages waiting for their turn.

One list, read by the turn runner (vision/cli.py drain_turns: the next message goes when the reply
ends) and by the queued strip over the input box (vision/ui.py), where ↑ picks a message to edit or
remove. While a message is being picked or edited the queue is held: nothing is sent, so an
edited message goes back in its own place rather than behind the next one.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class QueuedTurn:
    text: str
    speak: bool = False   # the phone asked for this reply spoken
    follow: bool = False  # a turn the phone already started on the joined chat (see run_turn)
    voice: bool = False
    talk: bool = False
    shown: bool = False   # sent behind a running reply: it sits in the queued strip until its turn
    phone: bool = False   # sent from the phone: its reply is spoken there, never on this machine
    compare: tuple = ()   # a /compare: the picks (vision.compare.Pick) the text goes to instead of the chat's brain
    id: int = field(default_factory=lambda: next(_ids))


class TurnQueue:
    def __init__(self):
        self._items: list[QueuedTurn] = []
        self._lock = threading.Lock()
        self._free = threading.Event()
        self._free.set()

    def put(self, item: QueuedTurn) -> QueuedTurn:
        with self._lock:
            self._items.append(item)
        return item

    def insert(self, index: int, item: QueuedTurn) -> None:
        """Back where it was among the shown messages (an edited one), or at the end."""
        with self._lock:
            shown = [i for i, it in enumerate(self._items) if it.shown]
            at = shown[index] if 0 <= index < len(shown) else len(self._items)
            self._items.insert(at, item)

    def pop(self) -> QueuedTurn | None:
        with self._lock:
            return self._items.pop(0) if self._items else None

    def pop_if_free(self) -> tuple[bool, QueuedTurn | None]:
        """Take the next turn only if editing has not claimed the queue."""
        with self._lock:
            if not self._free.is_set():
                return False, None
            return True, self._items.pop(0) if self._items else None

    def take(self, qid: int) -> QueuedTurn | None:
        """Out of the queue (to edit, remove or send now); None if it has already gone."""
        with self._lock:
            for i, it in enumerate(self._items):
                if it.id == qid:
                    return self._items.pop(i)
        return None

    def take_where(self, pred, first_only: bool = False) -> list[QueuedTurn]:
        with self._lock:
            taken = []
            for it in list(self._items):
                if pred(it) and not (first_only and taken):
                    taken.append(it)
                    self._items.remove(it)
            return taken

    def shown(self) -> list[QueuedTurn]:
        with self._lock:
            return [it for it in self._items if it.shown]

    def has_text(self, text: str) -> bool:
        with self._lock:
            return any(it.text == text for it in self._items)

    def empty(self) -> bool:
        with self._lock:
            return not self._items

    # -- hold: picking or editing in the strip
    def hold(self) -> None:
        with self._lock:
            self._free.clear()

    def release(self) -> None:
        with self._lock:
            self._free.set()

    @property
    def held(self) -> bool:
        return not self._free.is_set()

    def wait_free(self, timeout: float) -> bool:
        return self._free.wait(timeout)
