from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit
from prompt_toolkit.utils import get_cwidth

from vision.buddy import FACE_MOODS, HEIGHT, WIDTH, Buddy, face, face_pixels, mini, palette
from vision.theme import VISION_HUE
from vision.ui import PREFIX_W, ChatScreen


def _text(fragments) -> str:
    return "".join(text for _, text in fragments)


def _ink_columns(frame, y: int) -> list[int]:
    """Where the ink (eyes, mouth) is on one pixel row."""
    ink = palette(VISION_HUE, frame.mood)["ink"]
    return [x for x, pixel in enumerate(frame.pixels[y]) if pixel == ink]


class BuddyRendererTests(unittest.TestCase):
    def setUp(self):
        self.buddy = Buddy(sleep_after_s=1000)
        self.buddy.last_activity = 0
        self.buddy._blink_phase = 1.0

    def test_every_state_is_exactly_sixteen_by_four(self):
        cases = []

        def capture(busy):
            # Several moments, so every animation frame is covered, including the slower sleeping mark.
            cases.extend(self.buddy._paint(busy, now) for now in (10, 10.3, 10.6, 11.1))

        self.buddy.state = "idle"
        capture(False)
        capture(True)

        self.buddy.using("Bash")
        capture(True)

        self.buddy.listening()
        self.buddy.hear(1)
        capture(True)

        self.buddy.chore("warming up")
        capture(True)

        self.buddy.state = "idle"
        self.buddy.speaking_fn = lambda: True
        capture(False)
        self.buddy.speaking_fn = lambda: False

        self.buddy.state = "error"
        capture(False)

        self.buddy.state = "idle"
        self.buddy.sleep_after_s = 1
        capture(False)

        self.assertEqual({frame.state for frame in cases}, {
            "idle", "thinking", "tool", "chore", "listening", "speaking", "error", "asleep",
        })
        for frame in cases:
            with self.subTest(state=frame.state, caption=frame.caption):
                rows = frame.text()
                self.assertEqual(len(rows), HEIGHT)
                self.assertEqual([get_cwidth(row) for row in rows], [WIDTH] * HEIGHT)
                self.assertEqual([len(row) for row in frame.cells()], [WIDTH] * HEIGHT)
                self.assertLessEqual(get_cwidth(frame.caption), WIDTH)
                self.assertTrue(all(row[-2:] == "  " for row in rows))  # the gap to the input frame
                self.assertTrue(all(row[:3] == "   " for row in rows))  # the body sits in columns 3-11

    def test_states_take_the_moods_of_the_apps_vision_face(self):
        def mood(busy=False, now=10.0):
            return self.buddy._paint(busy, now).mood

        self.assertEqual(mood(), "curious")
        self.assertEqual(mood(busy=True), "focused")
        self.buddy.using("Bash")
        self.assertEqual(mood(busy=True), "focused")
        self.buddy.listening()
        self.assertEqual(mood(), "listening")
        self.buddy.chore("transcribing")
        self.assertEqual(mood(), "focused")
        self.buddy.rest()
        self.buddy.speaking_fn = lambda: True
        self.assertEqual(mood(), "proud")
        self.buddy.speaking_fn = lambda: False
        self.buddy.fail()
        self.assertEqual(mood(), "worried")
        self.buddy.touch()
        self.buddy.sleep_after_s = 1
        self.assertEqual(self.buddy.resolve(False, self.buddy.last_activity + 2), "asleep")
        self.assertEqual(mood(now=self.buddy.last_activity + 2), "asleep")

    def test_full_render_is_the_gutter_in_prompt_toolkit_colours(self):
        with patch("vision.buddy.time.time", return_value=10.0):
            fragments = self.buddy.render(False)
        rows = _text(fragments).split("\n")
        self.assertEqual(len(rows), HEIGHT)
        self.assertEqual([get_cwidth(row) for row in rows], [WIDTH] * HEIGHT)
        c = palette(VISION_HUE)
        self.assertTrue(any(style.startswith(f"fg:{c['top']} bg:#") for style, _ in fragments))  # the body's light top
        self.buddy.state = "error"
        with patch("vision.buddy.time.time", return_value=10.0):
            self.assertNotIn("error", _text(self.buddy.render(False)))  # the caption is only for the inline form

    def test_idle_blink_is_brief_and_infrequent(self):
        self.buddy._blink_phase = 0.0
        blink = self.buddy._paint(False, 0.05)
        open_eyes = self.buddy._paint(False, 0.20)
        next_blink = self.buddy._paint(False, 4.05)

        self.assertEqual((blink.mood, open_eyes.mood, next_blink.mood), ("blink", "curious", "blink"))
        self.assertEqual(_ink_columns(open_eyes, 3), [5, 9])
        self.assertEqual(_ink_columns(blink, 3), [])  # shut: a line lower down instead
        self.assertEqual(_ink_columns(blink, 4), [4, 5, 9, 10])
        self.assertEqual(blink.pixels[:2], open_eyes.pixels[:2])  # the sprout stays put

    def test_idle_glance_moves_both_eyes_one_column_and_settles(self):
        self.buddy._blink_phase = 0.0
        centre = self.buddy._paint(False, 1.0)
        right = self.buddy._paint(False, 2.5)
        left = self.buddy._paint(False, 5.0)
        back = self.buddy._paint(False, 6.5)
        self.assertEqual(_ink_columns(centre, 3), [5, 9])
        self.assertEqual(_ink_columns(right, 3), [6, 10])
        self.assertEqual(_ink_columns(left, 3), [4, 8])
        self.assertEqual(back.pixels, centre.pixels)
        self.buddy.state = "error"  # only idle glances
        self.assertEqual(self.buddy._paint(False, 2.5).pixels, self.buddy._paint(False, 1.0).pixels)

    def test_thinking_counts_dots_by_its_head_without_moving_the_face(self):
        frames = [self.buddy._paint(True, t) for t in (0, .5, .9, 1.3)]  # timed from the first frame
        self.assertEqual([frame.prop[0] for frame in frames], ["․", "‥", "…", "․"])
        self.assertTrue(all(frame.pixels == frames[0].pixels for frame in frames))
        self.assertEqual(frames[0].caption, "thinking")

    def test_tool_runs_a_dot_along_the_mouth_then_rests(self):
        self.buddy.using("Bash")
        early, later, resting = (self.buddy._paint(True, t) for t in (0.0, 0.5, 1.2))
        body = palette(VISION_HUE, "focused")

        def dot(frame):
            row = frame.pixels[5]
            return [x for x in range(4, 11) if row[x] != resting.pixels[5][x]]

        self.assertEqual(dot(early), [5])
        self.assertEqual(dot(later), [7])
        self.assertEqual(dot(resting), [])
        self.assertNotIn(body["ink"], early.pixels[5])  # dimmer than the eyes: a touch, not a mouth
        for frame in (early, later, resting):
            self.assertEqual(frame.pixels[:5], resting.pixels[:5])
            self.assertEqual(frame.caption, "› Bash")
        self.assertIsNone(early.prop)

    def test_chore_without_progress_is_still(self):
        self.buddy.chore("transcribing")
        steady = self.buddy._paint(True, 0.0)
        later = self.buddy._paint(True, 5.0)
        self.assertEqual(steady.state, "chore")
        self.assertEqual(steady.pixels, later.pixels)  # unreported progress cannot advance with time
        self.assertEqual(steady.caption, "transcribing")
        self.buddy.rest()
        self.assertEqual(self.buddy._paint(True, 0.0).state, "thinking")

    def test_speaking_opens_and_closes_only_the_mouth(self):
        self.buddy.speaking_fn = lambda: True
        closed = self.buddy._paint(False, 0)
        opened = self.buddy._paint(False, .3)
        self.assertEqual(closed.mood, "proud")
        self.assertEqual(closed.pixels[:5], opened.pixels[:5])
        self.assertNotEqual(closed.pixels[5:], opened.pixels[5:])
        self.assertEqual(_ink_columns(closed, 5), [6, 7, 8])
        self.assertEqual(_ink_columns(opened, 5), [7])
        self.assertEqual(_ink_columns(opened, 6), [7])

    def test_listening_breathes_while_quiet_and_follows_real_levels(self):
        self.buddy.listening()
        quiet = self.buddy._paint(False, 0)
        breath = self.buddy._paint(False, 1.5)
        self.assertEqual(_ink_columns(quiet, 5), [])
        self.assertNotEqual(quiet.pixels[4][3], breath.pixels[4][3])  # the sides brighten and fade
        self.assertEqual(quiet.pixels[4][4:11], breath.pixels[4][4:11])
        with patch("vision.buddy.time.time", return_value=2):
            self.buddy.hear(.3)
        soft = self.buddy._paint(False, 2)
        with patch("vision.buddy.time.time", return_value=2):
            self.buddy.hear(1)
        loud = self.buddy._paint(False, 2)
        self.assertEqual(_ink_columns(soft, 5), [7])
        self.assertEqual(_ink_columns(loud, 5), [6, 7, 8])
        self.assertEqual(_ink_columns(loud, 6), [7])
        self.assertEqual(_ink_columns(loud, 3), _ink_columns(quiet, 3))  # the eyes don't move
        self.assertEqual(_ink_columns(self.buddy._paint(False, 2.3), 5), [])  # a level fades when the mic stops
        self.buddy.listening(False)
        self.buddy.hear(1)  # heard after it stopped listening: ignored
        self.buddy.listening()
        self.assertEqual(_ink_columns(self.buddy._paint(False, 3), 5), [])

    def test_error_lasts_until_touched(self):
        self.buddy.fail()
        error = self.buddy._paint(False, 0.0)
        self.assertEqual((error.state, error.mood, error.prop[0]), ("error", "worried", "✗"))
        self.buddy.rest()
        self.assertEqual(self.buddy._paint(False, 1.0).state, "error")
        self.buddy.touch()
        self.assertEqual(self.buddy.resolve(False), "idle")

    def test_asleep_after_sleep_after_s_with_a_z_that_grows(self):
        self.buddy.sleep_after_s = 1
        self.assertEqual(self.buddy.resolve(False, 0.5), "idle")
        asleep = self.buddy._paint(False, 2.0)
        later = self.buddy._paint(False, 3.0)
        self.assertEqual((asleep.state, asleep.mood, asleep.caption), ("asleep", "asleep", "zz"))
        self.assertEqual((asleep.prop[0], later.prop[0]), ("z", "Z"))
        self.assertEqual(asleep.text()[0][13], "z")
        self.assertEqual(asleep.pixels, later.pixels)
        self.assertEqual(self.buddy.resolve(True, 2.0), "thinking")  # a turn wakes it
        self.buddy.touch()
        self.assertEqual(self.buddy.resolve(False), "idle")

    def test_inline_form_is_a_tiny_face_and_the_caption(self):
        with patch("vision.buddy.time.time", return_value=0.20):
            self.assertEqual(_text(self.buddy.render_inline(False)), " • • ")
            self.buddy.listening()
            self.assertEqual(_text(self.buddy.render_inline(False)), " • •  listening")
            self.buddy.using("Bash")
            self.assertEqual(_text(self.buddy.render_inline(True)), " ━ ━  › Bash")
            self.buddy.fail()
            self.assertEqual(_text(self.buddy.render_inline(False)), " •~•  error")
            face_style = self.buddy.render_inline(False)[0][0]
        self.assertEqual(face_style, f"fg:{palette(VISION_HUE)['ink']} bg:{palette(VISION_HUE)['top']}")
        self.buddy.touch()
        self.buddy.sleep_after_s = 0
        with patch("vision.buddy.time.time", return_value=self.buddy.last_activity + 5):
            fragments = self.buddy.render_inline(False)
        self.assertEqual(_text(fragments), " ─ ─  zz")
        self.assertEqual(fragments[0][0], f"fg:{palette(VISION_HUE, 'asleep')['ink']} bg:{palette(VISION_HUE, 'asleep')['top']}")

    def test_slide_in_enters_from_the_left_and_settles_into_the_still_gutter(self):
        from vision.buddy import SLIDE_S

        def rows(buddy, now):
            with patch("vision.buddy.time.time", return_value=now):
                return _text(buddy.render(False)).split("\n")

        still = rows(self.buddy, 100.0)
        sliding = Buddy(sleep_after_s=1000, slide_in=True)
        sliding.last_activity, sliding._blink_phase = 0, 1.0

        self.assertEqual(rows(sliding, 100.0), [" " * WIDTH] * HEIGHT)  # the clock starts at the first frame drawn
        hidden = sliding._hidden(100.0 + SLIDE_S / 3)
        self.assertTrue(0 < hidden < WIDTH)
        self.assertEqual(rows(sliding, 100.0 + SLIDE_S / 3), [r[hidden:] + " " * hidden for r in still])  # its right side, flush left
        with patch("vision.buddy.time.time", return_value=100.0 + SLIDE_S / 3):
            self.assertFalse(sliding.slid_in())
        with patch("vision.buddy.time.time", return_value=100.0 + SLIDE_S):
            self.assertTrue(sliding.slid_in())
        self.assertEqual(rows(sliding, 100.0 + SLIDE_S), still)
        self.assertTrue(self.buddy.slid_in())  # off: nothing to wait for

    def test_default_name_is_vision(self):
        self.assertEqual(Buddy().name, "Vision")
        self.assertEqual(Buddy("Juniper").name, "Juniper")


class FaceTests(unittest.TestCase):
    def test_every_mood_is_sixteen_by_four_and_its_own(self):
        drawn = set()
        for mood in FACE_MOODS:
            rows = face(mood, level=.8)  # listening shows its mouth only while it hears something
            self.assertEqual(len(rows), HEIGHT)
            self.assertEqual([row.cell_len for row in rows], [WIDTH] * HEIGHT)
            drawn.add(tuple((row.plain, tuple(map(str, row.spans))) for row in rows))
        self.assertEqual(len(drawn), len(FACE_MOODS))

    def test_colours_come_from_the_hue_and_sleep_dims_them(self):
        violet, orange = face_pixels("curious", 248), face_pixels("curious", 30)
        self.assertNotEqual(violet[4][7], orange[4][7])
        self.assertEqual(violet[4][7], face_pixels("curious")[4][7])  # Vision's own hue by default

        def lightness(hex_colour):
            return sum(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))

        self.assertLess(lightness(face_pixels("asleep")[4][7]), lightness(violet[4][7]))
        self.assertLess(lightness(palette(248, "asleep")["top"]), lightness(palette(248)["top"]))

    def test_mini_is_six_by_two(self):
        rows = mini(30, "ears", "asleep")
        self.assertEqual([row.cell_len for row in rows], [6, 6])


class BuddyLayoutTests(unittest.TestCase):
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
