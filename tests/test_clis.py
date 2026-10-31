from __future__ import annotations

import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fake_exe import python_command
from rich.console import Console

from vision import clis
from vision.config import BrainConfig
from vision.cli import _update_summary, _usage_selection, _versions_renderable


def _fake_cli(dir: str, name: str, version_lines: list[str], update_rc: int = 0) -> str:
    """A command that reports the next version each time it is asked, and whose `update` bumps it."""
    counter = os.path.join(dir, f"{name}.count")
    return python_command(os.path.join(dir, name), (
        "import os, sys\n"
        f"counter, lines = {counter!r}, {version_lines!r}\n"
        "n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "if sys.argv[1:2] == ['--version']:\n"
        "    print(lines[min(n, len(lines) - 1)])\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:2] == ['update']:\n"
        "    open(counter, 'w').write(str(n + 1))\n"
        "    print('Updating…')\n"
        f"    sys.exit({update_rc})\n"
        "sys.exit(2)\n"
    ))


def _render(renderable) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120, color_system=None).print(renderable)
    return buf.getvalue()


class ParseTests(unittest.TestCase):
    def test_versions_from_each_cli_format(self):
        self.assertEqual(clis.parse_version("2.1.278 (Claude Code)"), "2.1.278")
        self.assertEqual(clis.parse_version("codex-cli 0.155.1"), "0.155.1")
        self.assertEqual(clis.parse_version("grok 1.0.34 (3736acbc8658) [stable]"), "1.0.34")
        self.assertEqual(clis.parse_version("grok 0.1.151-alpha.2"), "0.1.151-alpha.2")
        self.assertIsNone(clis.parse_version("no digits here"))

    def test_update_arg(self):
        self.assertEqual(clis.parse_update_arg(""), clis.PROVIDERS)
        self.assertEqual(clis.parse_update_arg("all"), clis.PROVIDERS)
        self.assertEqual(clis.parse_update_arg("grok claude"), ("claude", "grok"))
        with self.assertRaises(ValueError):
            clis.parse_update_arg("gemini")

    def test_version_arg(self):
        self.assertEqual(clis.parse_version_arg(""), (True, clis.PROVIDERS))
        self.assertEqual(clis.parse_version_arg("all"), (True, clis.PROVIDERS))
        self.assertEqual(clis.parse_version_arg("codex"), (False, ("codex",)))
        self.assertEqual(clis.parse_version_arg("grok vision"), (True, ("grok",)))
        self.assertEqual(clis.parse_version_arg("vision"), (True, ()))
        with self.assertRaises(ValueError):
            clis.parse_version_arg("update")

    def test_versions_table_can_show_one_cli_only(self):
        with patch("vision.clis.cli_version", return_value=clis.CliInfo("codex", path="/x/codex", version="0.155.1")):
            text = _render(_versions_renderable(providers=("codex",), vision=False, check=False))
        self.assertIn("0.155.1", text)
        self.assertNotIn("Vision", text)
        self.assertNotIn("Claude", text)


class VersionTests(unittest.TestCase):
    def test_reads_a_version_and_reports_a_missing_cli(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_cli(d, "codex", ["codex-cli 0.155.1"])
            with patch("vision.clis.find_cli", return_value=exe):
                info = clis.cli_version("codex")
            self.assertTrue(info.ok)
            self.assertEqual((info.version, info.path, info.label), ("0.155.1", exe, "Codex"))
            self.assertEqual(info.line(), f"Codex 0.155.1 ({exe})")

        with patch("vision.clis.find_cli", side_effect=RuntimeError("not on PATH")):
            info = clis.cli_version("grok")
        self.assertFalse(info.ok)
        self.assertIn("not installed", info.error)

    def test_versions_table_lists_vision_and_every_cli(self):
        infos = [
            clis.CliInfo("claude", path="/x/claude", version="2.1.278"),
            clis.CliInfo("codex", error="not installed (no codex)"),
            clis.CliInfo("grok", path="/x/grok", version="1.0.34"),
        ]
        text = _render(_versions_renderable(infos, check=False))
        self.assertIn("Vision", text)
        self.assertIn("2.1.278", text)
        self.assertIn("missing", text)
        self.assertIn("not installed (no codex)", text)
        self.assertIn("1.0.34", text)


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        clis.forget_checks()

    def tearDown(self):
        clis.forget_checks()

    def test_newer(self):
        self.assertTrue(clis._newer("2.1.280", "2.1.278"))
        self.assertTrue(clis._newer("0.156.0", "0.155.1"))
        self.assertFalse(clis._newer("1.0.34", "1.0.34"))
        self.assertFalse(clis._newer("1.0.34", "1.0.35"))
        self.assertTrue(clis._newer("0.1.151", "0.1.151-alpha.2"))  # the release beats its pre-release

    def test_check_is_cached_and_status_copy(self):
        info = clis.CliInfo("codex", path="/x/codex", version="0.155.1")
        with patch("vision.clis.cli_version", return_value=info), patch("vision.clis.latest_version", return_value="0.156.0") as latest:
            c = clis.check_update("codex")
            again = clis.check_update("codex")
        self.assertIs(c, again)
        self.assertEqual(latest.call_count, 1)
        self.assertTrue(c.available)
        self.assertEqual(c.status(), "Codex 0.156.0 available · /update")
        self.assertIs(clis.peek_check("codex"), c)
        clis.forget_checks(("codex",))
        self.assertIsNone(clis.peek_check("codex"))

    def test_up_to_date_and_unknown(self):
        info = clis.CliInfo("grok", path="/x/grok", version="1.0.34")
        with patch("vision.clis.cli_version", return_value=info), patch("vision.clis.latest_version", return_value="1.0.34"):
            self.assertEqual(clis.check_update("grok").status(), "")  # current: no notice
        clis.forget_checks()
        with patch("vision.clis.cli_version", return_value=info), patch("vision.clis.latest_version", side_effect=OSError("offline")):
            c = clis.check_update("grok")
        self.assertIsNone(c.available)
        self.assertEqual(c.status(), "")
        self.assertEqual(c.error, "offline")
        self.assertIsNone(clis.peek_check("grok", max_age=0))

    def test_versions_table_shows_the_update_column(self):
        infos = [clis.CliInfo("claude", path="/x/claude", version="2.1.278")]
        checks = {"claude": clis.UpdateCheck("claude", current="2.1.278", latest="2.1.280", at=1)}
        text = _render(_versions_renderable(infos, checks=checks))
        self.assertIn("2.1.280 available", text)
        checks["claude"].latest = "2.1.278"
        self.assertIn("up to date", _render(_versions_renderable(infos, checks=checks)))

    def test_grok_latest_uses_its_own_checker(self):
        out = '{"currentVersion":"1.0.34","latestVersion":"1.0.35","updateAvailable":true,"error":null}\n'
        with patch("vision.clis.find_cli", return_value="/x/grok"), patch("vision.clis.subprocess.run", return_value=SimpleNamespace(stdout=out, stderr="", returncode=0)) as run:
            self.assertEqual(clis.latest_version("grok"), "1.0.35")
        self.assertEqual(run.call_args[0][0], ["/x/grok", "update", "--check", "--json"])


class UpdateTests(unittest.TestCase):
    def test_update_reports_the_version_change(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_cli(d, "claude", ["2.1.278 (Claude Code)", "2.1.280 (Claude Code)"])
            with patch("vision.clis.find_cli", return_value=exe):
                r = clis.update_cli("claude")
        self.assertTrue(r.ok and r.changed)
        self.assertEqual(r.summary(), "Claude Code 2.1.278 → 2.1.280")
        self.assertIn("Updating", r.output)

    def test_update_already_current(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_cli(d, "grok", ["grok 1.0.34 (abc) [stable]"])
            with patch("vision.clis.find_cli", return_value=exe):
                r = clis.update_cli("grok")
        self.assertTrue(r.ok)
        self.assertFalse(r.changed)
        self.assertEqual(r.summary(), "Grok 1.0.34 (already up to date)")

    def test_update_failure_and_missing_cli(self):
        with tempfile.TemporaryDirectory() as d:
            exe = _fake_cli(d, "codex", ["codex-cli 0.155.1"], update_rc=3)
            with patch("vision.clis.find_cli", return_value=exe):
                r = clis.update_cli("codex")
        self.assertFalse(r.ok)
        self.assertEqual(r.summary(), "Codex update failed: Updating…")

        with patch("vision.clis.find_cli", side_effect=RuntimeError("gone")):
            r = clis.update_cli("codex")
        self.assertFalse(r.ok)
        self.assertIn("not installed", r.summary())

    def test_update_summary_marks_each_outcome(self):
        results = [
            clis.UpdateResult("claude", "1", "2", 0),
            clis.UpdateResult("codex", "1", "1", 0),
            clis.UpdateResult("grok", None, None, 1, error="not installed"),
        ]
        text = _update_summary(results)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[ok]✓[/ok] Claude Code 1 → 2"))
        self.assertTrue(lines[1].startswith("[dim]·[/dim] Codex 1 (already up to date)"))
        self.assertTrue(lines[2].startswith("[err]✗[/err] Grok: not installed"))

    def test_update_clis_runs_in_order_and_announces_each(self):
        started = []
        with patch("vision.clis.update_cli", side_effect=lambda p, stream=False: clis.UpdateResult(p, "1", "1", 0)):
            results = clis.update_clis(("grok", "claude"), on_start=started.append)
        self.assertEqual(started, ["grok", "claude"])
        self.assertEqual([r.provider for r in results], ["grok", "claude"])


class ModelStampTests(unittest.TestCase):
    def test_a_fetched_model_list_is_not_asked_for_again_by_the_next_launch(self):
        """Every launch started the Claude initialize handshake, `codex debug models` and `grok models`
        in the background: the refresh time lived in memory only. A fetch that worked is now stamped on disk."""
        from unittest.mock import Mock

        hook = Mock(return_value=True)
        provider = SimpleNamespace(hook=lambda name: hook)
        with tempfile.TemporaryDirectory() as d, \
             patch.object(clis, "_stamp_file", lambda: __import__("pathlib").Path(d) / "models-refreshed.json"), \
             patch.dict(clis._models_at, clear=True), patch.object(clis, "_stamps_loaded", False), \
             patch.object(clis._registry, "get", return_value=provider):
            self.assertTrue(clis.refresh_models("claude"))
            hook.assert_called_once_with("claude")
            clis._models_at.clear()  # a new process
            clis._stamps_loaded = False
            self.assertFalse(clis.refresh_models("claude"))  # within MODELS_TTL: the stamp says so
            hook.assert_called_once()
            self.assertTrue(clis.refresh_models("claude", max_age=0))
            self.assertEqual(hook.call_count, 2)
            # A fetch that fails leaves no stamp: the next launch tries again.
            hook.return_value = False
            clis._models_at.clear()
            clis._stamps_loaded = False
            self.assertFalse(clis.refresh_models("codex", max_age=0))
            clis._models_at.clear()
            clis._stamps_loaded = False
            self.assertFalse(clis.refresh_models("codex"))  # tried (and failed) again, not skipped by a stamp
            self.assertEqual(hook.call_count, 4)


class DoctorUsageTests(unittest.TestCase):
    def test_usage_for_all_needs_no_current_brain(self):
        """`vision doctor` reads every provider's usage without a chat brain to start from."""
        made = []

        def create_brain(cfg, continue_session=False):
            made.append(cfg.model)
            return SimpleNamespace(provider=cfg.model)

        with patch("vision.brain.create_brain", side_effect=create_brain), patch("vision.cli._usage_for", side_effect=lambda b, full: f"{b.provider} usage"):
            result = _usage_selection(SimpleNamespace(brain=BrainConfig()), None, "all")
        self.assertEqual(made, ["opus", "gpt-6-astra", "grok-4.6"])  # each provider's default (the fallback lists here)
        self.assertIn("grok-4.6 usage", _render(result))


if __name__ == "__main__":
    unittest.main()
