"""`vision doctor --fix`: the mechanical repairs, each asked about unless -y, each re-checked after."""
import importlib.util
import re
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from vision import cli, clis


def _plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


class DoctorFixTests(unittest.TestCase):
    def setUp(self):
        real = importlib.util.find_spec
        self.text_only = lambda name, *a, **k: None if name in ("numpy", "sounddevice", "soundfile", "faster_whisper") else real(name, *a, **k)

    def run_doctor(self, *args, infos, after=None, runs=None, service_state=None):
        """doctor with fake CLIs: `infos` is what cli_versions reports first, `after` what cli_version
        says once a fix ran; shell commands land in `runs`."""
        after = after or {}
        runs = runs if runs is not None else []
        installed, running, server = service_state or (False, False, None)
        patches = [
            patch("vision.clis.cli_versions", return_value=list(infos)),
            patch("vision.clis.cli_version", side_effect=lambda p, *a, **k: after.get(p) or next(i for i in infos if i.provider == p)),
            patch("vision.clis.check_update", side_effect=lambda p, **k: clis.UpdateCheck(p, current="1.0", latest="1.0")),
            patch("vision.clis.login_status", return_value=(True, "ok")),
            patch("vision.cli.subprocess.run", side_effect=lambda cmd, **k: runs.append(cmd)),
            patch("importlib.util.find_spec", self.text_only),
            patch("vision.service.installed", return_value=installed),
            patch("vision.service.running", return_value=running),
            patch("vision.service.waiting", return_value=False),
            patch("vision.service.install", side_effect=lambda: runs.append("service.install")),
            patch("vision.remote.running_server", return_value=server),
        ]
        for p in patches:
            p.start()
        try:
            return CliRunner().invoke(cli.app, ["doctor", "--no-usage", *args])
        finally:
            for p in patches:
                p.stop()

    def test_a_missing_cli_with_an_installer_is_installed_and_checked_again(self):
        missing = [clis.CliInfo(p, error="not installed") for p in clis.PROVIDERS]
        claude_ok = clis.CliInfo("claude", version="2.1.300", path="/usr/bin/claude")
        runs = []
        r = self.run_doctor("--fix", "--yes", infos=missing, after={"claude": claude_ok}, runs=runs)
        self.assertEqual(r.exit_code, 0, r.output)
        text = _plain(r.output)
        self.assertIn("fixing: install Claude Code", text)
        self.assertIn("curl -fsSL https://claude.ai/install.sh | bash", runs)
        self.assertIn("fixing: install Codex", text)
        self.assertNotIn("fixing: install Grok", text)  # no installer, only a page: left as a hint
        self.assertIn("Grok CLI: not installed", text)
        self.assertIn("fixed: install Claude Code", text)

    def test_without_fix_nothing_runs(self):
        missing = [clis.CliInfo(p, error="not installed") for p in clis.PROVIDERS]
        runs = []
        r = self.run_doctor(infos=missing, runs=runs)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(runs, [])
        self.assertNotIn("fixing", r.output)
        self.assertNotIn("nothing to fix", r.output)

    def test_fix_asks_before_each_one_unless_yes(self):
        missing = [clis.CliInfo(p, error="not installed") for p in clis.PROVIDERS]
        runs = []
        with patch("rich.prompt.Confirm.ask", return_value=False):
            r = self.run_doctor("--fix", infos=missing, runs=runs)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(runs, [])
        self.assertIn("nothing to fix", _plain(r.output))

    def test_a_service_that_is_installed_but_not_answering_is_restarted(self):
        fine = [clis.CliInfo(p, version="1.0", path=f"/usr/bin/{p}") for p in clis.PROVIDERS]
        runs = []
        r = self.run_doctor("--fix", "-y", infos=fine, runs=runs, service_state=(True, False, None))
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("service.install", runs)
        self.assertIn("background service restarted", _plain(r.output))
        runs.clear()
        r = self.run_doctor("--fix", "-y", infos=fine, runs=runs, service_state=(True, True, {"pid": 1, "url": "http://x:8765"}))
        self.assertEqual(runs, [])
        self.assertIn("background service running", _plain(r.output))


if __name__ == "__main__":
    unittest.main()
