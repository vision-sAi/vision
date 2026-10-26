from __future__ import annotations

import math
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from vision.tts import StreamingSpeaker
from vision.ui import ChatScreen, _ReplyEntry, _Reveal


def plain(rows):
    return "\n".join("".join(fragment[1] for fragment in row) for row in rows)


def fake_clock(now: list, sleep) -> types.SimpleNamespace:
    """A clock for the pacer under test, with `sleep(delay)` advancing `now`. Other threads keep real
    time: the pacers that earlier tests leave running (a started reply is never finished there) call
    the patched module's sleep too, and must not move this test's clock or feed its reply."""
    me = threading.current_thread()
    real_sleep, real_monotonic = time.sleep, time.monotonic

    def _sleep(delay):
        if threading.current_thread() is me:
            sleep(delay)
        else:
            real_sleep(delay)

    return types.SimpleNamespace(monotonic=lambda: now[0] if threading.current_thread() is me else real_monotonic(),
                                 sleep=_sleep)


class TextRevealTests(unittest.TestCase):
    def test_incoming_letter_has_intermediate_frames(self):
        reveal = _Reveal("Hello world\n", 11)
        frames = [reveal.cut(2 + fraction) for fraction in (0.1, 0.3, 0.6, 0.9)]
        self.assertEqual([plain(frame) for frame in frames], ["Hel"] * 4)
        self.assertEqual(len({frame[-1][-1][0] for frame in frames}), 4)
        self.assertTrue(all(frame[-1][-2] == ("", "He") for frame in frames))
        self.assertEqual(plain(reveal.cut(3)), "Hel")
        self.assertEqual(reveal.cut(reveal.total), reveal.rows)
        self.assertEqual(reveal.rows, [[("", "Hello world")]])

    def test_fade_preserves_wrapping_and_completed_styles(self):
        reveal = _Reveal("\x1b[1mHi\x1b[0m there\nnext\n", 13)
        self.assertEqual(plain(reveal.cut(0)), "")
        self.assertEqual(plain(reveal.cut(2.5)), "Hi t")
        self.assertEqual(reveal.cut(2.5)[0][0], ("bold", "Hi"))
        self.assertEqual(plain(reveal.cut(7.5)), "Hi there\nn")
        for position in (0.1, 1.5, 3.2, 7.5, 10.9):
            self.assertEqual(
                plain(reveal.cut(position)), plain(reveal.cut(math.ceil(position))),
            )

    def test_progress_change_during_render_cannot_leave_stale_cached_text(self):
        entry = _ReplyEntry(markdown=False)
        entry.buf, entry.shown, entry.ended = "abcdef", 1, entry.started
        reveal = entry.full(80)
        original_cut = reveal.cut

        def advance_while_drawing(position):
            entry.shown = 3
            entry.invalidate()
            return original_cut(position)

        with patch.object(reveal, "cut", side_effect=advance_while_drawing):
            entry.lines(80)
        self.assertTrue(plain(entry.lines(80)).endswith("abc"))

    def test_voice_clock_keeps_sub_character_precision(self):
        speaker = StreamingSpeaker.__new__(StreamingSpeaker)
        speaker._audible = lambda: 0.125
        speaker._chunks = [[20, 2.0, 0.0, True]]
        speaker._est = speaker._blend = None
        speaker._floor = 0
        self.assertEqual(speaker.spoken_position, 1.25)
        self.assertEqual(speaker.spoken, 1)

    def test_pacer_animates_between_letters_without_running_ahead_of_voice(self):
        entry = _ReplyEntry(markdown=False)
        entry.buf = "abcdefghij" * 3
        now, frames = [0.0], []
        entry.gate = lambda: min(30.0, now[0] * 10)

        def sleep(delay):
            now[0] += delay
            if now[0] >= 3:
                entry.finished = True
            if now[0] > 4:
                self.fail("reveal never finished")

        def redraw():
            frames.append((now[0], entry.shown))

        screen = ChatScreen.__new__(ChatScreen)
        screen.reveal_cps, screen.max_reveal_cps, screen.catch_up = 42, 72, 2.5
        screen._width = lambda: 80
        screen._settle = Mock()
        screen.app = types.SimpleNamespace(invalidate=redraw)
        with patch("vision.ui.time", fake_clock(now, sleep)):
            screen._pace(entry)

        self.assertGreater(len(frames), 150, "slow speech still needs intermediate visual frames")
        self.assertTrue(any(position != int(position) for _, position in frames))
        self.assertTrue(all(position <= t * 10 + 1e-8 for t, position in frames))
        self.assertLess(max(b - a for (_, a), (_, b) in zip(frames, frames[1:])), 0.2)
        self.assertEqual(entry.shown, 30)
        screen._settle.assert_called_once_with(entry)

    def test_pacer_waits_at_least_twice_what_the_last_render_took(self):
        """The markdown pass is O(reply); re-rendering every 50 ms regardless kept a core busy from
        5 KB on. The wait between renders now grows with the render itself."""
        entry = _ReplyEntry(markdown=True)
        now, renders = [0.0], []
        original = entry.full

        def full(width, fresh=True):
            r = original(width, fresh)
            if fresh:
                renders.append(now[0])
                r.cost = 0.2  # a long reply: each render takes 200 ms
            return r

        entry.full = full

        def sleep(delay):
            now[0] += delay
            if now[0] < 3:
                entry.buf += "word "  # text keeps arriving every tick
            elif now[0] >= 3.5:
                entry.finished = True
            if now[0] > 6:
                self.fail("reveal never finished")

        screen = ChatScreen.__new__(ChatScreen)
        screen.reveal_cps, screen.max_reveal_cps, screen.catch_up = 42, 72, 2.5
        screen._width = lambda: 80
        screen._settle = Mock()
        screen.app = types.SimpleNamespace(invalidate=lambda: None)
        with patch("vision.ui.time", fake_clock(now, sleep)):
            screen._pace(entry)

        self.assertGreaterEqual(len(renders), 3)
        gaps = [b - a for a, b in zip(renders, renders[1:])]
        self.assertGreaterEqual(min(gaps[:-1]), 0.4 - 1e-9)  # the last render, after `finished`, may come sooner
        self.assertIn("word", plain(entry.lines(80)))

    def test_one_shot_view_paints_on_its_own_thread_no_faster_than_its_interval(self):
        """`vision ask` re-parsed the whole reply's markdown on the brain's thread for every delta and
        Rich redrew it twelve times a second whether or not it had changed."""
        from io import StringIO

        from rich.console import Console

        from vision.ui import ReplyView

        console = Console(file=StringIO(), force_terminal=True, width=60, color_system="truecolor")
        renders = []
        with ReplyView(console, markdown=True) as view:
            original = view._render
            with patch.object(view, "_render", side_effect=lambda: (renders.append(time.monotonic()), original())):
                for i in range(40):
                    view.append(f"word{i} ")  # forty deltas inside a couple of paint intervals
                    time.sleep(0.002)
                time.sleep(0.3)
        self.assertLess(len(renders), 10)
        self.assertIn("word39", console.file.getvalue())


if __name__ == "__main__":
    unittest.main()


class StaleRenderTests(unittest.TestCase):
    def test_rows_taken_out_of_a_reply_do_not_leave_a_cut_pointing_past_the_marks(self):
        # A render cut while two agent rows sat in the text, then both moved to the next part of a
        # split reply (take_running): the old cut's mark indexes are past the end. The entry must
        # drop that render and, if one is still handed back, skip the indexes that are gone.
        from vision.brain import AgentRun

        entry = _ReplyEntry(markdown=False)
        entry.buf = "sending two off\n"
        for i in (1, 2):
            entry.place(AgentRun(f"a{i}", "fork", f"job {i}", started=time.monotonic()))
        before = entry.full(80)
        self.assertEqual(before.nmarks, 2)
        taken = entry.take_running()
        self.assertEqual([r.id for r in taken], ["a1", "a2"])
        self.assertIsNone(entry._full)
        self.assertEqual(entry.full(80).nmarks, 0)
        self.assertEqual(entry._mark_rows([0, 1], "", 60), [])
        entry.lines(80)  # repaints without raising
