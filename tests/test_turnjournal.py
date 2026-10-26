import os
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vision.turnjournal import (
    ChatJournal,
    compact_agent_frame,
    latest_terminal_recovery,
    recover,
    recovery_context,
    saved_chats,
)


class TurnJournalTests(unittest.TestCase):
    def test_compact_agent_updates_restore_every_step(self):
        with TemporaryDirectory() as tmp:
            journal = ChatJournal("steps", Path(tmp))
            journal.append("chat", model="opus")
            journal.append("start", text="Check it")
            first = {"id": "agent1", "steps": [{"tool": "Read", "detail": "a.py"}], "done": False}
            second = {"id": "agent1", "steps": first["steps"] + [{"tool": "Edit", "detail": "a.py"}], "done": True}
            journal.append("agent", frame=compact_agent_frame(first, None))
            journal.append("agent", frame=compact_agent_frame(second, first))
            restored = recover(journal.read())["history"][1]["agents"][0]
            self.assertEqual(restored["steps"], second["steps"])

    def test_old_completed_scheduled_run_stays_on_disk_without_reopening(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            scheduled = ChatJournal("scheduled1", directory)
            scheduled.append("chat", model="opus")
            scheduled.append("scheduled", task_id="daily")
            scheduled.append("start", text="Report")
            scheduled.append("done", text="Ready")
            old = scheduled.path.stat().st_mtime - 31 * 86400
            os.utime(scheduled.path, (old, old))
            self.assertEqual(saved_chats(directory), [])
            self.assertTrue(scheduled.path.is_file())


    def test_interrupted_turn_keeps_prompt_stream_and_tool(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            journal = ChatJournal("chat1", directory)
            journal.append("chat", model="opus", effort="high", session_id=None)
            journal.append("base", history=[])
            journal.append("queued", text="Fix it")
            journal.append("start", text="Fix it")
            journal.append("delta", text="I changed the ")
            journal.append("tool", frame={"id": "tool1", "type": "tool", "name": "Edit",
                                          "detail": "app.py", "done": False, "at": 14})
            journal.append("tool", frame={"id": "tool2", "type": "tool", "name": "Read",
                                          "detail": "settings.toml", "done": True, "output": "mode=auto", "at": 14})
            journal.append("delta", text="file")
            state = recover(saved_chats(directory)[0][1])
            self.assertEqual(state["pending"], [])
            self.assertEqual(state["history"][0]["text"], "Fix it")
            self.assertEqual(state["history"][1]["text"], "I changed the file")
            self.assertIn("stopped", state["history"][1]["error"])
            self.assertEqual(state["history"][1]["tools"][0]["id"], "tool1")
            self.assertTrue(state["history"][1]["tools"][0]["is_error"])
            self.assertTrue(state["history"][1]["tools"][0]["cut_off"])
            self.assertIn("Interrupted", state["history"][1]["tools"][0]["output"])
            self.assertFalse(state["history"][1]["tools"][1].get("cut_off", False))
            context = recovery_context(state["history"], state["recovery_rows"])
            self.assertIn("Edit app.py: interrupted", context)
            self.assertIn("Read settings.toml: completed", context)
            self.assertIn("mode=auto", context)

    def test_completed_turn_and_queued_message_survive(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            journal = ChatJournal("chat2", directory)
            journal.append("chat", model="opus", effort="high", session_id=None)
            journal.append("base", history=[])
            journal.append("start", text="First")
            journal.append("delta", text="Done")
            journal.append("done", text="Done", error="", session_id="sid")
            journal.append("queued", text="Second")
            state = recover(journal.read())
            self.assertEqual(state["session_id"], "sid")
            self.assertEqual([row["text"] for row in state["history"]], ["First", "Done", "Second", ""])
            self.assertIn("Queued", state["history"][-1]["error"])
            journal.append("close")
            self.assertEqual(saved_chats(directory), [])

    def test_torn_tail_does_not_consume_the_next_event(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            journal = ChatJournal("chat4", directory)
            journal.append("chat", model="opus")
            with journal.path.open("ab") as file:
                file.write(b'{"event": "delta", "text": "half')
            reopened = ChatJournal("chat4", directory)
            reopened.append("start", text="Still here")
            self.assertEqual([r["event"] for r in reopened.read()], ["chat", "start"])

    def test_crash_between_steer_request_and_provider_acceptance_keeps_text(self):
        with TemporaryDirectory() as tmp:
            journal = ChatJournal("chat5", Path(tmp))
            journal.append("chat", model="opus")
            journal.append("start", text="First")
            journal.append("steer_attempt", text="Also check this")
            state = recover(journal.read())
            self.assertEqual(state["pending"], ["Also check this"])

    def test_latest_terminal_turn_reopens_only_if_it_was_interrupted(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = ChatJournal("terminal-first", directory)
            first.append("chat", source="terminal", model="opus")
            first.append("start", text="Older")
            first.append("delta", text="partial")
            self.assertEqual(latest_terminal_recovery(directory)[0], "terminal-first")
            second = ChatJournal("terminal-second", directory)
            second.append("chat", source="terminal", model="opus")
            second.append("start", text="Newer")
            second.append("done", text="complete")
            newer = first.path.stat().st_mtime + 10
            os.utime(second.path, (newer, newer))
            self.assertIsNone(latest_terminal_recovery(directory))

    def test_chat_still_open_in_another_window_is_not_recovered(self):
        import subprocess
        import sys

        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = ChatJournal("terminal-live", directory)
            live.append("chat", source="terminal", model="opus")
            live.append("start", text="Still working")
            # Another process holds it, the way a second `vision` window would.
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; from pathlib import Path; from vision.turnjournal import ChatJournal;"
                 "j = ChatJournal('terminal-live', Path(sys.argv[1])); assert j.claim();"
                 "print('held', flush=True); sys.stdin.read()", tmp],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                self.assertIsNone(latest_terminal_recovery(directory))
                self.assertFalse(ChatJournal("terminal-live", directory).claim())
            finally:
                holder.stdin.close()
                holder.wait()
            # Once that process is gone the chat really was cut off.
            self.assertEqual(latest_terminal_recovery(directory)[0], "terminal-live")

    def test_server_restores_interrupted_chat_as_visible_history(self):
        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider = "claude"
            workdir = "/tmp"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None

            def resolved_model(self):
                return self.cfg.model

        with TemporaryDirectory() as tmp, patch("vision.turnjournal.JOURNAL_DIR", Path(tmp)):
            journal = ChatJournal("chat3", Path(tmp))
            journal.append("chat", model="opus", effort="high", session_id=None)
            journal.append("base", history=[])
            journal.append("start", text="Check this")
            journal.append("delta", text="I found")
            cfg = Config()
            with patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id"))):
                hub = Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None,
                          open_initial=False, journal_enabled=True)
            self.assertEqual(len(hub.chats), 1)
            chat = hub.chats["chat3"]
            self.assertEqual([row["text"] for row in chat.history()], ["Check this", "I found"])
            self.assertTrue(chat.brain.handoff)
            self.assertIn("stopped", chat.history()[-1]["error"])


class GracefulRestartTests(unittest.TestCase):
    """`vision restart`, `r` in the serve window, /restart on the phone: wait for quiet, then restart,
    and run what was queued in the last instant in the new process instead of calling it lost."""

    def _hub(self, tmp):
        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider = "claude"
            workdir = "/tmp"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        # For the whole test, not just the Hub: open_chat() makes brains too, and CI has no Claude Code.
        creating = patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id")))
        creating.start()
        self.addCleanup(creating.stop)
        return Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=True)

    def test_restart_waits_for_busy_chats_then_holds_new_messages(self):
        with TemporaryDirectory() as tmp, patch("vision.turnjournal.JOURNAL_DIR", Path(tmp)), \
                patch("vision.server.RESTART_MARKER", Path(tmp) / "restart.json"):
            hub = self._hub(tmp)
            chat = hub.open_chat()
            execs = []
            hub.exec_restart = lambda: execs.append(1)
            chat.busy = True
            self.assertEqual(hub.request_restart("test"), 1)
            self.assertFalse(hub.restarting)
            self.assertEqual(execs, [])
            chat.busy = False  # the reply finished (run_turn's finally calls this)
            hub.maybe_restart()
            self.assertTrue(hub.restarting)
            self.assertEqual(execs, [1])
            self.assertTrue((Path(tmp) / "restart.json").exists())
            chat.queue("one more thing", False, False)  # arrives in the last instant: held, not run
            self.assertFalse(chat.busy)
            self.assertEqual([p[0] for p in chat._pending], ["one more thing"])

    def test_after_a_graceful_restart_queued_messages_run_instead_of_showing_lost(self):
        with TemporaryDirectory() as tmp, patch("vision.turnjournal.JOURNAL_DIR", Path(tmp)), \
                patch("vision.server.RESTART_MARKER", Path(tmp) / "restart.json"):
            journal = ChatJournal("chat4", Path(tmp))
            journal.append("chat", model="opus", effort="high", session_id="s1")
            journal.append("base", history=[])
            journal.append("queued", text="one more thing")
            from vision.server import write_restart_marker

            write_restart_marker()
            hub = self._hub(tmp)
            chat = hub.chats["chat4"]
            self.assertEqual(hub._resume, [(chat, ["one more thing"])])
            self.assertFalse(any(row.get("error") for row in chat.history()))
            self.assertFalse((Path(tmp) / "restart.json").exists())  # taken once

    def test_restart_command_line_drops_one_off_flags(self):
        from vision.server import restart_argv

        argv = restart_argv(["/x/vision/__main__.py", "serve", "--new", "--port", "9000", "--new-token", "-c"])
        self.assertEqual(argv[1:], ["-m", "vision", "serve", "--port", "9000"])


class RestoredFolderTests(unittest.TestCase):
    """A chat a restart brings back works in the folder it was opened in, not the server's own."""

    SERVER_FOLDER = "/server-default"

    def _hub(self, tmp):
        from vision.config import Config
        from vision.server import Hub

        server_folder = self.SERVER_FOLDER

        class Brain:
            provider = "claude"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None
                self.workdir = cfg.workdir or server_folder  # as Brain does: "" means the server's cwd

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        cfg.brain.workdir = ""
        creating = patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id")))
        creating.start()
        self.addCleanup(creating.stop)
        return Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=True)

    def _patches(self, tmp):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("vision.turnjournal.JOURNAL_DIR", Path(tmp) / "journals"))
        stack.enter_context(patch("vision.server.RESTART_MARKER", Path(tmp) / "restart.json"))
        stack.enter_context(patch("vision.sessions.CLAUDE_PROJECTS", str(Path(tmp) / "claude-projects")))
        return stack

    def test_a_chat_opened_in_a_folder_comes_back_in_it_after_a_restart(self):
        with TemporaryDirectory() as tmp, self._patches(tmp):
            picked = Path(tmp) / "picked"
            picked.mkdir()
            hub = self._hub(tmp)
            brain, cfg, _ = hub._prepare_chat(workdir=str(picked))  # the folder picked on the phone
            chat = hub.open_chat(brain, cfg)
            self.assertEqual(chat.workdir, str(picked))
            chat.journal.release()  # the old process is gone

            restarted = self._hub(tmp)
            self.assertEqual(restarted.chats[chat.id].workdir, str(picked))

    def test_an_older_journal_without_a_folder_takes_its_claude_conversations(self):
        with TemporaryDirectory() as tmp, self._patches(tmp):
            picked = Path(tmp) / "picked"
            picked.mkdir()
            journal = ChatJournal("old1", Path(tmp) / "journals")
            journal.append("chat", model="opus", effort="high", session_id="sess-1")  # no workdir: saved before
            journal.append("base", history=[])
            session = Path(tmp) / "claude-projects" / "-picked" / "sess-1.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text('{"type":"queue-operation"}\n{"type":"user","cwd":"%s"}\n' % picked)
            self.assertEqual(self._hub(tmp).chats["old1"].workdir, str(picked))

    def test_a_folder_that_is_gone_falls_back_to_the_servers_own(self):
        with TemporaryDirectory() as tmp, self._patches(tmp):
            journal = ChatJournal("gone1", Path(tmp) / "journals")
            journal.append("chat", model="opus", effort="high", session_id=None, workdir=str(Path(tmp) / "deleted"))
            journal.append("base", history=[])
            journal2 = ChatJournal("plain1", Path(tmp) / "journals")
            journal2.append("chat", model="opus", effort="high", session_id=None)  # older, and no conversation yet
            journal2.append("base", history=[])
            hub = self._hub(tmp)
            self.assertEqual(hub.chats["gone1"].workdir, self.SERVER_FOLDER)
            self.assertEqual(hub.chats["plain1"].workdir, self.SERVER_FOLDER)
            said = [row["text"] for row in hub.chats["gone1"].history()]  # the chat says it moved
            self.assertEqual(len(said), 1)
            self.assertIn("no such folder", said[0])
            self.assertIn("this chat works in the default folder instead", said[0])
            self.assertEqual(hub.chats["plain1"].history(), [])  # it never had a folder: nothing to say
