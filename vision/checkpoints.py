"""Checkpoints: the working tree as it was before each brain turn, so `/diff` shows what the model
changed since and `/rewind` puts it back.

A checkpoint is a git tree object of the whole repository (tracked and untracked files alike, ignored
ones left out), written through a temporary index so the user's own index and HEAD never move. The
trees are chained into commits under one ref, `refs/vision/checkpoints`, so `git gc` keeps them
(without the ref it would drop them as unreachable after its two-week grace period). Nothing of this
shows in `git log` or `git status`; `git update-ref -d refs/vision/checkpoints` drops the lot.

Which checkpoint belongs to which conversation is kept in STATE_DIR/checkpoints.json, keyed by
provider and session id, so a resumed conversation (`vision -c`, /session) can still /diff.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass

from vision.config import STATE_DIR

REF = "refs/vision/checkpoints"
STORE = STATE_DIR / "checkpoints.json"
PER_SESSION = 50  # checkpoints remembered per conversation
SESSIONS = 100  # conversations remembered in the store
TEXT_CHARS = 72  # of the message a checkpoint was taken for, as its label
GIT_TIMEOUT = 60
# Commits under the ref need an author; the user's git identity may not be set, and these are
# Vision's anyway.
_IDENTITY = {"GIT_AUTHOR_NAME": "Vision", "GIT_AUTHOR_EMAIL": "vision@localhost",
             "GIT_COMMITTER_NAME": "Vision", "GIT_COMMITTER_EMAIL": "vision@localhost"}


@dataclass
class Checkpoint:
    tree: str  # the tree object
    root: str  # the repository's top-level directory
    at: float  # when it was taken
    text: str = ""  # the message the turn was for, trimmed

    def label(self) -> str:
        return self.text or "(no message)"


class CheckpointError(RuntimeError):
    pass


def _git(root: str, *args: str, env: dict | None = None, check: bool = True, timeout: float = GIT_TIMEOUT) -> str:
    """Run git in `root`; stdout stripped. Raises CheckpointError when it fails (and `check`)."""
    full = {**os.environ, **(env or {})}
    full.pop("GIT_DIR", None)  # the caller's environment must not point us at another repository
    full.pop("GIT_WORK_TREE", None)
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, env=full,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckpointError(f"git {args[0]}: {e}") from e
    if check and r.returncode != 0:
        raise CheckpointError(f"git {args[0]}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout.strip()


def repo_root(workdir: str) -> str | None:
    """The top-level directory of the repository `workdir` is in, None outside one (or without git)."""
    if not shutil.which("git") or not os.path.isdir(workdir):
        return None
    try:
        return _git(workdir, "rev-parse", "--show-toplevel")
    except CheckpointError:
        return None


def _write_tree(root: str) -> str:
    """The working tree as a tree object, through a temporary index. The real index is copied in
    first where it exists: its stat cache means only files that changed since are hashed, so a
    large repository costs little per turn. `add -A` then records every tracked and untracked file
    that is not ignored, and drops the deleted ones.

    The copy keeps the real index's mtime. git re-reads any file modified at or after the moment
    its index was written, since the stat cache cannot tell a same-size rewrite within that second
    apart from no change at all; a copy stamped with the time of copying would be newer than every
    such file and lose that guard, and a turn's edit could be checkpointed as the old content."""
    with tempfile.TemporaryDirectory(prefix="vision-checkpoint-") as tmp:
        index = os.path.join(tmp, "index")
        try:
            real = _git(root, "rev-parse", "--git-path", "index")
            real = real if os.path.isabs(real) else os.path.join(root, real)
            if os.path.isfile(real):
                shutil.copy2(real, index)  # copy2: the mtime too, see above
        except (CheckpointError, OSError):
            pass  # a repository without an index yet: start from nothing
        env = {"GIT_INDEX_FILE": index}
        _git(root, "add", "-A", "--", ".", env=env)
        return _git(root, "write-tree", env=env)


def _keep(root: str, tree: str) -> None:
    """Chain the tree into a commit under REF so gc keeps it. An unchanged tree adds nothing."""
    try:
        prev = _git(root, "rev-parse", "-q", "--verify", REF, check=False)
        if prev and _git(root, "rev-parse", f"{prev}^{{tree}}", check=False) == tree:
            return
        args = ["commit-tree", tree, "-m", "vision checkpoint"] + (["-p", prev] if prev else [])
        commit = _git(root, *args, env=_IDENTITY)
        _git(root, "update-ref", REF, commit)
    except CheckpointError:
        pass  # the tree still exists; it just has the grace period rather than a ref


def snapshot(workdir: str, text: str = "") -> Checkpoint | None:
    """Record the working tree `workdir` is in; None outside git or when git fails (a checkpoint
    is a nicety that must never stop a turn)."""
    root = repo_root(workdir)
    if root is None:
        return None
    try:
        tree = _write_tree(root)
    except CheckpointError:
        return None
    _keep(root, tree)
    return Checkpoint(tree=tree, root=root, at=time.time(), text=_trim(text))


def _trim(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= TEXT_CHARS else text[: TEXT_CHARS - 1].rstrip() + "…"


def diff(cp: Checkpoint) -> tuple[str, str]:
    """(stat, patch) of the working tree now against the checkpoint; both empty when nothing changed.
    Raises CheckpointError when the repository is gone."""
    if not os.path.isdir(cp.root):
        raise CheckpointError(f"{cp.root} is gone")
    now = _write_tree(cp.root)
    if now == cp.tree:
        return "", ""
    stat = _git(cp.root, "diff", "--stat", cp.tree, now)
    patch = _git(cp.root, "diff", "--no-color", cp.tree, now)
    return stat, patch


def restore(cp: Checkpoint) -> Checkpoint:
    """Put the working tree back as the checkpoint recorded it: files written, changed or deleted
    since are undone, untracked files that appeared since are removed (ignored ones are left alone).
    The index and HEAD stay where they are. Returns a checkpoint of the tree as it was just before,
    so a rewind can itself be rewound."""
    if not os.path.isdir(cp.root):
        raise CheckpointError(f"{cp.root} is gone")
    before = snapshot(cp.root, "before /rewind")
    if before is None:
        raise CheckpointError("could not record the tree before rewinding")
    if before.tree == cp.tree:
        return before
    with tempfile.TemporaryDirectory(prefix="vision-checkpoint-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _git(cp.root, "read-tree", cp.tree, env=env)
        _git(cp.root, "checkout-index", "-a", "-f", env=env)  # every file of the checkpoint, over what is there
    # Files that exist now but did not then (added since the checkpoint): checkout-index does not
    # touch them, so remove them, and the directories they leave empty.
    added = _git(cp.root, "diff", "--name-only", "--diff-filter=A", "-z", cp.tree, before.tree)
    for rel in filter(None, added.split("\0")):
        path = os.path.join(cp.root, rel)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        _prune_dirs(cp.root, os.path.dirname(path))
    return before


def _prune_dirs(root: str, path: str) -> None:
    """Remove `path` and its parents while they are empty, stopping at the repository root."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    while path != root and path.startswith(root + os.sep):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


# -- the store: which checkpoints belong to which conversation ------------------------------------

def _key(provider: str, session_id: str) -> str:
    return f"{provider}:{session_id}"


def _read_store() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(provider: str, session_id: str | None) -> list[Checkpoint]:
    """The checkpoints saved for a conversation, oldest first; [] for one without any (or no id yet)."""
    if not session_id:
        return []
    rows = _read_store().get(_key(provider, session_id)) or []
    out = []
    for row in rows:
        try:
            out.append(Checkpoint(tree=str(row["tree"]), root=str(row["root"]), at=float(row["at"]), text=str(row.get("text", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save(provider: str, session_id: str | None, cps: list[Checkpoint]) -> None:
    """Keep a conversation's checkpoints (the last PER_SESSION) for when it is resumed. Quietly
    does nothing when the state directory cannot be written."""
    if not session_id or not cps:
        return
    data = _read_store()
    key = _key(provider, session_id)
    data.pop(key, None)  # re-added at the end: the store is in order of last use
    data[key] = [asdict(cp) for cp in cps[-PER_SESSION:]]
    for stale in list(data)[:-SESSIONS]:
        del data[stale]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, STORE)
    except OSError:
        pass


def pick(cps: list[Checkpoint], arg: str) -> Checkpoint | None:
    """`/diff` → the latest checkpoint, `/diff 3` → the third latest; None when there is no such one."""
    arg = (arg or "").strip()
    if not cps:
        return None
    if not arg:
        return cps[-1]
    try:
        n = int(arg)
    except ValueError:
        return None
    return cps[-n] if 1 <= n <= len(cps) else None


def rows(cps: list[Checkpoint], now: float | None = None) -> list[tuple[str, str, str]]:
    """(number, age, label) for `/diff list`, latest first: the number is what /diff and /rewind take."""
    now = time.time() if now is None else now
    out = []
    for i, cp in enumerate(reversed(cps), start=1):
        age = int(max(0, now - cp.at))
        when = f"{age}s" if age < 60 else f"{age // 60}m" if age < 3600 else f"{age // 3600}h" if age < 86400 else f"{age // 86400}d"
        out.append((str(i), f"{when} ago", cp.label()))
    return out
