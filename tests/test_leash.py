"""A scout's leash and rules: the setting and its migration from `autonomy`, what
each rule asks first or refuses, the gate that turns a call that asks first into an approval request, and the
approvals' round trip through the server."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from test_projects import _Brain

from vision import projects
from vision.projects import (ACTIONS, AUTONOMY, ProjectStore, add_approval, approvals, approved_call, clean_settings,
                             decide_approval, effective_rules, inside_rm, prompt_section, rule_patterns, unwrap_turn)
from vision.toolrules import first_match, gate


def _project(**settings):
    return {"id": "p1", "title": "T", "goal": "g", "settings": clean_settings(settings)}


class LeashTest(unittest.TestCase):
    def test_autonomy_and_the_leash_stay_in_step(self):
        self.assertEqual(clean_settings({})["leash"], "checkins")
        old = {"autonomy": "free"}  # a project from before the leash
        self.assertEqual(clean_settings({}, old)["leash"], "autonomous")
        s = clean_settings({"leash": "on_call"}, clean_settings({}, old))
        self.assertEqual((s["leash"], s["autonomy"]), ("on_call", "ask"))
        s = clean_settings({"autonomy": "balanced"}, s)  # an older app or `vision project set autonomy=…`
        self.assertEqual(s["leash"], "checkins")
        for leash, autonomy in AUTONOMY.items():
            self.assertEqual(clean_settings({"leash": leash})["autonomy"], autonomy)
        with self.assertRaises(ValueError):
            clean_settings({"leash": "loose"})

    def test_a_stored_project_is_migrated_when_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "aaaa").mkdir()
            (root / "aaaa" / "project.json").write_text(json.dumps({"id": "aaaa", "goal": "g", "settings": {"autonomy": "free"}}))
            p = ProjectStore(root).get("aaaa")
            self.assertEqual((p["settings"]["leash"], p["settings"]["rules"]), ("autonomous", {}))

    def test_rules_follow_the_leash_unless_set(self):
        self.assertEqual(effective_rules(_project(leash="checkins")), {"land": "asks", "publish": "asks", "send": "asks", "destructive": "asks"})
        self.assertEqual(effective_rules(_project(leash="autonomous"))["publish"], "does")
        self.assertEqual(effective_rules(_project(leash="on_call")), effective_rules(_project(leash="checkins")))
        p = _project(leash="autonomous", rules={"publish": "never", "send": "does"})
        self.assertEqual(effective_rules(p), {"land": "asks", "publish": "never", "send": "does", "destructive": "asks"})
        cleared = clean_settings({"rules": {"publish": "default"}}, p["settings"])
        self.assertEqual(cleared["rules"], {"send": "does"})
        for bad in ({"fly": "does"}, {"publish": "maybe"}, "never"):
            with self.assertRaises(ValueError, msg=bad):
                clean_settings({"rules": bad})

    def test_each_rule_kind_becomes_permission_rules(self):
        p = _project(leash="autonomous", rules={"send": "never"})
        ask, deny = rule_patterns(p, "coordinator")
        self.assertIn("Bash(vision thread land:*)", ask)  # land asks
        self.assertNotIn("Bash(git push:*)", ask + deny)  # publish: does it
        self.assertIn("Bash(gh issue comment:*)", deny)  # send: never
        self.assertIn("Bash(sudo:*)", ask)  # destructive asks
        t_ask, _ = rule_patterns(p, "thread")
        self.assertNotIn("Bash(vision thread land:*)", t_ask)  # threads can't land: not their rule

    def test_the_patterns_catch_what_they_mean(self):
        ask, _ = rule_patterns(_project(leash="checkins"), "coordinator")
        caught = {
            "git push origin feature": True, "cd repo && git push": True, "gh pr create --draft": True,
            "vision thread land t1": True, "sudo dnf upgrade": True, "npm install -g typescript": True,
            "rm -rf ../elsewhere": True, "gh issue comment 12 -b hi": True,
            "git commit -m fix": False, "npm install": False, "pytest -q": False, "git status": False,
        }
        for command, want in caught.items():
            self.assertEqual(first_match(ask, "Bash", {"command": command}) is not None, want, command)
        self.assertIsNotNone(first_match(ask, "mcp__gmail__send_email", {}))
        self.assertIsNone(first_match(ask, "mcp__gmail__search", {}))

    def test_rm_inside_its_own_folder_is_fine(self):
        for command, inside in (("rm -rf build dist", True), ("rm -rf ./out/x && ls", True), ("rm -rf ..", False),
                                ("rm -rf /", False), ("rm -rf .", False), ("rm -rf $HOME/x", False), ("rm -rf ~/x", False),
                                ("cd /tmp && rm -rf x", False), ("rm -rf build ../other", False), ("ls", False)):
            self.assertEqual(inside_rm(command, "/work/tree"), inside, command)

    def test_the_coordinator_is_told_the_same_table(self):
        text = prompt_section(_project(leash="autonomous", rules={"land": "never"}))
        self.assertIn("set to `On its own`", text)
        self.assertIn("Never land", text)
        self.assertIn("you and your threads may do these alone: push or open a pull request", text)
        self.assertIn("never: land a branch into the project folder", text)


class GateTest(unittest.TestCase):
    def test_approved_once_inside_rm_and_filing(self):
        brain = SimpleNamespace(allowed_once=["git push origin feature"], on_allowed_used=None, on_approval=None, workdir="/w")
        used = []
        brain.on_allowed_used = used.append
        self.assertEqual(gate(brain, "Bash", {"command": "git push origin feature"}, "Bash(git push:*)"), (True, ""))
        self.assertEqual((brain.allowed_once, used), ([], ["git push origin feature"]))  # once
        self.assertIsNone(gate(brain, "Bash", {"command": "git push origin feature"}, "Bash(git push:*)"))  # asked as usual
        self.assertEqual(gate(brain, "Bash", {"command": "rm -rf build"}, "Bash(rm -rf *)"), (True, ""))
        self.assertIsNone(gate(brain, "Bash", {"command": "rm -rf build"}, "Bash(rm -rf:*)"))  # the user's own rule always asks
        filed = []
        brain.on_approval = lambda tool, args, rule: filed.append((tool, args, rule)) or "ab12"
        ok, message = gate(brain, "Bash", {"command": "git push"}, "Bash(git push:*)")
        self.assertFalse(ok)
        self.assertIn("approval ab12", message)
        self.assertEqual(filed, [("Bash", {"command": "git push"}, "Bash(git push:*)")])

    def test_claudes_worker_files_a_request_instead_of_giving_up(self):
        from vision.brain import Brain
        from vision.config import BrainConfig

        brain = Brain.__new__(Brain)
        brain.cfg = BrainConfig(ask_tools=["Bash(git push:*)"])
        brain.workdir, brain.task_mode, brain._turn_allowed, brain._plan_approved = "/w", True, set(), False
        brain.allowed_once, brain.on_allowed_used, brain.on_approval = [], None, None
        refused = brain._ask_to_run("Bash", {"command": "git push"}, None)
        self.assertIn("needs_input", refused["message"])  # a worker with nowhere to file: as before
        brain.on_approval = lambda *a: "cd34"
        filed = brain._ask_to_run("Bash", {"command": "git push"}, None)
        self.assertEqual(filed["behavior"], "deny")
        self.assertIn("approval cd34", filed["message"])
        brain.allowed_once.append("git push")
        self.assertEqual(brain._ask_to_run("Bash", {"command": "git push"}, None)["behavior"], "allow")


class ApprovalStoreTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            a, new = add_approval(folder, target="t1", tool="Bash", tool_input={"command": "git push"}, rule="Bash(git push:*)", title="Tray")
            self.assertTrue(new)
            self.assertEqual((a["action"], a["command"], a["state"]), ("publish", "git push", "pending"))
            again, new = add_approval(folder, target="t1", tool="Bash", tool_input={"command": "git push"}, rule="Bash(git push:*)")
            self.assertEqual((again["id"], new), (a["id"], False))  # the same call isn't asked twice
            done = decide_approval(folder, a["id"], True, "git push origin tray")
            self.assertEqual((done["state"], approved_call(done)), ("approved", "git push origin tray"))
            with self.assertRaises(ValueError):
                decide_approval(folder, a["id"], False)
            with self.assertRaises(KeyError):
                decide_approval(folder, "zzzz", True)
            b, _ = add_approval(folder, target="coordinator", tool="mcp__gmail__send_email", tool_input={"to": "x"}, rule="mcp__*__send*")
            with self.assertRaises(ValueError):
                decide_approval(folder, b["id"], True, "something else")  # only a shell command can be edited
            self.assertEqual([x["id"] for x in approvals(folder, pending_only=True)], [b["id"]])

    def test_a_decision_shows_as_a_note(self):
        self.assertEqual(unwrap_turn("[Approval] The user approved: git push\nRun exactly that now."), ("note", "The user approved: git push"))


class _Recorder(_Brain):
    prompts: list = []

    def ask(self, prompt, **_):
        _Recorder.prompts.append(prompt)
        self.session_id = "s1"
        return SimpleNamespace(text="Pushed it.", session_id="s1", model="m", error="", is_error=False)

    def take_untaken_steers(self):
        return []


class ServerTest(unittest.TestCase):
    def setUp(self):
        from vision import server
        from vision.config import Config
        from vision.scheduled import Schedule

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        _Recorder.prompts = []
        for p in (patch.object(projects, "STORE_DIR", self.tmp / "projects"),
                  patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Recorder(cfg, **kw)),
                  patch("vision.link.LIVE_DIR", self.tmp / "live"),
                  patch("vision.agentlog.AGENTS_DIR", self.tmp / "agents"),
                  patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []),
                  patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain),
                  patch.object(server.Hub, "warm_up", lambda self: None),
                  patch("vision.server.warm_soon", lambda brain: None)):
            p.start()
            self.addCleanup(p.stop)
        cfg = Config()
        cfg.brain.ask_tools = ["Edit(.env)"]  # the user's own rule stays under the project's
        self.hub = server.Hub(cfg, _Recorder(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
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

    def test_rules_reach_the_coordinator_and_its_threads(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc:
            p = tc.post("/projects", json={"goal": "Ship it", "workdir": self.dir.name, "settings": {"leash": "autonomous"}},
                        headers=self.auth).json()
            self.assertEqual((p["leash"], p["settings"]["autonomy"]), ("autonomous", "free"))
            self.assertEqual({r["action"]: r["rule"] for r in p["rules"]}["publish"], "does")
            chat = self.hub.chats[tc.post(f"/projects/{p['id']}/open", headers=self.auth).json()["chat"]]
            self.assertIn("Bash(vision thread land:*)", chat.brain.cfg.ask_tools)
            self.assertIn("Edit(.env)", chat.brain.cfg.ask_tools)
            self.assertNotIn("Bash(git push:*)", chat.brain.cfg.ask_tools)
            r = tc.post("/projects", json={"id": p["id"], "settings": {"rules": {"publish": "never"}}}, headers=self.auth).json()
            self.assertEqual({x["action"]: (x["rule"], x["set"]) for x in r["rules"]}["publish"], ("never", True))
            self.assertIn("Bash(git push:*)", chat.brain.cfg.denied_tools)  # the open chat takes it at once
            ask, deny = self.hub.thread_rules(p["id"])
            self.assertIn("Edit(.env)", ask)
            self.assertNotIn("Bash(vision thread land:*)", ask)
            self.assertIn("Bash(git push:*)", deny)
            tc.post("/projects", json={"id": p["id"], "settings": {"leash": "on_call"}}, headers=self.auth)
            self.assertIn("set to `When asked`", chat.brain.project_prompt)
            self.assertIsNone(asyncio_run(self.hub, self.hub.checkin(p["id"], why="scheduled")))  # on call: no scheduled check-in

    def test_a_threads_request_round_trip(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc, tc.websocket_connect("/ws", headers=self.auth) as ws:
            ws.receive_json()
            p = tc.post("/projects", json={"goal": "Ship it", "workdir": self.dir.name, "scout": {"name": "Atlas"}}, headers=self.auth).json()
            pid = p["id"]
            folder = self.hub.projects.root / pid
            (folder / "threads.json").write_text(json.dumps([{"id": "t1", "title": "Tray fix", "state": "waiting_on_you",
                                                              "updated": time.time(), "workdir": self.dir.name}]))
            pt = self.hub.project_threads(self.hub.projects.get(pid))
            told = []
            pt.tell = lambda tid, text: told.append((tid, text)) or pt.store.get(tid)
            # The thread's worker hits a rule that asks first: it files a request.
            worker = pt._factory(pt.store.get("t1"))("opus", "high", [])
            self.assertIn("Bash(git push:*)", worker.cfg.ask_tools)
            aid = worker.on_approval("Bash", {"command": "git push origin tray"}, "Bash(git push:*)")
            frame = self._until(ws, lambda ev: ev["type"] == "approval")
            self.assertEqual((frame["approval"]["id"], frame["approval"]["title"], frame["approval"]["action"]), (aid, "Tray fix", "publish"))
            view = tc.get(f"/projects/{pid}", headers=self.auth).json()
            self.assertEqual(([a["id"] for a in view["approvals"]], view["mood"]), ([aid], "needs_you"))
            self.assertEqual(self.pushed, [("Atlas asks to run", "git push origin tray")])
            # Approved, as edited: let through once in the live worker and the next one, and the thread is told.
            r = tc.post(f"/projects/{pid}/approvals/{aid}", json={"decision": "approve", "command": "git push -u origin tray"}, headers=self.auth)
            self.assertEqual(r.json()["state"], "approved", r.text)
            self.assertEqual(worker.allowed_once, ["git push -u origin tray"])
            self.assertEqual(pt.store.get("t1")["allowed"], ["git push -u origin tray"])
            self.assertIn("The user approved: git push -u origin tray (as they edited it)", told[-1][1])
            self.assertEqual(tc.post(f"/projects/{pid}/approvals/{aid}", json={"decision": "deny"}, headers=self.auth).status_code, 409)
            self.assertEqual(tc.get(f"/projects/{pid}/approvals", headers=self.auth).json(), [])
            kinds = [e["kind"] for e in tc.get(f"/projects/{pid}/activity", headers=self.auth).json()["events"]]
            self.assertEqual(kinds[:2], ["approved", "approval"])
            # Denied while it waits on the answer: it's told so it doesn't hang.
            aid2 = worker.on_approval("Bash", {"command": "sudo dnf install x"}, "Bash(sudo:*)")
            tc.post(f"/projects/{pid}/approvals/{aid2}", json={"decision": "deny"}, headers=self.auth)
            self.assertIn("The user declined: sudo dnf install x", told[-1][1])

    def test_a_checkin_files_a_request_and_the_coordinator_is_told(self):
        from fastapi.testclient import TestClient

        with TestClient(self.server.create_app(self.hub)) as tc:
            p = tc.post("/projects", json={"goal": "Ship it", "workdir": self.dir.name}, headers=self.auth).json()
            chat = self.hub.chats[tc.post(f"/projects/{p['id']}/open", headers=self.auth).json()["chat"]]
            aid = self.hub.request_approval(p["id"], "coordinator", "Bash", {"command": "vision thread land t1"}, "Bash(vision thread land:*)")
            r = tc.post(f"/projects/{p['id']}/approvals/{aid}", json={"decision": "approve"}, headers=self.auth)
            self.assertEqual(r.status_code, 200, r.text)
            for _ in range(50):
                if any("[Approval] The user approved: vision thread land t1" in x for x in _Recorder.prompts):
                    break
                time.sleep(0.05)
            self.assertTrue(any("[Approval] The user approved: vision thread land t1" in x for x in _Recorder.prompts))
            self.assertEqual(chat.brain.allowed_once, ["vision thread land t1"])
            rows = tc.get("/history", params={"chat": chat.id}, headers=self.auth).json()
            self.assertEqual(rows[0], {"role": "note", "text": "The user approved: vision thread land t1"})


class PhoneTest(unittest.TestCase):
    """The server side of buttons on a push, and scouts in the widgets' answer."""

    def test_a_push_carries_its_buttons_sealed(self):
        from vision import push

        msg = push.message("Atlas asks to run", "git push", "c1", project="p1", thread="t1", category="approval", approval="ab12")
        self.assertEqual((msg["category"], msg["approval"], msg["thread"]), ("approval", "ab12", "t1"))
        self.assertNotIn("category", push.message("t", "b", "c1", project="p1"))
        self.assertNotIn("category", push.message("t", "b", "c1", category="land"))  # buttons only on a project's news

    def test_requests_and_ready_threads_push_with_buttons(self):
        from vision import server
        from vision.config import Config

        with tempfile.TemporaryDirectory() as d, patch.object(projects, "STORE_DIR", Path(d) / "projects"), \
                patch.object(server.Hub, "warm_up", lambda self: None), patch("vision.link.LIVE_DIR", Path(d) / "live"):
            cfg = Config()
            hub = server.Hub(cfg, _Recorder(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
            sent = []
            hub.push = lambda *a, **kw: sent.append(kw)
            p = hub.projects.upsert({"goal": "g", "workdirs": [d]})
            aid = hub.request_approval(p["id"], "coordinator", "Bash", {"command": "git push"}, "Bash(git push:*)")
            self.assertEqual((sent[-1]["category"], sent[-1]["approval"]), ("approval", aid))
            hub.report_soon = lambda *a, **kw: None
            hub.thread_stopped(p["id"], {"id": "t1", "title": "T", "state": "ready_for_review", "branch": "vision/x/t1",
                                         "last_result": {"summary": "done"}})
            hub.projects.upsert({"id": p["id"], "settings": {"notify": "all"}})
            hub.thread_stopped(p["id"], {"id": "t1", "title": "T", "state": "ready_for_review", "branch": "vision/x/t1",
                                         "last_result": {"summary": "done"}})
            self.assertEqual(sent[-1]["category"], "land")
            hub.thread_stopped(p["id"], {"id": "t2", "title": "T2", "state": "idle", "last_result": {"summary": "looked"}})
            self.assertNotIn("category", sent[-1])

            scouts = hub._glance_scouts()
            self.assertEqual(len(scouts), 1)
            s = scouts[0]
            self.assertEqual((s["id"], s["needs"], s["line"], s["mood"]), (p["id"], 1, "1 needs you", "needs_you"))
            self.assertEqual(set(s), {"id", "name", "hue", "trait", "mood", "needs", "line", "next_checkin"})


def asyncio_run(hub, coro):
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, hub.loop).result(10)


if __name__ == "__main__":
    unittest.main()
