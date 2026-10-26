from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from prompt_toolkit.utils import get_cwidth

from vision.buddy import Buddy, HEIGHT, WIDTH
from vision.warmup import WarmupProgress, warm_voice


class WarmupProgressTests(unittest.TestCase):
    def test_counts_successful_components_once_and_holds_through_failure(self):
        updates = []
        progress = WarmupProgress(("ears", "voice", "listener"), updates.append)
        self.assertEqual(updates[0].percent, 0)
        self.assertEqual(progress.run("voice", lambda: "cached"), "cached")
        progress.run("voice", lambda: None)
        self.assertEqual(progress.status.percent, 33)
        self.assertEqual(len(updates), 2)

        def fail():
            raise RuntimeError("could not load")

        with self.assertRaisesRegex(RuntimeError, "could not load"):
            progress.run("listener", fail)
        progress.run("ears", lambda: None)
        self.assertEqual(progress.status.percent, 66)
        self.assertEqual(progress.status.failed, ("listener",))
        progress.run("listener", lambda: None)
        self.assertEqual(progress.status.percent, 100)
        self.assertEqual(progress.status.failed, ())
        self.assertEqual(updates[0].ready, ())  # snapshots are not mutated by later reports

    def test_parallel_completions_are_published_in_order(self):
        updates = []
        progress = WarmupProgress(("ears", "voice", "listener"), updates.append)
        barrier = threading.Barrier(3, timeout=2)
        threads = [threading.Thread(target=progress.run, args=(name, barrier.wait)) for name in progress.status.components]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual([status.percent for status in updates], [0, 33, 66, 100])

    def test_components_are_validated_before_work_is_started(self):
        for components in ((), ("ears", "ears")):
            with self.assertRaises(ValueError):
                WarmupProgress(components)
        calls = []
        with self.assertRaises(ValueError):
            WarmupProgress(("ears",)).run("unknown", lambda: calls.append(True))
        self.assertEqual(calls, [])


class VoiceWarmupTests(unittest.TestCase):
    def test_stalled_voice_does_not_advance_and_all_loads_run_in_parallel(self):
        voice_started, release_voice, ears_done = threading.Event(), threading.Event(), threading.Event()
        updates, results = [], []

        def report(status):
            updates.append(status)
            if "ears" in status.ready:
                ears_done.set()

        progress = WarmupProgress(("ears", "voice"), report)

        def voice():
            voice_started.set()
            if not release_voice.wait(3):
                raise TimeoutError("test failed to release voice")

        def ears():
            if not voice_started.wait(2):
                raise TimeoutError("voice did not start in parallel")

        thread = threading.Thread(target=lambda: results.append(warm_voice(progress, ears, voice)))
        thread.start()
        try:
            self.assertTrue(ears_done.wait(2))
            self.assertEqual(progress.status.percent, 50)
            self.assertTrue(thread.is_alive())
            self.assertEqual([status.percent for status in updates], [0, 50])
        finally:
            release_voice.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual([status.percent for status in updates], [0, 50, 100])

    def test_optional_listener_failure_does_not_count_as_ready(self):
        progress = WarmupProgress(("ears", "voice", "listener"))

        def listener():
            raise RuntimeError("listener failed")

        error = warm_voice(progress, lambda: None, lambda: None, listener)
        self.assertEqual(str(error), "listener failed")
        self.assertEqual(progress.status.percent, 66)
        self.assertEqual(progress.status.failed, ("listener",))

    def test_required_failures_propagate_after_other_loads_finish(self):
        for failed in ("ears", "voice"):
            with self.subTest(failed=failed):
                progress = WarmupProgress(("ears", "voice", "listener"))

                def fail():
                    raise RuntimeError(failed + " failed")

                with self.assertRaisesRegex(RuntimeError, failed + " failed"):
                    warm_voice(progress, fail if failed == "ears" else lambda: None,
                               fail if failed == "voice" else lambda: None, lambda: None)
                self.assertEqual(progress.status.percent, 66)
                self.assertEqual(progress.status.failed, (failed,))
                self.assertIn("listener", progress.status.ready)


class PipWarmupTests(unittest.TestCase):
    def setUp(self):
        self.pip = Buddy()
        self.progress = WarmupProgress(("ears", "voice", "listener"))
        self.pip.warming(self.progress)

    def render(self, now):
        with patch("vision.buddy.time.time", return_value=now):
            return self.pip.render(True)

    def test_fill_is_driven_by_completion_and_preserves_terminal_geometry(self):
        self.assertEqual(self.render(0), self.render(10000))
        initial = self.render(0)
        for name, percent in (("listener", 33), ("ears", 66), ("voice", 100)):
            self.progress.run(name, lambda: None)
            full = self.render(0)
            self.assertEqual(full, self.render(10000))
            self.assertNotEqual(full, initial)
            rows = "".join(text for _, text in full).split("\n")
            self.assertEqual(len(rows), HEIGHT)
            self.assertEqual([get_cwidth(row) for row in rows], [WIDTH] * HEIGHT)
            self.assertTrue(all(row[-1] == " " for row in rows))
            self.assertIn(f"{percent}%", "".join(text for _, text in self.pip.render_inline(True)))
        self.assertEqual(sum(style == "#a9d8bd" for style, _ in full), 7)

    def test_detached_attempt_cannot_restore_warmup_or_update_new_attempt(self):
        self.pip.rest()
        self.progress.run("ears", lambda: None)
        self.assertEqual(self.pip.resolve(False), "idle")
        self.assertNotIn("warmup", "".join(text for _, text in self.pip.render_inline(False)))
        new = WarmupProgress(("ears", "voice"))
        self.pip.warming(new)
        self.progress.run("voice", lambda: None)
        self.progress.run("listener", lambda: None)
        with patch("vision.buddy.time.time", return_value=0):
            self.assertIn("warmup 0%", "".join(text for _, text in self.pip.render_inline(True)))

    def test_other_states_detach_the_progress_display(self):
        for transition in (lambda: self.pip.chore("transcribing"), self.pip.listening,
                           self.pip.fail, lambda: self.pip.using("Bash")):
            self.pip.warming(self.progress)
            transition()
            self.assertNotIn("warmup", "".join(text for _, text in self.render(0)))
            self.assertIsNone(self.pip._warmup)
            self.assertFalse(any(style == "#a9d8bd" for style, _ in self.render(0)))


if __name__ == "__main__":
    unittest.main()
