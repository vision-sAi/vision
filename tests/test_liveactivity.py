from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vision import liveactivity as la
from vision import push
from vision.config import PushConfig


def thread(title, state, updated=0):
    return {"title": title, "state": state, "updated": updated}


class SummaryTests(unittest.TestCase):
    def test_the_card_names_threads_needing_you_first_then_working_ones_and_counts_them_all(self):
        state = la.summary([thread("Docs", "working", 1), thread("Storage", "waiting_on_you", 2), thread("Old", "idle"),
                            thread("Login", "ready_for_review", 3), thread("Merge", "landing", 4), thread("Gone", "resolved")])
        self.assertEqual([r["title"] for r in state["rows"]], ["Storage", "Docs", "Merge", "Login"])
        self.assertEqual((state["working"], state["needsYou"], state["ready"]), (2, 1, 1))
        self.assertTrue(la.active(state))

    def test_only_a_few_names_fit_and_long_ones_are_cut(self):
        state = la.summary([thread("x" * 200, "working", i) for i in range(9)])
        self.assertEqual(len(state["rows"]), la.ROWS)
        self.assertEqual(len(state["rows"][0]["title"]), la.TITLE_MAX)
        self.assertEqual(state["working"], 9)

    def test_nothing_running_wants_no_card(self):
        self.assertFalse(la.active(la.summary([thread("Login", "ready_for_review")])))
        self.assertFalse(la.active(None))

    def test_the_headline(self):
        self.assertEqual(la.headline({"working": 2, "needsYou": 1, "ready": 0}), "2 threads working · 1 needs you")
        self.assertEqual(la.headline({"working": 0, "needsYou": 0, "ready": 1}), "All done · 1 ready to review")


class PayloadTests(unittest.TestCase):
    STATE = {"rows": [{"title": "Docs", "state": "working"}], "working": 1, "needsYou": 0, "ready": 0}

    def test_a_start_names_the_cards_type_and_says_what_is_going_on(self):
        aps = la.payload("start", self.STATE, now=100)["aps"]
        self.assertEqual((aps["event"], aps["timestamp"], aps["attributes-type"], aps["attributes"]),
                         ("start", 100, "VisionThreadsAttributes", {}))
        self.assertEqual(aps["content-state"], {"rows": [], "working": 1, "needsYou": 0, "ready": 0})
        self.assertEqual(aps["alert"]["body"], "1 thread working")

    def test_the_names_travel_sealed_for_the_phone_or_not_at_all(self):
        key = base64.b64encode(os.urandom(32)).decode()
        state = la.payload("update", self.STATE, key=key)["aps"]["content-state"]
        self.assertEqual((state["rows"], state["working"]), ([], 1))
        self.assertNotIn("Docs", json.dumps(state))
        self.assertEqual(push.unseal(key, state["enc"]), self.STATE["rows"])
        self.assertNotIn("enc", la.payload("update", self.STATE)["aps"]["content-state"])  # no key: counts alone

    def test_an_update_is_quiet_unless_something_needs_you_and_an_end_lingers(self):
        self.assertNotIn("alert", la.payload("update", self.STATE)["aps"])
        waiting = dict(self.STATE, needsYou=1)
        self.assertEqual(la.payload("update", waiting)["aps"]["alert"]["body"], "1 thread working · 1 needs you")
        end = la.payload("end", dict(self.STATE, working=0), now=100)["aps"]
        self.assertEqual(end["dismissal-date"], 100 + la.LINGER_S)


class TokenTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "live.json"

    def test_tokens_are_kept_by_kind_and_a_repeat_replaces_itself(self):
        la.register({"kind": "start", "token": "AB12", "sandbox": True}, self.path)
        la.register({"kind": "card", "token": "cd34"}, self.path)
        la.register({"kind": "card", "token": "cd34"}, self.path)
        data = la.tokens(self.path)
        self.assertEqual([(t["token"], t["sandbox"]) for t in data["start"]], [("ab12", True)])
        self.assertEqual([t["token"] for t in data["cards"]], ["cd34"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_token_keeps_its_phones_key(self):
        key = base64.b64encode(os.urandom(32)).decode()
        la.register({"kind": "card", "token": "cd34", "key": key}, self.path)
        self.assertEqual(la.tokens(self.path)["cards"][0]["key"], key)

    def test_bad_tokens_are_refused(self):
        for body in ({"kind": "other", "token": "ab"}, {"kind": "start", "token": "not hex"}, {"kind": "card"},
                     {"kind": "card", "token": "ab", "key": "c2hvcnQ="}):
            with self.assertRaises(ValueError):
                la.register(body, self.path)


class FeedTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "live.json"
        self.sent: list[tuple[str, dict]] = []
        self.answer = (200, "")

        def post(cfg, token, sandbox, data, timeout, kind="alert"):
            self.assertEqual(kind, "liveactivity")
            self.sent.append((token, json.loads(data)["aps"]))
            return self.answer

        self.feed = la.Feed(lambda: PushConfig(), path=self.path, post=post)
        self.ready = patch.object(push, "ready", return_value=True)
        self.ready.start()
        self.addCleanup(self.ready.stop)

        class InStep:  # the feed's background send, run before changed() returns
            def __init__(self, target, args=(), **_):
                self.run = lambda: target(*args)

            def start(self):
                self.run()

        steps = patch.object(la.threading, "Thread", InStep)
        steps.start()
        self.addCleanup(steps.stop)

    def change(self, threads):
        return self.feed.changed(threads)

    def test_a_thread_starting_starts_the_card_through_the_start_token_then_updates_and_ends_it(self):
        la.register({"kind": "start", "token": "aa"}, self.path)
        self.change([thread("Docs", "working")])
        self.assertEqual(self.sent[-1][0], "aa")
        self.assertEqual(self.sent[-1][1]["event"], "start")

        la.register({"kind": "card", "token": "bb"}, self.path)  # the card came up and handed over its own token
        self.change([thread("Docs", "waiting_on_you")])
        self.assertEqual((self.sent[-1][0], self.sent[-1][1]["event"]), ("bb", "update"))

        self.change([thread("Docs", "ready_for_review")])
        self.assertEqual((self.sent[-1][0], self.sent[-1][1]["event"]), ("bb", "end"))
        self.assertEqual(la.tokens(self.path)["cards"], [], "an ended card's token is spent")

    def test_the_same_card_twice_says_nothing_and_a_card_already_up_is_updated_not_started_again(self):
        la.register({"kind": "start", "token": "aa"}, self.path)
        la.register({"kind": "card", "token": "bb"}, self.path)  # the open app started one itself
        self.assertIsNotNone(self.change([thread("Docs", "working")]))
        self.assertEqual(self.sent[-1], ("bb", self.sent[-1][1]))
        self.assertEqual(self.sent[-1][1]["event"], "update")
        n = len(self.sent)
        self.assertIsNone(self.feed.changed([thread("Docs", "working")]))
        self.assertEqual(len(self.sent), n)

    def test_nothing_running_pushes_nothing_and_a_dead_token_is_dropped(self):
        la.register({"kind": "start", "token": "aa"}, self.path)
        self.change([thread("Login", "ready_for_review")])
        self.assertEqual(self.sent, [])
        self.answer = (410, "Unregistered")
        self.change([thread("Docs", "working")])
        self.assertEqual(la.tokens(self.path)["start"], [])

    def test_each_phone_gets_the_names_sealed_with_its_own_key(self):
        mine, theirs = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))
        la.register({"kind": "card", "token": "aa", "key": mine}, self.path)
        la.register({"kind": "card", "token": "bb", "key": theirs}, self.path)
        self.change([thread("Docs", "working")])
        sent = {token: aps["content-state"] for token, aps in self.sent}
        self.assertEqual(push.unseal(mine, sent["aa"]["enc"]), [{"title": "Docs", "state": "working"}])
        self.assertEqual(push.unseal(theirs, sent["bb"]["enc"]), [{"title": "Docs", "state": "working"}])
        self.assertNotEqual(sent["aa"]["enc"], sent["bb"]["enc"])

    def test_without_push_set_up_nothing_is_sent(self):
        self.ready.stop()
        with patch.object(push, "ready", return_value=False):
            la.register({"kind": "start", "token": "aa"}, self.path)
            self.feed.push("start", la.summary([thread("Docs", "working")]))
        self.assertEqual(self.sent, [])
        self.ready.start()


class PostTests(unittest.TestCase):
    def test_a_card_change_goes_on_the_live_activity_topic(self):
        cfg = PushConfig(team_id="T", key_id="K", topic="com.example.app")
        mine = patch.object(push, "direct", return_value=True)  # a key of one's own, not the relay
        mine.start()
        self.addCleanup(mine.stop)
        with patch.object(push, "make_token", return_value="jwt"), patch("subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "\n200", ""
            push._post(cfg, "aa", False, "{}", 5, kind="liveactivity")
        conf = run.call_args.kwargs["input"]
        self.assertIn('apns-topic: com.example.app.push-type.liveactivity"', conf)
        self.assertIn('apns-push-type: liveactivity"', conf)
        with patch.object(push, "make_token", return_value="jwt"), patch("subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "\n200", ""
            push._post(cfg, "aa", False, "{}", 5)
        self.assertIn('apns-topic: com.example.app"', run.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
