import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import _nopush
from vision import push
from vision.config import PushConfig

try:
    import cryptography  # noqa: F401
except ImportError:
    cryptography = None


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "push_devices.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_register_replaces_the_same_token_and_checks_input(self):
        first, second = _key(), _key()
        push.register({"token": "AB12", "key": first, "name": "phone"}, self.path)
        push.register({"token": "ab12", "key": second, "sandbox": True}, self.path)
        self.assertEqual([(d["token"], d["sandbox"], d["key"]) for d in push.devices(self.path)], [("ab12", True, second)])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)  # it holds the sealing keys
        for bad in ({"token": "zz", "key": first}, {"token": "", "key": first}, {"token": "ab"},
                    {"token": "ab", "key": "c2hvcnQ="}, {"token": "ab", "key": "not base64!"}):
            with self.assertRaises(ValueError):
                push.register(bad, self.path)
        self.assertTrue(push.unregister("AB12", self.path))
        self.assertFalse(push.unregister("ab12", self.path))

    def test_message_trims_a_long_reply(self):
        msg = push.message("Build check", "word " * 400, chat="c1")
        self.assertLessEqual(len(msg["body"]), push.BODY_MAX)
        self.assertTrue(msg["body"].endswith("…"))
        self.assertEqual((msg["title"], msg["chat"]), ("Build check", "c1"))

    def test_a_project_thread_rides_in_the_message(self):
        self.assertNotIn("project", push.message("T", "b", chat="c1"))
        msg = push.message("Calendar", "“Tests” needs you", chat="c1", project="p1", thread="t1")
        self.assertEqual((msg["project"], msg["thread"]), ("p1", "t1"))  # a tap opens that thread
        self.assertNotIn("thread", push.message("Calendar", "b", thread="t1"))  # a thread only with its project


@unittest.skipIf(cryptography is None, "cryptography not installed")
class SendTests(unittest.TestCase):
    def setUp(self):
        real = patch.object(push, "send", _nopush.REAL_SEND)  # the rest of the suite stubs it out
        real.start()
        self.addCleanup(real.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "push_devices.json"
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        (Path(self.dir.name) / "apns.p8").write_bytes(key)
        self.cfg = PushConfig(team_id="TEAM123456", key_id="KEY1234567", key_file=str(Path(self.dir.name) / "apns.p8"),
                              topic="com.example.VisionRemote")

    def tearDown(self):
        self.dir.cleanup()

    def test_apple_only_ever_sees_sealed_text(self):
        key = _key()
        push.register({"token": "aa", "key": key}, self.path)
        seen = []

        def post(cfg, token, sandbox, data, timeout):
            seen.append((token, sandbox, json.loads(data)))
            return 200, ""

        self.assertEqual(push.send(self.cfg, "Reminder", "call the dentist", "c1", path=self.path, post=post), [])
        token, sandbox, body = seen[0]
        self.assertEqual((token, sandbox, sorted(body)), ("aa", False, ["aps", "enc"]))
        self.assertNotIn("dentist", json.dumps(body))
        self.assertEqual(push.unseal(key, body["enc"]), {"title": "Reminder", "body": "call the dentist", "chat": "c1"})
        raw = base64.b64decode(body["enc"])
        self.assertGreater(len(raw), 12 + 16)  # nonce, ciphertext, tag: CryptoKit's combined layout
        with self.assertRaises(Exception):
            push.unseal(_key(), body["enc"])  # another phone's key opens nothing

    def test_the_provider_token_is_signed_and_reused(self):
        push._jwt = None
        first = push.make_token(self.cfg, now=1000)
        header = json.loads(base64.urlsafe_b64decode(first.split(".")[0] + "=="))
        self.assertEqual(header, {"alg": "ES256", "kid": "KEY1234567"})
        self.assertEqual(push.make_token(self.cfg, now=1000 + 60), first)
        self.assertNotEqual(push.make_token(self.cfg, now=1000 + push.TOKEN_LIFETIME_S), first)

    def test_a_dead_token_is_dropped_and_reported(self):
        push.register({"token": "aa", "key": _key(), "name": "old"}, self.path)
        push.register({"token": "bb", "key": _key(), "name": "new"}, self.path)
        post = lambda cfg, token, sandbox, data, timeout: (410, "Unregistered") if token == "aa" else (200, "")  # noqa: E731
        self.assertEqual(push.send(self.cfg, "Reminder", "tea", path=self.path, post=post), ["old: 410 Unregistered"])
        self.assertEqual([d["token"] for d in push.devices(self.path)], ["bb"])

    def test_a_token_from_the_other_environment_is_retried_there_and_remembered(self):
        push.register({"token": "aa", "key": _key()}, self.path)
        seen = []

        def post(cfg, token, sandbox, data, timeout):
            seen.append(sandbox)
            return (200, "") if sandbox else (400, "BadDeviceToken")

        self.assertEqual(push.send(self.cfg, "Reminder", "tea", path=self.path, post=post), [])
        self.assertEqual(seen, [False, True])
        self.assertTrue(push.devices(self.path)[0]["sandbox"])

    def test_apple_out_of_reach_is_reported_per_phone(self):
        push.register({"token": "aa", "key": _key(), "name": "phone"}, self.path)

        def post(cfg, token, sandbox, data, timeout):
            raise push.PushError("could not reach api.push.apple.com: timed out")

        self.assertEqual(push.send(self.cfg, "x", path=self.path, post=post),
                         ["phone: could not reach api.push.apple.com: timed out"])
        self.assertEqual(len(push.devices(self.path)), 1)  # not its fault: it stays

    def test_off_raises_and_no_phones_is_quiet(self):
        from dataclasses import replace

        self.assertEqual(push.send(self.cfg, "x", path=self.path, post=lambda *a: (200, "")), [])
        bare = replace(self.cfg, relay="")
        for off in (replace(self.cfg, enabled=False), replace(bare, key_id=""), replace(bare, topic=""),
                    replace(bare, key_file=str(Path(self.dir.name) / "missing.p8"))):
            self.assertFalse(push.ready(off))
            with self.assertRaises(push.PushError):
                push.send(off, "x", path=self.path)

    def test_without_a_key_of_ones_own_the_relay_carries_it_sealed(self):
        from dataclasses import replace

        cfg = replace(self.cfg, key_id="", relay="https://relay.example")
        self.assertTrue(push.ready(cfg))
        self.assertFalse(push.direct(cfg))
        key = _key()
        push.register({"token": "aa", "key": key}, self.path)
        seen = []

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"status": 200, "reason": ""}'

        def urlopen(req, timeout):
            seen.append((req.full_url, json.loads(req.data)))
            return Answer()

        with patch.object(push.urllib.request, "urlopen", urlopen):
            self.assertEqual(push.send(cfg, "Reminder", "call the dentist", "c1", path=self.path), [])
            push._post(cfg, "bb", True, '{"aps": {"event": "end"}}', 5, kind="liveactivity")
        url, body = seen[0]
        self.assertEqual((url, body["token"], body["kind"], body["sandbox"]), ("https://relay.example/push", "aa", "alert", False))
        self.assertNotIn("dentist", json.dumps(body))
        self.assertEqual(push.unseal(key, body["payload"]["enc"])["body"], "call the dentist")
        self.assertEqual(seen[1][1], {"token": "bb", "sandbox": True, "kind": "liveactivity",
                                      "payload": {"aps": {"event": "end"}}})

    def test_a_key_of_ones_own_goes_straight_to_apple(self):
        self.assertTrue(push.direct(self.cfg))
        with patch.object(push, "_post_relay") as relay, patch.object(push, "make_token", return_value="jwt"), \
                patch("subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "\n200", ""
            self.assertEqual(push._post(self.cfg, "aa", False, "{}", 5), (200, ""))
        relay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
