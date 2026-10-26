import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vision import checkpoints


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _write(repo, rel, text):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _files(repo):
    """Every file under the repo except .git, with its content."""
    out = {}
    for base, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            p = os.path.join(base, n)
            out[os.path.relpath(p, repo)] = Path(p).read_text(encoding="utf-8")
    return out


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self.tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "Test")
        _write(self.repo, "kept.txt", "kept\n")
        _write(self.repo, "changed.txt", "one\n")
        _write(self.repo, "gone.txt", "gone\n")
        _write(self.repo, "src/deep/nested.py", "print(1)\n")
        _write(self.repo, ".gitignore", "*.log\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.head_index = _git(self.repo, "write-tree")

    def tearDown(self):
        self.tmp.cleanup()


class SnapshotTests(RepoCase):
    def test_outside_git_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(checkpoints.snapshot(d, "hi"))
        self.assertIsNone(checkpoints.snapshot("/nonexistent/dir", "hi"))

    def test_snapshot_records_untracked_and_uncommitted_but_not_ignored(self):
        _write(self.repo, "changed.txt", "two\n")
        _write(self.repo, "new.txt", "new\n")
        _write(self.repo, "debug.log", "ignored\n")
        os.remove(os.path.join(self.repo, "gone.txt"))
        cp = checkpoints.snapshot(os.path.join(self.repo, "src"), "  make   it so  ")
        self.assertIsNotNone(cp)
        self.assertEqual(cp.root, self.repo)
        self.assertEqual(cp.text, "make it so")
        listed = _git(self.repo, "ls-tree", "-r", "--name-only", cp.tree).splitlines()
        self.assertIn("new.txt", listed)
        self.assertIn("src/deep/nested.py", listed)
        self.assertNotIn("gone.txt", listed)
        self.assertNotIn("debug.log", listed)
        self.assertEqual(_git(self.repo, "show", f"{cp.tree}:changed.txt"), "two")
        # The user's own index and HEAD did not move.
        self.assertEqual(_git(self.repo, "write-tree"), self.head_index)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), _git(self.repo, "rev-parse", "main"))
        # The tree is reachable from Vision's ref, so gc keeps it.
        self.assertEqual(_git(self.repo, "rev-parse", f"{checkpoints.REF}^{{tree}}"), cp.tree)
        self.assertNotIn("vision checkpoint", _git(self.repo, "log", "--oneline"))

    def test_ref_chains_only_changed_trees(self):
        a = checkpoints.snapshot(self.repo, "a")
        b = checkpoints.snapshot(self.repo, "b")  # same tree: no new commit
        self.assertEqual(a.tree, b.tree)
        self.assertEqual(len(_git(self.repo, "rev-list", checkpoints.REF).splitlines()), 1)
        _write(self.repo, "kept.txt", "kept more\n")
        c = checkpoints.snapshot(self.repo, "c")
        self.assertNotEqual(a.tree, c.tree)
        commits = _git(self.repo, "rev-list", checkpoints.REF).splitlines()
        self.assertEqual(len(commits), 2)
        self.assertEqual(_git(self.repo, "rev-parse", f"{commits[1]}^{{tree}}"), a.tree)

    def test_edit_in_the_second_the_index_was_written_is_seen(self):
        # A file rewritten within the second its index entry was recorded, keeping its size, looks
        # unchanged to the stat cache: same mtime (to the second), same inode, same length. git's own
        # guard is the index file's mtime: entries modified at or after it are re-read. That guard
        # only holds when the temporary index keeps the real one's mtime, so pin file and index to one
        # instant, far enough back that the copy's own age cannot stand in for it.
        path = os.path.join(self.repo, "changed.txt")
        index = os.path.join(self.repo, ".git", "index")
        _git(self.repo, "config", "core.trustctime", "false")  # ctime cannot be set from here
        then = time.time_ns() - 10 * 10**9
        os.utime(path, ns=(then, then))
        _git(self.repo, "update-index", "--", "changed.txt")  # records that stat for "one"
        _write(self.repo, "changed.txt", "two\n")
        os.utime(path, ns=(then, then))
        os.utime(index, ns=(then, then))
        cp = checkpoints.snapshot(self.repo, "racy")
        self.assertEqual(_git(self.repo, "show", f"{cp.tree}:changed.txt"), "two")

    def test_fresh_repo_without_commits(self):
        with tempfile.TemporaryDirectory() as d:
            _git(d, "init", "-q")
            _write(d, "a.txt", "a\n")
            cp = checkpoints.snapshot(d, "first")
            self.assertIsNotNone(cp)
            self.assertEqual(_git(d, "ls-tree", "-r", "--name-only", cp.tree), "a.txt")


class DiffRestoreTests(RepoCase):
    def test_diff_and_restore_round_trip(self):
        start = _files(self.repo)
        cp = checkpoints.snapshot(self.repo, "before")
        self.assertEqual(checkpoints.diff(cp), ("", ""))
        # What a turn might do: edit, add (nested too), delete, and leave an ignored file around.
        _write(self.repo, "changed.txt", "two\n")
        _write(self.repo, "new.txt", "new\n")
        _write(self.repo, "pkg/sub/added.py", "x = 1\n")
        _write(self.repo, "src/deep/nested.py", "print(2)\n")
        os.remove(os.path.join(self.repo, "gone.txt"))
        _write(self.repo, "build.log", "ignored\n")
        stat, patch = checkpoints.diff(cp)
        for name in ("changed.txt", "new.txt", "pkg/sub/added.py", "gone.txt", "src/deep/nested.py"):
            self.assertIn(name, stat)
        self.assertIn("-one\n+two", patch)
        self.assertIn("5 files changed", stat)
        self.assertNotIn("build.log", stat)
        before = checkpoints.restore(cp)
        after = _files(self.repo)
        self.assertEqual(after.pop("build.log"), "ignored\n")  # ignored files are not Vision's to touch
        self.assertEqual(after, start)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pkg")))  # emptied directories go too
        self.assertEqual(checkpoints.diff(cp), ("", ""))
        # The index and HEAD were left alone...
        self.assertEqual(_git(self.repo, "write-tree"), self.head_index)
        # ...and the rewind is itself rewindable: `before` holds the state the rewind undid.
        self.assertEqual(before.text, "before /rewind")
        checkpoints.restore(before)
        self.assertEqual(_files(self.repo)["changed.txt"], "two\n")
        self.assertEqual(_files(self.repo)["pkg/sub/added.py"], "x = 1\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "gone.txt")))

    def test_restore_unchanged_tree_is_a_no_op(self):
        cp = checkpoints.snapshot(self.repo, "same")
        before = checkpoints.restore(cp)
        self.assertEqual(before.tree, cp.tree)

    def test_gone_repo_raises(self):
        cp = checkpoints.Checkpoint(tree="0" * 40, root=os.path.join(self.repo, "nope"), at=0.0)
        with self.assertRaises(checkpoints.CheckpointError):
            checkpoints.diff(cp)
        with self.assertRaises(checkpoints.CheckpointError):
            checkpoints.restore(cp)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(checkpoints, "STATE_DIR", d), mock.patch.object(checkpoints, "STORE", d / "checkpoints.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def cp(self, n):
        return checkpoints.Checkpoint(tree=f"{n:040x}", root="/repo", at=float(n), text=f"turn {n}")

    def test_save_and_load_per_session(self):
        self.assertEqual(checkpoints.load("claude", None), [])
        self.assertEqual(checkpoints.load("claude", "abc"), [])
        checkpoints.save("claude", "abc", [self.cp(1), self.cp(2)])
        checkpoints.save("codex", "abc", [self.cp(3)])
        checkpoints.save("claude", None, [self.cp(9)])  # no id yet: nothing to file it under
        self.assertEqual([c.text for c in checkpoints.load("claude", "abc")], ["turn 1", "turn 2"])
        self.assertEqual([c.text for c in checkpoints.load("codex", "abc")], ["turn 3"])
        self.assertEqual(checkpoints.load("claude", "abc")[0], self.cp(1))

    def test_caps(self):
        checkpoints.save("claude", "s", [self.cp(i) for i in range(checkpoints.PER_SESSION + 5)])
        kept = checkpoints.load("claude", "s")
        self.assertEqual(len(kept), checkpoints.PER_SESSION)
        self.assertEqual(kept[-1].text, f"turn {checkpoints.PER_SESSION + 4}")
        for i in range(checkpoints.SESSIONS + 3):
            checkpoints.save("claude", f"s{i}", [self.cp(i)])
        data = json.loads(checkpoints.STORE.read_text())
        self.assertEqual(len(data), checkpoints.SESSIONS)
        self.assertNotIn("claude:s0", data)
        self.assertIn(f"claude:s{checkpoints.SESSIONS + 2}", data)

    def test_corrupt_store_reads_as_empty(self):
        checkpoints.STORE.write_text("{not json")
        self.assertEqual(checkpoints.load("claude", "x"), [])
        checkpoints.save("claude", "x", [self.cp(1)])
        self.assertEqual(len(checkpoints.load("claude", "x")), 1)


class PickTests(unittest.TestCase):
    def test_pick_and_rows(self):
        cps = [checkpoints.Checkpoint(tree=str(i), root="/r", at=1000.0 + i, text=f"t{i}") for i in range(3)]
        self.assertIsNone(checkpoints.pick([], ""))
        self.assertEqual(checkpoints.pick(cps, "").text, "t2")
        self.assertEqual(checkpoints.pick(cps, "1").text, "t2")
        self.assertEqual(checkpoints.pick(cps, "3").text, "t0")
        self.assertIsNone(checkpoints.pick(cps, "4"))
        self.assertIsNone(checkpoints.pick(cps, "x"))
        rows = checkpoints.rows(cps, now=1002.0 + 90)
        self.assertEqual([r[0] for r in rows], ["1", "2", "3"])
        self.assertEqual(rows[0], ("1", "1m ago", "t2"))
        self.assertEqual(rows[2][1], "1m ago")
        self.assertEqual(checkpoints.rows([checkpoints.Checkpoint(tree="", root="", at=0.0)], now=5.0)[0][2], "(no message)")


class UiTests(unittest.TestCase):
    def test_diff_grid_renders_and_caps(self):
        from rich.console import Console

        from vision import ui

        patch = "\n".join(f"+line {i}" for i in range(ui.PATCH_SHOWN + 20))
        grid = ui.diff_grid("since before x:", " a.txt | 1 +", "--- a/a.txt\n+++ b/a.txt\n" + patch)
        con = Console(width=100, record=True, force_terminal=False)
        con.print(grid)
        text = con.export_text()
        self.assertIn("since before x:", text)
        self.assertIn("a.txt | 1 +", text)
        self.assertIn("+line 0", text)
        self.assertIn("… 22 more lines", text)  # the two header lines count too

    def test_config_default(self):
        from vision.config import BrainConfig, DEFAULT_CONFIG

        self.assertTrue(BrainConfig().checkpoints)
        self.assertIn("checkpoints = true", DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()
