from __future__ import annotations

import io
import os
import tempfile
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from vision.ui import ChatScreen, ConversationHistory, answered_grid

QS = [
    {"question": "Which library?", "header": "Library", "multiSelect": False, "options": [{"label": "date-fns"}, {"label": "dayjs"}]},
    {"question": "Which features?", "header": "Features", "multiSelect": True, "options": [{"label": "Auth"}, {"label": "Logging"}]},
]


def render(renderable, width: int = 60) -> list[str]:
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False).print(renderable)
    return [line.rstrip() for line in buf.getvalue().rstrip("\n").split("\n")]


class AnsweredBlockTests(unittest.TestCase):
    def test_answers_render_like_claude_code(self):
        out = render(answered_grid(QS, {"Which library?": "date-fns", "Which features?": "Auth, Logging"}))
        self.assertEqual(
            out,
            [
                "● You answered Vision's questions:",
                "  ⎿  · Which library? → date-fns",
                "     · Which features? → Auth, Logging",
            ],
        )

    def test_one_question_is_singular(self):
        out = render(answered_grid(QS[:1], {"Which library?": "dayjs"}))
        self.assertEqual(out, ["● You answered Vision's question:", "  ⎿  · Which library? → dayjs"])

    def test_dismissed_form_lists_the_options(self):
        out = render(answered_grid(QS, None))
        self.assertEqual(
            out,
            [
                "● You declined to answer Vision's questions",
                "  ⎿  · Which library? (date-fns / dayjs)",
                "     · Which features? (Auth / Logging)",
            ],
        )

    def test_long_lines_wrap_under_the_hook(self):
        q = "Which library should we use for date formatting in the new reporting module?"
        lines = render(answered_grid([{"question": q, "options": []}], {q: "date-fns"}), width=50)
        self.assertTrue(lines[1].startswith("  ⎿  · Which library"))
        self.assertTrue(lines[2].startswith("     ") and not lines[2].startswith("      "))


def _chat_screen(test: unittest.TestCase) -> ChatScreen:
    from prompt_toolkit.application import Application

    pipe = test.enterContext(create_pipe_input())
    orig = Application.__init__

    def init(app, *a, **kw):
        kw.setdefault("input", pipe)
        kw.setdefault("output", DummyOutput())
        orig(app, *a, **kw)

    Application.__init__ = init
    test.addCleanup(setattr, Application, "__init__", orig)
    d = tempfile.mkdtemp()
    return ChatScreen(os.path.join(d, "history"), lambda: "")


class SplitReplyTests(unittest.TestCase):
    def _screen(self) -> ChatScreen:
        return _chat_screen(self)

    def test_queue_hold_wins_after_worker_observed_readiness(self):
        from vision.turnqueue import QueuedTurn, TurnQueue

        turns = TurnQueue()
        item = turns.put(QueuedTurn("edit me", shown=True))
        self.assertTrue(turns.wait_free(0))
        turns.hold()
        self.assertEqual(turns.pop_if_free(), (False, None))
        self.assertEqual(turns.shown(), [item])
        turns.release()
        self.assertEqual(turns.pop_if_free(), (True, item))

    def test_past_tool_calls_keep_their_place_and_can_be_opened(self):
        from vision.brain import ToolCall

        screen = self._screen()
        call = ToolCall("t1", "Bash", "git status", done=True, output="clean")
        entry = screen.add_past_reply("Checking. All clean.", [(10, call)])
        self.assertIn("Ran 1 command", "".join(fragment[1] for row in entry.lines(80) for fragment in row))
        entry.toggle_fold()
        rows = entry.lines(80)
        rendered = "".join(fragment[1] for row in rows for fragment in row)
        self.assertLess(rendered.index("Checking."), rendered.index("Bash(git status)"))
        self.assertLess(rendered.index("Bash(git status)"), rendered.index("All clean."))
        entry.expanded.add("t1")
        entry.invalidate()
        self.assertIn("clean", "".join(fragment[1] for row in entry.lines(80) for fragment in row))

    def test_split_keeps_a_reply_that_said_something(self):
        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="Two options here.")
        n = s.split_reply(e, answered_grid(QS[:1], {"Which library?": "dayjs"}))
        self.assertTrue(e.finished)
        self.assertIsNot(n, e)
        self.assertEqual(s.entries[-3], e)
        self.assertIs(s.entries[-1], n)
        self.assertFalse(s.entries[-2].gap_before)  # the finished reply already ends in a blank line
        self.assertTrue(s.busy)

    def test_split_drops_a_reply_that_said_nothing(self):
        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, status="waiting for your answer…")
        n = s.split_reply(e, answered_grid(QS[:1], None))
        self.assertNotIn(e, s.entries)
        self.assertTrue(s.entries[-2].gap_before)
        self.assertIs(s.entries[-1], n)

    def test_split_carries_running_agents_into_the_fresh_reply(self):
        """A workflow launched just before a question: its agents still at work must not fold away
        with the reply the answer closes off."""
        from vision.brain import AgentRun

        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="Launching the analysts.")
        done, running = AgentRun("w#1", "Analyse", "a", done=True), AgentRun("w#2", "Analyse", "b")
        s.update_reply(e, agent=done)
        s.update_reply(e, agent=running)
        n = s.split_reply(e, answered_grid(QS[:1], {"Which library?": "dayjs"}))
        self.assertEqual(list(e.agents), ["w#1"])
        self.assertEqual([m[1].id for m in e.marks], ["w#1"])
        self.assertIs(n.agents["w#2"], running)
        self.assertEqual(n.marks, [(0, running)])

    def test_split_carries_running_tool_calls_into_the_fresh_reply(self):
        from vision.brain import ToolCall

        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="Running it.")
        done, running = ToolCall("t1", "Read", done=True), ToolCall("t2", "Bash")
        s.update_reply(e, tool=done)
        s.update_reply(e, tool=running)
        n = s.split_reply(e, answered_grid(QS[:1], None))
        self.assertEqual(list(e.tools), ["t1"])
        self.assertIs(n.tools["t2"], running)
        self.assertEqual(n.marks, [(0, running)])

    def test_steered_split_keeps_the_timer_and_live_line(self):
        """Ctrl-X mid-turn: the turn goes on, so the clock doesn't restart and the background count stays."""
        s = self._screen()
        e = s.start_reply()
        e.started -= 30
        s.update_reply(e, status="1 background job still running…")
        n = s.split_reply(e, answered_grid(QS[:1], None), same_turn=True)
        self.assertEqual(n.started, e.started)
        self.assertEqual(n.status, "1 background job still running…")
        fresh = s.split_reply(n, answered_grid(QS[:1], None))
        self.assertLess(fresh.elapsed, 5)  # a question's split still starts over

    def test_held_reply_closes_but_stays_busy(self):
        """Only background jobs left: the reply finishes on screen as a done one does, the job count
        goes on its footer, and the turn still counts as running (no model switch mid-turn)."""
        import time

        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="Started the build in the background.")
        s.hold_reply(e, "1 background job still running…")
        for _ in range(100):
            if e.done:
                break
            time.sleep(0.01)
        self.assertTrue(e.done)
        self.assertIsNotNone(e.ended)
        self.assertTrue(s.busy)
        self.assertIs(s._revealing, e)  # Enter still reaches the running turn
        self.assertRegex(e.footer, r"^Worked for \d+\.\ds · done \d{1,2}:\d\d [AP]M · 1 background job still running$")
        self.assertNotIn("background", "".join(t for _, t in s._status_text()))
        s.set_background("2 background jobs still running…")
        self.assertTrue(e.footer.endswith("· 2 background jobs still running"))
        s.release_hold()
        self.assertFalse(s.busy)
        self.assertFalse(s.holding)
        self.assertRegex(e.footer, r"^Worked for \d+\.\ds · done \d{1,2}:\d\d [AP]M$")  # the count goes once the jobs are done

    def test_release_with_a_fresh_reply_keeps_busy(self):
        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="Waiting on it.")
        s.hold_reply(e, "1 background job still running…")
        n = s.start_reply()
        s.release_hold()
        self.assertTrue(s.busy)
        self.assertIs(s._revealing, n)

    def test_fresh_reply_ignores_the_leading_block_gap(self):
        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="\n\n")
        s.update_reply(e, delta="\n\nNext step.")
        self.assertEqual(e.buf, "Next step.")

    def test_cancel_freezes_timer_then_adds_usage(self):
        s = self._screen()
        e = s.start_reply()
        s.update_reply(e, delta="partial")
        s._cancel()
        frozen = e.elapsed
        ended = e.ended
        self.assertTrue(e.cancelled)
        self.assertEqual(e.footer, f"Worked for {frozen:.1f}s · cancelled")

        s.end_reply(e, stats="in 120 · out 7", cancelled=True)
        self.assertEqual(e.ended, ended)
        self.assertEqual(e.footer, f"Worked for {e.elapsed:.1f}s · cancelled · in 120 · out 7")

    def test_message_can_be_submitted_while_a_reply_is_live(self):
        s = self._screen()
        sent = []
        s.on_submit = sent.append
        e = s.start_reply()

        s._submit("one more thing")

        self.assertEqual(sent, ["one more thing"])
        self.assertEqual(s.area.text, "")
        s.end_reply(e)

    def test_non_reply_busy_operation_still_blocks_messages(self):
        s = self._screen()
        sent = []
        s.on_submit = sent.append
        s.busy = True

        s._submit("do not race the model switch")

        self.assertEqual(sent, [])


class ConversationHistoryTests(unittest.TestCase):
    def test_replace_then_append_keeps_oldest_first(self):
        h = ConversationHistory()
        h.replace(["first", "second"])
        self.assertEqual(h.get_strings(), ["first", "second"])
        h.append_string("third")
        self.assertEqual(h.get_strings(), ["first", "second", "third"])

    def test_screen_up_arrow_follows_the_conversation(self):
        s = _chat_screen(self)
        s.set_history(["hello from earlier"])
        s.remember_user("and this turn")
        self.assertEqual(s.area.buffer.history.get_strings(), ["hello from earlier", "and this turn"])
        s.set_history([])
        self.assertEqual(s.area.buffer.history.get_strings(), [])


if __name__ == "__main__":
    unittest.main()


class QueuedStripTests(unittest.TestCase):
    def _screen(self, *texts, can_steer=True):
        from vision.turnqueue import QueuedTurn, TurnQueue

        screen = _chat_screen(self)
        screen.turns = TurnQueue()
        screen.can_steer_fn = lambda: can_steer
        for t in texts:
            screen.turns.put(QueuedTurn(t, shown=True))
        return screen

    @staticmethod
    def _strip(screen):
        return "".join(t for _, t in screen._queued_text())

    def test_queued_messages_show_over_the_input_until_their_turn(self):
        screen = self._screen("use cli.py instead\nand check the tests", "then commit it")
        text = self._strip(screen)
        self.assertIn("⏸ queued  use cli.py instead and check the tests", text)
        self.assertIn("⏸ queued  then commit it", text)
        self.assertIn("Ctrl-X sends the top one into the reply now · ↑ to edit", text)
        self.assertEqual(len(screen.entries), 0)  # not in the transcript yet
        self.assertIn("goes when this reply ends", self._strip(self._screen("later", can_steer=False)))

    def test_mid_reply_enter_queues_and_send_now_holds_the_selected_message(self):
        from vision.turnqueue import QueuedTurn

        screen = self._screen()
        screen.start_reply()
        screen.on_submit = lambda text: screen.turns.put(QueuedTurn(text, shown=True))
        seen = []

        def steer(text, queued_id=None):
            seen.append((text, screen.turns.held, queued_id))
            screen.turns.take(queued_id)

        screen.on_steer = steer
        screen._submit("follow up")
        self.assertEqual(screen.queued, ["follow up"])
        queued_id = screen.turns.shown()[0].id
        screen._queue_pick()
        screen._queue_send_now()
        self.assertEqual(seen, [("follow up", True, queued_id)])
        self.assertEqual(screen.queued, [])
        self.assertFalse(screen.turns.held)

    def test_picking_holds_the_queue_and_removes_one(self):
        screen = self._screen("one", "two", "three")
        screen._queue_pick()
        self.assertTrue(screen.turns.held)
        self.assertIn("› queued  three", self._strip(screen))
        screen._queue_move(-1)
        screen._queue_remove()  # "two"
        self.assertEqual(screen.queued, ["one", "three"])
        self.assertTrue(screen.turns.held)
        screen._queue_done()
        self.assertFalse(screen.turns.held)

    def test_an_edited_message_goes_back_in_its_place(self):
        screen = self._screen("one", "two", "three")
        screen._queue_pick()
        screen._queue_move(-1)
        screen._queue_edit()  # "two" into the box
        self.assertEqual((screen.area.text, screen.queued), ("two", ["one", "three"]))
        self.assertTrue(screen.turns.held)  # nothing goes while it is being edited
        self.assertIn("✎ in the box below", self._strip(screen))
        screen._submit("two, but with tests")
        self.assertEqual(screen.queued, ["one", "two, but with tests", "three"])
        self.assertFalse(screen.turns.held)
        self.assertEqual(screen.area.text, "")

    def test_esc_keeps_the_original_and_an_empty_box_removes_it(self):
        screen = self._screen("one", "two")
        screen._queue_pick()
        screen._queue_edit()
        screen.area.text = "changed my mind"
        screen._queue_edit_finish(None)  # Esc
        self.assertEqual(screen.queued, ["one", "two"])
        screen._queue_pick()
        screen._queue_edit()
        screen._submit("")  # Enter on an emptied box
        self.assertEqual(screen.queued, ["one"])
        self.assertFalse(screen.turns.held)


class LoopErrorTests(unittest.TestCase):
    def test_an_escaped_exception_is_logged_not_shown_full_screen(self):
        from unittest import mock

        s = _chat_screen(self)
        d = tempfile.mkdtemp()
        with mock.patch("vision.config.STATE_DIR", __import__("pathlib").Path(d)):
            try:
                1 / 0
            except ZeroDivisionError as e:
                s._loop_error(None, {"exception": e})
        self.assertIn("ZeroDivisionError", open(os.path.join(d, "errors.log")).read())
        self.assertIn("ZeroDivisionError", s._notice[0])
