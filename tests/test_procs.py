"""vision.procs and the Windows side of the live link. The loopback listener is plain sockets, so it runs
anywhere; the rest fakes the platform, and the Windows CI job runs the real thing."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _nopush  # noqa: F401  (keeps the suite off the real phones)

from vision import link, procs


def _read_frames(conn: socket.socket, count: int) -> list[dict]:
    buf = b""
    conn.settimeout(2)
    while buf.count(b"\n") < count:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return [json.loads(line) for line in buf.decode().splitlines()]


class LoopbackLink(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (mock.patch.object(link, "LIVE_DIR", Path(tmp.name)), mock.patch.object(link, "WINDOWS", True),
                      mock.patch.object(link, "pid_alive", lambda pid: True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.frames: list[dict] = []
        self.host = link.LinkHost(lambda: {"id": "c1"}, self.frames.append)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.info = link.list_links()[0]

    def test_the_descriptor_says_how_to_reach_it(self):
        self.assertEqual(self.host._server.getsockname()[0], "127.0.0.1")
        self.assertEqual(self.info["port"], self.host._server.getsockname()[1])
        self.assertEqual(len(self.info["key"]), 64)

    def test_the_right_key_gets_the_chat(self):
        conn = link.dial(self.info)
        self.addCleanup(conn.close)
        conn.sendall(b'{"type": "hello"}\n')
        self.assertEqual(_read_frames(conn, 1), [{"type": "chat", "id": "c1"}])

    def test_a_frame_sent_along_with_the_key_is_not_lost(self):
        conn = socket.create_connection(("127.0.0.1", self.info["port"]))
        self.addCleanup(conn.close)
        conn.sendall(self.info["key"].encode() + b'\n{"type": "cancel"}\n')
        deadline = time.time() + 2
        while not self.frames and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.frames, [{"type": "cancel"}])

    def test_a_wrong_key_is_dropped_and_hears_nothing(self):
        conn = link.dial({**self.info, "key": "0" * 64})
        self.addCleanup(conn.close)
        conn.sendall(b'{"type": "cancel"}\n')
        self.host.post({"type": "delta", "text": "private"})
        conn.settimeout(2)
        try:
            self.assertEqual(conn.recv(4096), b"")
        except ConnectionError:
            pass  # a reset says the same thing
        self.assertEqual(self.frames, [])

    def test_nothing_reaches_a_connection_before_its_key(self):
        conn = socket.create_connection(("127.0.0.1", self.info["port"]))
        self.addCleanup(conn.close)
        time.sleep(0.1)
        self.host.post({"type": "delta", "text": "private"})
        self.assertFalse(self.host.connected)


class Shells(unittest.TestCase):
    def test_powershell_on_windows_and_never_the_unix_shell_variable(self):
        with mock.patch.object(procs, "WINDOWS", True), mock.patch.dict(os.environ, {"SHELL": "/usr/bin/bash"}), \
                mock.patch.object(procs.shutil, "which", lambda name: r"C:\ps\pwsh.exe" if name == "pwsh" else None):
            argv = procs.user_shell("dir")
        self.assertEqual(argv[:4], [r"C:\ps\pwsh.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertTrue(argv[4].endswith("dir"))

    def test_wsls_launcher_does_not_count_as_bash(self):
        system32 = r"C:\Windows\System32\bash.exe"
        with mock.patch.object(procs, "WINDOWS", True), \
                mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}, clear=True), \
                mock.patch.object(procs.shutil, "which", lambda name: system32 if name == "bash" else None), \
                mock.patch.object(procs.Path, "is_file", lambda self: True):
            self.assertIsNone(procs.bash())


@unittest.skipIf(procs.WINDOWS, "os.killpg's side")
class UnixTree(unittest.TestCase):
    def test_kill_tree_ends_the_group(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **procs.group_kwargs())
        self.addCleanup(proc.wait)
        procs.kill_tree(proc.pid)
        self.assertIsNotNone(proc.wait(5))


@unittest.skipUnless(procs.WINDOWS, "the real Windows calls")
class WindowsProcesses(unittest.TestCase):
    def test_pid_alive_probes_without_touching_the_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **procs.group_kwargs())
        self.addCleanup(proc.wait)
        self.assertTrue(procs.windows_pid_alive(proc.pid))
        self.assertIsNone(proc.poll())
        procs.kill_tree(proc.pid)
        proc.wait(5)
        self.assertFalse(procs.windows_pid_alive(proc.pid))


if __name__ == "__main__":
    unittest.main()
