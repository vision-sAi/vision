"""Whether this Vision is behind the one it was installed from.

Vision runs from a git checkout (install.sh makes one, and a working copy is one), so "is there an
update" is one question to its remote: is the newest commit of the branch checked out here part of
what runs here? Nothing is downloaded (`git ls-remote`), a checkout that is ahead (work in progress on
top of the newest commit) is up to date, and one with no answer says nothing: installed from a tarball
with no git, a detached HEAD, a working copy's branch the remote doesn't have, offline, or a remote that
wants a password. An installed Vision whose branch is gone from the remote (merged and deleted) is out
of date: `vision upgrade` moves it to the remote's default branch.

The terminal says so once a session and `vision serve` tells the phone (hello "update", then an
"update" event whenever the answer changes). The answer is cached, as the provider CLIs' checks are
(clis.check_update), so nothing blocks on the network."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CHECK_TTL = 3600  # one look at the remote is good for this long (it is one small `git ls-remote`)
RETRY = 30 * 60  # after a look that couldn't tell (offline at boot, say), `watch` tries again this soon
WATCH_EVERY = 10 * 60  # how often `vision serve` looks on its own
NUDGE_GAP = 60  # an app coming into view makes `watch` look now, unless it looked this recently
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Check:
    at: float
    available: bool | None  # None: couldn't tell

    def status(self) -> str:
        """The line for the chat; "" when there is nothing to say."""
        return f"Vision update available · run {how()}" if self.available else ""


def is_checkout() -> bool:
    return (ROOT / ".git").exists()


def installed() -> bool:
    """Whether this is the copy install.sh made (and can update), not a working checkout."""
    from vision.config import DATA_DIR

    return ROOT == DATA_DIR / "app" and (ROOT / "install.sh").exists()


def how() -> str:
    """The one command that updates this Vision and restarts `vision serve` on it: `vision upgrade` for
    an installed Vision, a pull where it lives for a working checkout."""
    if installed():
        return "vision upgrade"
    return f"git -C {str(ROOT).replace(str(Path.home()), '~', 1)} pull && vision restart"


def _git(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    # Never a password prompt in the middle of a chat: a remote that wants one is an unknown.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=8"}
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def branch() -> str | None:
    """The branch checked out here; None on a detached HEAD."""
    out = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def remote_head(name: str) -> str | None:
    """The newest commit of `name` on the remote; "" when the remote answers and has no such branch, None
    when it can't be asked."""
    out = _git("ls-remote", "origin", f"refs/heads/{name}")
    if out.returncode != 0:
        return None
    sha = out.stdout.split()[0] if out.stdout.split() else ""
    return sha if len(sha) >= 40 else ""


def orphaned() -> str | None:
    """The branch checked out here when the remote answers and no longer has it; None otherwise."""
    name = branch()
    return name if name and remote_head(name) == "" else None


def default_branch() -> str | None:
    """The branch the remote's HEAD points at, or None when it can't be asked."""
    out = _git("ls-remote", "--symref", "origin", "HEAD")
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        if line.startswith("ref: refs/heads/"):
            return line.split()[1].removeprefix("refs/heads/")
    return None


def follow_default() -> str:
    """Move this checkout onto the remote's default branch and fetch only that one from now on (a branch
    the remote dropped fails every later fetch), returning its name. The old branch stays here, with
    whatever was committed on it. RuntimeError, in git's words, when it can't be done."""
    name = default_branch()
    if not name:
        raise RuntimeError("the remote didn't say which branch is its main one")
    for args in (("remote", "set-branches", "origin", name), ("fetch", "--quiet", "origin"),
                 ("checkout", "--quiet", "-B", name, f"origin/{name}")):
        out = _git(*args, timeout=120)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "").strip() or f"git {args[0]} failed")
    return name


def behind() -> bool | None:
    """True when the remote's copy of this branch has a commit this checkout does not carry; None when
    that can't be told. Following the branch checked out, not the remote's default, keeps a working
    copy on a feature branch quiet when master moves, and an install of another branch honest."""
    if not is_checkout():
        return None
    name = branch()
    sha = remote_head(name) if name else None
    if sha is None:
        return None
    if not sha:
        # A working copy's branch may simply not be pushed. An installed Vision's was there to install
        # from, so it has been dropped since, and nothing will ever arrive on it again.
        return True if installed() else None
    # Part of what runs here (the same commit, or work on top of it): up to date. A commit this
    # checkout has never seen fails the same way as one it has but is not built on.
    return _git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0


_check: Check | None = None
_lock = threading.Lock()


def check(*, max_age: float = CHECK_TTL) -> Check:
    """Ask the remote, at most once per `max_age` seconds. Never raises: trouble is "couldn't tell"."""
    global _check
    with _lock:
        cached = _check
    if cached and time.time() - cached.at < max_age:
        return cached
    try:
        available = behind()
    except Exception:  # noqa: BLE001 - no git, a hung remote, a broken checkout: all mean "unknown"
        available = None
    fresh = Check(time.time(), available)
    with _lock:
        _check = fresh
    return fresh


def last() -> Check | None:
    """The latest answer, however old; never touches the network."""
    with _lock:
        return _check


def forget() -> None:
    global _check
    with _lock:
        _check = None


_asking = threading.Event()


def peek_or_ask(on_answer=None) -> Check | None:
    """The latest answer. With none, or one past CHECK_TTL, a look starts in the background (the stale
    answer stands meanwhile) and `on_answer(check)` is called once it is in."""
    cached = last()
    if (cached is None or time.time() - cached.at >= CHECK_TTL) and not _asking.is_set():
        _asking.set()

        def go():
            try:
                fresh = check()
            finally:
                _asking.clear()
            if on_answer:
                on_answer(fresh)

        threading.Thread(target=go, daemon=True, name="selfupdate").start()
    return cached


_wake = threading.Event()


def nudge() -> None:
    """Someone is looking (an app connected or came to the front): `watch` looks now, not at its next
    turn, so a merge shows within seconds of opening the app. At most once per NUDGE_GAP."""
    cached = last()
    if cached is None or time.time() - cached.at >= NUDGE_GAP:
        _wake.set()


def watch(on_change, *, every: float = WATCH_EVERY, retry: float = RETRY) -> threading.Thread:
    """For `vision serve`: look now and every `every` seconds after (`retry` after a look that couldn't
    tell), or sooner when nudged, calling `on_change(check)` whenever the answer moves between update
    and none."""
    def loop():
        said = False
        while True:
            fresh = check(max_age=0)
            if fresh.available is not None and fresh.available != said:
                said = fresh.available
                on_change(fresh)
            _wake.wait(every if fresh.available is not None else retry)
            _wake.clear()

    thread = threading.Thread(target=loop, daemon=True, name="selfupdate-watch")
    thread.start()
    return thread
