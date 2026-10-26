"""`vision config get` and `set`: one setting read or changed in place, comments and order kept."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vision import config


class ConfigSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(config.DEFAULT_CONFIG, encoding="utf-8")
        self.patch = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_known_keys_cover_every_section_and_the_router_tables(self):
        keys = config.config_keys()
        for k in ("brain.mode", "voice.voice", "listen.input_device", "router.limits.timeout_s", "router.permissions.approval", "remote.port"):
            self.assertIn(k, keys, k)
        self.assertNotIn("brain.codex", keys)  # a sub-config, not a setting
        self.assertNotIn("router.agents", keys)  # free-form: any agent name

    def test_literals_keep_their_type_and_words_become_strings(self):
        self.assertEqual(config.parse_config_literal("true"), ("true", True))
        self.assertEqual(config.parse_config_literal("12"), ("12", 12))
        self.assertEqual(config.parse_config_literal('["a", "b"]'), ('["a", "b"]', ["a", "b"]))
        self.assertEqual(config.parse_config_literal("plan"), ('"plan"', "plan"))
        self.assertEqual(config.parse_config_literal("Austin, TX"), ('"Austin, TX"', "Austin, TX"))

    def test_set_edits_the_line_in_place(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(config.set_config_value("brain.mode", "plan"), '"plan"')
        after = self.path.read_text(encoding="utf-8")
        self.assertIn('\nmode = "plan"\n', after)
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))  # nothing added or lost, comments included
        self.assertEqual(config.load_config().brain.mode, "plan")
        self.assertEqual(config.get_config_value("brain.mode"), ("plan", True))

    def test_a_string_setting_takes_a_bare_number_as_text(self):
        self.assertEqual(config.set_config_value("brain.address_user_as", "7"), '"7"')
        self.assertEqual(config.load_config().brain.address_user_as, "7")

    def test_types_are_checked(self):
        with self.assertRaises(ValueError):
            config.set_config_value("brain.read_cli_logins", "yes")
        with self.assertRaises(ValueError):
            config.set_config_value("brain.stall_s", "soon")
        with self.assertRaises(ValueError):
            config.set_config_value("brain.denied_tools", "Bash(sudo:*)")
        config.set_config_value("brain.stall_s", "30")  # an int is a fine float
        config.set_config_value("brain.denied_tools", '["Bash(sudo:*)"]')
        self.assertEqual(config.load_config().brain.denied_tools, ["Bash(sudo:*)"])

    def test_unknown_keys_are_refused_with_the_nearest_ones(self):
        with self.assertRaises(ValueError) as cm:
            config.set_config_value("brain.modes", "plan")
        self.assertIn("brain.mode", str(cm.exception))
        with self.assertRaises(ValueError):
            config.set_config_value("mode", "plan")
        with self.assertRaises(KeyError):
            config.get_config_value("nothing.here")

    def test_sub_tables_take_any_key(self):
        config.set_config_value("router.agents.fast", "haiku")
        config.set_config_value("providers.ollama.base_url", "http://localhost:11434/v1")
        config.set_config_value("router.limits.max_rounds", "6")
        cfg = config.load_config()
        self.assertEqual(cfg.router.agents["fast"], "haiku")
        self.assertEqual(cfg.router.max_rounds, 6)
        self.assertEqual(config.get_config_value("providers.ollama.base_url"), ("http://localhost:11434/v1", True))

    def test_get_falls_back_to_the_default(self):
        self.path.write_text("[brain]\nmodel = \"sonnet\"\n", encoding="utf-8")
        self.assertEqual(config.get_config_value("brain.model"), ("sonnet", True))
        self.assertEqual(config.get_config_value("brain.mode"), ("auto", False))

    def test_the_commands(self):
        from typer.testing import CliRunner

        from vision.cli import app

        runner = CliRunner()
        with mock.patch("vision.cli.CONFIG_PATH", self.path):
            r = runner.invoke(app, ["config", "set", "brain.mode", "plan"])
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertIn('[brain] mode = "plan"', r.output)
            r = runner.invoke(app, ["config", "get", "brain.mode"])
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertEqual(r.output.strip(), "plan")
            r = runner.invoke(app, ["config", "set", "brain.nope", "1"])
            self.assertEqual(r.exit_code, 1)
            self.assertIn("no setting called brain.nope", r.output)
            r = runner.invoke(app, ["config"])
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertIn("[brain]", r.output)


if __name__ == "__main__":
    unittest.main()
