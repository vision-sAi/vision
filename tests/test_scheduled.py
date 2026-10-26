import datetime as dt
import tempfile
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path

from vision.scheduled import CATCH_UP, Schedule, clean, next_run


def at(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm).timestamp()


class NextRunTest(unittest.TestCase):
    def test_daily_rolls_to_tomorrow(self):
        t = {"repeat": "daily", "time": "08:30"}
        self.assertEqual(next_run(t, at(2026, 9, 25, 9, 0)), at(2026, 9, 26, 8, 30))
        self.assertEqual(next_run(t, at(2026, 9, 25, 8, 0)), at(2026, 9, 25, 8, 30))

    def test_weekdays_skip_weekend(self):
        t = {"repeat": "weekdays", "time": "07:00"}
        # Friday 25 Sep 2026 after 7 → Monday 28th
        self.assertEqual(next_run(t, at(2026, 9, 25, 9, 0)), at(2026, 9, 28, 7, 0))

    def test_weekly(self):
        t = {"repeat": "weekly", "time": "18:00", "weekday": 6}
        self.assertEqual(next_run(t, at(2026, 9, 25, 9, 0)), at(2026, 9, 27, 18, 0))

    def test_once_in_the_past_is_none(self):
        t = {"repeat": "once", "time": "07:00", "date": "2026-09-25"}
        self.assertIsNone(next_run(t, at(2026, 9, 25, 9, 0)))

    def test_clean_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            clean({"prompt": "", "repeat": "daily", "time": "08:00"})
        with self.assertRaises(ValueError):
            clean({"prompt": "x", "repeat": "hourly", "time": "08:00"})
        with self.assertRaises(ValueError):
            clean({"prompt": "x", "repeat": "daily", "time": "25:00"})


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "scheduled.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_due_moves_on_and_persists(self):
        s = Schedule(self.path)
        task = s.upsert({"prompt": "weather", "repeat": "daily", "time": "08:00"})
        self.assertEqual(task["title"], "weather")
        when = task["next_run"]
        self.assertEqual([t["id"] for t in s.due(when + 1)], [task["id"]])
        self.assertEqual(s.due(when + 2), [])  # not twice
        again = Schedule(self.path).get(task["id"])
        self.assertGreater(again["next_run"], when)

    def test_missed_run_is_skipped(self):
        s = Schedule(self.path)
        task = s.upsert({"prompt": "x", "repeat": "daily", "time": "08:00"})
        self.assertEqual(s.due(task["next_run"] + CATCH_UP + 60), [])
        self.assertGreater(s.get(task["id"])["next_run"], task["next_run"])

    def test_once_switches_off_after_running(self):
        s = Schedule(self.path)
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        task = s.upsert({"prompt": "x", "repeat": "once", "time": "08:00", "date": tomorrow})
        self.assertEqual(len(s.due(task["next_run"])), 1)
        self.assertFalse(s.get(task["id"])["enabled"])

    def test_edit_and_delete(self):
        s = Schedule(self.path)
        task = s.upsert({"prompt": "x", "repeat": "daily", "time": "08:00"})
        s.upsert({"id": task["id"], "enabled": False})
        self.assertFalse(s.get(task["id"])["enabled"])
        self.assertIsNone(s.get(task["id"])["next_run"])
        self.assertTrue(s.delete(task["id"]))
        self.assertEqual(s.list(), [])

    def test_kind_and_chat_are_kept_through_an_edit(self):
        s = Schedule(self.path)
        task = s.upsert({"prompt": "tea", "repeat": "daily", "time": "08:00", "kind": "reminder", "chat": "c1"})
        self.assertEqual((task["kind"], task["chat"]), ("reminder", "c1"))
        self.assertEqual(s.upsert({"prompt": "x", "repeat": "daily", "time": "08:00"})["kind"], "task")
        edited = s.upsert({"id": task["id"], "time": "09:00"})  # the phone's form knows neither field
        self.assertEqual((edited["kind"], edited["chat"]), ("reminder", "c1"))
        with self.assertRaises(ValueError):
            s.upsert({"prompt": "x", "repeat": "daily", "time": "08:00", "kind": "alarm"})



class CheckinScheduleTest(unittest.TestCase):
    """A scout's check-in: tied to a project, may repeat every N minutes, and a missed
    one runs once when Vision is back instead of being skipped."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "scheduled.json"

    def test_a_checkin_needs_its_project_but_no_prompt(self):
        with self.assertRaises(ValueError):
            clean({"kind": "checkin", "repeat": "daily", "time": "08:00"})
        t = clean({"kind": "checkin", "project": "p1", "repeat": "daily", "time": "08:00"})
        self.assertEqual((t["title"], t["prompt"], t["project"]), ("Check-in", "", "p1"))

    def test_every_n_minutes_counts_from_when_it_was_set(self):
        with self.assertRaises(ValueError):
            clean({"kind": "checkin", "project": "p", "repeat": "every", "every_minutes": 5})
        t = clean({"kind": "checkin", "project": "p", "repeat": "every", "every_minutes": 120})
        self.assertAlmostEqual(t["next_run"], t["anchor"] + 7200, delta=1)
        self.assertEqual(next_run(t, t["anchor"] + 7200 * 3 + 10), t["anchor"] + 7200 * 4)
        kept = clean({"enabled": True}, t)  # an edit that leaves the interval alone keeps its rhythm
        self.assertEqual(kept["anchor"], t["anchor"])
        daily = clean({"repeat": "daily", "time": "08:00"}, t)
        self.assertNotIn("every_minutes", daily)

    def test_a_missed_checkin_runs_once_late(self):
        s = Schedule(self.path)
        task = s.upsert({"kind": "checkin", "project": "p1", "repeat": "daily", "time": "08:00"})
        late = task["next_run"] + 3 * 86400 + 60  # three days asleep
        due = s.due(late)
        self.assertEqual([(t["id"], t["missed"]) for t in due], [(task["id"], task["next_run"])])
        self.assertEqual(s.due(late + 1), [])  # once, not three times
        self.assertGreater(s.get(task["id"])["next_run"], late)
        self.assertEqual(s.for_project("p1")["id"], task["id"])
        self.assertIsNone(s.for_project("p2"))


class WhenTest(unittest.TestCase):
    def test_clock_spellings(self):
        from vision.scheduled import parse_clock

        self.assertEqual([parse_clock(t) for t in ("18:00", "6pm", "6.30 PM", "12am", "12pm", "7")],
                         ["18:00", "18:00", "18:30", "00:00", "12:00", "07:00"])
        for bad in ("25:00", "13pm", "soon", ""):
            with self.assertRaises(ValueError):
                parse_clock(bad)

    def test_delay_rounds_up_to_the_minute(self):
        from vision.scheduled import when

        now = dt.datetime(2026, 10, 5, 23, 50, 30).timestamp()
        self.assertEqual(when("20m", now=now), ("2026-10-06", "00:11"))
        self.assertEqual(when("1h30m", now=at(2026, 10, 5, 9, 0)), ("2026-10-05", "10:30"))
        self.assertEqual(when("2 days", now=at(2026, 10, 5, 9, 0)), ("2026-10-07", "09:00"))
        with self.assertRaises(ValueError):
            when("a while", now=now)

    def test_clock_means_its_next_occurrence_unless_a_day_is_named(self):
        from vision.scheduled import when

        now = at(2026, 10, 5, 15, 0)
        self.assertEqual(when(clock="6pm", now=now), ("2026-10-05", "18:00"))
        self.assertEqual(when(clock="9:00", now=now), ("2026-10-06", "09:00"))
        self.assertEqual(when(clock="9:00", day="tomorrow", now=now), ("2026-10-06", "09:00"))
        self.assertEqual(when(clock="9:00", day="2026-10-20", now=now), ("2026-10-20", "09:00"))


class FiringTest(unittest.TestCase):
    """Hub.run_task: a task goes back to the chat it was set from; a reminder runs no model."""

    def _hub(self, d):
        from unittest.mock import Mock

        from vision import server
        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, Mock(session_id=None), "tok", log=lambda *_: None, follow_defaults=False,
                         open_initial=False, journal_enabled=False)
        hub.schedule = Schedule(Path(d) / "s.json")
        hub.pushed = []
        hub.push = lambda title, body, chat="": hub.pushed.append((title, body, chat))
        return hub

    def test_a_task_runs_in_its_chat_and_pushes_that_reply(self):
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as d:
            hub = self._hub(d)
            chat = Mock(id="c1", closed=False, busy=True, _pending=[])
            hub.chats = {"c1": chat}
            task = hub.schedule.upsert({"prompt": "check the build", "repeat": "daily", "time": "08:00", "chat": "c1"})
            self.assertIs(hub.run_task(task), chat)
            chat.queue.assert_called_once_with(f"[Scheduled: {task['title']}] check the build", speak=False, voice=False)
            self.assertEqual(hub.schedule.get(task["id"])["last_chat"], "c1")
            hub.post({"type": "done", "chat": "c1", "text": "the reply already under way"})
            self.assertEqual(hub.pushed, [])
            hub.post({"type": "done", "chat": "c1", "text": "build is green"})
            hub.post({"type": "done", "chat": "c1", "text": "a later turn"})
            self.assertEqual(hub.pushed, [(task["title"], "build is green", "c1")])
            # Its chat has gone: a fresh one, as a task from the phone's form gets.
            hub.chats = {}
            fresh = Mock(id="c2", journal=None, title="t")
            with patch.object(hub, "open_chat", return_value=fresh) as opened:
                self.assertIs(hub.run_task(task, brain=Mock(), cfg=hub.cfg), fresh)
            opened.assert_called_once()
            fresh.queue.assert_called_once_with("check the build", speak=False, voice=False)

    def test_a_reminder_notes_its_chat_and_pushes_without_a_turn(self):
        from unittest.mock import Mock, patch

        from vision import server

        with tempfile.TemporaryDirectory() as d, patch.object(server.shutil, "which", lambda _: None):
            hub = self._hub(d)
            chat = Mock(id="c1", closed=False)
            hub.chats = {"c1": chat}
            task = hub.schedule.upsert({"prompt": "tea", "repeat": "daily", "time": "08:00", "chat": "c1", "kind": "reminder"})
            hub.run_task(task)
            chat.remind.assert_called_once_with("Reminder: tea")
            chat.queue.assert_not_called()
            self.assertEqual(hub.pushed, [("Reminder", "tea", "c1")])
            hub.chats = {}
            self.assertIsNone(hub.run_task(task))  # no chat is opened for a reminder
            self.assertEqual(hub.pushed[-1], ("Reminder", "tea", ""))


if __name__ == "__main__":
    unittest.main()


class EndpointTest(unittest.TestCase):
    def test_library_thumb_and_scheduled_routes(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi.testclient import TestClient
        from PIL import Image

        from vision import library, server

        with tempfile.TemporaryDirectory() as d:
            uploads = Path(d) / "uploads"
            uploads.mkdir()
            Image.new("RGB", (800, 600), (200, 30, 30)).save(uploads / "20260925-101010-abcdef.jpg")
            (uploads / "20260925-101011-abcdef-notes.txt").write_text("hi")
            posted = []
            hub = SimpleNamespace(authorized=lambda t: t == "t", log=lambda _: None, post=posted.append,
                                  schedule=Schedule(Path(d) / "s.json"))
            client = TestClient(server.create_app(hub))
            auth = {"Authorization": "Bearer t"}
            with patch.object(server, "UPLOAD_DIR", uploads), patch.object(library, "THUMB_DIR", Path(d) / "thumbs"), \
                    patch.object(library, "_session_logs", lambda: []):
                items = client.get("/library", headers=auth).json()
                self.assertEqual({(i["kind"], i["name"]) for i in items},
                                 {("image", "20260925-101010-abcdef.jpg"), ("file", "notes.txt")})
                pic = next(i for i in items if i["kind"] == "image")["path"]
                r = client.get("/thumb", params={"path": pic, "size": 100}, headers=auth)
                self.assertEqual(r.status_code, 200)
                import io
                self.assertEqual(max(Image.open(io.BytesIO(r.content)).size), 100)
                self.assertEqual(client.get("/thumb", params={"path": "/etc/hostname"}, headers=auth).status_code, 404)

            r = client.post("/scheduled", json={"prompt": "news", "repeat": "daily", "time": "07:15"}, headers=auth)
            self.assertEqual(r.status_code, 200)
            tid = r.json()["id"]
            self.assertEqual(client.post("/scheduled", json={"prompt": "x", "repeat": "daily", "time": "7"},
                                         headers=auth).status_code, 400)
            self.assertEqual([t["id"] for t in client.get("/scheduled", headers=auth).json()], [tid])
            self.assertEqual(client.delete(f"/scheduled/{tid}", headers=auth).status_code, 200)
            self.assertEqual(client.get("/scheduled", headers=auth).json(), [])
            self.assertIn({"type": "scheduled"}, posted)
