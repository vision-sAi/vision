from __future__ import annotations

import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vision import selfupdate

NEW = "a" * 40
REPO = Path(__file__).resolve().parent.parent


def git(answers: dict):
    """A fake `_git`: the first argument picks the answer (returncode, stdout), and ls-remote must ask for
    the branch checked out."""
    def run(*args, **_):
        if args[0] == "ls-remote":
            assert args[2] == "refs/heads/master", args
        code, out = answers[args[0]]
        return SimpleNamespace(returncode=code, stdout=out)
    return run


ON_MASTER = {"symbolic-ref": (0, "master\n")}


class BehindTests(unittest.TestCase):
    def behind(self, answers, checkout=True):
        with patch.object(selfupdate, "is_checkout", return_value=checkout), patch.object(selfupdate, "_git", git(answers)):
            return selfupdate.behind()

    def test_a_remote_commit_this_checkout_carries_means_up_to_date(self):
        self.assertIs(self.behind({**ON_MASTER, "ls-remote": (0, f"{NEW}\trefs/heads/master\n"), "merge-base": (0, "")}), False)

    def test_a_remote_commit_it_does_not_carry_means_an_update(self):
        answers = {**ON_MASTER, "ls-remote": (0, f"{NEW}\trefs/heads/master\n")}
        self.assertIs(self.behind({**answers, "merge-base": (1, "")}), True)
        # a commit it has never seen: git fails outright, which is the same answer
        self.assertIs(self.behind({**answers, "merge-base": (128, "")}), True)

    def test_a_remote_that_cannot_be_asked_or_lacks_the_branch_is_unknown(self):
        self.assertIsNone(self.behind({**ON_MASTER, "ls-remote": (128, "")}))
        self.assertIsNone(self.behind({**ON_MASTER, "ls-remote": (0, "")}))

    def test_an_installed_vision_whose_branch_is_gone_from_the_remote_is_out_of_date(self):
        with patch.object(selfupdate, "installed", return_value=True):
            self.assertIs(self.behind({**ON_MASTER, "ls-remote": (0, "")}), True)
            self.assertIsNone(self.behind({**ON_MASTER, "ls-remote": (128, "")}))  # offline is still unknown

    def test_a_detached_head_is_unknown(self):
        self.assertIsNone(self.behind({"symbolic-ref": (1, "")}))

    def test_a_vision_that_is_not_a_git_checkout_is_unknown(self):
        self.assertIsNone(self.behind({}, checkout=False))


class CheckTests(unittest.TestCase):
    def setUp(self):
        selfupdate.forget()
        self.addCleanup(selfupdate.forget)

    def test_the_answer_is_cached(self):
        self.assertIsNone(selfupdate.last())
        with patch.object(selfupdate, "behind", return_value=True) as behind:
            first = selfupdate.check()
            self.assertIs(selfupdate.check(), first)
            behind.assert_called_once()
        self.assertIs(selfupdate.last(), first)
        self.assertIn("Vision update available", first.status())

    def test_trouble_asking_is_unknown_and_says_nothing(self):
        with patch.object(selfupdate, "behind", side_effect=OSError("no git")):
            check = selfupdate.check()
        self.assertIsNone(check.available)
        self.assertEqual(check.status(), "")

    def test_a_stale_answer_stands_while_a_fresh_look_runs(self):
        selfupdate._check = selfupdate.Check(time.time() - selfupdate.CHECK_TTL - 1, True)
        answered = threading.Event()
        with patch.object(selfupdate, "behind", return_value=False):
            stale = selfupdate.peek_or_ask(on_answer=lambda _: answered.set())
            self.assertIs(stale.available, True)
            self.assertTrue(answered.wait(5))
        self.assertIs(selfupdate.last().available, False)

    def test_how_to_update_is_upgrade_for_an_installed_vision_and_a_pull_there_for_a_checkout(self):
        with patch.object(selfupdate, "ROOT", Path("/nowhere/vision/app")):
            # a working checkout, not the installed one
            self.assertEqual(selfupdate.how(), "git -C /nowhere/vision/app pull && vision restart")
        with patch.object(selfupdate, "installed", return_value=True):
            self.assertEqual(selfupdate.how(), "vision upgrade")


class FollowDefaultTests(unittest.TestCase):
    """A checkout whose branch the remote has dropped, moved onto the remote's main one."""

    def run_git(self, answers: dict):
        calls = []

        def run(*args, **_):
            calls.append(args)
            code, out = answers.get(args[0], (0, ""))
            return SimpleNamespace(returncode=code, stdout=out, stderr="git said no" if code else "")
        return calls, patch.object(selfupdate, "_git", run)

    def test_a_branch_the_remote_dropped_is_orphaned_and_one_it_has_or_cannot_say_is_not(self):
        for listing, orphan in (((0, ""), "old"), ((0, f"{NEW}\trefs/heads/old\n"), None), ((128, ""), None)):
            _, git_ = self.run_git({"symbolic-ref": (0, "old\n"), "ls-remote": listing})
            with git_:
                self.assertEqual(selfupdate.orphaned(), orphan)

    def test_it_moves_to_the_remotes_main_branch_and_fetches_only_that(self):
        calls, git_ = self.run_git({"ls-remote": (0, f"ref: refs/heads/main\tHEAD\n{NEW}\tHEAD\n")})
        with git_:
            self.assertEqual(selfupdate.follow_default(), "main")
        self.assertEqual(calls[1:], [("remote", "set-branches", "origin", "main"), ("fetch", "--quiet", "origin"),
                                     ("checkout", "--quiet", "-B", "main", "origin/main")])

    def test_trouble_is_said_in_gits_words(self):
        _, git_ = self.run_git({"ls-remote": (128, "")})
        with git_, self.assertRaisesRegex(RuntimeError, "main one"):
            selfupdate.follow_default()
        _, git_ = self.run_git({"ls-remote": (0, "ref: refs/heads/main\tHEAD\n"), "checkout": (1, "")})
        with git_, self.assertRaisesRegex(RuntimeError, "git said no"):
            selfupdate.follow_default()


class UpgradeTests(unittest.TestCase):
    def run_upgrade(self, *, installed=True, code=0, serving=True, orphan=None, follow="master"):
        from typer.testing import CliRunner

        from vision import cli

        moved = {"side_effect": follow} if isinstance(follow, Exception) else {"return_value": follow}
        with patch.object(selfupdate, "installed", return_value=installed), \
                patch.object(selfupdate, "orphaned", return_value=orphan), \
                patch.object(selfupdate, "follow_default", **moved) as self.followed, \
                patch.object(selfupdate, "forget") as forget, \
                patch("vision.cli.subprocess.run", return_value=SimpleNamespace(returncode=code)) as run, \
                patch("vision.remote.running_server", return_value={"url": "x"} if serving else None), \
                patch("vision.cli.restart") as restart:
            result = CliRunner().invoke(cli.app, ["upgrade"])
        return result, run, forget, restart

    def test_it_runs_the_installer_then_restarts_the_server(self):
        result, run, forget, restart = self.run_upgrade()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.args[0][0], "sh")
        forget.assert_called_once()
        restart.assert_called_once()

    def test_with_no_server_running_it_only_updates(self):
        result, _, _, restart = self.run_upgrade(serving=False)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("nothing to restart", result.output)
        restart.assert_not_called()

    def test_a_branch_gone_from_the_remote_is_left_for_the_main_one_before_installing(self):
        result, run, _, restart = self.run_upgrade(orphan="out-of-date")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("now following `master`", " ".join(result.output.split()))
        self.followed.assert_called_once()
        run.assert_called_once()
        restart.assert_called_once()

        result, run, _, restart = self.run_upgrade()  # a branch the remote still has stays
        self.followed.assert_not_called()

        result, run, _, restart = self.run_upgrade(orphan="out-of-date", follow=RuntimeError("local changes"))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("local changes", result.output)
        run.assert_not_called()
        restart.assert_not_called()

    def test_a_failed_installer_restarts_nothing(self):
        result, _, _, restart = self.run_upgrade(code=3)
        self.assertEqual(result.exit_code, 3)
        restart.assert_not_called()

    def test_a_working_checkout_is_told_to_pull_and_the_installer_is_not_run(self):
        result, run, _, restart = self.run_upgrade(installed=False)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("git pull", " ".join(result.output.split()))
        run.assert_not_called()
        restart.assert_not_called()


class WatchTests(unittest.TestCase):
    def setUp(self):
        selfupdate.forget()
        self.addCleanup(selfupdate.forget)

    def test_watch_says_only_when_the_answer_changes(self):
        answers = iter([None, False, True, True, False])
        said, done = [], threading.Event()

        def behind():
            try:
                return next(answers)
            except StopIteration:
                done.set()
                time.sleep(60)

        with patch.object(selfupdate, "behind", side_effect=behind):
            selfupdate.watch(lambda c: said.append(c.available), every=0, retry=0)
            self.assertTrue(done.wait(5))
        # unknown and "none" at first say nothing; then the update, and its going
        self.assertEqual(said, [True, False])

    def test_a_nudge_looks_now_but_not_twice_in_a_minute(self):
        # Other tests' watch threads live on in the suite: count only this one's looks.
        callers = []
        mine = lambda: [t for t in callers if t is thread]  # noqa: E731
        with patch.object(selfupdate, "behind", side_effect=lambda: callers.append(threading.current_thread()) or False):
            thread = selfupdate.watch(lambda c: None, every=3600, retry=3600)
            deadline = time.time() + 5
            while not mine() and time.time() < deadline:
                time.sleep(0.01)
            selfupdate.nudge()  # it looked a moment ago: nothing
            time.sleep(0.2)
            self.assertEqual(len(mine()), 1)
            with patch.object(selfupdate, "_check", selfupdate.Check(time.time() - selfupdate.NUDGE_GAP, False)):
                selfupdate.nudge()
                deadline = time.time() + 5
                while len(mine()) < 2 and time.time() < deadline:
                    time.sleep(0.01)
            self.assertEqual(len(mine()), 2)


class HelloTests(unittest.TestCase):
    """The phone hears the compatibility number, and about an update only from `vision serve`."""

    def hub(self):
        from vision import server

        hub = server.Hub.__new__(server.Hub)
        hub.chats, hub.posted, hub.logged = {}, [], []
        hub.projects = SimpleNamespace(list=lambda: [])  # hello lists the projects too
        hub.cfg = SimpleNamespace(voice=SimpleNamespace(enabled=True))  # …and the voice switch
        hub.post = hub.posted.append
        hub.log = hub.logged.append
        return hub

    def test_hello_carries_the_protocol_number_and_no_update_by_default(self):
        from vision import server

        hello = self.hub().hello()
        self.assertEqual(hello["protocol"], server.PROTOCOL)
        self.assertIsNone(hello["update"])

    def test_vision_serve_tells_the_phones_when_an_update_comes_and_goes(self):
        hub = self.hub()
        with patch.object(selfupdate, "watch") as watch, patch.object(selfupdate, "how", return_value="git pull"):
            hub.watch_updates()
            changed = watch.call_args.args[0]
            changed(selfupdate.Check(0, True))
            self.assertEqual(hub.hello()["update"], "git pull")
            changed(selfupdate.Check(0, False))
        self.assertIsNone(hub.hello()["update"])
        self.assertEqual(hub.posted, [{"type": "update", "update": "git pull"}, {"type": "update", "update": None}])
        self.assertEqual(len(hub.logged), 1)


class ProtocolTests(unittest.TestCase):
    """The number lives in three places, raised together by hand: this catches one left behind."""

    def test_the_server_app_and_fake_server_agree(self):
        from vision import server

        swift = REPO / "ios/VisionRemote/VisionRemote/VisionClient.swift"
        if not swift.exists():
            self.skipTest("no ios/ in this checkout")
        app = re.search(r"static let protocolVersion = (\d+)", swift.read_text())
        fake_n = re.search(r"^PROTOCOL = (\d+)", (REPO / "ios/fake_server.py").read_text(), re.M)
        self.assertEqual(int(app.group(1)), server.PROTOCOL, "VisionClient.protocolVersion")
        self.assertEqual(int(fake_n.group(1)), server.PROTOCOL, "ios/fake_server.py PROTOCOL")


if __name__ == "__main__":
    unittest.main()
