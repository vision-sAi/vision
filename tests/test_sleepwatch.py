import unittest

from vision.sleepwatch import SleepWatch


class Clocks:
    """A wall clock and a monotonic clock to move by hand: asleep, only the wall clock moves."""

    def __init__(self):
        self.wall, self.mono = 1_000_000.0, 50.0

    def awake(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def asleep(self, seconds):
        self.wall += seconds


class SleepWatchTests(unittest.TestCase):
    def watch(self):
        c = Clocks()
        return c, SleepWatch(wall=lambda: c.wall, mono=lambda: c.mono)

    def test_ticks_while_awake_see_no_sleep(self):
        c, w = self.watch()
        for _ in range(5):
            c.awake(10)
            self.assertIsNone(w.tick())
        self.assertEqual(w.recent(), [])

    def test_a_suspend_is_reported_from_when_it_started_to_waking(self):
        # Ticked as it wakes; awake time between ticks counts as before the sleep, so the start is good
        # to one tick (SLEEP_POLL) either way.
        c, w = self.watch()
        c.awake(4)
        went = c.wall
        c.asleep(3600)
        self.assertEqual(w.tick(), (went, went + 3600))
        self.assertEqual(w.recent(), [{"from": round(went, 1), "to": round(went + 3600, 1)}])

    def test_a_small_clock_step_is_not_a_sleep(self):
        c, w = self.watch()
        c.asleep(30)  # NTP setting the clock, or a slow tick
        self.assertIsNone(w.tick())

    def test_only_the_last_day_is_kept(self):
        c, w = self.watch()
        c.asleep(600)
        w.tick()
        c.awake(25 * 3600)
        w.tick()
        c.asleep(600)
        w.tick()
        self.assertEqual(len(w.recent()), 1)
        self.assertEqual(len(w.gaps), 1)


class HelloTests(unittest.TestCase):
    def test_hello_lists_recent_sleeps_and_copes_without_a_watch(self):
        from types import SimpleNamespace

        from vision import server

        hub = server.Hub.__new__(server.Hub)
        hub.chats, hub.projects = {}, SimpleNamespace(list=lambda: [])
        hub.cfg = SimpleNamespace(voice=SimpleNamespace(enabled=True))
        self.assertEqual(hub.hello()["asleep"], [])
        c = Clocks()
        hub.sleep = SleepWatch(wall=lambda: c.wall, mono=lambda: c.mono)
        c.asleep(900)
        hub.sleep.tick()
        self.assertEqual(len(hub.hello()["asleep"]), 1)


if __name__ == "__main__":
    unittest.main()
