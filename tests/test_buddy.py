from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit
from prompt_toolkit.utils import get_cwidth

from vision.buddy import HEIGHT, WIDTH, Buddy
from vision.ui import PREFIX_W, ChatScreen


class PipRendererTests(unittest.TestCase):
    def setUp(self):
        self.pip = Buddy(sleep_after_s=1000)
        self.pip.last_activity = 0
        self.pip._blink_phase = 1.0

    def test_every_state_is_exactly_sixteen_by_five(self):
        cases = []

        def capture(busy):
            # Cover both animation frames, including the slower sleeping marker.
            cases.extend(self.pip._frame(busy, now) for now in (10, 10.6, 11.1))

        self.pip.state = "idle"
        capture(False)
        capture(True)

        self.pip.state = "tool"
        self.pip.tool = "Bash"
        capture(True)

        self.pip.state = "listening"
        capture(True)

        self.pip.chore("warming up")
        capture(True)

        self.pip.state = "idle"
        self.pip.speaking_fn = lambda: True
        capture(False)
        self.pip.speaking_fn = lambda: False

        self.pip.state = "error"
        capture(False)

        self.pip.state = "idle"
        self.pip.sleep_after_s = 1
        capture(False)

        self.assertEqual({frame[0] for frame in cases}, {
            "idle", "thinking", "tool", "chore", "listening", "speaking", "error", "asleep",
        })
        for _, rows, _, caption, _ in cases:
            with self.subTest(caption=caption):
                self.assertEqual(len(rows), HEIGHT)
                self.assertEqual([get_cwidth(row) for row in rows], [WIDTH] * HEIGHT)
                self.assertLessEqual(get_cwidth(caption), WIDTH)
                self.assertTrue(all(row[-1] == " " for row in rows))
                self.assertEqual(rows[0][3:12], "▄▄▄▄▄▄▄▄▄")
                self.assertEqual(rows[3][3:12], "▀▀▀▀▀▀▀▀▀")
                self.assertEqual((rows[1][2], rows[1][12], rows[2][2], rows[2][12]), ("▐", "▌", "▐", "▌"))
                # Two eyes four cells apart (an idle glance may shift both by one); the lower
                # face carries the state-specific motion.
                eyes = [x for x in range(3, 12) if rows[1][x] != " "]
                self.assertEqual(len(eyes), 2)
                self.assertEqual(eyes[1] - eyes[0], 4)
                self.assertIn(eyes[0], (4, 5, 6))

    def test_idle_fills_the_gutter_with_a_spaced_face(self):
        rows = self.pip._frame(False, 0.20)[1]
        self.assertEqual(rows, ("   ▄▄▄▄▄▄▄▄▄    ", "  ▐  ▰   ▰  ▌   ", "  ▐    ━    ▌   ", "   ▀▀▀▀▀▀▀▀▀    "))
        with patch("vision.buddy.time.time", return_value=.20):
            fragments = self.pip.render(False)
        self.assertEqual(sum("bg:#202d26" in style for style, _ in fragments), 18)
        self.assertIn(("#c1ebd1 bg:#202d26", "▰"), fragments)
        self.assertIn(("#899a90", "▄"), fragments)

    def test_idle_blink_is_brief_and_infrequent(self):
        self.pip._blink_phase = 0.0
        blink = self.pip._frame(False, 0.05)[1]
        open_eyes = self.pip._frame(False, 0.20)[1]
        next_blink = self.pip._frame(False, 4.05)[1]

        self.assertIn("▐  ─   ─  ▌", blink[1])
        self.assertIn("▐  ▰   ▰  ▌", open_eyes[1])
        self.assertIn("▐  ─   ─  ▌", next_blink[1])
        self.assertEqual(blink[0], open_eyes[0])
        self.assertEqual(blink[2:], open_eyes[2:])

    def test_idle_glance_moves_both_eyes_one_cell_and_settles(self):
        self.pip._blink_phase = 0.0
        centre = self.pip._frame(False, 1.0)[1]
        right = self.pip._frame(False, 2.5)[1]
        left = self.pip._frame(False, 5.0)[1]
        back = self.pip._frame(False, 6.5)[1]
        self.assertIn("▐  ▰   ▰  ▌", centre[1])
        self.assertIn("▐   ▰   ▰ ▌", right[1])
        self.assertIn("▐ ▰   ▰   ▌", left[1])
        self.assertEqual(back, centre)
        self.assertEqual(centre[2], right[2])
        with patch("vision.buddy.time.time", return_value=2.5):
            self.assertEqual("".join(part[1] for part in self.pip.render_inline(False)), "[▰ ━ ▰]")
        self.pip.state = "error"  # only idle glances
        self.assertEqual(self.pip._frame(False, 2.5)[1][1], self.pip._frame(False, 1.0)[1][1])

    def test_tool_cursor_scans_then_fades_before_resetting(self):
        self.pip.using("Bash")
        start = self.pip._frame(True, 0)
        early = self.pip._frame(True, .3)
        late = self.pip._frame(True, 1.5)
        rest = self.pip._frame(True, 2.0)
        for frame in (early, late, rest):
            self.assertEqual(frame[1][:2], start[1][:2])
            self.assertEqual(frame[1][3], start[1][3])
            self.assertEqual(frame[1][2][5], "›")
            self.assertEqual(frame[3], "› Bash")
        self.assertGreater(late[1][2].index("━"), early[1][2].index("━"))
        self.assertNotIn("━", rest[1][2])
        self.assertNotIn("─", rest[1][2])

    def test_thinking_changes_dot_highlight_without_shifting_eyes(self):
        first = self.pip._paint(True, 0)
        second = self.pip._paint(True, .8)
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[1][2][5:10], "· · ·")
        self.assertNotEqual(first[5][2], second[5][2])
        self.assertEqual(first[5][1], second[5][1])

    def test_chore_is_busy_without_the_thinking_face(self):
        self.pip.chore("warming up")
        steady = self.pip._frame(True, 0.0)
        alt = self.pip._frame(True, 0.50)

        self.assertEqual(steady[0], "chore")
        self.assertIn("▐  ▰   ▰  ▌", steady[1][1])
        self.assertEqual(steady, alt)  # unreported progress cannot advance with time
        self.assertIn("▐    ━    ▌", steady[1][2])
        self.assertEqual(steady[3], "warming up")
        self.assertEqual(steady[4], "#c1ebd1")
        self.pip.rest()
        self.assertEqual(self.pip._frame(True, 0.0)[0], "thinking")

    def test_listening_uses_integrated_rails_and_real_levels(self):
        self.pip.listening()
        quiet = self.pip._paint(False, 0)
        pulse = self.pip._paint(False, 1.5)
        self.assertEqual(quiet[1], pulse[1])
        self.assertNotEqual(quiet[5][1][2], pulse[5][1][2])
        with patch("vision.buddy.time.time", return_value=2):
            self.pip.hear(.8)
        active = self.pip._frame(False, 2)
        self.assertNotEqual(active[1][2], quiet[1][2])
        self.assertEqual(active[1][1], quiet[1][1])
        self.assertEqual(self.pip._frame(False, 2.3)[1], quiet[1])
        self.pip.listening(False)
        self.pip.hear(1)
        self.pip.listening()
        self.assertEqual(self.pip._frame(False, 3)[1], quiet[1])

    def test_error_and_sleep_faces_have_readable_spaced_glyphs(self):
        self.pip.state = "error"
        error = self.pip._frame(False, 0.0)
        self.assertIn("▐  ╱   ╲  ▌", error[1][1])
        self.assertIn("▐    ⌁    ▌", error[1][2])

        self.pip.state = "idle"
        self.pip.sleep_after_s = 1
        asleep = self.pip._frame(False, 2.0)
        self.assertEqual(asleep[1][0], "   ▄▄▄▄▄▄▄▄▄  z ")
        self.assertIn("▐  ─   ─  ▌", asleep[1][1])
        self.assertIn("▐    ·    ▌", asleep[1][2])
        self.assertEqual(asleep[3], "zz")

    def test_inline_faces_keep_the_eye_and_mouth_gaps(self):
        with patch("vision.buddy.time.time", return_value=0.20):
            self.assertEqual("".join(part[1] for part in self.pip.render_inline(False)), "[▰ ━ ▰]")
            self.pip.state = "listening"
            self.assertEqual("".join(part[1] for part in self.pip.render_inline(False)), "[▰ ━ ▰] listening")
            self.pip.using("Bash")
            self.assertEqual("".join(part[1] for part in self.pip.render_inline(True)), "[▰ › ▰] › Bash")

    def test_speaking_changes_only_the_mouth(self):
        self.pip.speaking_fn = lambda: True
        small = self.pip._frame(False, 0)[1]
        large = self.pip._frame(False, .5)[1]
        self.assertEqual(small[:2], large[:2])
        self.assertEqual(small[3], large[3])
        self.assertEqual(small[2][7], "o")
        self.assertEqual(large[2][7], "O")

    def test_full_render_is_the_sixteen_by_four_gutter(self):
        self.pip.state = "error"
        text = "".join(fragment[1] for fragment in self.pip.render(False))
        rows = text.split("\n")
        self.assertEqual(len(rows), HEIGHT)
        self.assertEqual([get_cwidth(row) for row in rows], [WIDTH] * HEIGHT)
        self.assertNotIn("error", text)  # no caption row under the head

    def test_slide_in_enters_from_the_left_and_settles_into_the_still_gutter(self):
        from vision.buddy import SLIDE_S

        def rows(pip, now):
            with patch("vision.buddy.time.time", return_value=now):
                return "".join(t for _, t in pip.render(False)).split("\n")

        still = rows(self.pip, 100.0)
        sliding = Buddy(sleep_after_s=1000, slide_in=True)
        sliding.last_activity, sliding._blink_phase = 0, 1.0

        self.assertEqual(rows(sliding, 100.0), [" " * WIDTH] * HEIGHT)  # the clock starts at the first frame drawn
        hidden = sliding._hidden(100.0 + SLIDE_S / 3)
        self.assertTrue(0 < hidden < WIDTH)
        self.assertEqual(rows(sliding, 100.0 + SLIDE_S / 3), [r[hidden:] + " " * hidden for r in still])  # his right side, flush left
        with patch("vision.buddy.time.time", return_value=100.0 + SLIDE_S / 3):
            self.assertFalse(sliding.slid_in())
        with patch("vision.buddy.time.time", return_value=100.0 + SLIDE_S):
            self.assertTrue(sliding.slid_in())
        self.assertEqual(rows(sliding, 100.0 + SLIDE_S), still)
        self.assertTrue(self.pip.slid_in())  # off: nothing to wait for


class PipLayoutTests(unittest.TestCase):
    def test_buddy_is_the_leftmost_bottom_gutter(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = ChatScreen(str(Path(directory) / "history"), lambda: "ready", buddy=Buddy())

        root = screen.app.layout.container
        bottom = root.children[-1]
        self.assertIsInstance(bottom, HSplit)  # composer row over a full-width status row
        composer = bottom.children[0]
        self.assertIsInstance(composer, VSplit)
        self.assertIsInstance(composer.children[0], ConditionalContainer)

        buddy_stack = composer.children[0].content
        buddy_window = buddy_stack.children[-1]
        self.assertEqual(WIDTH, PREFIX_W)
        self.assertEqual(buddy_window.width.preferred, WIDTH)


if __name__ == "__main__":
    unittest.main()
