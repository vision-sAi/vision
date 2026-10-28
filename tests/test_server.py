from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from unittest.mock import patch

from vision import planvoice, server


class AuthTests(unittest.TestCase):
    """The token is read from the Authorization header and nowhere else, and nothing describes the API
    to a caller, with or without it."""

    def _client(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "tok", log=lambda _: None, clients=set(), phones=set(),
                              hello=lambda: {"type": "hello", "chats": []}, status=lambda: {"rows": [], "problems": []})
        return TestClient(server.create_app(hub))

    def test_a_token_in_the_url_is_not_accepted(self):
        client = self._client()
        ok = client.get("/health", headers={"Authorization": "Bearer tok"})
        self.assertEqual((ok.status_code, ok.json()["status"]), (200, {"rows": [], "problems": []}))
        self.assertEqual(ok.json()["phones"], 0)
        self.assertEqual(client.get("/health", params={"token": "tok"}).status_code, 401)
        self.assertEqual(client.get("/health").status_code, 401)

    def test_the_socket_takes_the_token_from_the_header_only(self):
        from starlette.websockets import WebSocketDisconnect

        client = self._client()
        auth = {"Authorization": "Bearer tok"}
        with client.websocket_connect("/ws", headers=auth) as ws:
            self.assertEqual(ws.receive_json()["type"], "hello")
            self.assertEqual(client.get("/health", headers=auth).json()["phones"], 1)  # the app is on: pairing worked
        with client.websocket_connect("/ws?client=terminal", headers=auth) as ws:
            ws.receive_json()
            self.assertEqual(client.get("/health", headers=auth).json()["phones"], 0)  # a terminal is not a phone
        with client.websocket_connect("/ws?token=tok") as ws:
            with self.assertRaises(WebSocketDisconnect) as closed:
                ws.receive_json()
            self.assertEqual(closed.exception.code, 4401)

    def test_a_reply_is_cut_into_pieces_to_read_aloud_without_the_voice(self):
        client = self._client()  # the hub has no speaker at all: the pieces need no GPU
        auth = {"Authorization": "Bearer tok"}
        r = client.post("/speak/pieces", json={"text": "Here you go.\n\n```sh\nls\n```"}, headers=auth)
        self.assertEqual(r.json(), {"pieces": ["Here you go. Code skipped."]})
        self.assertEqual(client.post("/speak/pieces", json={"text": "hi"}).status_code, 401)

    def test_the_api_description_is_not_served(self):
        client = self._client()
        for path in ("/openapi.json", "/docs", "/redoc"):
            for headers in ({}, {"Authorization": "Bearer tok"}):
                self.assertEqual(client.get(path, headers=headers).status_code, 404, path)


class UploadTests(unittest.TestCase):
    def test_uploaded_image_reaches_the_message_as_a_readable_file(self):
        from pathlib import Path
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "upload-test", log=lambda _: None)
        client = TestClient(server.create_app(hub))
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "UPLOAD_DIR", Path(directory)):
            payload = b"test image bytes"
            response = client.post("/upload", files={"file": ("photo.jpg", payload, "image/jpeg")},
                                   headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(response.status_code, 200)
            path = response.json()["path"]
            self.assertEqual(Path(path).read_bytes(), payload)
            self.assertIn(f"[Attached image: {path}]", server.with_attachments("Look at this", [path]))
            self.assertIn("Look at the attached image", server.with_attachments("", [path]))

    def test_reply_image_is_served_from_home_or_tmp_only(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "image-test", log=lambda _: None)
        client = TestClient(server.create_app(hub))
        auth = {"Authorization": "Bearer image-test"}
        with tempfile.NamedTemporaryFile(suffix=".png", dir="/tmp") as f:
            f.write(b"png bytes")
            f.flush()
            ok = client.get("/image", params={"path": f.name}, headers=auth)
            self.assertEqual((ok.status_code, ok.content, ok.headers["content-type"]), (200, b"png bytes", "image/png"))
            self.assertEqual(client.get("/image", params={"path": f.name}).status_code, 401)
        with tempfile.NamedTemporaryFile(suffix=".txt", dir="/tmp") as f:
            self.assertEqual(client.get("/image", params={"path": f.name}, headers=auth).status_code, 404)
        self.assertEqual(client.get("/image", params={"path": "/tmp/../etc/passwd"}, headers=auth).status_code, 404)
        self.assertFalse(server.image_path_allowed(__import__("pathlib").Path("/usr/share/pixmaps/x.png")))
        # The user's own temp folder counts too: on macOS that is /private/var/folders/…, not /tmp.
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(server.image_path_allowed(__import__("pathlib").Path(d).resolve() / "shot.png"))
        self.assertFalse(server.image_path_allowed(__import__("pathlib").Path("/private/var/db/x.png")))

    def test_uploaded_file_keeps_its_name_and_reaches_the_brain(self):
        from pathlib import Path
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "upload-test", log=lambda _: None)
        client = TestClient(server.create_app(hub))
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "UPLOAD_DIR", Path(directory)):
            payload = b"%PDF-1.4 test"
            response = client.post("/upload", files={"file": ("Q3 report (final).pdf", payload, "application/pdf")},
                                   headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(response.status_code, 200, response.text)
            path = Path(response.json()["path"])
            self.assertEqual(path.parent, Path(directory))
            self.assertTrue(path.name.endswith("-Q3 report _final_.pdf"), path.name)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(server.with_attachments("", [str(path)]), f"[Attached file: {path}]\n\nRead the attached file(s).")
            image = Path(directory) / "x.jpg"
            image.write_bytes(b"jpg")
            note = server.with_attachments("", [str(image), str(path)])
            self.assertTrue(note.endswith("Look at the attached image(s).\nRead the attached file(s)."), note)
            escape = client.post("/upload", files={"file": ("../../evil.sh", b"echo", "text/plain")},
                                 headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(Path(escape.json()["path"]).parent, Path(directory))

    def test_failed_upload_returns_an_error_instead_of_a_path(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = SimpleNamespace(authorized=lambda token: token == "upload-test", log=lambda _: None)
        client = TestClient(server.create_app(hub))
        self.assertEqual(client.post("/upload", files={"file": ("photo.jpg", b"x")}).status_code, 401)
        response = client.post("/upload", files={"file": ("photo.jpg", b"", "image/jpeg")},
                               headers={"Authorization": "Bearer upload-test"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("path", response.json())


    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_uploaded_video_reaches_the_brain_as_frames_and_words(self):
        from pathlib import Path
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        heard = []
        hub = SimpleNamespace(authorized=lambda token: token == "upload-test", log=lambda _: None,
                              transcribe_bytes=lambda wav: heard.append(wav) or "hello from the clip")
        client = TestClient(server.create_app(hub))
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "UPLOAD_DIR", Path(directory)):
            clip = Path(directory) / "source.mp4"
            # Portrait, 12 s, a hard cut from red to blue at 6 s, with sound.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=360x640:d=6:r=10",
                            "-f", "lavfi", "-i", "color=blue:s=360x640:d=6:r=10", "-f", "lavfi", "-i", "sine=d=12",
                            "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2",
                            "-c:a", "aac", "-shortest", str(clip)], check=True)
            response = client.post("/upload", files={"file": ("video.mp4", clip.read_bytes(), "video/mp4")},
                                   headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(response.status_code, 200, response.text)
            path = response.json()["path"]
            note = server.with_attachments("", [path])
            self.assertIn(f"[Attached video: {path}]", note)
            frames = [line.split(": ", 1)[1].rstrip("]") for line in note.splitlines() if line.startswith("[Frame ")]
            self.assertGreaterEqual(len(frames), 2)
            self.assertLessEqual(len(frames), server.MAX_VIDEO_FRAMES)
            self.assertTrue(all(os.path.isfile(f) for f in frames))
            self.assertIn("[Frame 0:06:", note)  # the cut is a frame of its own
            self.assertIn("[Video audio, transcribed: hello from the clip]", note)
            self.assertTrue(heard and heard[0][:4] == b"RIFF")
            self.assertTrue(note.endswith("Watch the attached video(s) through their frames."))
            thumb = client.get(f"/uploads/{Path(path).name}", headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(thumb.headers["content-type"], "image/jpeg")
            auth = {"Authorization": "Bearer upload-test"}
            whole = client.get(f"/videos/{Path(path).name}", headers=auth)
            self.assertEqual(whole.content, Path(path).read_bytes())
            part = client.get(f"/videos/{Path(path).name}", headers={**auth, "Range": "bytes=0-99"})
            self.assertEqual((part.status_code, len(part.content)), (206, 100))
            self.assertEqual(client.get(f"/videos/{Path(path).name}").status_code, 401)
            self.assertEqual(client.get(f"/videos/{Path(path).with_suffix('.json').name}", headers=auth).status_code, 404)

            broken = client.post("/upload", files={"file": ("video.mp4", b"not a video", "video/mp4")},
                                 headers={"Authorization": "Bearer upload-test"})
            self.assertEqual(broken.status_code, 422)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir() if p.name != "source.mp4"),
                             sorted([Path(path).name, Path(path).with_suffix(".json").name, Path(path).stem + "-frames"]))

class TokenTests(unittest.TestCase):
    def test_token_is_created_once_and_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "remote_token")
            with patch.object(server, "TOKEN_FILE", server.TOKEN_FILE.__class__(path)), patch.object(server, "CONFIG_DIR", server.CONFIG_DIR.__class__(d)):
                first = server.load_token()
                self.assertGreaterEqual(len(first), 30)
                self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
                self.assertEqual(server.load_token(), first)
                self.assertNotEqual(server.load_token(regenerate=True), first)


class PairingTests(unittest.TestCase):
    def test_payload_round_trips(self):
        payload = server.pairing_payload("https://machine.tail.ts.net", "abc")
        self.assertEqual(json.loads(payload), {"url": "https://machine.tail.ts.net", "token": "abc"})

    def test_a_tailscale_url_brings_the_wifi_address_along(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(remote=SimpleNamespace(host="0.0.0.0", port=8765, public_url=""))
        with patch.object(server, "load_token", return_value="abc"), patch.object(server, "lan_ip", return_value="10.0.1.5"), \
                patch.object(server, "machine_name", return_value=""):
            with patch.object(server, "tailscale_url", return_value="https://machine.tail.ts.net"):
                info = server.pairing_info(cfg, None, None, None)
            self.assertEqual(json.loads(info["payload"]),
                             {"url": "https://machine.tail.ts.net", "token": "abc", "local": "http://10.0.1.5:8765"})
            with patch.object(server, "tailscale_url", return_value=None):  # Wi-Fi only: nothing to fall back to
                info = server.pairing_info(cfg, None, None, None)
            self.assertEqual(json.loads(info["payload"]), {"url": "http://10.0.1.5:8765", "token": "abc"})

    def test_the_payload_carries_the_computers_name_for_the_phones_list(self):
        payload = server.pairing_payload("https://machine.tail.ts.net", "abc", name="machine")
        self.assertEqual(json.loads(payload), {"url": "https://machine.tail.ts.net", "token": "abc", "name": "machine"})
        with patch("socket.gethostname", return_value="machine.lan"), patch.object(server, "load_token", return_value="abc"), \
                patch.object(server, "tailscale_url", return_value="https://machine.tail.ts.net"), patch.object(server, "lan_ip", return_value="192.0.2.7"):
            from vision.config import Config

            info = server.pairing_info(Config(), None, None, None)
        self.assertEqual(info["name"], "machine")
        self.assertEqual(json.loads(info["payload"]),  # the name and the Wi-Fi address travel together
                         {"url": "https://machine.tail.ts.net", "token": "abc", "local": "http://192.0.2.7:8765", "name": "machine"})

    def test_qr_is_square_ish_and_uses_half_blocks(self):
        lines = server.qr_lines(server.pairing_payload("https://example.ts.net", "x" * 32))
        self.assertTrue(lines)
        self.assertEqual(len({len(l) for l in lines}), 1)
        self.assertTrue(set("".join(lines)) <= set("█▀▄ "))


class HistoryTests(unittest.TestCase):
    def test_reads_user_and_assistant_text_and_merges_streamed_assistant_entries(self):
        from vision import sessions

        sid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "-home-x")
            os.makedirs(proj)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "hello"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Bash"}]}},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-stdout>x</local-command-stdout>"}},
                {"type": "user", "message": {"role": "user", "content": "You are taking over an ongoing conversation from another model. Its handoff note:\n\nnote\n\n---\n\nContinue naturally from here; do not mention the handoff unless asked. The user's next message:\n\nwhat next?"}},
            ]
            with open(os.path.join(proj, f"{sid}.jsonl"), "w") as f:
                f.write("\n".join(json.dumps(l) for l in lines) + "\n")
            with patch.object(sessions, "CLAUDE_PROJECTS", d):
                hist = server.claude_history(sid)
        self.assertEqual(hist, [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "hi\n\ndone"},
            {"role": "user", "text": "what next?"},
        ])

    def test_unknown_session_is_empty(self):
        self.assertEqual(server.claude_history("nope"), [])


class WavTests(unittest.TestCase):
    def test_wav_bytes_is_riff_pcm16_mono(self):
        import numpy as np

        data = server.wav_bytes(np.zeros(2400, dtype=np.float32))
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertIn(b"WAVE", data[:16])
        self.assertEqual(len(data), 44 + 2400 * 2)


if __name__ == "__main__":
    unittest.main()


class _Turn:
    def __init__(self, text, sid):
        self.text, self.session_id, self.model, self.error, self.is_error = text, sid, "stub", "", False


class _StubBrain:
    """Answers "echo: <text>" after a short pause, streaming one delta; independent per instance."""

    provider = "claude"
    workdir = "/tmp"

    def __init__(self, cfg, delay=0.2):
        self.cfg, self.delay = cfg, delay
        self.session_id = None
        self.calls = 0

    def resolved_model(self):
        return self.cfg.model

    def cancel(self):
        pass

    def ask(self, text, on_text=None, on_status=None, on_question=None, on_agent=None, on_tool=None):
        import time as _t

        self.calls += 1
        self.session_id = self.session_id or f"sess-{id(self) % 1000}"
        _t.sleep(self.delay)
        reply = f"echo: {text}"
        if on_text:
            on_text(reply)
        return _Turn(reply, self.session_id)


class WidgetTests(unittest.TestCase):
    """GET /widgets: what the phone's Home Screen widgets show, in one answer."""

    def hub(self, tasks=(), usage=None):
        from pathlib import Path
        from types import SimpleNamespace
        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
        threads = {"p1": [{"id": "a", "title": "Fix the flaky test", "state": "working", "updated": 1},
                          {"id": "b", "title": "Old idea", "state": "idle", "updated": 2}],
                   "p2": [{"id": "c", "title": "Pick a database", "state": "waiting_on_you", "updated": 3},
                          {"id": "d", "title": "Land the docs", "state": "ready_for_review", "updated": 4}]}
        hub.projects = SimpleNamespace(list=lambda: [{"id": "p1", "title": "Vision"}, {"id": "p2", "title": "Homelab"}],
                                       root=Path("/nonexistent"))
        hub.threads = {pid: SimpleNamespace(list=lambda ts=ts: ts, suspend=lambda: None) for pid, ts in threads.items()}
        hub.schedule = SimpleNamespace(list=lambda: list(tasks))
        self.usage_reads = 0

        def usage_all(cfg, brain):
            self.usage_reads += 1
            return usage if usage is not None else [
                {"provider": "claude", "label": "Claude", "error": None,
                 "windows": [{"label": "Current session", "used_percent": 9.0, "resets_at": 1792271940.0, "resets": "x"}]},
                {"provider": "grok", "label": "Grok", "error": "Grok usage unavailable: not signed in", "windows": []}]

        self.addCleanup(patch.stopall)
        patch("vision.usage.usage_all", usage_all).start()
        return hub

    def test_threads_usage_and_next_up(self):
        tasks = [{"id": "t1", "title": "Call the dentist", "prompt": "call the dentist", "kind": "remind",
                  "enabled": True, "next_run": 1792300000.0, "repeat": "once"},
                 {"id": "t2", "title": "Off", "prompt": "off", "enabled": False, "next_run": None}]
        g = self.hub(tasks).glance()
        self.assertEqual((g["threads"]["working"], g["threads"]["needsYou"], g["threads"]["ready"]), (1, 1, 1))
        self.assertEqual([(r["title"], r["project"], r["project_title"]) for r in g["threads"]["rows"]], [
            ("Pick a database", "p2", "Homelab"),       # waiting on you first, as the Lock Screen card has it
            ("Fix the flaky test", "p1", "Vision"),
            ("Land the docs", "p2", "Homelab"),
        ], "an idle thread is not shown")
        self.assertEqual(g["usage"], [{"provider": "claude", "label": "Claude",
                                       "windows": [{"label": "Current session", "used_percent": 9.0, "resets_at": 1792271940.0}]}],
                         "a provider that can't say is left out")
        self.assertEqual(g["next"], {"id": "t1", "title": "Call the dentist", "kind": "remind", "at": 1792300000.0, "repeat": "once"})
        self.assertEqual(g["upcoming"], [g["next"]], "a task that's off isn't coming up")

    def test_the_three_chats_last_spoken_in(self):
        from types import SimpleNamespace

        def chat(cid, title, updated, busy=False, reply=""):
            return SimpleNamespace(id=cid, title=title, updated=updated, busy=busy,
                                   history=lambda n: [{"role": "user", "text": "hi"}, {"role": "assistant", "text": reply}])

        hub = self.hub()
        hub.chats = {"a": chat("a", "Loop bug", 4, reply="**Fixed.** The `return` was inside the loop.\n\n```py\nfor x in y: pass\n```"),
                     "b": chat("b", "Dentist", 5, busy=True, reply="old"),
                     "c": chat("c", "", 3, reply="Hello."),
                     "d": chat("d", "Oldest", 1, reply="Bye.")}
        rows = hub.glance()["chats"]
        self.assertEqual([(r["chat"], r["title"], r["busy"]) for r in rows],
                         [("b", "Dentist", True), ("a", "Loop bug", False), ("c", "New chat", False)])
        self.assertEqual(rows[0]["last"], "", "a reply still coming has nothing to show yet")
        self.assertEqual(rows[1]["last"], "Fixed. The return was inside the loop. (code)", "plain words, code left out")

    def test_how_the_computer_is(self):
        hub = self.hub()
        hub.cfg.voice.enabled = False
        hub.update_how = "vision upgrade"
        c = hub.glance()["computer"]
        self.assertEqual((c["voice_on"], c["update"]), (False, "vision upgrade"))
        for key in ("cpu", "memory", "disk"):
            self.assertTrue(c[key] is None or 0 <= c[key] <= 1000, (key, c[key]))  # load per core can top 100
        self.assertIn("uptime", c)

    def test_usage_is_read_again_only_after_a_while(self):
        hub = self.hub()
        hub.glance(), hub.glance()
        self.assertEqual(self.usage_reads, 1, "a widget refresh doesn't run the providers' CLIs every time")
        hub._glance_usage = (time.time() - hub.GLANCE_USAGE_S - 1, [])
        hub.glance()
        self.assertEqual(self.usage_reads, 2)

    def test_nothing_scheduled_and_no_projects(self):
        from pathlib import Path
        from types import SimpleNamespace

        hub = self.hub()
        hub.projects = SimpleNamespace(list=lambda: [], root=Path("/nonexistent"))
        g = hub.glance()
        self.assertIsNone(g["next"])
        self.assertEqual(g["upcoming"], [])
        self.assertEqual(g["threads"], {"working": 0, "needsYou": 0, "ready": 0, "rows": [], "projects": 0})

    def test_served_with_the_token_only(self):
        from fastapi.testclient import TestClient

        hub = self.hub()
        with patch.object(server.Hub, "warm_up", lambda self: None), TestClient(server.create_app(hub)) as tc:
            self.assertEqual(tc.get("/widgets").status_code, 401)
            body = tc.get("/widgets", headers={"Authorization": "Bearer tok"}).json()
            self.assertEqual(body["threads"]["working"], 1)
            hub.projects.list = lambda: []  # hello lists each project in full, which these stand-ins can't
            self.assertTrue(tc.get("/health", headers={"Authorization": "Bearer tok"}).json()["widgets"])


class FocusTerminalTests(unittest.TestCase):
    """"Open on computer" for a chat that already has a window: Hyprland brings that window forward."""

    def _focus(self, clients, parents, pid=300):
        calls = []

        def run(argv, **kw):
            calls.append(argv[1:])
            out = json.dumps(clients) if argv[1] == "clients" else "ok"
            return subprocess.CompletedProcess(argv, 0, out, "")

        with patch.object(server.shutil, "which", lambda name: "/usr/bin/hyprctl"), \
             patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "sig"}), \
             patch.object(server.subprocess, "run", run), \
             patch.object(server, "_parent_pid", lambda p: parents.get(p, 0)):
            shown = server.focus_terminal(pid)
        return shown, [c for c in calls if c[0] == "dispatch"]

    def test_the_terminal_window_above_vision_is_focused(self):
        # vision 300 runs under `sh -c` 200, under the terminal 100 that owns the window
        clients = [{"pid": 100, "address": "0xa", "title": "Vision"}, {"pid": 900, "address": "0xb", "title": "Vision"}]
        shown, dispatched = self._focus(clients, {300: 200, 200: 100, 100: 1})
        self.assertTrue(shown)
        self.assertEqual(dispatched, [["dispatch", "focuswindow", "address:0xa"]])

    def test_a_single_instance_terminal_is_told_apart_by_vision_s_title(self):
        clients = [{"pid": 100, "address": "0xa", "title": "~/code"},
                   {"pid": 100, "address": "0xb", "title": "✳ Vision - Weather"},
                   {"pid": 100, "address": "0xc", "title": "◑ Vision setup"}]
        shown, dispatched = self._focus(clients, {300: 100})
        self.assertTrue(shown)
        self.assertEqual(dispatched, [["dispatch", "focuswindow", "address:0xb"]])

    def test_no_window_or_no_hyprland_does_nothing(self):
        shown, dispatched = self._focus([{"pid": 900, "address": "0xb", "title": "Vision"}], {300: 1})
        self.assertEqual((shown, dispatched), (False, []))
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": ""}):
            self.assertFalse(server.focus_terminal(os.getpid()))


class OpenTerminalTests(unittest.TestCase):
    """"Open on computer" from a server started outside the desktop session, as the background service is."""

    def _open(self, own: dict, systemd: str | None):
        """Open a terminal with `own` as the server's environment and `systemd` as what the user's
        systemd answers (None: there is no systemctl). Returns the environment the window was given."""
        opened = []

        def run(argv, **kw):
            self.assertEqual(argv[1:], ["--user", "show-environment"])
            return subprocess.CompletedProcess(argv, 0, systemd, "")

        def which(name):
            return None if name == "systemctl" and systemd is None else f"/usr/bin/{name}" if name in ("systemctl", "konsole") else None

        with patch.dict(os.environ, own, clear=True), patch.object(server.shutil, "which", which), \
                patch.object(server.sys, "platform", "linux"), patch.object(server.subprocess, "run", run), \
                patch.object(server.subprocess, "Popen", lambda argv, **kw: opened.append(kw["env"])):
            term = server.open_terminal("c1", "")
        self.assertEqual(term, "konsole")
        return opened[0]

    def test_a_server_inside_the_session_uses_its_own_display(self):
        env = self._open({"WAYLAND_DISPLAY": "wayland-9", "PATH": "/usr/bin"}, None)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-9")

    def test_a_server_started_before_the_desktop_takes_the_display_from_systemd(self):
        listing = "HOME=/home/me\nWAYLAND_DISPLAY=wayland-0\nDISPLAY=:0\nXDG_CURRENT_DESKTOP=KDE\nSECRET=x\nPATH=/somewhere/else\n"
        env = self._open({"PATH": "/usr/bin"}, listing)
        self.assertEqual((env["WAYLAND_DISPLAY"], env["DISPLAY"], env["XDG_CURRENT_DESKTOP"]), ("wayland-0", ":0", "KDE"))
        self.assertEqual(env["PATH"], "/usr/bin")  # only what a window needs is taken
        self.assertNotIn("SECRET", env)

    def test_with_no_desktop_anywhere_it_says_so(self):
        for systemd in ("HOME=/home/me\n", None):
            with self.assertRaisesRegex(RuntimeError, "no desktop session"):
                self._open({"PATH": "/usr/bin"}, systemd)


class MacOpenTerminalTests(unittest.TestCase):
    """"Open on computer" on a Mac: no DISPLAY there, a terminal app is driven over Apple Events."""

    def _open(self, workdir: str, iterm: bool = False, result=(0, "")):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, result[0], "", result[1])

        isdir = os.path.isdir
        with patch.object(server.sys, "platform", "darwin"), patch.object(server.subprocess, "run", run), \
                patch.object(server.shutil, "which", lambda name: "/opt/bin/vision" if name == "vision" else None), \
                patch.object(server.os.path, "isdir", lambda p: iterm if p.endswith("iTerm.app") else isdir(p)):
            term = server.open_terminal("c1", workdir)
        return term, calls

    def test_terminal_runs_the_join_in_the_chat_folder_without_a_display(self):
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "it's here")
            os.mkdir(folder)
            with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
                term, calls = self._open(folder)
        self.assertEqual(term, "Terminal")
        self.assertEqual(calls[0][:2], ["osascript", "-e"])
        self.assertIn('tell application "Terminal"', calls[0][2])
        self.assertEqual(calls[0][3], f"cd {shlex.quote(folder)} && /opt/bin/vision --join c1")

    def test_iterm_is_used_when_installed(self):
        term, calls = self._open("")
        self.assertEqual(term, "Terminal")
        term, calls = self._open("", iterm=True)
        self.assertEqual(term, "iTerm")
        self.assertIn('tell application "iTerm"', calls[0][2])

    def test_a_refused_automation_permission_says_where_to_allow_it(self):
        with self.assertRaisesRegex(RuntimeError, "Automation"):
            self._open("", result=(1, "execution error: Not authorized to send Apple events to Terminal. (-1743)"))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self._open("", result=(1, "boom"))


class MultiChatTests(unittest.TestCase):
    """Several chats run at the same time and every frame names its chat."""

    def _hub(self):
        from vision.config import Config

        cfg = Config()
        cfg.conversation.model = "haiku"
        brain = _StubBrain(cfg.brain)
        with patch.object(server, "load_token", return_value="tok"):
            hub = server.Hub(cfg, brain, "tok", log=lambda *_: None)
        return hub

    def _drain(self, ws, until, limit=60):
        seen = []
        for _ in range(limit):
            ev = ws.receive_json()
            seen.append(ev)
            if until(ev, seen):
                return seen
        raise AssertionError(f"never saw the frame; got {[e['type'] for e in seen]}")

    def test_startup_can_leave_the_phone_without_an_open_chat(self):
        from fastapi.testclient import TestClient
        from vision.config import Config

        cfg = Config()
        cfg.brain.model, cfg.brain.effort = "opus", "high"
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None,
                         follow_defaults=False, open_initial=False)
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _StubBrain(cfg)), \
             patch.object(server, "save_brain_defaults") as save:
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as ws:
                self.assertEqual(ws.receive_json()["chats"], [])
                auth = {"Authorization": "Bearer tok"}
                self.assertEqual(tc.get("/chats", headers=auth).json(), [])
                self.assertEqual(tc.get("/models", headers=auth).json()["current"], "opus")
                # A pick with no chat open: the catalogue answers for it, and only the next chat takes it.
                picked = tc.get("/models", headers=auth, params={"model": "sonnet", "effort": "low"}).json()
                self.assertEqual((picked["current"], picked["effort"]), ("sonnet", "low"))
                ws.send_json({"type": "new", "model": "sonnet", "effort": "low"})
                opened = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]
                self.assertEqual((hub.chats[opened["chat"]].brain.cfg.model, hub.chats[opened["chat"]].brain.cfg.effort), ("sonnet", "low"))
                ws.send_json({"type": "new"})
                opened = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]
                self.assertEqual(hub.chats[opened["chat"]].brain.cfg.model, "opus")
                save.assert_not_called()

    def test_a_reply_says_how_long_it_took_and_how_long_it_has_been_going(self):
        from fastapi.testclient import TestClient

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _StubBrain(cfg, delay=0.3)), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                chat = ws.receive_json()["chats"][0]["chat"]
                self.assertEqual(tc.get("/chats", headers=auth).json()[0]["elapsed_ms"], 0)  # idle: nothing to count
                ws.send_json({"type": "message", "chat": chat, "text": "one"})
                self._drain(ws, lambda ev, _: ev["type"] == "start")
                time.sleep(0.1)
                running = tc.get("/chats", headers=auth).json()[0]  # what a phone opening mid-reply is told
                self.assertTrue(running["busy"])
                self.assertGreaterEqual(running["elapsed_ms"], 100)
                done = self._drain(ws, lambda ev, _: ev["type"] == "done")[-1]
                self.assertGreaterEqual(done["duration_ms"], running["elapsed_ms"])  # the same clock, read later
                history = tc.get("/history", params={"chat": chat}, headers=auth).json()
                self.assertEqual([m["role"] for m in history], ["user", "assistant"])
                self.assertEqual(history[-1]["duration_ms"], done["duration_ms"])  # the footer survives a refetch
                self.assertNotIn("duration_ms", history[0])
                self.assertEqual(tc.get("/chats", headers=auth).json()[0]["elapsed_ms"], 0)

    def test_the_phone_switches_a_chat_between_auto_and_plan(self):
        from fastapi.testclient import TestClient

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _StubBrain(cfg, delay=0.3)), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                hello = ws.receive_json()["chats"][0]
                chat = hello["chat"]
                self.assertEqual(hello["mode"], "auto")

                ws.send_json({"type": "mode", "chat": chat, "mode": "plan"})
                changed = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("mode") == "plan")[-1]
                self.assertEqual(changed["chat"], chat)
                self.assertEqual(hub.chats[chat].brain.cfg.mode, "plan")  # the next turn runs read-only
                self.assertEqual(tc.get("/chats", headers=auth).json()[0]["mode"], "plan")

                ws.send_json({"type": "mode", "chat": chat, "mode": "yolo"})
                self.assertEqual(self._drain(ws, lambda ev, _: ev["type"] == "error")[-1]["text"], "The mode is auto or plan.")

                ws.send_json({"type": "message", "chat": chat, "text": "one"})
                self._drain(ws, lambda ev, _: ev["type"] == "start")
                ws.send_json({"type": "mode", "chat": chat, "mode": "auto"})  # mid-reply: refused, as a model switch is
                seen = self._drain(ws, lambda ev, _: ev["type"] == "done")
                self.assertIn("switch modes once it has finished", " ".join(e.get("text", "") for e in seen if e["type"] == "error"))
                self.assertEqual(hub.chats[chat].brain.cfg.mode, "plan")

    def test_an_approved_plan_shows_auto_before_the_turn_ends(self):
        from fastapi.testclient import TestClient
        from vision.config import Config

        class Approving(_StubBrain):
            def ask(self, text, **kw):
                self.cfg.mode = "auto"  # as Brain._answer_control does on "Yes"
                self.on_mode_change("auto")
                return super().ask(text, **kw)

        cfg = Config()
        cfg.conversation.model = "haiku"
        with patch.object(server, "load_token", return_value="tok"):
            hub = server.Hub(cfg, Approving(cfg.brain, delay=0.3), "tok", log=lambda *_: None)
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                chat = ws.receive_json()["chats"][0]["chat"]
                ws.send_json({"type": "mode", "chat": chat, "mode": "plan"})
                self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("mode") == "plan")
                ws.send_json({"type": "message", "chat": chat, "text": "go"})
                seen = self._drain(ws, lambda ev, _: ev["type"] == "done")
                flipped = next(i for i, ev in enumerate(seen) if ev["type"] == "chat" and ev.get("mode") == "auto")
                self.assertLess(flipped, len(seen) - 1)  # the phone heard before the reply finished

    def test_a_chats_mode_comes_back_after_a_restart(self):
        from vision.turnjournal import recover

        meta = {"event": "chat", "at": 1.0, "title": "t", "model": "opus", "effort": "high", "session_id": None}
        self.assertEqual(recover([meta], requeue=False)["mode"], "")  # never set: the configured one applies
        self.assertEqual(recover([meta, {"event": "mode", "at": 2.0, "mode": "plan"}], requeue=False)["mode"], "plan")
        self.assertEqual(recover([meta, {"event": "mode", "at": 2.0, "mode": "plan"},
                                  {"event": "mode", "at": 3.0, "mode": "auto"}], requeue=False)["mode"], "auto")

    def test_a_new_chat_works_in_the_folder_the_phone_picked(self):
        from fastapi.testclient import TestClient

        class Brain(_StubBrain):
            def __init__(self, cfg, **kw):
                super().__init__(cfg, **kw)
                self.workdir = cfg.workdir or "/tmp"  # as the real brains take their directory

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, tempfile.TemporaryDirectory() as project, \
             patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: Brain(cfg)):
            project = os.path.realpath(project)
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                ws.receive_json()
                ws.send_json({"type": "new", "workdir": project})
                opened = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]
                self.assertEqual(opened["workdir"], project)
                self.assertEqual(hub.chats[opened["chat"]].brain.workdir, project)
                self.assertEqual(opened["folder"], server.tilde(project))

                ws.send_json({"type": "new"})  # no folder named: the server's own, as before
                plain = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]
                self.assertEqual(plain["workdir"], "/tmp")

                before = set(hub.chats)
                ws.send_json({"type": "new", "workdir": os.path.join(project, "gone")})
                refused = self._drain(ws, lambda ev, _: ev["type"] == "error")[-1]
                self.assertIn("no such folder", refused["text"])
                self.assertEqual(set(hub.chats), before)  # and no chat was opened somewhere else instead

    def test_the_phone_can_browse_folders_to_start_a_chat_in(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as root, patch.object(server.Hub, "warm_up", lambda self: None):
            root = os.path.realpath(root)
            for name in ("beta", "Alpha", ".git"):
                os.mkdir(os.path.join(root, name))
            open(os.path.join(root, "notes.txt"), "w").close()
            hub.cfg.brain.workdir = root
            earlier = [SimpleNamespace(cwd=os.path.join(root, "beta"), last_active=2.0),
                       SimpleNamespace(cwd=os.path.join(root, "vanished"), last_active=1.0)]
            with patch("vision.sessions.list_all_sessions", return_value=earlier), TestClient(server.create_app(hub)) as tc:
                start = tc.get("/folders", headers=auth).json()  # no path: the default folder, with the recent ones
                self.assertEqual((start["path"], start["parent"]), (root, os.path.dirname(root)))
                self.assertEqual([f["name"] for f in start["folders"]], ["Alpha", "beta"])  # folders only, hidden ones out, by name
                self.assertEqual([r["path"] for r in start["recent"]][:1], [root])
                self.assertIn(os.path.join(root, "beta"), [r["path"] for r in start["recent"]])
                self.assertNotIn(os.path.join(root, "vanished"), [r["path"] for r in start["recent"]])

                inside = tc.get("/folders", params={"path": start["folders"][1]["path"]}, headers=auth).json()
                self.assertEqual((inside["path"], inside["parent"], inside["folders"]), (os.path.join(root, "beta"), root, []))
                self.assertNotIn("recent", inside)

                self.assertEqual(tc.get("/folders", params={"path": os.path.join(root, "nope")}, headers=auth).status_code, 404)
                self.assertEqual(tc.get("/folders", params={"path": os.path.join(root, "notes.txt")}, headers=auth).status_code, 404)
                self.assertEqual(tc.get("/folders").status_code, 401)

    def test_a_folder_is_shown_with_a_tilde_for_home(self):
        home = os.path.expanduser("~")
        self.assertEqual(server.tilde(home), "~")
        self.assertEqual(server.tilde(os.path.join(home, "code", "vision")), "~/code/vision")
        self.assertEqual(server.tilde(home + "-other/x"), home + "-other/x")  # a sibling of home is not under it
        self.assertEqual(server.tilde("/srv/www"), "/srv/www")

    def test_every_phone_sees_the_queue_change(self):
        """A message queued (or taken out) by one client reaches the others as a chat snapshot."""
        from fastapi.testclient import TestClient

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _StubBrain(cfg, delay=1.0)), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as phone, \
                 tc.websocket_connect("/ws", headers=auth) as other:
                chat = phone.receive_json()["chats"][0]["chat"]
                other.receive_json()
                other.send_json({"type": "message", "chat": chat, "text": "first"})
                self._drain(phone, lambda ev, _: ev["type"] == "start")
                other.send_json({"type": "message", "chat": chat, "text": "second"})
                self._drain(phone, lambda ev, _: ev["type"] == "chat" and ev.get("queued") == ["second"])
                other.send_json({"type": "unqueue", "chat": chat, "text": "second"})
                self._drain(phone, lambda ev, _: ev["type"] == "chat" and ev.get("queued") == [])

    def test_closing_a_chat_drops_its_queue(self):
        """Closed mid-reply: the reply stops, nothing queued behind it runs, and the chat stays gone."""
        from fastapi.testclient import TestClient

        hub = self._hub()
        auth = {"Authorization": "Bearer tok"}
        asked = []

        def brain(cfg, **kw):
            b = _StubBrain(cfg, delay=0.6)
            real = b.ask
            b.ask = lambda text, **k: (asked.append(text), real(text, **k))[1]
            return b

        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=brain), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                ws.receive_json()
                ws.send_json({"type": "new"})
                chat = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]["chat"]
                ws.send_json({"type": "message", "chat": chat, "text": "long one"})
                self._drain(ws, lambda ev, _: ev["type"] == "start")
                ws.send_json({"type": "message", "chat": chat, "text": "queued one"})
                ws.send_json({"type": "close", "chat": chat})
                self._drain(ws, lambda ev, _: ev["type"] == "chat_closed")
                __import__("time").sleep(1.5)
                self.assertNotIn(chat, hub.chats)
                self.assertEqual(asked, ["long one"])

    def test_two_chats_run_concurrently_and_frames_carry_chat_ids(self):
        from fastapi.testclient import TestClient

        hub = self._hub()
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _StubBrain(cfg, delay=0.3)), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            app = server.create_app(hub)
            with TestClient(app) as tc, tc.websocket_connect("/ws", headers={"Authorization": "Bearer tok"}) as ws:
                hello = ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertEqual(len(hello["chats"]), 1)
                first = hello["chats"][0]["chat"]

                ws.send_json({"type": "new"})
                opened = self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))[-1]
                second = opened["chat"]
                self.assertNotEqual(first, second)

                ws.send_json({"type": "message", "chat": first, "text": "one"})
                ws.send_json({"type": "message", "chat": second, "text": "two"})
                seen = self._drain(ws, lambda ev, all_: sum(e["type"] == "done" for e in all_) == 2)
                starts = [e for e in seen if e["type"] == "start"]
                dones = {e["chat"]: e for e in seen if e["type"] == "done"}
                self.assertEqual({e["chat"] for e in starts}, {first, second})
                # both started before either finished: they ran in parallel, not queued
                first_done = next(i for i, e in enumerate(seen) if e["type"] == "done")
                self.assertTrue(all(i < first_done for i, e in enumerate(seen) if e["type"] == "start"))
                self.assertEqual(dones[first]["text"], "echo: one")
                self.assertEqual(dones[second]["text"], "echo: two")
                self.assertEqual(hub.chats[first].title, "one")
                self.assertEqual(hub.chats[second].title, "two")

                # closing one chat leaves the other untouched
                ws.send_json({"type": "close", "chat": second})
                self._drain(ws, lambda ev, _: ev["type"] == "chat_closed" and ev["chat"] == second)
                self.assertEqual(set(hub.chats), {first})

                r = tc.get("/chats", headers={"Authorization": "Bearer tok"})
                self.assertEqual([c["chat"] for c in r.json()], [first])
                r = tc.get("/history", params={"chat": first}, headers={"Authorization": "Bearer tok"})
                self.assertEqual(r.status_code, 200)

                # closing the last chat leaves none open: no replacement is forced on the phone
                ws.send_json({"type": "close", "chat": first})
                self._drain(ws, lambda ev, _: ev["type"] == "chat_closed" and ev["chat"] == first)
                self.assertEqual(hub.chats, {})
                r = tc.get("/chats", headers={"Authorization": "Bearer tok"})
                self.assertEqual(r.json(), [])
                ws.send_json({"type": "new"})
                self._drain(ws, lambda ev, _: ev["type"] == "chat" and ev.get("opened"))
                self.assertEqual(len(hub.chats), 1)

    def test_a_message_while_a_plan_waits_answers_it(self):
        from fastapi.testclient import TestClient
        from vision.brain import PLAN_QUESTION

        answers = []

        class _Planner(_StubBrain):
            def ask(self, text, on_text=None, on_question=None, **kw):
                answers.append(on_question([PLAN_QUESTION]))
                return _Turn("done", "sess-plan")

        from vision.config import Config

        cfg = Config()
        cfg.conversation.model = "haiku"
        with patch.object(server, "load_token", return_value="tok"):
            hub = server.Hub(cfg, _Planner(cfg.brain), "tok", log=lambda *_: None)
        auth = {"Authorization": "Bearer tok"}
        with tempfile.TemporaryDirectory() as live, patch("vision.link.LIVE_DIR", __import__("pathlib").Path(live)), \
             patch.object(server.Hub, "warm_up", lambda self: None), \
             patch("vision.sessions.session_history", lambda provider, sid, limit=0, **kw: []), \
             patch("vision.brain.create_brain", side_effect=lambda cfg, **kw: _Planner(cfg)), \
             patch("vision.cli._turn_brain", lambda brain, conv, voice, text="", **kw: brain):
            with TestClient(server.create_app(hub)) as tc, tc.websocket_connect("/ws", headers=auth) as ws:
                chat = ws.receive_json()["chats"][0]["chat"]
                ws.send_json({"type": "message", "chat": chat, "text": "tidy the parser"})
                self._drain(ws, lambda ev, _: ev["type"] == "question")
                ws.send_json({"type": "message", "chat": chat, "text": "Yeah, go for it."})
                self._drain(ws, lambda ev, _: ev["type"] == "done")
                self.assertEqual(answers, [{PLAN_QUESTION["question"]: "Yes"}])
                self.assertFalse(hub.chats[chat]._pending)  # the answer was not queued as a new turn


class PlanByVoiceTests(unittest.TestCase):
    """A plan waiting for approval is said aloud in a sentence, and the reply in words answers it."""

    Q = {"question": "Carry out this plan?\n\n## Plan\n1. **Move** the cache out of the handler. Then test it.", "header": "Plan"}

    def test_go_words_approve_and_stop_words_decline(self):
        for said in ("go", "Yeah, go for it.", "yes please", "OK then mate", "crack on", "let's go"):
            self.assertEqual(planvoice.plan_answer(self.Q, said), {self.Q["question"]: "Yes"}, said)
        for said in ("no", "Nah.", "hold on", "stop, thanks"):
            self.assertEqual(planvoice.plan_answer(self.Q, said), {self.Q["question"]: "No"}, said)

    def test_anything_else_goes_back_as_changes(self):
        for said in ("yes but keep the tests", "go with Redis instead", "no, use the old parser"):
            self.assertEqual(planvoice.plan_answer(self.Q, said), {self.Q["question"]: said}, said)

    def test_the_summary_is_the_plans_first_sentence(self):
        self.assertEqual(planvoice.plan_summary(self.Q), f"Here's the plan: Move the cache out of the handler. {planvoice.PROMPT}")
        self.assertEqual(planvoice.plan_summary({"question": "Carry out this plan?", "header": "Plan"}), f"The plan's on screen. {planvoice.PROMPT}")

    def test_only_the_plan_form_counts(self):
        self.assertIsNone(planvoice.plan_question([{"question": "Which?", "header": "Pick"}]))
        self.assertIsNone(planvoice.plan_question([self.Q, self.Q]))
        self.assertIs(planvoice.plan_question([self.Q]), self.Q)


class FollowDefaultsTests(unittest.TestCase):
    """A new chat picks up /default saved while `vision serve` runs, unless flags pinned the model."""

    def _new_chat_model(self, config_text, follow=True):
        from pathlib import Path
        from vision import config
        from vision.config import Config

        cfg = Config()
        cfg.brain.model, cfg.brain.effort = "opus", "high"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(config_text)
            with patch.object(config, "CONFIG_DIR", Path(d)), patch.object(config, "CONFIG_PATH", path), \
                 patch("vision.brain.create_brain", side_effect=lambda c, **kw: _StubBrain(c)):
                hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=follow)
                chat = hub.open_chat()
        return chat.brain.cfg.model, chat.brain.cfg.effort

    def test_new_chat_uses_the_default_saved_after_start_up(self):
        self.assertEqual(self._new_chat_model('[brain]\nmodel = "sonnet"\neffort = "low"\n'), ("sonnet", "low"))

    def test_broken_config_falls_back_to_the_start_up_defaults(self):
        self.assertEqual(self._new_chat_model('[brain\nmodel = "sonnet"\n'), ("opus", "high"))

    def test_model_flag_pins_new_chats(self):
        self.assertEqual(self._new_chat_model('[brain]\nmodel = "sonnet"\neffort = "low"\n', follow=False), ("opus", "high"))


class DefaultsEndpointTests(unittest.TestCase):
    """The phone reads and saves the default model for new chats, the same config.toml lines /default writes."""

    def test_saves_the_default_and_new_chats_follow_it(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from vision import config
        from vision.config import Config

        cfg = Config()
        cfg.brain.model, cfg.brain.effort = "opus", "high"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('# mine\n[brain]\nmodel = "opus"\neffort = "high"\n')
            with patch.object(config, "CONFIG_DIR", Path(d)), patch.object(config, "CONFIG_PATH", path), \
                 patch("vision.brain.create_brain", side_effect=lambda c, **kw: _StubBrain(c)):
                hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False)
                tc = TestClient(server.create_app(hub))
                auth = {"Authorization": "Bearer tok"}
                self.assertEqual(tc.get("/defaults", headers=auth).json()["current"], "opus")
                saved = tc.post("/defaults", json={"model": "sonnet", "effort": "low"}, headers=auth).json()
                self.assertEqual((saved["current"], saved["effort"]), ("sonnet", "low"))
                self.assertIn('model = "sonnet"', path.read_text())
                self.assertIn("# mine", path.read_text())
                chat = hub.open_chat()
                self.assertEqual((chat.brain.cfg.model, chat.brain.cfg.effort), ("sonnet", "low"))
                self.assertEqual(tc.post("/defaults", json={"model": "nope"}, headers=auth).status_code, 400)
                self.assertEqual(tc.get("/defaults").status_code, 401)

    def test_saves_the_voice_rate_clamped_and_uses_it(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from vision import config
        from vision.config import Config

        cfg = Config()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text("[voice]\n# pace\nrate = 1.0\n")
            with patch.object(config, "CONFIG_DIR", Path(d)), patch.object(config, "CONFIG_PATH", path):
                hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
                tc = TestClient(server.create_app(hub))
                auth = {"Authorization": "Bearer tok"}
                self.assertEqual(tc.get("/voice/rate", headers=auth).json(), {"rate": 1.0})
                self.assertEqual(tc.post("/voice/rate", json={"rate": 1.15}, headers=auth).json(), {"rate": 1.15})
                self.assertIn("rate = 1.15", path.read_text())
                self.assertIn("# pace", path.read_text())
                self.assertEqual(hub.cfg.voice.rate, 1.15)
                self.assertEqual(tc.post("/voice/rate", json={"rate": 9}, headers=auth).json(), {"rate": 2.0})
                self.assertEqual(tc.post("/voice/rate", json={"rate": 0.1}, headers=auth).json(), {"rate": 0.5})
                self.assertEqual(config.load_config().voice.rate, 0.5)
                self.assertEqual(tc.post("/voice/rate", json={"rate": "fast"}, headers=auth).status_code, 400)
                self.assertEqual(tc.get("/voice/rate").status_code, 401)

    def test_saves_the_voice_and_speaks_in_it(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from vision import config
        from vision.config import Config

        class Voice:
            _loaded, voice, fillers = True, "jarvis", None

            def set_voice(self, name):
                self.voice = name

            def prepare_fillers(self, phrases):
                self.fillers = (self.voice, phrases)

        cfg = Config()
        with tempfile.TemporaryDirectory() as d:
            path, voices = Path(d) / "config.toml", Path(d) / "voices"
            path.write_text('[voice]\n# who speaks\nvoice = "jarvis"\n')
            for name in ("jarvis", "narrator"):
                (voices / name).mkdir(parents=True)
                (voices / name / "ref.wav").write_bytes(name.encode())
            (voices / "jarvis" / "design.txt").write_text("a butler")
            with patch.object(config, "CONFIG_DIR", Path(d)), patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config, "VOICES_DIR", voices):
                hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
                tc = TestClient(server.create_app(hub))
                auth = {"Authorization": "Bearer tok"}
                # friday is a built-in design nobody has made here, so it is not offered
                listed = {"voice": "jarvis", "voices": [{"id": "jarvis", "note": "designed", "sample": True},
                                                        {"id": "narrator", "note": "cloned", "sample": True}], "on": True}
                self.assertEqual(tc.get("/voice", headers=auth).json(), listed)
                self.assertEqual(tc.post("/voice", json={"voice": "narrator"}, headers=auth).json()["voice"], "narrator")  # not loaded yet
                hub._speaker = sp = Voice()
                self.assertEqual(tc.post("/voice", json={"voice": "jarvis"}, headers=auth).json(), listed)
                self.assertEqual(sp.fillers, ("jarvis", cfg.voice.filler_phrases))
                self.assertEqual(tc.post("/voice", json={"voice": "narrator"}, headers=auth).json()["voice"], "narrator")
                self.assertEqual((sp.voice, hub.cfg.voice.voice), ("narrator", "narrator"))
                self.assertIn('voice = "narrator"', path.read_text())
                self.assertIn("# who speaks", path.read_text())
                self.assertEqual(config.load_config().voice.voice, "narrator")
                self.assertEqual(tc.post("/voice", json={"voice": "friday"}, headers=auth).status_code, 400)
                self.assertEqual(tc.get("/voice").status_code, 401)
                # each one's clip can be heard before choosing it; hearing one changes nothing
                heard = tc.get("/voice/sample", params={"voice": "jarvis"}, headers=auth)
                self.assertEqual((heard.status_code, heard.content, heard.headers["content-type"]), (200, b"jarvis", "audio/wav"))
                self.assertEqual(hub.cfg.voice.voice, "narrator")
                for unknown in ("friday", "../narrator", ""):
                    self.assertEqual(tc.get("/voice/sample", params={"voice": unknown}, headers=auth).status_code, 404, unknown)
                self.assertEqual(tc.get("/voice/sample", params={"voice": "jarvis"}).status_code, 401)



class WarmStartTests(unittest.TestCase):
    """A chat someone opens starts its provider's process at once (Brain.warm), so the first message,
    seconds later, finds it booted. The chats a restart brings back are not warmed."""

    def test_open_chat_warms_a_brain_that_can_be_and_not_a_restored_one(self):
        from unittest.mock import Mock

        from vision.config import Config

        class _WarmBrain(_StubBrain):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.warm = Mock()

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
        opened, restored = _WarmBrain(cfg.brain), _WarmBrain(cfg.brain)
        hub.open_chat(opened)
        hub.open_chat(restored, restored={"has_base": False, "history": [], "created": 1.0, "updated": 1.0, "recovery_rows": []})
        hub.open_chat(_StubBrain(cfg.brain))  # a brain with no warm: nothing to do
        for _ in range(100):
            if opened.warm.called:
                break
            time.sleep(0.01)
        opened.warm.assert_called_once()
        restored.warm.assert_not_called()

    def test_switch_model_warms_the_brain_it_ends_with(self):
        """/model from the phone: the next message would start the new pick's process; it boots now."""
        import asyncio
        from unittest.mock import Mock

        from vision.config import Config

        class _WarmBrain(_StubBrain):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.warm = Mock()

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
        chat = hub.open_chat(_StubBrain(cfg.brain))
        switched = _WarmBrain(cfg.brain)
        with patch("vision.cli._switch_model", return_value=(switched, "")), \
             patch("vision.providers.unavailable_reason", return_value=None):
            asyncio.run(chat.switch_model("opus", None))
        for _ in range(100):
            if switched.warm.called:
                break
            time.sleep(0.01)
        switched.warm.assert_called_once()
        self.assertIs(chat.brain, switched)

    def test_a_switch_is_noted_in_the_chat_at_the_next_turn_only(self):
        """Flicking through models doesn't fill the chat: one note above the next message, none if it went back."""
        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
        chat = hub.open_chat(_StubBrain(cfg.brain))
        posted = []
        chat.post = posted.append
        start = cfg.brain.model
        cfg.brain.model = "sonnet"
        cfg.brain.model = start  # and back again before anything was sent
        chat._note_switch()
        self.assertEqual(posted, [])
        cfg.brain.model = "opus" if start != "opus" else "sonnet"
        chat._note_switch()
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0]["text"].startswith("Switched to"))
        chat._note_switch()  # the turn after: already said
        self.assertEqual(len(posted), 1)


class WirePieceTests(unittest.TestCase):
    def _wire(self):
        import queue
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock

        wire = server.WireSpeaker.__new__(server.WireSpeaker)  # no synthesis threads: the loop is driven by hand
        wire.sink, wire.seq, wire.hushed = Mock(), 0, False
        wire._audio_q = queue.Queue()
        wire.speaker = SimpleNamespace(_stop=threading.Event())
        return wire

    def test_piece_size_follows_how_much_the_phone_has_left_to_play(self):
        """The opening was one 0.32 s piece with a second's worth behind it, which took longer to make
        than the 0.32 s took to play: the phone went quiet after the first word. Now a little is
        gathered up front, pieces go as made while the phone runs low, and a second at a time once it
        has plenty."""
        from vision.tts import StreamingSpeaker

        wire = self._wire()
        self.assertEqual(wire._min_piece_s(True, 0), wire.PREBUFFER_S)  # nothing sent yet
        now = time.monotonic()
        wire._until = now + 0.4
        self.assertEqual(wire._min_piece_s(False, 0), StreamingSpeaker.MIN_PIECE_S)
        wire._until = now + 5
        self.assertEqual(wire._min_piece_s(True, 1), 1.0)
        wire._until = now - 2  # played out during a pause: start again with a cushion
        self.assertEqual(wire._min_piece_s(True, 3), wire.PREBUFFER_S)

    def test_a_late_piece_mid_sentence_is_logged_as_a_phone_gap(self):
        from unittest.mock import patch

        import numpy as np

        wire = self._wire()
        second = np.zeros(server.SAMPLE_RATE, dtype="float32")
        wire._audio_q.put((second, 0, False))
        wire._audio_q.put(None)
        with patch.object(server, "log_voice_line") as log:
            wire._play_loop()
            log.assert_not_called()  # the opening piece has nothing before it to be late for
            wire._until = time.monotonic() - 0.2  # the phone ran out 0.2 s ago, halfway through the chunk
            wire._audio_q.put((second, 0, True))
            wire._audio_q.put(None)
            wire._play_loop()
            log.assert_called_once()
            self.assertEqual(log.call_args[0][0], "phone-gap")
            wire._until = time.monotonic() - 0.5  # after the chunk's last piece a pause is just a pause
            wire._audio_q.put((second, 0, True))
            wire._audio_q.put(None)
            wire._play_loop()
            log.assert_called_once()
        self.assertEqual(wire.sink.call_count, 3)


class HushTests(unittest.TestCase):
    """A call hung up on the phone: the reply stops being spoken to it, and nothing else stops."""

    def _wire(self):
        import queue
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock

        wire = server.WireSpeaker.__new__(server.WireSpeaker)  # no synthesis threads: the loop is driven by hand
        wire.sink, wire.seq, wire.hushed = Mock(), 0, False
        wire._audio_q = queue.Queue()
        wire.speaker = SimpleNamespace(_stop=threading.Event())
        return wire

    def test_a_hushed_reply_sends_no_more_audio_and_takes_no_more_text(self):
        import numpy as np
        from unittest.mock import patch

        wire = self._wire()
        wire._audio_q.put((np.zeros(8, dtype="float32"), 0, False))
        wire.hush()
        wire._audio_q.put((np.zeros(8, dtype="float32"), 0, False))  # a sentence already being made
        wire._audio_q.put(None)
        wire._play_loop()
        wire.sink.assert_not_called()
        self.assertFalse(wire.speaker._stop.is_set())  # the shared voice is left to the other chats
        with patch.object(server.StreamingSpeaker, "feed") as feed, patch.object(server.StreamingSpeaker, "flush") as flush:
            wire.feed("more words.")
            wire.flush()
        feed.assert_not_called()
        flush.assert_not_called()

    def test_hush_leaves_the_end_marker_so_the_reply_can_finish(self):
        import threading

        # The synth thread has finished (its None is queued) when the phone hangs up: the play loop must
        # still see that marker and return, or finish() waits on it for ever and the chat stays busy.
        wire = self._wire()
        wire._audio_q.put(None)
        wire.hush()
        loop = threading.Thread(target=wire._play_loop, daemon=True)
        loop.start()
        loop.join(timeout=2)
        self.assertFalse(loop.is_alive())

    def _hub(self):
        from unittest.mock import Mock

        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        hub.chats = {"a": Mock(), "b": Mock()}
        return hub

    def test_the_hush_frame_quiets_the_chat_the_call_was_on(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock

        hub = self._hub()
        ws = Mock(send_json=AsyncMock())
        asyncio.run(hub.handle(ws, {"type": "hush", "chat": "a"}))
        hub.chats["a"].hush.assert_called_once_with()
        hub.chats["b"].hush.assert_not_called()  # someone else's call carries on
        for chat in hub.chats.values():
            chat.cancel.assert_not_called()
        ws.send_json.assert_not_called()
        # The chat is already gone: nothing to quiet, and not everything else instead.
        asyncio.run(hub.handle(ws, {"type": "hush", "chat": "zzz"}))
        hub.chats["b"].hush.assert_not_called()
        ws.send_json.assert_not_called()

    def test_a_hush_naming_no_chat_reaches_every_chat(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock

        hub = self._hub()  # an older app: it does not say which chat its call was on
        asyncio.run(hub.handle(Mock(send_json=AsyncMock()), {"type": "hush"}))
        for chat in hub.chats.values():
            chat.hush.assert_called_once_with()


class UnqueueTests(unittest.TestCase):
    """A queued message removed on the phone never runs, so the uploads it carried are deleted with it."""

    def _handle(self, chat, msg, upload_dir):
        import asyncio
        from unittest.mock import AsyncMock, Mock

        from vision.config import Config

        cfg = Config()
        hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, follow_defaults=False, open_initial=False)
        hub.chats = {"c": chat}
        with patch.object(server, "UPLOAD_DIR", upload_dir):
            asyncio.run(hub.handle(Mock(send_json=AsyncMock()), {"chat": "c", **msg}))

    def test_removing_a_queued_message_deletes_its_uploads(self):
        from pathlib import Path
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as d:
            up = Path(d)
            photo, other = up / "20260930-131257-a151c7.jpg", up / "20260930-131258-b151c7.jpg"
            photo.write_bytes(b"x")
            other.write_bytes(b"x")
            chat = Mock(spec=server.Chat)
            chat.unqueue.return_value = True
            self._handle(chat, {"type": "unqueue", "text": "look", "images": [str(photo), "/etc/passwd"]}, up)
            chat.unqueue.assert_called_once()
            self.assertFalse(photo.exists())
            self.assertTrue(other.exists())

    def test_uploads_stay_when_the_message_is_taken_back_to_edit(self):
        from pathlib import Path
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as d:
            photo = Path(d) / "20260930-131257-a151c7.jpg"
            photo.write_bytes(b"x")
            chat = Mock(spec=server.Chat)
            chat.unqueue.return_value = True
            # `keep`: the photo went back into the phone's composer, pointing at this upload.
            self._handle(chat, {"type": "unqueue", "text": "look", "images": [str(photo)], "keep": True}, Path(d))
            chat.unqueue.assert_called_once()
            self.assertTrue(photo.exists())

    def test_uploads_stay_when_the_message_had_already_run(self):
        from pathlib import Path
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as d:
            photo = Path(d) / "20260930-131257-a151c7.jpg"
            photo.write_bytes(b"x")
            chat = Mock(spec=server.Chat)
            chat.unqueue.return_value = False
            self._handle(chat, {"type": "unqueue", "text": "look", "images": [str(photo)]}, Path(d))
            self.assertTrue(photo.exists())


class PinTests(unittest.TestCase):
    """A pinned chat says so in its summary on every app, stays pinned across a restart, and loses its pin when closed."""

    def test_a_pin_is_shared_kept_and_dropped_with_the_chat(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, Mock
        from vision.config import Config

        cfg = Config()
        with tempfile.TemporaryDirectory() as d, patch.object(server, "PINNED", Path(d) / "pinned.json"), \
             patch("vision.brain.create_brain", side_effect=lambda c, **kw: _StubBrain(c)):
            hub = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
            chat = hub.open_chat()
            posted = []
            hub.post = posted.append
            self.assertFalse(chat.summary()["pinned"])
            asyncio.run(hub.handle(Mock(send_json=AsyncMock()), {"type": "pin", "chat": chat.id, "pinned": True}))
            self.assertTrue(chat.summary()["pinned"])
            self.assertTrue(posted[-1]["pinned"], "every app hears of it")
            again = server.Hub(cfg, _StubBrain(cfg.brain), "tok", log=lambda *_: None, open_initial=False)
            self.assertTrue(again.is_pinned(chat.id), "kept across a restart")
            hub.close_chat(chat)
            self.assertFalse(hub.is_pinned(chat.id))
            self.assertEqual(server.load_pinned(), set())


class SearchTests(unittest.TestCase):
    """GET /search: what was said in the open chats, one hit per chat, newest first, with a snippet."""

    def test_every_word_must_turn_up_in_one_message_and_the_newest_mention_is_shown(self):
        from types import SimpleNamespace

        hub = SimpleNamespace(chats={
            "a": SimpleNamespace(id="a", title="Router trouble", updated=1, project="", history=lambda n: [
                {"role": "user", "text": "the Router keeps dropping wifi"},
                {"role": "assistant", "text": "Reboot the router; the wifi firmware is old."}]),
            "b": SimpleNamespace(id="b", title="Groceries", updated=2, project="p1", history=lambda n: [
                {"role": "user", "text": "milk, eggs, a new router?"}]),
            "c": SimpleNamespace(id="c", title="Taxes", updated=3, project="", history=lambda n: []),
        })
        search = server.Hub.search.__get__(hub)
        hits = search("router WIFI")
        self.assertEqual([h["chat"] for h in hits], ["a"], "both words in one message")
        self.assertEqual((hits[0]["role"], hits[0]["snippet"]), ("assistant", "Reboot the router; the wifi firmware is old."))
        self.assertEqual([h["chat"] for h in search("router")], ["b", "a"], "newest chat first")
        self.assertEqual(search("taxes")[0]["role"], "title", "a title counts")
        self.assertEqual(search("x"), [], "too short to search")
        self.assertEqual(search("router", limit=1)[0]["project"], "p1")

    def test_a_snippet_is_cut_around_the_first_hit_at_whole_words(self):
        text = " ".join(f"word{i}" for i in range(60)) + " the needle is here " + " ".join(f"tail{i}" for i in range(60))
        cut = server.snippet(text, "needle", width=80)
        self.assertIn("needle", cut)
        self.assertTrue(cut.startswith("…word") and cut.endswith("…"), cut)
        self.assertLessEqual(len(cut), 82)
        self.assertEqual(server.snippet("short\n  one", "one"), "short one")
