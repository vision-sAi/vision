"""The first run (vision/onboard.py) with scripted answers."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_welcome import ScriptedIO, WizardCase
from vision import clis, config, onboard, providers, service, turnjournal, welcome


class FirstRunTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.path.write_text('[brain]\nmodel = "opus"\neffort = "high"\naddress_user_as = ""\nmode = "auto"\n', encoding="utf-8")
        root = Path(self.tmp.name)
        self.state = root / "onboarding.json"
        self.journals = root / "chat-journals"
        for p in (patch.object(onboard, "STATE", self.state), patch.object(turnjournal, "JOURNAL_DIR", self.journals),
                  patch.object(onboard, "STATE_DIR", root / "state")):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("VISION_NO_SETUP", None)

    def claude(self, signed_in=True):
        """Claude Code on this machine, signed in or not."""
        for p in (patch("vision.brain.find_claude", return_value="/usr/local/bin/claude"),
                  patch("shutil.which", side_effect=lambda n: "/usr/local/bin/claude" if n == "claude" else None),
                  patch.object(providers, "model_tabs", return_value=[("Claude", [("opus", "Opus", ""), ("sonnet", "Sonnet", "")], "")])):
            p.start()
            self.addCleanup(p.stop)
        providers.forget_ready()
        if signed_in:
            self.sign_in()

    def sign_in(self):
        creds = Path(self.tmp.name) / ".claude"
        creds.mkdir(exist_ok=True)
        (creds / ".credentials.json").write_text("{}")

    def first_run(self, io):
        return onboard.first_run(config.load_config(), io, interactive=True)

    def saved_state(self):
        return json.loads(self.state.read_text())

    def test_with_a_provider_already_working_there_is_no_provider_question(self):
        self.claude()
        io = ScriptedIO(picks=["plan"], answers=["  Sam  "])
        self.assertIs(self.first_run(io), True)
        text = io.text()
        self.assertIsNone(io.listed)  # the tick list never came up
        self.assertIn("Claude is ready. `vision providers` adds more.", text)
        self.assertIn("◆ Your providers · step 1 of 3", text)  # no voice step: this machine can't run it
        self.assertLess(text.index("◆ You · step 2 of 3"), text.index("◆ Your phone · step 3 of 3"))
        self.assertIn("Vision will call you Sam.", text)
        self.assertIn("Vision starts in plan mode.", text)
        self.assertNotIn("◆ Voice", text)
        self.assertNotIn("All set.", text)  # the chat opens next and says so itself (welcome.set_up_note)
        cfg = config.load_config()
        self.assertEqual((cfg.brain.address_user_as, cfg.brain.mode), ("Sam", "plan"))
        self.assertEqual(self.saved_state(), {"done": ["providers", "you", "voice", "phone"], "finished": True})
        self.assertIsNone(self.first_run(ScriptedIO()))  # once is enough

    def test_with_nothing_working_the_tick_list_comes_first(self):
        self.servers["http://localhost:11434/v1"] = ["qwen3", "llama4"]
        io = ScriptedIO(ticks=["_server:ollama"], picks=["ollama/qwen3", "auto"])
        with patch.object(clis, "local_model_ids", return_value=["qwen3", "llama4"]), \
                patch.object(providers, "_server_answers", side_effect=lambda url: "11434" in url):
            self.assertIs(self.first_run(io), True)
        self.assertEqual(io.listed[1], {"_server:ollama"})  # found running: ticked for a first run
        self.assertIn("Tick everything you've got", io.text())
        self.assertEqual(io.text().count("◆ Vision"), 1)  # one welcome card, not the wizard's as well
        self.assertEqual(self.saved, [("ollama/qwen3", "off")])
        self.assertLess(io.text().index("Ollama added"), io.text().index("◆ You ·"))

    def test_a_cli_that_is_installed_but_signed_out_gets_its_login(self):
        self.claude(signed_in=False)
        io = ScriptedIO(ticks=["claude"], picks=["opus", "auto"], confirms=[True])
        io.run = lambda cmd, shell=False: (io.ran.append(cmd), self.sign_in(), 0)[-1]
        self.assertIs(self.first_run(io), True)
        self.assertEqual(io.ran, [["/usr/local/bin/claude", "auth", "login"]])
        self.assertIn("Claude is ready", io.text())
        self.assertIn("◆ You ·", io.text())

    def test_the_voice_is_a_step_of_its_own_where_the_machine_can_run_it(self):
        self.claude()
        self.voice[1] = ("nvidia", "RTX 4070 · 12 GB")
        io = ScriptedIO(picks=["auto", "yes"])
        self.assertIs(self.first_run(io), True)
        text = io.text()
        self.assertIn("◆ Voice · step 3 of 4", text)
        self.assertEqual(text.count("◆ Voice"), 1)  # the step's header, not the wizard's own as well
        self.assertEqual(io.ran[0], ["setup.sh", "--voice", "--nvidia"])
        self.assertEqual(io.ran[1][-3:], ["vision", "voice", "check"])  # then the speakers and the microphone, in a fresh process
        self.assertEqual(len(io.ran), 2)
        self.assertIn("RTX 4070 · 12 GB", text)
        self.assertIn("Voice is installed.", text)
        self.assertLess(text.index("Voice is installed."), text.index("◆ Your phone · step 4 of 4"))

    def test_skipped_questions_keep_what_is_saved(self):
        self.claude()
        io = ScriptedIO()  # Enter at the name, Esc at the mode
        self.assertIs(self.first_run(io), True)
        cfg = config.load_config()
        self.assertEqual((cfg.brain.address_user_as, cfg.brain.mode), ("", "auto"))
        self.assertNotIn("Vision will call you", io.text())
        self.assertIn("Vision starts in auto mode.", io.text())

    def test_without_a_provider_it_stops_and_resumes_at_that_step(self):
        io = ScriptedIO(ticks=None)  # Esc on the tick list
        self.assertIs(self.first_run(io), False)
        self.assertIn("Setup stops here for now.", io.text())
        self.assertNotIn("◆ You ·", io.text())
        self.assertFalse(self.state.exists())

        self.claude()
        io = ScriptedIO(picks=["auto"])
        self.assertIs(self.first_run(io), True)
        self.assertIn("◆ You ·", io.text())

    def test_leaving_halfway_resumes_after_the_steps_already_done(self):
        self.claude()
        io = ScriptedIO()
        io.ask = lambda question, default="", secret=False: (_ for _ in ()).throw(KeyboardInterrupt())  # Ctrl-C at the name
        self.assertIs(self.first_run(io), False)
        self.assertIn("Setup paused.", io.text())
        self.assertEqual(self.saved_state(), {"done": ["providers"], "finished": False})

        io = ScriptedIO(picks=["auto"], answers=["Sam"])
        self.assertIs(self.first_run(io), True)
        self.assertIn("Welcome back.", io.text())
        self.assertNotIn("◆ Your providers", io.text())
        self.assertIn("◆ You · step 2 of 3", io.text())
        self.assertEqual(config.load_config().brain.address_user_as, "Sam")

    def test_it_only_runs_for_a_person_and_can_be_switched_off(self):
        cfg = config.load_config()
        self.assertIsNone(onboard.first_run(cfg, ScriptedIO(), interactive=False))
        with patch.dict(os.environ, {"VISION_NO_SETUP": "1"}):
            self.assertIsNone(onboard.first_run(cfg, ScriptedIO(), interactive=True))
        self.assertFalse(self.state.exists())

    def test_a_machine_that_has_chatted_before_is_not_asked(self):
        self.claude()
        self.journals.mkdir()
        (self.journals / "terminal-abc.jsonl").write_text("{}\n")
        io = ScriptedIO()
        self.assertIsNone(self.first_run(io))
        self.assertEqual((io.said, io.asked), ([], []))
        self.assertTrue(self.saved_state()["finished"])

    def test_a_machine_used_before_the_journals_is_not_asked_either(self):
        self.claude()
        for marker in ("welcomed", "history", "last_session", "last_session.codex"):
            state = Path(self.tmp.name) / "state"
            state.mkdir(exist_ok=True)
            (state / marker).write_text("")
            io = ScriptedIO()
            self.assertIsNone(self.first_run(io), marker)
            self.assertEqual((io.said, io.asked), ([], []))
            (state / marker).unlink()
            self.state.unlink()

    def test_a_finished_machine_with_nothing_ready_gets_the_provider_wizard(self):
        self.state.write_text(json.dumps({"done": ["providers", "you", "voice", "phone"], "finished": True}))
        io = ScriptedIO(ticks=None)
        self.assertIs(self.first_run(io), True)
        self.assertIn("Welcome. Tick everything you've got", io.text())
        self.assertIsNotNone(io.listed)
        self.assertNotIn("◆ You ·", io.text())

    def test_ctrl_c_in_that_wizard_leaves_quietly(self):
        self.state.write_text(json.dumps({"done": ["providers", "you", "voice", "phone"], "finished": True}))
        io = ScriptedIO()
        io.pick_many = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
        self.assertIs(self.first_run(io), True)
        self.assertIn("Setup stopped", io.text())

    def test_onboard_again_asks_everything_and_ends_on_the_card(self):
        self.claude()
        self.path.write_text('[brain]\nmodel = "opus"\neffort = "high"\naddress_user_as = "Sam"\nmode = "plan"\n', encoding="utf-8")
        self.state.write_text(json.dumps({"done": ["providers", "you", "voice", "phone"], "finished": True}))
        io = ScriptedIO(ticks=["claude"])
        self.assertTrue(onboard.Flow(io, config.load_config()).run(again=True))
        self.assertEqual(io.listed[1], {"claude"})  # the tick list comes up even though Claude already works
        cfg = config.load_config()
        self.assertEqual((cfg.brain.address_user_as, cfg.brain.mode), ("Sam", "plan"))
        self.assertIn("All set.", io.text())
        self.assertIn("Vision calls you Sam and starts in plan mode.", io.text())


class PhoneStepTests(unittest.TestCase):
    """The phone step alone: each thing it needs is asked for, and it never holds the flow up."""

    SERVER = {"pid": 1, "url": "http://127.0.0.1:8765", "port": 8765, "token": "tok"}
    INFO = {"port": 8765, "url": "http://192.0.2.7:8765", "local": "http://192.0.2.7:8765", "payload": '{"url": "u", "token": "t"}'}

    def setUp(self):
        self.up = None  # the server running_server finds
        self.installed = True
        patches = [patch("vision.remote.running_server", side_effect=lambda: self.up),
                   patch.object(onboard, "serve_installed", side_effect=lambda: self.installed),
                   patch.object(onboard, "wait_for_server", side_effect=lambda: self.SERVER),
                   patch.object(onboard, "wait_for_phone", return_value=True),
                   patch.object(service, "kind", return_value="systemd"), patch.object(service, "install"),
                   patch("vision.server.pairing_info", return_value=self.INFO)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_step(self, **answers):
        io = ScriptedIO(**answers)
        self.assertTrue(onboard.phone_step(onboard.Flow(io, config.Config())))
        return io

    def test_a_no_leaves_everything_alone(self):
        io = self.run_step(confirms=[False])
        self.assertIn("Skipped.", io.text())
        service.install.assert_not_called()
        self.assertEqual(io.ran, [])

    def test_a_yes_starts_the_service_shows_the_code_and_waits_for_the_scan(self):
        io = self.run_step(confirms=[True, True])
        service.install.assert_called_once()
        self.assertIn("Vision runs in the background now and at every login.", io.text())
        self.assertIn("█", io.text())  # the QR
        self.assertIn("This code works on this Wi-Fi.", io.text())
        self.assertEqual(io.checks, ["starting vision serve…", "waiting for your phone… Ctrl-C skips"])
        self.assertIn("Your phone is connected.", io.text())

    def test_a_server_already_up_is_used_as_it_is(self):
        self.up = self.SERVER
        io = self.run_step(confirms=[True])
        service.install.assert_not_called()
        self.assertEqual(io.asked, ["Set up your iPhone now?"])
        self.assertIn("Your phone is connected.", io.text())

    def test_the_server_packages_are_installed_on_a_yes(self):
        self.installed = False
        io = ScriptedIO(confirms=[True, True, True])

        def run(cmd, shell=False):
            io.ran.append(cmd)
            self.installed = True
            return 0

        io.run = run
        self.assertTrue(onboard.phone_step(onboard.Flow(io, config.Config())))
        self.assertEqual(len(io.ran), 1)
        self.assertIn("--extra serve", io.ran[0])
        self.assertIn("Your phone is connected.", io.text())

    def test_without_the_service_it_points_at_vision_serve(self):
        io = self.run_step(confirms=[True, False])
        service.install.assert_not_called()
        self.assertIn("`vision serve` in a terminal starts it", io.text())
        self.assertNotIn("scan this", io.text())

    def test_a_service_that_will_not_start_says_why_and_moves_on(self):
        service.install.side_effect = service.ServiceError("`systemctl --user enable vision.service` failed: no bus")
        io = self.run_step(confirms=[True, True])
        self.assertIn("The background service didn't start:", io.text())
        self.assertIn("no bus", io.text())

    def test_no_scan_is_not_a_failure(self):
        with patch.object(onboard, "wait_for_phone", side_effect=KeyboardInterrupt):
            io = self.run_step(confirms=[True, True])
        self.assertIn("No phone yet. `vision pair` shows the code again", io.text())


class ServiceUnitTests(unittest.TestCase):
    def test_the_systemd_unit_runs_this_interpreter_with_the_installing_shells_path(self):
        unit = service.systemd_unit(["/opt/my vision/.venv/bin/python", "-m", "vision", "serve", "--no-qr"], "/home/me/.local/bin:/usr/bin")
        self.assertIn("ExecStart='/opt/my vision/.venv/bin/python' -m vision serve --no-qr\n", unit)
        self.assertIn('Environment="PATH=/home/me/.local/bin:/usr/bin"\n', unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn('Environment="VISION_SERVICE=1"\n', unit)  # serve waits for one already up, then takes over

    def test_the_service_never_keeps_visions_shims_on_its_path(self):
        # Installed from a shell inside a Vision turn, the PATH starts with the shims that block `claude`;
        # saved as is, every chat the service runs would be blocked. Another Vision's (a dev copy's) too.
        import tempfile

        from vision import brain

        with tempfile.TemporaryDirectory() as tmp:
            ours, dev, real = (os.path.join(tmp, d) for d in ("vision/shims", "vision-dev/vision/shims", "bin"))
            for d in (ours, dev, real):
                os.makedirs(d)
            for d in (ours, dev):
                Path(d, "claude").write_text(brain._SHIM, encoding="utf-8")
            Path(real, "claude").write_text("#!/bin/sh\n", encoding="utf-8")
            path = os.pathsep.join([ours, dev, real, "/usr/bin"])
            self.assertEqual(brain.without_shims(path), os.pathsep.join([real, "/usr/bin"]))

            plist = Path(tmp, "agent.plist")
            with patch.dict(os.environ, {"PATH": path}), patch.object(service, "kind", return_value="launchd"), \
                    patch.object(service, "unit_path", return_value=plist), patch.object(service, "_run"), \
                    patch.object(service, "STATE_DIR", Path(tmp, "state")):
                service.install()
            import plistlib

            saved = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"]
            self.assertEqual(saved, os.pathsep.join([real, "/usr/bin"]))

    def test_the_service_waits_for_a_server_already_up_and_then_goes_on(self):
        answers, naps = [True, True, False], []
        service.wait_while(lambda: answers.pop(0), poll=2, sleep=naps.append)
        self.assertEqual((answers, naps), ([], [2, 2]))

    def test_the_launchd_agent_is_a_valid_plist_that_starts_at_login(self):
        import plistlib

        agent = plistlib.loads(service.launchd_plist(["/opt/v/.venv/bin/python", "-m", "vision", "serve", "--no-qr"], "/a:/b&c").encode())
        self.assertEqual(agent["Label"], "com.vision.serve")
        self.assertEqual(agent["ProgramArguments"][-2:], ["serve", "--no-qr"])
        self.assertEqual((agent["RunAtLoad"], agent["KeepAlive"], agent["EnvironmentVariables"]["PATH"]), (True, True, "/a:/b&c"))
        self.assertEqual(agent["EnvironmentVariables"]["VISION_SERVICE"], "1")

    def test_on_a_mac_serving_holds_the_computer_awake_with_caffeinate_for_as_long_as_the_server_lives(self):
        from unittest import mock

        calls = []
        with mock.patch.object(service.sys, "platform", "darwin"), mock.patch.object(service.shutil, "which", return_value="/usr/bin/caffeinate"):
            proc = service.stay_awake(pid=4242, popen=lambda argv, **kw: calls.append(argv) or "proc")
        self.assertEqual((proc, calls), ("proc", [["/usr/bin/caffeinate", "-s", "-w", "4242"]]))
        with mock.patch.object(service.sys, "platform", "linux"):
            self.assertIsNone(service.stay_awake(pid=1, popen=lambda *a, **kw: self.fail("no caffeinate on Linux")))


if __name__ == "__main__":
    unittest.main()
