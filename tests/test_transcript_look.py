"""The transcript's gutter marks, notices, header and live line."""
import io
import unittest

from rich.console import Console


def _print(renderable, width: int = 60) -> str:
    console = Console(width=width, force_terminal=False, file=io.StringIO())
    console.print(renderable)
    return console.file.getvalue()


class GutterTests(unittest.TestCase):
    def test_user_turn_hangs_off_its_mark(self):
        from vision.ui import user_grid

        out = _print(user_grid("one two three four five six seven eight nine ten eleven twelve"), width=40)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("› one two"))
        self.assertTrue(lines[1].startswith("  ") and not lines[1].startswith("   "))  # continuation rows sit under the text, not the mark

    def test_hearing_line_says_listening_until_the_first_word(self):
        from vision.ui import hearing_grid

        self.assertIn("◉ listening…", _print(hearing_grid("")))
        self.assertIn("◉ what about …", _print(hearing_grid("what about")))

    def test_notices_carry_their_kind_as_a_mark(self):
        from vision.ui import notice_grid

        self.assertTrue(_print(notice_grid("speech off")).startswith("· speech off"))
        self.assertTrue(_print(notice_grid("mic not found", "warn")).startswith("⚠ mic not found"))
        self.assertTrue(_print(notice_grid("it broke", "error")).startswith("✗ it broke"))
        lines = _print(notice_grid("forgot:\nline one")).splitlines()
        self.assertEqual([lines[0].rstrip(), lines[1].rstrip()], ["· forgot:", "  line one"])

    def test_header_is_the_open_card(self):
        from vision.ui import header_renderable

        out = _print(header_renderable(model="Opus 5 · high", directory="~/code/project", voice="off"))
        self.assertIn("◆ Vision", out)
        self.assertIn("model:", out)
        self.assertIn("Opus 5 · high", out)
        self.assertIn("directory:", out)
        self.assertIn("~/code/project", out)
        self.assertIn("voice:", out)
        self.assertIn("off", out)
        self.assertIn("▰", out)  # Pip's idle eyes
        self.assertIn("╭", out)
        self.assertNotIn("/model to change", out)
        self.assertNotIn("resumed", out)
        resumed = _print(header_renderable(model="Opus 5 · high", directory="~/code/project", voice="jarvis", resumed=True))
        self.assertIn("resumed", resumed)
        self.assertIn("jarvis", resumed)

    def test_open_card_survives_the_transcript_ansi_path(self):
        from vision.ui import _Entry, header_renderable

        text = "".join(frag[1] for row in _Entry(header_renderable(
            model="Opus 5 · high", directory="~/code/project", voice="off",
        )).lines(80) for frag in row)
        self.assertIn("◆ Vision", text)
        self.assertIn("▰", text)
        self.assertIn("model:", text)
        self.assertIn("directory:", text)
        self.assertIn("voice:", text)

    def test_live_reply_rows_get_the_mark_and_the_live_line_its_hints(self):
        from vision.ui import WORKING, _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.buf, e.shown, e.status = "hello there", 1 << 30, WORKING
        e.tokens_fn = lambda: 1234
        text = ["".join(f[1] for f in row) for row in e.lines(60)]
        self.assertEqual(text[0].rstrip(), "")  # a blank line keeps the reply off the user's band
        self.assertEqual(text[1].rstrip(), "● hello there")
        self.assertRegex(text[-1], r"^. working… \(0s · ↓ 1\.2k · esc to stop\)$")
        e.status = "using Bash…"
        e.invalidate()
        self.assertIn("using Bash…", "".join(f[1] for f in e.lines(60)[-1]))  # a tool status is shown as given
        from vision.brain import ToolCall

        e.place(ToolCall(id="t1", name="Bash", detail="du -sh ~"))
        e.invalidate()
        live = "".join(f[1] for f in e.lines(60)[-1])
        self.assertRegex(live, r"^0s · ↓ 1\.2k · esc to stop$")  # the tool's row spins, not the live line too

    def test_working_verbs_rotate_with_time(self):
        from vision.ui import VERB_SECONDS, WORKING, WORKING_VERBS, _ReplyEntry

        e = _ReplyEntry(markdown=True)
        e.status = WORKING
        self.assertEqual(e.live_label(), WORKING_VERBS[0])
        e.started -= VERB_SECONDS + 0.5
        self.assertEqual(e.live_label(), WORKING_VERBS[1])

    def test_short_count(self):
        from vision.ui import short_count

        self.assertEqual([short_count(n) for n in (842, 1234, 13400, 1_100_000)], ["842", "1.2k", "13k", "1.1M"])


if __name__ == "__main__":
    unittest.main()
