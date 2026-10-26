"""Vision's project threads on the iPhone's Lock Screen and in its Dynamic Island: a Live Activity.

While any thread is working or waiting on you, the phone shows a card: how many are working, how many need
you, and the names of the first few with where each stands. It changes as they do, and when none is left
running it says so for a while, then goes.

It is drawn on the phone by the app's VisionActivity extension (VisionThreadsAttributes). `vision serve`
works out what it shows (`summary`) whenever a thread changes and, when that is different, tells the app
over the socket (a "threads_live" frame, so an open app starts or updates the card itself) and Apple, so the
card follows with the app shut: a push starts the card (the app hands over its push-to-start token) and later
ones update and end it (the app hands over each card's own token). Those pushes go through push.py's
connection to Apple, so they need [push] set up.

What goes through Apple (and a relay, if one is used) is the counts in the clear, which the phone's system needs to start
and light up the card, and the rows sealed like a notification (push.seal): each token comes with the key of
the phone it belongs to, and the extension unseals the names as it draws the card. A token handed over
without a key (an older app) gets the counts alone; the names never travel readable. Over the paired
connection (a "threads_live" frame) the rows go as they are.

Tokens live in ~/.local/share/vision/live_activity.json (0600):
{"start": [{token, sandbox, key?, seen}], "cards": [{token, sandbox, key?, seen}]}.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

from vision.config import DATA_DIR

TOKENS = DATA_DIR / "live_activity.json"
ATTRIBUTES_TYPE = "VisionThreadsAttributes"  # the Swift type the app's extension draws
SHOWN = ("waiting_on_you", "working", "landing", "ready_for_review")  # on the card, in this order
ROWS = 4  # names on the card; the counts cover the rest
TITLE_MAX = 60
LINGER_S = 15 * 60  # the "all done" card stays this long after the last thread stops

_lock = threading.Lock()


def summary(threads: list[dict]) -> dict:
    """What the card shows: {"rows": [{title, state}], "working", "needsYou", "ready"}, the keys of the app's
    VisionThreadsAttributes.ContentState. A thread being landed counts as working."""
    shown = [t for t in threads if t.get("state") in SHOWN]
    shown.sort(key=lambda t: (SHOWN.index(t["state"]), -(t.get("updated") or 0)))
    rows = [{"title": str(t.get("title") or "Thread")[:TITLE_MAX], "state": t["state"]} for t in shown[:ROWS]]
    count = lambda *states: sum(1 for t in threads if t.get("state") in states)  # noqa: E731
    return {"rows": rows, "working": count("working", "landing"), "needsYou": count("waiting_on_you"),
            "ready": count("ready_for_review")}


def active(state: dict | None) -> bool:
    """A card is wanted: something is working or waiting on you."""
    return bool(state) and (state["working"] > 0 or state["needsYou"] > 0)


def tokens(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or TOKENS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"start": list(data.get("start") or []), "cards": list(data.get("cards") or [])}


def _save(data: dict, path: Path | None = None) -> None:
    path = path or TOKENS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def register(body: dict, path: Path | None = None) -> None:
    """A token from the app: {kind: "start" (it may start a card) or "card" (one card's own), token (hex),
    sandbox?, key? (the phone's push key, base64, which the rows are sealed with)}. Raises ValueError."""
    kind = body.get("kind")
    if kind not in ("start", "card"):
        raise ValueError('kind must be "start" or "card"')
    token = str(body.get("token") or "").strip().lower()
    if not token or len(token) > 400 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("token must be hex")
    entry = {"token": token, "sandbox": bool(body.get("sandbox")), "seen": time.time()}
    if body.get("key"):
        try:
            raw = base64.b64decode(str(body["key"]), validate=True)
        except ValueError:
            raw = b""
        if len(raw) != 32:
            raise ValueError("key must be 32 random bytes in base64")
        entry["key"] = base64.b64encode(raw).decode()
    with _lock:
        data = tokens(path)
        key = "start" if kind == "start" else "cards"
        # One start token per phone install is all Apple gives; the newest replaces the rest from the same phone.
        data[key] = [t for t in data[key] if t["token"] != token] + [entry]
        _save(data, path)


def sealed(state: dict, key: str = "") -> dict:
    """The card's state as Apple carries it: the counts, no rows, and with the phone's `key` the rows sealed
    as `enc`, which the app's extension opens (VisionThreadsAttributes.ContentState)."""
    from vision import push

    out = {"rows": [], "working": state["working"], "needsYou": state["needsYou"], "ready": state["ready"]}
    if key and state["rows"]:
        out["enc"] = push.seal(key, state["rows"])
    return out


def payload(event: str, state: dict, now: float | None = None, key: str = "") -> dict:
    """What Apple carries for one change of the card: `event` is "start", "update" or "end"; the rows are
    sealed with `key`, the phone's, and left out without one."""
    now = time.time() if now is None else now
    aps: dict = {"timestamp": int(now), "event": event, "content-state": sealed(state, key)}
    if event == "start":
        aps["attributes-type"] = ATTRIBUTES_TYPE
        aps["attributes"] = {}
        aps["alert"] = {"title": "Vision", "body": headline(state)}
    elif event == "end":
        aps["dismissal-date"] = int(now + LINGER_S)
    elif state["needsYou"]:
        aps["alert"] = {"title": "Vision", "body": headline(state)}  # waiting on you: worth lighting the screen
    return {"aps": aps}


def headline(state: dict) -> str:
    """"2 threads working · 1 needs you", "All done · 1 ready to review"."""
    parts = []
    if state["working"]:
        parts.append(f"{state['working']} thread{'s' * (state['working'] != 1)} working")
    if state["needsYou"]:
        parts.append(f"{state['needsYou']} need{'s' * (state['needsYou'] == 1)} you")
    if not parts:
        parts.append("All done")
    if state["ready"]:
        parts.append(f"{state['ready']} ready to review")
    return " · ".join(parts)


class Feed:
    """Follows the threads for `vision serve`: `changed()` after any thread changes returns the card's new
    state when it is different (for the socket), and pushes the change to Apple in the background."""

    def __init__(self, push_cfg, path: Path | None = None, post=None, log=lambda *_: None):
        self._cfg = push_cfg  # callable → PushConfig, read when sending (the config can change under a running server)
        self._path = path
        self._post = post
        self._log = log
        self._last: dict | None = None
        self._lock = threading.Lock()

    @property
    def last(self) -> dict | None:
        return self._last

    def changed(self, threads: list[dict]) -> dict | None:
        state = summary(threads)
        with self._lock:
            if state == self._last:
                return None
            was, self._last = active(self._last), state
        now = active(state)
        if was or now:
            event = "update" if was and now else "start" if now else "end"
            threading.Thread(target=self.push, args=(event, state), daemon=True, name="live-activity").start()
        return state

    def push(self, event: str, state: dict) -> None:
        """Tell Apple; never raises (a card that misses a change catches up with the next one)."""
        from vision import push

        cfg = self._cfg()
        if not push.ready(cfg):
            return
        data = tokens(self._path)
        if event == "start" and data["cards"]:
            event = "update"  # the app started one itself, or one is still up from before a restart
        targets = data["start"] if event == "start" else data["cards"]
        gone = []
        for entry in targets:
            body = json.dumps(payload(event, state, key=entry.get("key") or ""), separators=(",", ":"))
            try:
                post = self._post or push._post
                status, reason = post(cfg, entry["token"], entry["sandbox"], body, 10, kind="liveactivity")
            except push.PushError as e:
                self._log(f"lock screen card: {e}")
                continue
            if status == 410 or reason in push.GONE:
                gone.append(entry["token"])
            elif status != 200:
                self._log(f"lock screen card: {status} {reason}".rstrip())
        if gone or event == "end":
            with _lock:
                data = tokens(self._path)
                data["start"] = [t for t in data["start"] if t["token"] not in gone]
                # An ended card's token is spent; the next card brings its own.
                data["cards"] = [] if event == "end" else [t for t in data["cards"] if t["token"] not in gone]
                _save(data, self._path)
