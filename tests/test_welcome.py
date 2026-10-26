"""The guided setup (vision/welcome.py) with scripted answers."""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vision import clis, config, providers, welcome


# Where each CLI keeps its login under the test's HOME. The tests "sign in" by writing the file, and
# clis.login_status is replaced by a look for it, so nothing runs a real CLI.
LOGIN_FILES = {"claude": ".claude/.credentials.json", "codex": ".codex/auth.json", "grok": ".grok/auth.json"}


def saved_login(name, thorough=False):
    there = (Path(os.environ["HOME"]) / LOGIN_FILES[name]).is_file()
    return there, "signed in" if there else "not signed in"


class ScriptedIO:
    """Answers in order; every question and command is kept for the assertions."""

    def __init__(self, ticks=None, picks=(), answers=(), confirms=(), run_rc=0):
        self.ticks = ticks  # what the tick list returns (None: Esc)
        self.picks, self.answers, self.confirms = list(picks), list(answers), list(confirms)
        self.listed = None  # the tick list: (rows' values, which started ticked)
        self.said: list[str] = []
        self.asked: list = []
        self.ran: list = []
        self.checks: list[str] = []  # the spinner lines
        self.run_rc = run_rc

    def say(self, text=""):
        from rich.console import Console

        c = Console(record=True, width=120, file=StringIO())  # as plain text, markup and cards drawn
        c.print(text)
        self.said.append(c.export_text().rstrip("\n"))

    def pick(self, title, options, current=""):
        rows = [r for o in options for r in (o[1] if isinstance(o[1], list) else [o])]  # provider tabs, flattened
        self.asked.append((title, [r[0] for r in rows]))
        return self.picks.pop(0) if self.picks else None

    def pick_many(self, title, options, ticked=()):
        self.listed = ([o[0] for o in options], set(ticked))
        return self.ticks

    def ask(self, question, default="", secret=False):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else default

    def confirm(self, question, default=True):
        self.asked.append(question)
        return self.confirms.pop(0) if self.confirms else default

    def run(self, cmd, shell=False):
        self.ran.append(cmd)
        return self.run_rc

    def open_url(self, url):
        return False

    @contextmanager
    def working(self, text):
        self.checks.append(text)
        yield

    def text(self):
        return "\n".join(self.said)


class WizardCase(unittest.TestCase):
    """A machine with nothing on it: its own config file and HOME, no CLI, no server, no voice hardware.
    Each test puts there what it needs (tests/test_onboard.py starts from the same place)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text('[brain]\nmodel = "opus"\neffort = "high"\n', encoding="utf-8")
        self.saved = []
        self._patches = [patch.object(config, "CONFIG_PATH", self.path), patch.object(config, "ensure_dirs"),
                         patch("vision.config.save_brain_defaults", side_effect=lambda m, e: self.saved.append((m, e))),
                         patch("vision.cli.save_brain_defaults", side_effect=lambda m, e: self.saved.append((m, e))),
                         patch.object(clis, "refresh_models_soon"),
                         # no CLI anywhere: not on PATH, and not at the ~/.local/bin fallbacks the finders try
                         patch.dict(os.environ, {"PATH": "/nonexistent", "HOME": self.tmp.name}),
                         patch.object(welcome, "list_models", side_effect=lambda url, key="", timeout=4: self.servers.get(url))]
        self._patches += [patch.object(clis, "login_status", side_effect=saved_login),
                          # nothing on this machine's ports counts (a dev box often has something on :8080)
                          patch.object(providers, "_server_answers", return_value=False),
                          patch.object(providers, "_is_model_server", return_value=True),  # what answers is one
                          patch("vision.install.voice_backend", side_effect=lambda: self.voice[0]),
                          patch("vision.install.voice_hardware", side_effect=lambda: self.voice[1]),
                          patch("vision.install.voice_install_cmd", side_effect=lambda b: ["setup.sh", "--voice", f"--{b}"])]
        self.voice = [None, None]  # the voice build installed, and what this machine could run it on
        self.servers = {}  # url -> the models a server there lists (missing: nothing answers)
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", *(p.key_env for p in welcome.HOSTED_APIS)):
            self._patches.append(patch.dict(os.environ, {var: ""}))
        for p in self._patches:
            p.start()
        providers.forget_ready()
        self.addCleanup(providers.register_endpoints, {})
        self.addCleanup(providers.forget_ready)
        self.addCleanup(self.tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def text(self, io):
        return "\n".join(io.said)

    def wizard(self, io, first_run=False):
        return welcome.Wizard(io, config.load_config(), first_run=first_run)


class WizardTests(WizardCase):
    def test_a_running_server_is_found_ticked_and_added_with_no_questions(self):
        self.servers["http://localhost:11434/v1"] = ["qwen3", "llama4"]
        io = ScriptedIO(ticks=["_server:ollama"], picks=["ollama/qwen3"])
        with patch.object(clis, "local_model_ids", return_value=["qwen3", "llama4"]), \
                patch.object(providers, "_server_answers", side_effect=lambda url: "11434" in url):
            ready = self.wizard(io, first_run=True).run()
        rows, ticked = io.listed
        self.assertEqual(rows, ["claude", "codex", "grok", "_server:ollama", welcome.SERVER, welcome.HOSTED, welcome.AGENT])
        self.assertEqual(ticked, {"_server:ollama"})  # running and free: ticked on a first run
        self.assertEqual(ready, ["ollama"])
        self.assertEqual([q for q in io.asked if isinstance(q, str)], [])  # not one typed question
        self.assertIn('base_url = "http://localhost:11434/v1"', self.path.read_text())
        self.assertIn("2 models: qwen3, llama4", self.text(io))
        self.assertEqual(io.asked[-1], ("Which model should Vision start on?", ["ollama/qwen3", "ollama/llama4"]))
        self.assertEqual(self.saved, [("ollama/qwen3", "off")])  # a local model starts with thinking off
        self.assertNotIn("All set.", self.text(io))  # the first chat says it instead (SET_UP_NOTE)

    def test_several_ticked_are_set_up_in_turn(self):
        self.servers["http://localhost:11434/v1"] = ["qwen3"]
        io = ScriptedIO(ticks=["grok", "_server:ollama", welcome.AGENT], answers=["", "gemini --experimental-acp"])
        with patch.object(clis, "local_model_ids", return_value=["qwen3"]):
            self.wizard(io).run()
        text = self.text(io)
        self.assertLess(text.index("◆ Grok"), text.index("◆ Ollama"))
        self.assertLess(text.index("◆ Ollama"), text.index("◆ An ACP agent"))
        self.assertIn("[providers.ollama]", self.path.read_text())
        self.assertIn("[providers.gemini]", self.path.read_text())

    def test_a_later_run_shows_what_is_on_and_unticking_switches_it_off(self):
        config.save_provider_table("ollama", {"base_url": "http://localhost:11434/v1", "label": "Ollama"})
        config.save_provider_table("gemini", {"type": "acp", "command": ["gemini"], "label": "Gemini"})
        config.save_enabled_providers(["claude", "ollama", "gemini"])
        io = ScriptedIO(ticks=["gemini"])
        self.wizard(io).run()
        rows, ticked = io.listed
        self.assertIn("ollama", rows)
        self.assertEqual(ticked, {"ollama", "gemini"})  # Claude is on but not installed here: unticked
        self.assertIn('enabled = ["gemini"]', self.path.read_text())
        self.assertIn("[providers.ollama]", self.path.read_text())  # off, not deleted
        self.assertIn("Switched off: Ollama. Its settings are kept; tick it again to bring it back.", self.text(io))  # Claude was never set up: no news

    def test_unticking_everything_keeps_what_is_on(self):
        config.save_provider_table("ollama", {"base_url": "http://localhost:11434/v1", "label": "Ollama"})
        config.save_enabled_providers(["ollama"])
        io = ScriptedIO(ticks=[])
        self.wizard(io).run()
        self.assertIn('enabled = ["ollama"]', self.path.read_text())
        self.assertIn("Vision needs at least one provider", self.text(io))

    def test_esc_on_the_tick_list_changes_nothing(self):
        before = self.path.read_text()
        io = ScriptedIO(ticks=None)
        self.assertEqual(self.wizard(io).run(), [])
        self.assertEqual(self.path.read_text(), before)

    def test_a_server_that_is_not_running_says_how_to_start_it_and_is_saved_anyway(self):
        io = ScriptedIO(ticks=[welcome.SERVER], picks=["lmstudio"])
        self.wizard(io).run()
        self.assertIn("Developer tab", self.text(io))
        self.assertIn("Saved anyway", self.text(io))
        self.assertIn("[providers.lmstudio]", self.path.read_text())

    def test_a_cli_is_installed_and_signed_in_on_request(self):
        io = ScriptedIO(ticks=["claude"], confirms=[True, True])

        def find():
            if not io.ran:  # not there until the installer has run
                raise RuntimeError("not found")
            return "/usr/local/bin/claude"

        def login(cmd, shell=False):
            io.ran.append(cmd)
            if isinstance(cmd, list):  # the login writes its credentials
                creds = Path(self.tmp.name) / ".claude"
                creds.mkdir(exist_ok=True)
                (creds / ".credentials.json").write_text("{}")
            return 0

        io.run = login
        with patch("vision.brain.find_claude", side_effect=find), patch("shutil.which", side_effect=lambda n: "/usr/local/bin/claude" if n == "claude" else None), \
                patch.object(providers, "model_tabs", return_value=[("Claude", [("opus", "Opus", ""), ("sonnet", "Sonnet", "")], "")]):
            ready = self.wizard(io).run()
        self.assertEqual(io.ran, ["curl -fsSL https://claude.ai/install.sh | bash", ["/usr/local/bin/claude", "auth", "login"]])
        self.assertIn("Claude Code is installed", self.text(io))
        self.assertIn("Claude is ready", self.text(io))
        self.assertEqual(ready, ["claude"])
        self.assertEqual(io.asked[-1], ("Which model should Vision start on?", ["opus", "sonnet"]))
        self.assertIn("All set.", self.text(io))

    def test_a_cli_that_is_ready_and_on_is_left_alone(self):
        creds = Path(self.tmp.name) / ".codex"
        creds.mkdir()
        (creds / "auth.json").write_text("{}")
        io = ScriptedIO(ticks=["codex", "local"])
        with patch("vision.codex.find_codex", return_value="/usr/bin/codex"):
            self.wizard(io).run()
        self.assertEqual(io.listed[1], {"codex", "local"})  # [local] is on, its server just isn't up
        self.assertNotIn("◆ Codex", self.text(io))
        self.assertEqual(io.ran, [])
        self.assertIn("Nothing changed", self.text(io))

    def test_an_existing_login_is_not_asked_for_again(self):
        creds = Path(self.tmp.name) / ".codex"
        creds.mkdir()
        (creds / "auth.json").write_text("{}")
        config.save_enabled_providers(["claude"])
        io = ScriptedIO(ticks=["codex"])
        with patch("vision.codex.find_codex", return_value="/usr/bin/codex"):
            self.wizard(io).run()
        self.assertIn("Codex is installed and signed in", self.text(io))
        self.assertEqual(io.ran, [])
        self.assertIn('enabled = ["codex"]', self.path.read_text())

    def test_a_cli_with_only_a_page_points_there_and_reports_still_missing(self):
        io = ScriptedIO(ticks=["grok"])
        ready = self.wizard(io).run()
        self.assertEqual(io.ran, [])
        self.assertIn("https://x.ai/cli", self.text(io))
        self.assertIn("Can't see `grok` yet", self.text(io))
        self.assertIn("Nothing's ready yet", self.text(io))
        self.assertEqual((ready, self.saved), ([], []))

    def test_a_pasted_key_is_saved_privately_and_a_long_model_list_is_narrowed(self):
        self.servers["https://openrouter.ai/api/v1"] = [f"m/{i}" for i in range(40)]
        io = ScriptedIO(ticks=[welcome.HOSTED], picks=["openrouter"], answers=["sk-or-123", "m/1, m/2"])
        with patch.object(clis, "local_model_ids", return_value=["m/1", "m/2"]):
            self.wizard(io).run()
        text = self.path.read_text()
        self.assertIn('api_key = "sk-or-123"', text)
        self.assertIn('models = ["m/1", "m/2"]', text)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("OpenRouter added · 2 models: m/1, m/2", self.text(io))

    def test_a_key_already_in_the_environment_is_used_by_name(self):
        self.servers["https://api.groq.com/openai/v1"] = ["llama"]
        io = ScriptedIO(ticks=[welcome.HOSTED], picks=["groq"])
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk"}):
            self.wizard(io).run()
        text = self.path.read_text()
        self.assertIn('api_key_env = "GROQ_API_KEY"', text)
        self.assertNotIn("gsk", text)
        self.assertIn("Using the key in $GROQ_API_KEY", self.text(io))

    def test_an_agent_is_named_after_its_command(self):
        io = ScriptedIO(ticks=[welcome.AGENT], answers=["gemini --experimental-acp"])
        self.wizard(io).run()
        self.assertIn('[providers.gemini]', self.path.read_text())
        self.assertIn('command = ["gemini", "--experimental-acp"]', self.path.read_text())
        self.assertIn("`gemini` isn't on PATH here", self.text(io))

    def test_esc_at_a_question_skips_that_one_and_carries_on(self):
        io = ScriptedIO(ticks=[welcome.AGENT, welcome.SERVER], picks=["lmstudio"])
        calls = []

        def ask(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise welcome.Back
            return ""

        io.ask = ask
        self.wizard(io).run()
        self.assertIn("Skipped.", self.text(io))
        self.assertNotIn("[providers.gemini]", self.path.read_text())
        self.assertIn("[providers.lmstudio]", self.path.read_text())

    def test_no_voice_offer_on_a_machine_that_cannot_run_it_or_already_has_it(self):
        for self.voice in ([None, None], ["mac", ("mac", "Apple Silicon")]):
            io = ScriptedIO()
            self.assertFalse(welcome.voice_step(io))
            self.assertEqual((io.asked, io.said), ([], []))
        self.voice = [None, None]
        io = ScriptedIO()
        welcome.voice_step(io, asked=True)  # `vision voice setup` says why
        self.assertIn("needs an Apple Silicon Mac or an NVIDIA graphics card", self.text(io))

    def test_a_small_card_is_warned_about_and_not_now_installs_nothing(self):
        self.voice[1] = ("nvidia", "GTX 1650 · 4 GB")
        io = ScriptedIO(picks=["no"])
        self.assertFalse(welcome.voice_step(io))
        self.assertIn("has 4 GB of memory and the voice wants 8", self.text(io))
        self.assertIn("`vision voice setup` adds the voice whenever", self.text(io))
        self.assertEqual(io.ran, [])

    def test_a_login_that_did_not_take_is_offered_again_and_is_never_the_default(self):
        io = ScriptedIO(ticks=["claude"], confirms=[True, False])  # sign in: yes; try again: no
        with patch("vision.brain.find_claude", return_value="/usr/local/bin/claude"), \
                patch("shutil.which", side_effect=lambda n: "/usr/local/bin/claude" if n == "claude" else None):
            ready = self.wizard(io).run()
        self.assertEqual(io.ran, [["/usr/local/bin/claude", "auth", "login"]])  # it ran, and wrote no credentials
        self.assertIn("Still not signed in.", self.text(io))
        self.assertIn("Try signing in again?", " ".join(q for q in io.asked if isinstance(q, str)))
        self.assertIn("Claude can't answer until it's signed in. Run `claude auth login`", self.text(io))
        self.assertIn("Nothing's ready yet", self.text(io))
        self.assertEqual((ready, self.saved), ([], []))

    def test_a_provider_that_is_on_survives_a_sign_in_check_that_failed_for_a_moment(self):
        config.save_enabled_providers(["claude"])
        io = ScriptedIO(ticks=["claude"])  # Enter on the list as it came up
        with patch("vision.brain.find_claude", return_value="/usr/local/bin/claude"), \
                patch("shutil.which", side_effect=lambda n: "/usr/local/bin/claude" if n == "claude" else None), \
                patch.object(clis, "login_status", side_effect=[(False, "the check timed out"), (True, "logged in")]):
            ready = self.wizard(io).run()
        self.assertIn("claude", io.listed[1])  # on, so ticked, though the first check said signed out
        self.assertEqual((ready, self.saved, io.ran), (["claude"], [], []))  # asked again: fine, nothing moved
        self.assertNotIn("Sign in now?", " ".join(q for q in io.asked if isinstance(q, str)))
        self.assertIn('enabled = ["claude"]', self.path.read_text())

    def test_vision_onboard_again_keeps_a_provider_that_is_on_ticked_too(self):
        config.save_enabled_providers(["claude"])
        io = ScriptedIO(ticks=["claude"])
        with patch("vision.brain.find_claude", return_value="/usr/local/bin/claude"), \
                patch("shutil.which", side_effect=lambda n: "/usr/local/bin/claude" if n == "claude" else None), \
                patch.object(clis, "login_status", side_effect=[(False, "the check timed out"), (True, "logged in")]):
            ready = welcome.Wizard(io, config.load_config(), first_run=True, again=True).run(intro=False)
        self.assertIn("claude", io.listed[1])
        self.assertEqual((ready, self.saved, io.ran), (["claude"], [], []))
        self.assertIn('enabled = ["claude"]', self.path.read_text())

    def test_a_port_that_answers_without_models_is_not_a_provider(self):
        io = ScriptedIO(ticks=None)
        with patch.object(providers, "_server_answers", return_value=True), \
                patch.object(clis, "local_model_ids", side_effect=OSError("HTTP Error 404: Not Found")):
            ready = self.wizard(io, first_run=True).run()
        self.assertNotIn("local", io.listed[0])  # something else is on :8080: no "Local · set up" row
        self.assertEqual((ready, self.saved), ([], []))
        self.assertIn("Nothing's ready yet", self.text(io))


if __name__ == "__main__":
    unittest.main()


class SetupRowsTests(unittest.TestCase):
    """`vision provider status --json`: what the desktop app's first run shows and runs."""

    def test_each_cli_says_installed_signed_in_and_how_to_set_it_up(self):
        from vision import welcome

        ready = {"claude": True, "codex": False, "grok": True}
        logins = {"claude": (True, "logged in"), "grok": (False, "not logged in")}
        with patch.object(welcome.providers, "ready", lambda name, cfg=None: ready.get(name, False)), \
             patch.object(welcome.clis, "login_status", lambda name, **kw: logins[name]), \
             patch.object(welcome, "_exe", lambda p: f"/opt/{p.login_cmd[0]}"):
            rows = {r["name"]: r for r in welcome.setup_rows(welcome.load_config())}
        self.assertEqual(set(rows), {"claude", "codex", "grok"})
        self.assertTrue(rows["claude"]["signed_in"])
        self.assertEqual(rows["claude"]["login_cmd"], ["/opt/claude", "auth", "login"])
        self.assertFalse(rows["codex"]["installed"])
        self.assertFalse(rows["codex"]["signed_in"])  # not installed: not asked
        self.assertEqual(rows["codex"]["install_cmd"], "npm i -g @openai/codex")
        self.assertEqual(rows["grok"]["detail"], "not logged in")
        self.assertTrue(rows["grok"]["install_url"])
