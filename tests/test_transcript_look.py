"""The transcript's gutter marks, notices, header and live line."""
import io
import unittest

from rich.console import Console


def _print(renderable, width: int = 60) -> str:
    console = Console(width=width, force_terminal=False, file=io.StringIO())
    console.print(renderable)
    return console.file.getvalue()


class GutterTests(unittest.TestCase):
    def test_user_turn_runs_a_violet_bar_down_every_row(self):
        from vision.theme import ACCENT
        from vision.ui import user_grid

        lines = _print(user_grid("one two three four five six seven eight nine ten eleven twelve"), width=40).splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[0].startswith("▌ one two"))
        self.assertTrue(all(line.startswith("▌ ") and not line.startswith("▌  ") for line in lines))  # wrapped rows keep the bar
        console = Console(width=40, force_terminal=True, color_system="truecolor", file=io.StringIO())
        segs = list(console.render(user_grid("hi"), console.options.update_width(40)))
        self.assertTrue(all(s.style is None or s.style.bgcolor is None for s in segs))  # no fill behind it
        bar = next(s for s in segs if s.text.startswith("▌"))
        self.assertEqual(bar.style.color.get_truecolor().hex, ACCENT)

    def test_shell_command_keeps_its_pink_mark(self):
        from vision.ui import shell_grid

        lines = _print(shell_grid("ls -la"), width=30).splitlines()
        self.assertEqual(lines[0].rstrip(), "! ls -la")

    def test_hearing_line_says_listening_until_the_first_word(self):
        from vision.ui import hearing_grid

        self.assertIn("◉ listening…", _print(hearing_grid("")))
        self.assertIn("◉ what about …", _print(hearing_grid("what about")))

    def test_notices_carry_their_kind_as_a_mark(self):
        from vision.ui import notice_grid

        self.assertTrue(_print(notice_grid("speech off")).startswith("  · speech off"))  # in line with the prose
        self.assertTrue(_print(notice_grid("mic not found", "warn")).startswith("  ⚠ mic not found"))
        self.assertTrue(_print(notice_grid("it broke", "error")).startswith("  ✗ it broke"))
        lines = _print(notice_grid("forgot:\nline one")).splitlines()
        self.assertEqual([lines[0].rstrip(), lines[1].rstrip()], ["  · forgot:", "    line one"])

    def test_header_is_the_open_card(self):
        from vision.ui import header_renderable

        out = _print(header_renderable(model="Opus 5 · high", directory="~/code/project", voice="off", version="2.4.0"), width=100)
        self.assertIn("Vision  2.4.0", out)
        self.assertIn("Opus 5 · high  ·  ~/code/project", out)
        self.assertIn("voice off", out)
        self.assertNotIn("scout", out.split("╯")[0])  # no scouts known: none mentioned on the card
        self.assertIn("▀", out)  # the buddy, in half blocks
        self.assertIn("╭", out)
        self.assertIn("/ commands", out)  # the keys under the card
        self.assertIn("shift-tab auto / plan", out)
        self.assertNotIn("resumed", out)
        resumed = _print(header_renderable(model="Opus 5 · high", directory="~/code/project", voice="jarvis", resumed=True))
        self.assertIn("resumed", resumed)
        self.assertIn("voice jarvis", resumed)

    def test_open_card_counts_the_scouts_and_those_waiting(self):
        from vision.ui import header_renderable

        out = _print(header_renderable(model="Opus 5 · high", directory="~", voice="off", scouts=3, needing=1), width=100)
        self.assertIn("voice off  ·  3 scouts, 1 needs you", out)
        out = _print(header_renderable(model="Opus 5 · high", directory="~", voice="off", scouts=1, needing=0), width=100)
        self.assertIn("voice off  ·  1 scout", out)
        self.assertNotIn("need", out)

    def test_open_tips_drop_what_does_not_fit(self):
        from vision.ui import open_tips

        self.assertEqual(_print(open_tips(), width=24).rstrip("\n"), "  / commands   ! shell")

    def test_open_card_survives_the_transcript_ansi_path(self):
        from vision.ui import _Entry, header_renderable

        text = "".join(frag[1] for row in _Entry(header_renderable(
            model="Opus 5 · high", directory="~/code/project", voice="off",
        )).lines(80) for frag in row)
        self.assertIn("Vision", text)
        self.assertIn("▀", text)
        self.assertIn("Opus 5 · high", text)
        self.assertIn("~/code/project", text)
        self.assertIn("voice off", text)

    def test_live_reply_rows_sit_in_the_gutter_and_the_live_line_has_its_hints(self):
        from vision.ui import WORKING, _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.buf, e.shown, e.status = "hello there", 1 << 30, WORKING
        e.tokens_fn = lambda: 1234
        text = ["".join(f[1] for f in row) for row in e.lines(60)]
        self.assertEqual(text[0].rstrip(), "")  # a blank line keeps the reply off the user's band
        self.assertEqual(text[1].rstrip(), "  hello there")  # no mark: the band says who is talking
        self.assertRegex(text[-1], r"^  . working… \(0s · ↓ 1\.2k · esc to stop\)$")
        e.status = "using Bash…"
        e.invalidate()
        self.assertIn("using Bash…", "".join(f[1] for f in e.lines(60)[-1]))  # a tool status is shown as given
        from vision.brain import ToolCall

        e.place(ToolCall(id="t1", name="Bash", detail="du -sh ~"))
        e.invalidate()
        live = "".join(f[1] for f in e.lines(60)[-1])
        self.assertRegex(live, r"^  0s · ↓ 1\.2k · esc to stop$")  # the tool's row spins, not the live line too

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


class CodeCardTests(unittest.TestCase):
    def test_a_code_block_sits_between_rules_naming_its_language(self):
        from vision.ui import ChatMarkdown

        lines = [ln.rstrip() for ln in _print(ChatMarkdown("Run:\n\n```python\nprint(1)\n```"), width=40).splitlines()]
        top = next(i for i, ln in enumerate(lines) if ln.startswith("─ python ─"))
        self.assertEqual(lines[top], "─ python " + "─" * 31)  # across the width
        self.assertEqual(lines[top + 2].strip(), "print(1)")  # a row of the code fill above it
        self.assertEqual(lines[top + 4], "─" * 40)
        self.assertFalse(any("│" in ln for ln in lines))  # no sides: a copied selection is only the code

    def test_the_typewriter_brings_a_rule_in_whole(self):
        from vision.ui import ChatMarkdown, _Reveal, render_ansi

        reveal = _Reveal(render_ansi(ChatMarkdown("```python\nx = 1\n```"), 40), 20)
        self.assertEqual(reveal.total, 1 + len("x=1") + 1)  # each rule counts as one character
        first = reveal.cut(1)
        self.assertEqual("".join(f[1] for f in first[-1]).rstrip(), "─ python " + "─" * 31)


class StatusRowTests(unittest.TestCase):
    PROJECTS = [
        {"scout": {"name": "Moss", "hue": 140}, "mood": "proud", "threads": {}},
        {"scout": {"name": "Wren", "hue": 270}, "mood": "asleep", "threads": {}},
        {"scout": {"name": "Atlas", "hue": 205}, "mood": "needs_you", "threads": {"waiting_on_you": 1}},
        {"scout": {"name": "Fern", "hue": 90}, "mood": "worried", "threads": {"ready_for_review": 1}, "approvals": [{"id": "x"}]},
        {"scout": {"name": "Juniper", "hue": 28}, "mood": "focused", "threads": {}},
        {"scout": {"name": "Old", "hue": 0}, "mood": "focused", "status": "archived", "threads": {}},
    ]

    def test_a_scout_says_what_it_is_up_to(self):
        from vision.theme import DANGER, OK, WARN
        from vision.ui import scout_state

        self.assertEqual(scout_state({"mood": "needs_you", "threads": {"waiting_on_you": 1}}), ("needs you", WARN))
        self.assertEqual(scout_state({"mood": "worried", "threads": {"ready_for_review": 1}, "approvals": [{}]}), ("2 need you", WARN))
        self.assertEqual(scout_state({"mood": "worried"}), ("something failed", DANGER))
        self.assertEqual(scout_state({"mood": "proud"}), ("landed something", OK))
        self.assertEqual(scout_state({"mood": "focused"})[0], "working")
        self.assertEqual(scout_state({"mood": "curious"})[0], "reading up")
        self.assertIsNone(scout_state({"mood": "asleep"}))  # nothing to say: off the row
        self.assertIsNone(scout_state({"mood": "focused", "status": "archived"}))

    def test_three_scouts_at_most_those_waiting_on_you_first(self):
        from vision.theme import hsl
        from vision.ui import scout_fragments

        frags = scout_fragments(self.PROJECTS)
        text = "".join(t for _, t in frags)
        self.assertEqual(text, "● Atlas needs you   ● Fern 2 need you   ● Moss landed something")
        self.assertIn((f"fg:{hsl(205, 54, 55)}", "● "), frags)  # the dot in the scout's own colour
        self.assertEqual(scout_fragments([]), [])

    def _screen(self):
        import os
        import tempfile

        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from vision.ui import ChatScreen

        pipe = self.enterContext(create_pipe_input())
        from prompt_toolkit.application import Application

        orig = Application.__init__

        def init(app, *a, **kw):
            kw.setdefault("input", pipe)
            kw.setdefault("output", DummyOutput())
            orig(app, *a, **kw)

        Application.__init__ = init
        self.addCleanup(setattr, Application, "__init__", orig)
        return ChatScreen(os.path.join(tempfile.mkdtemp(), "history"), lambda: "speech on", mode=lambda: "plan")

    def test_the_row_leads_with_the_scouts_and_the_mode_moves_to_the_composer(self):
        screen = self._screen()
        screen.scouts_fn = lambda: self.PROJECTS[:1]
        screen.folder_fn = lambda: "~/code/vision"
        text = "".join(t for _, t in screen._status_text())
        self.assertEqual(text.strip(), "● Moss landed something   speech on")
        self.assertNotIn("plan", text)
        self.assertEqual(screen._chips(), [("class:mode.plan", "⏸ plan"), ("class:frame.sep", " · "), ("class:frame.folder", "~/code/vision")])
        screen.scouts_fn = lambda: 1 / 0  # a broken source shows no scouts rather than breaking the screen
        self.assertEqual("".join(t for _, t in screen._status_text()).strip(), "speech on")

    def test_the_empty_box_shows_the_placeholder_but_enter_does_not_send_it(self):
        screen = self._screen()
        sent = []
        screen.on_submit = sent.append
        self.assertEqual(screen._ghost(), screen.placeholder)
        screen.suggestion = "run the tests"
        self.assertEqual(screen._ghost(), "run the tests")
        screen.suggestion = ""
        screen._submit(screen.area.text or screen._suggestion_shown())
        self.assertEqual(sent, [])
        screen.shell_mode = True
        self.assertEqual(screen._ghost(), "")

    def test_the_title_is_pinned_once_there_is_one(self):
        screen = self._screen()
        screen.folder_fn = lambda: "~/code/vision"
        screen.title_fn = lambda: "Fix the flaky upload test"
        screen._sync_title()
        rows = "".join(t for _, t in screen._title_text()).split("\n")
        self.assertEqual(rows[0], "  Fix the flaky upload test  ~/code/vision")
        self.assertTrue(rows[1].strip() and set(rows[1].strip()) == {"─"})


if __name__ == "__main__":
    unittest.main()
