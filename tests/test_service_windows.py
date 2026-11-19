"""The Windows side of vision.service, with the platform faked: the supervisor's loop and what stop() will kill."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _nopush  # noqa: F401  (keeps the suite off the real phones)

from vision import procs, service


class Supervisor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        unit = self.dir / "service.json"
        unit.write_text(json.dumps({"path": r"C:\tools"}), encoding="utf-8")
        for patch in (mock.patch.object(service, "WINDOWS", True), mock.patch.object(service, "STATE_DIR", self.dir),
                      mock.patch.object(service, "LOG", self.dir / "serve.log"),
                      mock.patch.object(service, "PIDS", self.dir / "pids.json"),
                      mock.patch.object(service, "unit_path", lambda: unit),
                      mock.patch.object(service.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_with(self, codes):
        sleeps = []
        waits = iter(codes)
        popen = mock.Mock(side_effect=lambda *a, **k: SimpleNamespace(pid=4242, wait=lambda: next(waits)))
        with mock.patch.object(service.subprocess, "Popen", popen):
            service.supervise(sleep=sleeps.append)
        return popen, sleeps

    def test_a_clean_exit_ends_it_and_clears_the_pids(self):
        popen, sleeps = self.run_with([0])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(sleeps, [])
        self.assertFalse(service.PIDS.exists())

    def test_a_restart_comes_straight_back_and_a_crash_after_a_pause(self):
        popen, sleeps = self.run_with([service.RESTART, 1, 0])
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(sleeps, [5])

    def test_the_server_gets_the_saved_path_a_hidden_console_and_the_service_flag(self):
        popen, _ = self.run_with([0])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["env"]["PATH"], r"C:\tools")
        self.assertEqual(kwargs["env"][service.ENV], "1")
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertTrue(popen.call_args.args[0][0].endswith("python.exe"))


class Stop(unittest.TestCase):
    def stop(self, image):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pids = Path(tmp.name) / "pids.json"
        pids.write_text(json.dumps({"supervisor": 77, "server": 78}), encoding="utf-8")
        with mock.patch.object(service, "WINDOWS", True), mock.patch.object(service, "PIDS", pids), \
                mock.patch.object(service, "windows_image", lambda pid: image), \
                mock.patch.object(service, "kill_tree") as kill:
            service.stop()
        self.assertFalse(pids.exists())
        return kill

    def test_it_ends_the_supervisor(self):
        self.stop(r"C:\v\.venv\Scripts\pythonw.exe".replace("\\", "/")).assert_called_once_with(77)

    def test_a_reused_pid_is_left_alone(self):
        self.stop("C:/Program Files/Mozilla Firefox/firefox.exe").assert_not_called()


class BatchWrappers(unittest.TestCase):
    def test_a_real_program_is_used_as_it_is(self):
        with mock.patch.object(procs, "WINDOWS", True):
            self.assertEqual(procs.native_cli(r"C:\bin\claude.exe"), r"C:\bin\claude.exe")

    def test_a_wrapper_gives_way_to_the_program_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(procs, "WINDOWS", True):
            inner = Path(tmp) / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
            inner.mkdir(parents=True)
            (inner / "codex.exe").touch()
            self.assertEqual(procs.native_cli(str(Path(tmp) / "codex.cmd")), str(inner / "codex.exe"))

    def test_a_wrapper_with_nothing_inside_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(procs, "WINDOWS", True):
            self.assertIsNone(procs.native_cli(str(Path(tmp) / "grok.CMD")))


if __name__ == "__main__":
    unittest.main()
