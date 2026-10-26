"""Projects (vision/projects.py): the store, what a project's chat is told, the server's endpoints and
frames, re-tagging after a restart, and the `vision project` commands."""
import json
import os
import tempfile
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from unittest.mock import patch

from vision import projects
from vision.projects import SETTINGS, ProjectStore, clean, prompt_section


class CleanTest(unittest.TestCase):
    def test_a_goal_is_needed_and_makes_the_title(self):
        with self.assertRaises(ValueError):
            clean({"goal": "  "})
        p = clean({"goal": "Ship the iPhone widget.\nWith tests.", "workdir": "~/x"})
        self.assertEqual(p["title"], "Ship the iPhone widget")
        self.assertEqual(p["workdirs"], [os.path.expanduser("~/x")])
        self.assertEqual((p["status"], p["chat"], p["instructions"]), ("active", "", ""))
        self.assertEqual(p["settings"], SETTINGS)
        long = clean({"goal": "word " * 40})
        self.assertLessEqual(len(long["title"]), projects.TITLE_CHARS + 1)
        self.assertTrue(long["title"].endswith("…"))

    def test_an_edit_keeps_what_it_leaves_out(self):
        p = clean({"goal": "g", "title": "T", "instructions": "be brief", "workdirs": ["/a", "/a", "/b"]})
        self.assertEqual(p["workdirs"], ["/a", "/b"])
        edited = clean({"instructions": "", "settings": {"max_threads": "5"}}, p)
        self.assertEqual((edited["id"], edited["title"], edited["goal"], edited["instructions"]), (p["id"], "T", "g", ""))
        self.assertEqual(edited["settings"]["max_threads"], 5)
        self.assertEqual(edited["settings"]["notify"], "needs_you")

    def test_bad_settings_and_status_are_refused(self):
        for body in ({"goal": "g", "settings": {"max_threads": 0}}, {"goal": "g", "settings": {"notify": "loud"}},
                     {"goal": "g", "status": "gone"}, {"goal": "g", "workdirs": 3}):
            with self.assertRaises(ValueError, msg=body):
                clean(body)
        self.assertNotIn("bogus", clean({"goal": "g", "settings": {"bogus": 1}})["settings"])


class PromptSectionTest(unittest.TestCase):
    def test_the_goal_and_instructions_reach_the_prompt(self):
        self.assertEqual(prompt_section(None), "")
        p = clean({"goal": "Fix the build", "title": "Build", "instructions": "Never push.", "workdirs": ["/a", "/b"]})
        text = prompt_section(p)
        self.assertIn('project "Build"', text)
        self.assertIn("Goal: Fix the build", text)
        self.assertIn("Never push.", text)
        self.assertIn("/a, /b", text)

    def test_every_provider_puts_it_in_the_system_prompt(self):
        from vision.persona import system_prompt

        section = prompt_section(clean({"goal": "Fix the build"}))
        self.assertIn("Goal: Fix the build", system_prompt(False, memory="", project=section))
        self.assertNotIn("PROJECT", system_prompt(False, memory=""))


class CoordinatorTest(unittest.TestCase):
    """What the coordinator is told: its rules (system prompt), the live status block (each turn), thread
    reports; and the project memory and the push rule."""

    def test_the_rules_follow_the_settings(self):
        ask = prompt_section(clean({"goal": "g", "settings": {"autonomy": "ask", "max_threads": 5}}))
        self.assertIn("The leash is `on call`", ask)
        self.assertIn("at most 5 at once", ask)
        self.assertIn("vision thread land", ask)
        self.assertIn("Land only when the user has said to", ask)
        free = prompt_section(clean({"goal": "g", "settings": {"autonomy": "free", "updates": "needs_you"}}))
        self.assertIn("The leash is `autonomous`", free)
        self.assertIn("Land only when the user has said to", free)  # landing asks first on every leash by default
        landing = prompt_section(clean({"goal": "g", "settings": {"autonomy": "free", "rules": {"land": "does"}}}))
        self.assertIn("Land a thread once its checks passed", landing)
        self.assertIn("Updates are `needs_you`", free)

    def test_the_status_block_lists_threads_memory_and_library(self):
        p = clean({"goal": "g"})
        threads = [{"id": "a1", "title": "Tests", "state": "working", "activity": "running pytest", "branch": "vision/x/tests-a1"},
                   {"id": "b2", "title": "Docs", "state": "waiting_on_you", "question": "Which style?", "told": "use the old one"},
                   {"id": "c3", "title": "Old", "state": "resolved"}]
        block = projects.status_block(p, threads, ["- 2026-10-11: SQLite, not Postgres (thread Storage)"],
                                      [{"name": "plan.md", "path": "/w/plan.md"}])
        self.assertTrue(block.startswith(projects.STATUS_OPEN) and block.endswith(projects.STATUS_CLOSE))
        self.assertIn("- a1 · working (running pytest) · Tests · vision/x/tests-a1", block)
        self.assertIn("b2 · waiting on you · Docs · Which style?", block)
        self.assertIn("the user told it directly: “use the old one”", block)
        self.assertIn("Closed: 1 (Old)", block)
        self.assertIn("SQLite, not Postgres", block)
        self.assertIn("plan.md (/w/plan.md)", block)
        paused = projects.status_block({**p, "status": "paused"}, [], [])
        self.assertIn("PAUSED", paused)
        self.assertIn("No threads yet.", paused)
        self.assertEqual(projects.strip_status(block + "\n\nhow's it going?"), "how's it going?")
        self.assertEqual(projects.strip_status("plain"), "plain")

    def test_a_report_shows_one_line_and_carries_the_results(self):
        done = {"id": "a1", "title": "Tests", "state": "ready_for_review", "branch": "vision/x/tests-a1",
                "last_result": {"status": "completed", "summary": "All green.", "changes": ["a.py"], "question": None}}
        asks = {"id": "b2", "title": "Docs", "state": "waiting_on_you", "question": "Which style?"}
        text = projects.report_message([done, asks], step=1)
        note = projects.report_note(text)
        self.assertEqual(note, "2 threads stopped: “Tests” is ready for review: All green. · “Docs” needs you: Which style?")
        self.assertIn('"branch": "vision/x/tests-a1"', text)
        self.assertIn('"changes": [', text)
        self.assertNotIn('"question": null', text)  # empty fields stay out
        self.assertEqual(projects.unwrap_turn(text), ("note", note))
        self.assertEqual(projects.unwrap_turn("<vision-project>\nx\n</vision-project>\n\nhi"), ("user", "hi"))
        self.assertIsNone(projects.report_note("[Thread] not one"))
        capped = projects.report_message([done], step=projects.AUTO_STEPS)
        self.assertIn("don't start or continue any thread now", capped)

    def test_reports_read_back_from_a_session_as_notes(self):
        from vision.sessions import _append_turn

        out = []
        _append_turn(out, "user", projects.report_message([{"id": "a", "title": "T", "state": "idle", "error": "boom"}], 1))
        _append_turn(out, "assistant", "It broke; I'll look.")
        _append_turn(out, "user", projects.STATUS_OPEN + "\n- x\n" + projects.STATUS_CLOSE + "\n\nand now?")
        self.assertEqual(out, [{"role": "note", "text": "Thread “T” stopped: boom"},
                               {"role": "assistant", "text": "It broke; I'll look."}, {"role": "user", "text": "and now?"}])

    def test_a_plan_window_past_80_percent_warns_while_threads_work(self):
        usage = {"info": {"unifiedWindows": {"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.86}}}}
        working = [{"id": "a1", "title": "Tests", "state": "working"}]
        warning = projects.usage_warning(usage, working)
        self.assertIn("the week at 86%", warning)
        self.assertNotIn("five-hour", warning)
        self.assertEqual(projects.usage_warning(usage, [{"state": "idle"}]), "")  # nothing running: nothing to warn of
        self.assertEqual(projects.usage_warning(None, working), "")
        self.assertIn(warning, projects.status_block(clean({"goal": "g"}), working, [], warning=warning))

    def test_the_push_rule(self):
        self.assertTrue(projects.should_push("needs_you", "needs_you"))
        self.assertTrue(projects.should_push("needs_you", "failed"))
        self.assertFalse(projects.should_push("needs_you", "failed", last="failed"))  # still failing: once is enough
        self.assertTrue(projects.should_push("needs_you", "failed", last="done"))
        self.assertFalse(projects.should_push("needs_you", "done"))
        self.assertTrue(projects.should_push("all", "done"))
        self.assertFalse(projects.should_push("none", "needs_you"))
        self.assertEqual(projects.push_kind({"state": "idle", "last_result": {"status": "blocked"}}), "failed")
        self.assertEqual(projects.push_kind({"state": "ready_for_review"}), "done")

    def test_memory_is_dated_deduped_capped_and_forgettable(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            added = projects.remember(folder, ["Use SQLite.", "  ", "Keep the API v1."], source="thread Storage")
            self.assertEqual(len(added), 2)
            self.assertRegex(added[0], r"^- \d{4}-\d{2}-\d{2}: Use SQLite\. \(thread Storage\)$")
            self.assertEqual(projects.remember(folder, ["use sqlite."]), [])  # already there, whoever said it
            self.assertEqual(len(projects.memory_lines(folder)), 2)
            self.assertTrue((folder / projects.MEMORY_FILE).read_text().startswith("# Project memory"))
            self.assertIn("Use SQLite.", projects.forget(folder, 1))
            with self.assertRaises(IndexError):
                projects.forget(folder, 5)
            with patch.object(projects, "MEMORY_MAX", 300):
                projects.remember(folder, [f"fact number {i} " + "x" * 40 for i in range(10)])
            lines = projects.memory_lines(folder)
            self.assertLessEqual(sum(len(x) + 1 for x in lines), 300)
            self.assertIn("fact number 9", lines[-1])  # the newest kept, the oldest dropped
            with patch.object(projects, "MEMORY_SHOWN", 120):
                shown = projects.memory_shown(lines)
            self.assertEqual(shown[-1], lines[-1])
            self.assertLess(len(shown), len(lines))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "projects"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_one_folder_each(self):
        s = ProjectStore(self.root)
        a = s.upsert({"goal": "first", "workdir": "/a"})
        b = s.upsert({"goal": "second"})
        self.assertTrue((self.root / a["id"] / "project.json").is_file())
        self.assertFalse(list(self.root.rglob("*.tmp")))
        again = ProjectStore(self.root)
        self.assertEqual({p["id"] for p in again.list()}, {a["id"], b["id"]})
        self.assertEqual(again.get(a["id"])["workdirs"], ["/a"])
        with self.assertRaises(KeyError):
            s.upsert({"id": "nope", "goal": "x"})

    def test_find_takes_a_prefix(self):
        s = ProjectStore(self.root)
        with patch("vision.projects.secrets.token_hex", side_effect=["ab12", "ab34", "cd56"]):
            s.upsert({"goal": "one"})
            s.upsert({"goal": "two"})
            s.upsert({"goal": "three"})
        self.assertEqual(s.find("cd")["goal"], "three")
        self.assertEqual(s.find("ab12")["goal"], "one")
        self.assertIsNone(s.find("zz"))
        with self.assertRaises(ValueError):
            s.find("ab")

    def test_archive_hides_it_and_frees_its_chat(self):
        s = ProjectStore(self.root)
        p = s.upsert({"goal": "g"})
        s.set_chat(p["id"], "chat1")
        self.assertEqual(s.by_chat("chat1")["id"], p["id"])
        s.archive(p["id"])
        self.assertEqual(s.list(), [])
        self.assertEqual([x["id"] for x in s.list(include_archived=True)], [p["id"]])
        self.assertIsNone(s.by_chat("chat1"))
        self.assertTrue((self.root / p["id"] / "project.json").is_file())  # nothing deleted

    def test_a_broken_file_is_skipped(self):
        (self.root / "bad").mkdir(parents=True)
        (self.root / "bad" / "project.json").write_text("{", encoding="utf-8")
        (self.root / "odd").mkdir()
        (self.root / "odd" / "project.json").write_text(json.dumps({"id": "other", "goal": "g"}), encoding="utf-8")
        self.assertEqual(ProjectStore(self.root).list(), [])


class _Brain:
    """Enough of a brain for a chat: it works where its config says."""

    provider = "claude"

    def __init__(self, cfg, session_id=None, **_):
        self.cfg, self.session_id, self.handoff = cfg, session_id, None
        self.workdir = os.path.abspath(cfg.workdir) if cfg.workdir else os.getcwd()

    def resolved_model(self):
        return self.cfg.model

    def cancel(self):
        pass

    def resume(self, session_id):
        self.session_id = session_id

    def new_session(self):
        self.session_id = None


class ServerTest(unittest.TestCase):
    def setUp(self):
        from vision import server
        from vision.config import Config

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        (self.tmp / "work").mkdir()
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

    def _drain(self, ws, until, limit=40):
        seen = []
        for _ in range(limit):
            seen.append(ws.receive_json())
            if until(seen[-1]):
                return seen
        raise AssertionError(f"never saw the frame; got {[e['type'] for e in seen]}")

    def test_make_open_edit_and_archive(self):
        from fastapi.testclient import TestClient

        work = str(self.tmp / "work")
        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            self.assertEqual(ws.receive_json()["projects"], [])
            r = tc.post("/projects", json={"goal": "Fix the flaky tests", "workdir": work, "chat": "forged"}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            p = r.json()
            self.assertEqual((p["title"], p["workdirs"], p["chat"]), ("Fix the flaky tests", [work], ""))
            self.assertEqual(p["folder"], self.server.tilde(work))
            self.assertEqual(self._drain(ws, lambda ev: ev["type"] == "project")[-1]["project"]["id"], p["id"])
            self.assertEqual(tc.post("/projects", json={"goal": "x", "workdir": str(self.tmp / "nope")}, headers=self.auth).status_code, 400)
            self.assertEqual(tc.post("/projects", json={"goal": ""}, headers=self.auth).status_code, 400)
            self.assertEqual(tc.post("/projects", json={"id": "nope", "goal": "x"}, headers=self.auth).status_code, 404)
            self.assertEqual(tc.get(f"/projects/{p['id'][:3]}", headers=self.auth).json()["id"], p["id"])
            self.assertEqual(tc.get("/projects/zzzz", headers=self.auth).status_code, 404)

            # The phone opens it: a chat in the project's folder, tagged, whose brain knows the goal.
            ws.send_json({"type": "new", "project": p["id"]})
            opened = self._drain(ws, lambda ev: ev["type"] == "chat" and ev.get("opened"))[-1]
            chat = self.hub.chats[opened["chat"]]
            self.assertEqual((opened["project"], opened["workdir"], opened["title"]), (p["id"], work, "Fix the flaky tests"))
            self.assertIn("Goal: Fix the flaky tests", chat.brain.project_prompt)
            self.assertEqual(self.hub.projects.get(p["id"])["chat"], chat.id)
            # Opening it again, from anywhere, finds the same chat.
            self.assertEqual(tc.post(f"/projects/{p['id']}/open", headers=self.auth).json()["chat"], chat.id)
            ws.send_json({"type": "new", "project": p["id"]})
            self.assertEqual(self._drain(ws, lambda ev: ev["type"] == "chat" and ev.get("opened"))[-1]["chat"], chat.id)
            self.assertEqual(len(self.hub.chats), 1)

            # An edit reaches the open chat's prompt and title.
            r = tc.post("/projects", json={"id": p["id"], "instructions": "Never push.", "title": "Flaky"}, headers=self.auth)
            self.assertEqual(r.json()["workdirs"], [work])  # left out of the edit, so kept
            self.assertIn("Never push.", chat.brain.project_prompt)
            self.assertEqual(chat.title, "Flaky")

            # A switch of model keeps the project in the new brain's prompt.
            chat.brain = _Brain(chat.cfg.brain)
            self.assertIn("Never push.", chat.brain.project_prompt)

            # Archived: off the list, and its chat stays open as an ordinary one.
            self.assertEqual(tc.delete(f"/projects/{p['id']}", headers=self.auth).status_code, 200)
            self._drain(ws, lambda ev: ev["type"] == "project_removed" and ev["project"] == p["id"])
            self.assertEqual(tc.get("/projects", headers=self.auth).json(), [])
            self.assertEqual(len(tc.get("/projects", params={"all": 1}, headers=self.auth).json()), 1)
            self.assertEqual((chat.project, chat.brain.project_prompt), ("", ""))
            self.assertIn(chat.id, self.hub.chats)
            self.assertEqual(tc.post(f"/projects/{p['id']}/open", headers=self.auth).status_code, 409)

    def test_an_open_chat_becomes_a_project_as_it_is(self):
        from fastapi.testclient import TestClient

        work = str(self.tmp / "work")
        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            ws.send_json({"type": "new", "workdir": work})
            opened = self._drain(ws, lambda ev: ev["type"] == "chat" and ev.get("opened"))[-1]
            chat = self.hub.chats[opened["chat"]]
            # Nothing said yet and no goal given: nothing to make a project of.
            r = tc.post("/projects/adopt", json={"chat": chat.id}, headers=self.auth)
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(tc.post("/projects/adopt", json={}, headers=self.auth).status_code, 400)
            self.assertEqual(tc.post("/projects/adopt", json={"chat": "nope"}, headers=self.auth).status_code, 404)
            # What the user first asked for is the goal; the chat's title stays; the chat keeps its folder.
            chat._transcript = [{"role": "user", "text": "Fix the flaky server tests,\n  then the slow ones"},
                                {"role": "assistant", "text": "On it."}]
            chat.title = "Flaky tests"
            r = tc.post("/projects/adopt", json={"chat": chat.id, "instructions": "Never push."}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            p, c = r.json()["project"], r.json()["chat"]
            self.assertEqual((p["goal"], p["title"], p["workdirs"], p["chat"], p["instructions"]),
                             ("Fix the flaky server tests, then the slow ones", "Flaky tests", [work], chat.id, "Never push."))
            self.assertEqual((c["chat"], c["project"], c["title"]), (chat.id, p["id"], "Flaky tests"))
            self.assertEqual(chat.project, p["id"])
            self.assertIn("Goal: Fix the flaky server tests", chat.brain.project_prompt)
            self.assertIn("Never push.", chat.brain.project_prompt)
            self.assertEqual(self.hub.projects.get(p["id"])["chat"], chat.id)
            self._drain(ws, lambda ev: ev["type"] == "project" and ev["project"]["id"] == p["id"])
            self.assertEqual(len(self.hub.chats), 1)
            # Once is enough.
            r = tc.post("/projects/adopt", json={"chat": chat.id, "goal": "again"}, headers=self.auth)
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("Flaky tests", r.json()["detail"])
            # A goal given wins over the conversation; the title comes from it when the chat has none.
            ws.send_json({"type": "new", "workdir": work})
            other = self.hub.chats[self._drain(ws, lambda ev: ev["type"] == "chat" and ev.get("opened"))[-1]["chat"]]
            other.title = ""
            r = tc.post("/projects/adopt", json={"chat": other.id, "goal": "Ship the events endpoint."}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["project"]["title"], other.title), ("Ship the events endpoint", "Ship the events endpoint"))

    def test_a_terminals_conversation_is_opened_here_and_adopted(self):
        from fastapi.testclient import TestClient

        from vision.sessions import SessionInfo

        work = str(self.tmp / "work")
        info = SessionInfo(id="abcd1234", provider="claude", title="Flaky tests", last_active=0, cwd=work)
        history = [{"role": "user", "text": "Fix the flaky tests"}, {"role": "assistant", "text": "Sure."}]
        with patch("vision.sessions.find_session", side_effect=lambda provider, sid, **_: info if sid == "abcd" else None), \
                patch("vision.sessions.find_any_session", return_value=None), \
                patch("vision.sessions.session_history", side_effect=lambda provider, sid, *a, **k: history if sid == info.id else []), \
                TestClient(self.server.create_app(self.hub)) as tc:
            r = tc.post("/projects/adopt", json={"session_id": "zzzz", "provider": "claude"}, headers=self.auth)
            self.assertEqual(r.status_code, 404, r.text)
            r = tc.post("/projects/adopt", json={"session_id": "abcd", "provider": "claude"}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            p, c = r.json()["project"], r.json()["chat"]
            chat = self.hub.chats[c["chat"]]
            # The conversation carries on here, in its own folder, as the project's chat.
            self.assertEqual((chat.brain.session_id, chat.workdir, chat.title), (info.id, work, "Flaky tests"))
            self.assertEqual((p["goal"], p["title"], p["workdirs"], p["chat"]), ("Fix the flaky tests", "Flaky tests", [work], chat.id))
            self.assertEqual((c["project"], c["session_id"]), (p["id"], info.id))
            self.assertIn("Goal: Fix the flaky tests", chat.brain.project_prompt)
            # Asked again for the same conversation: the chat it already has, which is a project's already.
            r = tc.post("/projects/adopt", json={"session_id": "abcd", "provider": "claude"}, headers=self.auth)
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(len(self.hub.chats), 1)

    def test_a_closed_chat_is_replaced_and_a_missing_folder_falls_back(self):
        import asyncio

        gone = self.tmp / "gone"
        gone.mkdir()
        p = self.hub.projects.upsert({"goal": "g", "workdir": str(gone)})
        gone.rmdir()

        async def go():
            first = await self.hub.open_project_chat(p)
            self.hub.close_chat(first)
            second = await self.hub.open_project_chat(self.hub.projects.get(p["id"]))
            both = await asyncio.gather(self.hub.open_project_chat(p), self.hub.open_project_chat(p))
            return first, second, both

        first, second, both = asyncio.run(go())
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.hub.projects.get(p["id"])["chat"], second.id)
        self.assertEqual(both, [second, second])
        self.assertEqual(second.workdir, os.getcwd())  # the folder went: the default one

    def test_two_phones_opening_at_once_get_one_chat(self):
        import asyncio

        p = self.hub.projects.upsert({"goal": "g", "workdir": str(self.tmp / "work")})

        async def go():
            return await asyncio.gather(self.hub.open_project_chat(p), self.hub.open_project_chat(p))

        a, b = asyncio.run(go())
        self.assertIs(a, b)
        self.assertEqual(len(self.hub.chats), 1)


class RestoreTest(unittest.TestCase):
    def test_a_restored_chat_is_its_project_chat_again(self):
        from vision.config import Config
        from vision.server import Hub
        from vision.turnjournal import ChatJournal

        with tempfile.TemporaryDirectory() as tmp, patch("vision.turnjournal.JOURNAL_DIR", Path(tmp)), \
                patch.object(projects, "STORE_DIR", Path(tmp) / "projects"), \
                patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Brain(cfg, **kw)):
            store = ProjectStore()
            p = store.upsert({"goal": "Keep going", "workdir": tmp})
            store.set_chat(p["id"], "chat9")
            journal = ChatJournal("chat9", Path(tmp))
            journal.append("chat", title="Keep going", model="opus", effort="high", session_id="s1")
            journal.append("base", history=[])
            cfg = Config()
            hub = Hub(cfg, _Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=True)
            chat = hub.chats["chat9"]
            self.assertEqual(chat.summary()["project"], p["id"])
            self.assertIn("Goal: Keep going", chat.brain.project_prompt)
            self.assertEqual(hub.hello()["projects"][0]["id"], p["id"])


class CliTest(unittest.TestCase):
    def _run(self, *args, answers=()):
        from typer.testing import CliRunner

        from vision import cli

        calls, answers = [], list(answers)

        def call(path, body=None, method="", why=""):
            calls.append((path, body, method))
            return answers.pop(0)

        with patch.object(cli, "_serve_call", side_effect=call):
            result = CliRunner().invoke(cli.app, ["project", *args])
        return result, calls

    def test_new_sends_the_goal_and_this_folder(self):
        p = {"id": "ab12", "title": "Fix it", "goal": "Fix it", "workdirs": [os.getcwd()], "chat": "", "status": "active"}
        result, calls = self._run("new", "Fix", "it", "--title", "Fix it", answers=[p])
        self.assertEqual(result.exit_code, 0, result.output)
        path, body, _ = calls[0]
        self.assertEqual(path, "/projects")
        self.assertEqual((body["goal"], body["title"], body["workdir"]), ("Fix it", "Fix it", os.getcwd()))
        self.assertIn("vision project open ab12", result.output)

    def test_list_edit_and_archive(self):
        p = {"id": "ab12", "title": "Fix it", "goal": "Fix it", "workdirs": ["/w"], "folder": "/w", "chat": "", "status": "active",
             "updated": 0}
        result, calls = self._run("list", "--all", answers=[[p]])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0][0], "/projects?all=1")
        self.assertIn("Fix it", result.output)
        result, calls = self._run("edit", "ab", "--instructions", "", answers=[p, p])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(calls[0][0], "/projects/ab")
        self.assertEqual(calls[1][1], {"instructions": "", "id": "ab12"})
        result, calls = self._run("archive", "ab", answers=[p])
        self.assertEqual((calls[0][0], calls[0][2]), ("/projects/ab", "DELETE"))
        self.assertIn("archived Fix it", result.output)


if __name__ == "__main__":
    unittest.main()
