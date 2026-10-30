import os
"""Scouts: every project's name, colour and trait (projects.suggest_scout), the
mood its buddy is shown in (projects.mood), its activity log, and how the server hands all three out."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from test_projects import _Brain

from vision import projects
from vision.projects import (ACTIVITY_KEEP, CHECKIN_PREFIX, PROUD_S, SCOUT_NAMES, STILL_WAITING_S, TRAITS, ProjectStore,
                             activity, checkin_message, checkin_of, checkins, clean, fold_checkins, log_activity, mood,
                             record_checkin, suggest_scout, thread_event, unwrap_turn)
from vision.runs import ProjectThreads


class SuggestTest(unittest.TestCase):
    def test_a_name_nobody_has_the_furthest_hue_and_the_rarest_trait(self):
        others = [{"name": "Atlas", "hue": 205, "trait": "antenna"}, {"name": "Wren", "hue": 30, "trait": "sprout"}]
        s = suggest_scout("Keep the homelab patched", others)
        self.assertIn(s["name"], SCOUT_NAMES)
        self.assertNotIn(s["name"], ("Atlas", "Wren"))
        self.assertIn(s["trait"], ("ears", "curl"))
        gap = min(min(abs(s["hue"] - h), 360 - abs(s["hue"] - h)) for h in (205, 30))
        self.assertGreaterEqual(gap, 60)
        self.assertEqual(suggest_scout("Keep the homelab patched", others), s)  # the same goal, the same scout

    def test_what_is_given_is_kept_and_names_run_out_gracefully(self):
        self.assertEqual(suggest_scout("g", [], {"name": "Bo", "hue": 7, "trait": "curl"}), {"name": "Bo", "hue": 7, "trait": "curl"})
        everyone = [{"name": n} for n in SCOUT_NAMES]
        name = suggest_scout("g", everyone)["name"]
        self.assertTrue(name.endswith(" 2") and name[:-2] in SCOUT_NAMES)

    def test_clean_checks_an_edit(self):
        p = clean({"goal": "g", "scout": {"name": "  Bo  ", "hue": 400, "trait": "ears"}})
        self.assertEqual(p["scout"], {"name": "Bo", "hue": 40, "trait": "ears"})
        self.assertEqual(clean({"scout": {"hue": 10}}, p)["scout"], {"name": "Bo", "hue": 10, "trait": "ears"})
        for bad in ({"name": ""}, {"name": "x" * 40}, {"hue": "red"}, {"trait": "wings"}, "Bo"):
            with self.assertRaises(ValueError, msg=bad):
                clean({"goal": "g", "scout": bad})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "projects"

    def test_every_project_gets_a_scout_of_its_own(self):
        s = ProjectStore(self.root)
        made = [s.upsert({"goal": f"goal {i}"}) for i in range(len(TRAITS) + 1)]
        names = [p["scout"]["name"] for p in made]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual({p["scout"]["trait"] for p in made[:len(TRAITS)]}, set(TRAITS))
        # An edit that leaves the scout out keeps it; one that renames it renames it.
        edited = s.upsert({"id": made[0]["id"], "instructions": "be brief"})
        self.assertEqual(edited["scout"], made[0]["scout"])
        self.assertEqual(s.upsert({"id": made[0]["id"], "scout": {"name": "Bo"}})["scout"]["name"], "Bo")
        self.assertEqual(ProjectStore(self.root).get(made[0]["id"])["scout"]["name"], "Bo")

    def test_projects_from_before_scouts_get_theirs_when_loaded(self):
        for pid, created in (("aaaa", 2), ("bbbb", 1)):
            (self.root / pid).mkdir(parents=True)
            (self.root / pid / "project.json").write_text(json.dumps({"id": pid, "goal": f"g {pid}", "created": created}))
        s = ProjectStore(self.root)
        a, b = s.get("aaaa")["scout"], s.get("bbbb")["scout"]
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(b["hue"], projects.SCOUT_HUES[0])  # the oldest chose first
        self.assertEqual(json.loads((self.root / "aaaa" / "project.json").read_text())["scout"], a)  # and it's saved


def _t(state, **kw):
    return {"id": kw.pop("id", state), "title": kw.pop("title", "Fix it"), "state": state, **kw}


class MoodTest(unittest.TestCase):
    NOW = 1_000_000.0

    def test_by_precedence(self):
        p = {"status": "active"}
        cases = [
            ([], "asleep"),
            ([_t("resolved", landed=self.NOW - 60)], "proud"),
            ([_t("resolved", landed=self.NOW - PROUD_S - 1)], "asleep"),
            ([_t("working", activity="inspecting the workspace")], "curious"),
            ([_t("working", activity="inspecting the workspace", id="a"), _t("working", activity="editing files", id="b")], "focused"),
            ([_t("working", activity="running tests"), _t("resolved", landed=self.NOW)], "focused"),
            ([_t("working", paused=True, activity="paused")], "asleep"),
            ([_t("ready_for_review", updated=self.NOW - 60), _t("working")], "needs_you"),
            ([_t("waiting_on_you", updated=self.NOW - STILL_WAITING_S)], "still_waiting"),
            ([_t("waiting_on_you", updated=self.NOW, id="a"), _t("idle", error="boom", id="b")], "worried"),
            ([_t("idle", last_result={"status": "blocked"})], "worried"),
            ([_t("idle", last_result={"status": "done"})], "asleep"),
            ([_t("resolved", error="boom")], "asleep"),  # closed: no longer anyone's worry
        ]
        for threads, want in cases:
            self.assertEqual(mood(p, threads, now=self.NOW), want, threads)
        self.assertEqual(mood({"status": "paused"}, [_t("working", activity="editing files")], now=self.NOW), "asleep")


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name)

    def test_newest_first_and_kept_small(self):
        for i in range(ACTIVITY_KEEP + 60):
            log_activity(self.folder, "started", f"event {i}", at=1000 + i)
        events = activity(self.folder)
        self.assertEqual(len(events), ACTIVITY_KEEP)
        self.assertEqual(events[0]["text"], f"event {ACTIVITY_KEEP + 59}")
        self.assertLessEqual(len((self.folder / projects.ACTIVITY_FILE).read_text().splitlines()), ACTIVITY_KEEP * 5 // 4)
        self.assertEqual([e["text"] for e in activity(self.folder, limit=2)], [f"event {ACTIVITY_KEEP + 59}", f"event {ACTIVITY_KEEP + 58}"])
        self.assertEqual(len(activity(self.folder, since=1000 + ACTIVITY_KEEP + 50)), 10)
        self.assertEqual(activity(self.folder / "nowhere"), [])

    def test_what_a_change_of_state_says(self):
        self.assertEqual(thread_event(None, _t("working", title="Tray")), ("started", "Started “Tray”"))
        self.assertIsNone(thread_event("working", _t("working")))
        self.assertIsNone(thread_event("working", _t("landing")))
        self.assertEqual(thread_event("idle", _t("working", title="Tray"))[0], "continued")
        kind, text = thread_event("working", _t("waiting_on_you", title="Tray", question="Push it?"))
        self.assertEqual((kind, text), ("needs_you", "“Tray” needs you: Push it?"))
        self.assertEqual(thread_event("working", _t("ready_for_review", last_result={"summary": "fixed"}))[0], "ready")
        self.assertEqual(thread_event("working", _t("idle", error="boom"))[0], "failed")
        self.assertEqual(thread_event("working", _t("idle", last_result={"summary": "looked"}))[0], "finished")
        self.assertEqual(thread_event("landing", _t("resolved", title="Tray", landed=1.0)), ("landed", "Landed “Tray”"))
        self.assertEqual(thread_event("idle", _t("resolved", title="Tray")), ("closed", "Closed “Tray”"))
        self.assertEqual(thread_event("landing", _t("ready_for_review", note="not landed: conflict"))[0], "not_landed")

    def test_a_projects_threads_log_their_changes(self):
        seen = []
        pt = SimpleNamespace(_states={"old": "working"}, _states_lock=threading.Lock(), folder=self.folder, on_activity=seen.append)
        log = ProjectThreads._log_change
        log(pt, _t("working", id="new", title="New"))
        log(pt, _t("working", id="new", title="New", activity="editing files"))  # a tool call: no line
        log(pt, _t("waiting_on_you", id="old", title="Old", question="ok?"))
        self.assertEqual([e["kind"] for e in seen], ["started", "needs_you"])
        self.assertEqual([e["kind"] for e in activity(self.folder)], ["needs_you", "started"])
        self.assertEqual(seen[1]["thread"], "old")


class _Threads:
    """Enough of a ProjectThreads for the hub: a list of threads."""

    def __init__(self, threads):
        self.threads = threads

    def list(self):
        return list(self.threads)

    def counts(self):
        out = {}
        for t in self.threads:
            out[t["state"]] = out.get(t["state"], 0) + 1
        return out

    def tokens_out(self):
        return 0

    def sweep(self):
        return False

    def suspend(self):
        pass


class ServerTest(unittest.TestCase):
    def setUp(self):
        from vision import server
        from vision.config import Config

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        for p in (patch.object(projects, "STORE_DIR", self.tmp / "projects"),
                  patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Brain(cfg, **kw)),
                  patch("vision.link.LIVE_DIR", self.tmp / "live"),
                  patch.object(server.Hub, "warm_up", lambda self: None),
                  patch("vision.server.warm_soon", lambda brain: None)):
            p.start()
            self.addCleanup(p.stop)
        cfg = Config()
        self.hub = server.Hub(cfg, _Brain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        self.server = server
        self.auth = {"Authorization": "Bearer tok"}

    def test_the_scout_its_mood_leash_and_activity(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            r = tc.post("/projects", json={"goal": "Triage issues", "workdir": self.dir.name,
                                           "scout": {"trait": "sprout"}, "settings": {"autonomy": "free"}}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            p = r.json()
            self.assertEqual((p["scout"]["trait"], p["mood"], p["leash"]), ("sprout", "asleep", "autonomous"))
            self.assertIn(p["scout"]["name"], SCOUT_NAMES)
            self.assertEqual(tc.post("/projects", json={"id": p["id"], "scout": {"trait": "wings"}}, headers=self.auth).status_code, 400)

            # A thread starts asking: the project goes out again with its new mood.
            pid = p["id"]
            self.hub.threads[pid] = _Threads([_t("waiting_on_you", updated=time.time())])
            self.hub.thread_changed(pid, _t("waiting_on_you"))
            frame = None
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "project" and frame["project"]["mood"] == "needs_you":
                    break
            self.assertEqual(frame["project"]["mood"], "needs_you")
            # Time alone can change it: hours later it's still waiting.
            self.hub.threads[pid] = _Threads([_t("waiting_on_you", updated=time.time() - STILL_WAITING_S - 5)])
            self.hub.moods_changed()
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "project":
                    break
            self.assertEqual(frame["project"]["mood"], "still_waiting")

            log_activity(self.hub.projects.root / pid, "started", "Started “A”", at=10)
            log_activity(self.hub.projects.root / pid, "needs_you", "“A” needs you: ok?", at=20)
            events = tc.get(f"/projects/{pid}/activity", headers=self.auth).json()["events"]
            self.assertEqual([e["at"] for e in events], [20, 10])
            self.assertEqual(len(tc.get(f"/projects/{pid}/activity", params={"since": 15}, headers=self.auth).json()["events"]), 1)
            self.assertEqual(tc.get("/projects/zzzz/activity", headers=self.auth).status_code, 404)



class CheckinTurnTest(unittest.TestCase):
    """A check-in: the turn it brings the coordinator and the card it makes."""

    def test_the_turn_says_what_changed_and_how_late(self):
        started = 1_000_000.0
        text = checkin_message(started, started - 14 * 3600, ["“Tray” finished: fixed"], missed=started - 3600, asleep_s=7200)
        self.assertTrue(text.startswith(CHECKIN_PREFIX))
        self.assertIn("14 hours ago", text)
        self.assertIn("slept for 2 hours", text)
        self.assertIn("runs now, once", text)
        self.assertIn("- “Tray” finished: fixed", text)
        info = checkin_of(text)
        self.assertEqual((info["started"], info["why"]), (started, "scheduled"))
        self.assertTrue(info["label"].endswith("status report"))
        self.assertIn("This is its first status report", checkin_message(started, None, []))
        self.assertIsNone(checkin_of("hello"))
        self.assertEqual(unwrap_turn(text)[0], "checkin")

    def test_history_folds_a_checkin_and_its_reply_into_one_card(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            text = checkin_message(500.0, None, [])
            record_checkin(folder, {"started": 500.0, "ended": 530.0, "needs_you": 2})
            rows = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"},
                    {"role": "checkin", "text": text}, {"role": "assistant", "text": "Fixed the tray.", "duration_ms": 30000},
                    {"role": "checkin", "text": checkin_message(900.0, 500.0, [])}]
            folded = fold_checkins(rows, checkins(folder))
            self.assertEqual([r["role"] for r in folded], ["user", "assistant", "checkin", "checkin"])
            card = folded[2]
            self.assertEqual((card["text"], card["started"], card["ended"], card["needs_you"], card["duration_ms"]),
                             ("Fixed the tray.", 500.0, 530.0, 2, 30000))
            self.assertEqual(folded[3]["text"], "")  # still running
            self.assertNotIn("Status report", projects.recap(rows[:2]))

    def test_turn_entries_mark_the_checkin(self):
        from vision.agentlog import turn_entries

        rows = turn_entries(checkin_message(500.0, None, []), "Did it.", "", "Did it.", [], [])
        self.assertEqual([r["role"] for r in rows], ["checkin", "assistant"])


class _Reporter(_Brain):
    """A coordinator that answers a check-in with a short report."""

    prompts: list = []

    def ask(self, prompt, **_):
        _Reporter.prompts.append(prompt)
        self.session_id = "s1"
        return SimpleNamespace(text="Fixed the tray icon on its branch.\nThe change is ready for your review.", session_id="s1",
                               model="m", error="", is_error=False)

    def take_untaken_steers(self):
        return []


class CheckinServerTest(unittest.TestCase):
    def setUp(self):
        from vision import server
        from vision.config import Config
        from vision.scheduled import Schedule

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        _Reporter.prompts = []
        for p in (patch.object(projects, "STORE_DIR", self.tmp / "projects"),
                  patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Reporter(cfg, **kw)),
                  patch("vision.link.LIVE_DIR", self.tmp / "live"),
                  patch("vision.agentlog.AGENTS_DIR", self.tmp / "agents"),
                  patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []),
                  patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain),
                  patch.object(server.Hub, "warm_up", lambda self: None),
                  patch("vision.server.warm_soon", lambda brain: None)):
            p.start()
            self.addCleanup(p.stop)
        cfg = Config()
        self.hub = server.Hub(cfg, _Reporter(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        self.hub.schedule = Schedule(self.tmp / "scheduled.json")
        self.pushed = []
        self.hub.push = lambda title, body, chat="", project="", thread="", **kw: self.pushed.append((title, body))
        self.server = server
        self.auth = {"Authorization": "Bearer tok"}

    def _until(self, ws, pred, limit=60):
        for _ in range(limit):
            ev = ws.receive_json()
            if pred(ev):
                return ev
        raise AssertionError("never came")

    def test_check_in_now_runs_a_turn_and_makes_a_card(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            p = tc.post("/projects", json={"goal": "Triage issues", "workdir": self.dir.name, "scout": {"name": "Atlas"}},
                        headers=self.auth).json()
            self.hub.threads[p["id"]] = _Threads([_t("ready_for_review", updated=time.time())])
            r = tc.post(f"/projects/{p['id']}/checkin", headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            started = self._until(ws, lambda ev: ev["type"] == "checkin")
            self.assertEqual(started["state"], "started")
            begun = self._until(ws, lambda ev: ev["type"] == "start")
            self.assertTrue(begun["note"].endswith("status report"))  # shown as a note, not as the user's words
            done = self._until(ws, lambda ev: ev["type"] == "checkin")
            self.assertEqual((done["state"], done["needs_you"]), ("done", 1))
            self.assertIn("Fixed the tray icon", done["summary"])
            self.assertIn(CHECKIN_PREFIX, _Reporter.prompts[-1])
            self.assertIn("<vision-project>", _Reporter.prompts[-1])  # with the project's status, like any turn
            # Kept: the history has it as one card, the project knows its last check-in, the activity says so.
            rows = tc.get("/history", params={"chat": r.json()["chat"]}, headers=self.auth).json()
            self.assertEqual([(x["role"], x.get("needs_you")) for x in rows], [("checkin", 1)])
            self.assertEqual(tc.get(f"/projects/{p['id']}", headers=self.auth).json()["checkin"]["last"]["needs_you"], 1)
            kinds = [e["kind"] for e in tc.get(f"/projects/{p['id']}/activity", headers=self.auth).json()["events"]]
            self.assertEqual(kinds[:2], ["checkin_done", "checkin"])
            # One push, because something needs the user (notify is needs_you).
            self.assertEqual(self.pushed, [("Atlas sent a status report · 1 needs you", "Fixed the tray icon on its branch.")])

    def test_schedule_skips_and_catch_up(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc:
            p = tc.post("/projects", json={"goal": "Inbox", "workdir": self.dir.name, "settings": {"autonomy": "ask"}},
                        headers=self.auth).json()
            pid = p["id"]
            view = tc.post(f"/projects/{pid}/checkins", json={"repeat": "daily", "time": "08:00"}, headers=self.auth).json()
            self.assertEqual(view["checkin"]["schedule"]["repeat"], "daily")
            self.assertEqual(tc.post(f"/projects/{pid}/checkins", json={"repeat": "every", "every_minutes": 1}, headers=self.auth).status_code, 400)
            task = self.hub.schedule.for_project(pid)
            run = lambda **kw: asyncio_run(self.hub, self.hub.run_task_off_loop(task, **kw))  # noqa: E731
            self.assertIsNone(run())  # on call: a scheduled check-in doesn't run
            self.assertIsNotNone(run(why="now"))  # asked for, it does
            tc.post(f"/projects/{pid}/pause", headers=self.auth)
            self.assertEqual(tc.post(f"/projects/{pid}/checkin", headers=self.auth).status_code, 409)
            self.assertIsNone(run(why="now"))
            tc.post(f"/projects/{pid}/checkins", json={"repeat": "off"}, headers=self.auth)
            self.assertIsNone(self.hub.schedule.for_project(pid))
            # Retiring a scout keeps its schedule (for when it's brought back); deleting it drops it.
            tc.post(f"/projects/{pid}/checkins", json={"repeat": "weekdays", "time": "09:00"}, headers=self.auth)
            tc.delete(f"/projects/{pid}", headers=self.auth)
            self.assertIsNotNone(self.hub.schedule.for_project(pid))
            tc.post(f"/projects/{pid}/delete", headers=self.auth)
            self.assertIsNone(self.hub.schedule.for_project(pid))

    def test_a_job_on_its_own_is_a_scout(self):
        from fastapi.testclient import TestClient

        # One from before scouts took them over turns into a scout as the server starts; one set from a
        # chat stays a task.
        old = self.hub.schedule.upsert({"prompt": "USA news brief", "repeat": "daily", "time": "07:00"})
        kept = self.hub.schedule.upsert({"prompt": "check the deploy", "repeat": "once", "time": "15:00",
                                         "date": "2099-01-01", "chat": "c1"})
        with TestClient(self.server.create_app(self.hub)) as tc:
            self.assertEqual([t["id"] for t in self.hub.schedule.list() if t["kind"] == "task"], [kept["id"]])
            self.assertNotIn(old["id"], [t["id"] for t in self.hub.schedule.list()])
            news = next(p for p in self.hub.projects.list() if p["goal"] == "USA news brief")
            task = self.hub.schedule.for_project(news["id"])
            self.assertEqual((task["repeat"], task["time"]), ("daily", "07:00"))

            # A new one from the apps is made a scout too, in the folder it names.
            r = tc.post("/scheduled", json={"prompt": "Tidy the notes", "repeat": "weekdays", "time": "09:00",
                                            "workdir": self.dir.name}, headers=self.auth)
            self.assertEqual((r.status_code, r.json()["kind"]), (200, "checkin"))
            notes = self.hub.projects.get(r.json()["project"])
            self.assertEqual((notes["goal"], notes["workdirs"]), ("Tidy the notes", [os.path.abspath(self.dir.name)]))
            self.assertEqual(tc.post("/scheduled", json={"prompt": "x", "repeat": "daily", "time": "7"},
                                     headers=self.auth).status_code, 400)
            self.assertEqual(len(self.hub.projects.list()), 2)  # the bad one left no scout behind

    def test_on_call_becomes_its_schedule_off(self):
        from fastapi.testclient import TestClient

        p = self.hub.projects.upsert({"goal": "Inbox", "workdirs": [self.dir.name], "settings": {"leash": "on_call"}})
        self.hub.schedule.upsert({"kind": "checkin", "project": p["id"], "repeat": "daily", "time": "08:00"})
        with TestClient(self.server.create_app(self.hub)):
            self.assertEqual(self.hub.projects.get(p["id"])["settings"]["leash"], "checkins")
            self.assertFalse(self.hub.schedule.for_project(p["id"])["enabled"])


def asyncio_run(hub, coro):
    """Run a hub coroutine on the server's own loop (TestClient runs it in another thread)."""
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, hub.loop).result(10)


if __name__ == "__main__":
    unittest.main()
