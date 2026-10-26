import io
import os
import tempfile
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)

from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys
from rich.console import Console

from vision import brain as brain_mod
from vision.ui import ChatScreen, shell_output_grid


class ShellModeBoxTests(unittest.TestCase):
    """`!` on an empty box turns it into a shell prompt, as in Claude Code."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.screen = ChatScreen(os.path.join(self.dir.name, "history"), lambda: "")
        self.ran, self.sent = [], []
        self.screen.on_shell = self.ran.append
        self.screen.on_submit = self.sent.append

    def press(self, key):
        binding = [b for b in self.screen.app.key_bindings.get_bindings_for_keys((key,)) if b.filter()][-1]
        binding.handler(type("Event", (), {"app": self.screen.app})())

    def type(self, text):
        self.screen.area.buffer.set_document(Document(text, len(text)))

    def test_bang_on_an_empty_box_enters_shell_mode_and_enter_runs_the_line(self):
        self.press("!")
        self.assertTrue(self.screen.shell_mode)
        self.assertEqual(self.screen.area.text, "", "the ! is the switch, not part of the command")
        self.type("ls -la")
        self.press(Keys.ControlM)
        self.assertEqual(self.ran, ["ls -la"])
        self.assertEqual(self.sent, [])
        self.assertFalse(self.screen.shell_mode, "back to a message box after it runs")
        self.assertEqual(self.screen.area.text, "")

    def test_a_bang_inside_a_message_is_just_typed(self):
        self.type("hi")
        self.assertFalse([b for b in self.screen.app.key_bindings.get_bindings_for_keys(("!",)) if b.filter()])

    def test_escape_or_backspace_on_an_empty_box_leaves_shell_mode(self):
        self.press("!")
        self.press(Keys.Backspace)
        self.assertFalse(self.screen.shell_mode)
        self.press("!")
        self.type("rm -rf build")
        self.press(Keys.Escape)
        self.assertFalse(self.screen.shell_mode)
        self.assertEqual(self.screen.area.text, "", "Esc drops the half-typed command")
        self.assertEqual(self.ran, [])

    def test_a_path_in_shell_mode_opens_no_slash_menu_and_no_suggestion_shows(self):
        from vision.ui import SlashCommand

        self.screen.commands = [SlashCommand("usage", "show usage")]
        self.screen.suggestion = "yes"
        self.press("!")
        self.type("/usr/bin/env")
        self.assertEqual(self.screen._menu_rows(), [])
        self.assertEqual(self.screen._suggestion_shown(), "")

    def test_the_output_shows_its_end_and_how_it_ended(self):
        def shown(output, code):
            console = Console(width=80, record=True, file=io.StringIO())
            console.print(shell_output_grid(output, code))
            return console.export_text()

        self.assertIn("hello", shown("hello\n", 0))
        self.assertNotIn("exit", shown("hello\n", 0))
        self.assertIn("exit 2", shown("nope\n", 2))
        self.assertIn("stopped", shown("", None))
        long = shown("\n".join(f"line {i}" for i in range(500)), 0)
        self.assertIn("300 earlier lines", long)
        self.assertIn("line 499", long)
        self.assertNotIn("line 10\n", long)

    def test_a_running_command_shows_what_it_has_printed_and_no_ending(self):
        console = Console(width=80, record=True, file=io.StringIO())
        console.print(shell_output_grid("", None, running=True))
        console.print(shell_output_grid("code: 7ADB-FA3A\n", None, running=True))
        text = console.export_text()
        self.assertIn("running…", text)
        self.assertIn("code: 7ADB-FA3A", text)
        self.assertNotIn("stopped", text, "it hasn't been stopped, it's still going")


class RunTests(unittest.TestCase):
    """shellmode.run: what a command prints arrives as it prints it, not only once it ends."""

    def test_output_arrives_while_the_command_still_runs(self):
        import threading
        import time

        from vision import shellmode

        first = threading.Event()
        chunks = []

        def on_output(text):
            chunks.append(text)
            first.set()

        began = time.monotonic()
        done = []
        runner = threading.Thread(target=lambda: done.append(shellmode.run("printf 'code: 7ADB-FA3A\\n'; sleep 1.5; echo done", None,
                                                                            on_output=on_output)))
        runner.start()
        self.assertTrue(first.wait(1.0), "the code shows before the command finishes waiting")
        self.assertLess(time.monotonic() - began, 1.2)
        self.assertIn("7ADB-FA3A", "".join(chunks))
        runner.join(5)
        self.assertEqual(done, [("code: 7ADB-FA3A\ndone\n", 0)], "all of it is still returned at the end")

    def test_stderr_and_the_exit_code_come_back(self):
        from vision import shellmode

        self.assertEqual(shellmode.run("echo out; echo err >&2; exit 3", None), ("out\nerr\n", 3))

    def test_a_character_split_across_reads_comes_out_whole(self):
        from vision import shellmode

        output, code = shellmode.run("printf 'caf\\303'; sleep 0.2; printf '\\251\\n'", None)
        self.assertEqual((output, code), ("café\n", 0))

    def test_something_left_running_in_the_background_doesnt_hold_it_up(self):
        import time
        from unittest.mock import patch

        from vision import shellmode

        began = time.monotonic()
        with patch.object(shellmode, "DRAIN_S", 0.3):
            output, code = shellmode.run("sleep 20 & echo started", None)
        self.assertEqual((output, code), ("started\n", 0))
        self.assertLess(time.monotonic() - began, 3, "the sleep keeps the pipe open; the command itself is done")


class LiveTests(unittest.TestCase):
    """shellmode.Live: a burst is shown at most every so often, and the last of it shortly after."""

    def test_a_burst_shows_the_first_chunk_now_and_the_rest_soon(self):
        import time

        from vision import shellmode

        shown = []
        live = shellmode.Live(shown.append, every=0.2)
        live.add("a")
        live.add("b")
        live.add("c")
        self.assertEqual(shown, ["a"], "the first straight away, the rest held for a moment")
        time.sleep(0.35)
        self.assertEqual(shown, ["a", "abc"], "the last line isn't held back until something else is printed")
        live.close()
        live.add("d")
        time.sleep(0.3)
        self.assertEqual(shown, ["a", "abc"], "nothing after it's over: the caller shows the final output")


class ShellNoteTests(unittest.TestCase):
    """What ran in shell mode reaches the brain with the next message, and history views take it off."""

    def test_the_next_prompt_carries_the_commands_once(self):
        class B:
            workdir, session_id, handoff, catchup = "/home/me/proj", "s1", None, None

        b = B()
        b.shell = brain_mod.shell_note([("git status", "clean\n", 0), ("make", "boom\n", 2), ("sleep 99", "", None)])
        prompt = brain_mod.inject_handoff(b, "why did make fail?")
        self.assertIn("<bash-input>git status</bash-input>", prompt)
        self.assertIn("<bash-exit>2</bash-exit>", prompt)
        self.assertIn("<bash-exit>stopped</bash-exit>", prompt)
        self.assertIn("/home/me/proj", prompt)
        self.assertTrue(prompt.endswith("why did make fail?"))
        self.assertEqual(brain_mod.inject_handoff(b, "again"), "again", "told once")

    def test_history_shows_only_the_message(self):
        from vision.sessions import _append_turn

        class B:
            workdir, session_id, handoff, catchup = "/p", "s1", None, None

        b = B()
        b.shell = brain_mod.shell_note([("ls", "a\n", 0)])
        out = []
        _append_turn(out, "user", brain_mod.inject_handoff(b, "what's in here?"))
        self.assertEqual(out, [{"role": "user", "text": "what's in here?"}])

    def test_a_joined_terminal_sends_them_with_its_message(self):
        from vision.config import BrainConfig
        from vision.remote import RemoteBrain

        rb = RemoteBrain({"url": "http://x", "token": "t"}, {"chat": "c1"}, BrainConfig())
        sent = []
        rb.send = sent.append
        rb._frames.put({"type": "done", "text": "ok"})
        rb.shell = "<bash-input>ls</bash-input>"
        rb.ask("what's here?")
        self.assertEqual(sent[0]["shell"], "<bash-input>ls</bash-input>")
        self.assertIsNone(rb.shell)


if __name__ == "__main__":
    unittest.main()


class PhoneShellTests(unittest.TestCase):
    """Shell mode from the phone: the server runs the command in the chat's folder and keeps it for the brain."""

    def _chat(self, workdir):
        from unittest.mock import patch

        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider = "claude"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff, self.workdir = cfg, session_id, None, workdir

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        creating = patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id")))
        creating.start()
        self.addCleanup(creating.stop)
        hub = Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=False)
        posted = []
        hub.post = posted.append
        chat = hub.open_chat()
        chat._transcript = []
        return chat, posted

    def test_a_command_runs_in_the_chats_folder_and_waits_for_the_next_turn(self):
        import asyncio

        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "marker.txt"), "w").close()
            chat, posted = self._chat(folder)
            asyncio.run(chat.run_shell("ls; exit 3"))
        frames = [e for e in posted if e.get("type") == "shell"]
        self.assertEqual(frames[0], {"type": "shell", "text": "ls; exit 3", "running": True, "chat": chat.id})
        self.assertEqual(frames[1]["code"], 3)
        self.assertIn("marker.txt", frames[1]["output"])
        row = chat.history()[-1]
        self.assertEqual((row["role"], row["text"], row["code"]), ("shell", "ls; exit 3", 3))
        self.assertNotIn("running", row)
        self.assertEqual(len(chat.shell_notes), 1)
        self.assertIn("<bash-input>ls; exit 3</bash-input>", chat.shell_notes[0])
        self.assertIn("<bash-exit>3</bash-exit>", chat.shell_notes[0])
        self.assertFalse(chat.summary()["shell_running"])

    def test_what_it_prints_reaches_the_phone_while_it_runs(self):
        import asyncio

        with tempfile.TemporaryDirectory() as folder:
            chat, posted = self._chat(folder)

            async def go():
                task = asyncio.ensure_future(chat.run_shell("printf 'code: 7ADB-FA3A\\n'; sleep 1.5; echo done"))
                live = None
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    live = next((e for e in posted if e.get("type") == "shell_output"), None)
                    if live:
                        break
                still = dict(chat.history()[-1])  # the row as it stands now: the end of the command updates it
                await task
                return live, still

            live, still = asyncio.run(go())
        self.assertIsNotNone(live, "the code reached the phone before the command ended")
        self.assertEqual((live["text"], live["output"]), ("printf 'code: 7ADB-FA3A\\n'; sleep 1.5; echo done", "code: 7ADB-FA3A"))
        self.assertTrue(still.get("running"))
        self.assertEqual(still["output"], "code: 7ADB-FA3A", "a phone opening the chat meanwhile sees it too")
        last = [e for e in posted if e.get("type") == "shell"][-1]
        self.assertEqual((last["output"], last["code"]), ("code: 7ADB-FA3A\ndone", 0))

    def test_stop_ends_a_running_command(self):
        import asyncio
        import time

        with tempfile.TemporaryDirectory() as folder:
            chat, posted = self._chat(folder)

            async def go():
                task = asyncio.ensure_future(chat.run_shell("sleep 30"))
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if chat._shell not in (None, True):
                        break
                self.assertTrue(chat.summary()["shell_running"])
                started = time.monotonic()
                self.assertTrue(chat.stop_shell())
                await task
                return time.monotonic() - started

            took = asyncio.run(go())
        self.assertLess(took, 5)
        self.assertIsNone([e for e in posted if e.get("type") == "shell"][-1]["code"], "stopped, not an exit code")
