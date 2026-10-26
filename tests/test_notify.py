"""The desktop notification for a long reply finished in another window, and the terminal focus
reports it depends on."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vision import notify
from vision.ui import strip_focus_reports


class FocusReportTests(unittest.TestCase):
    def test_reports_are_taken_out_and_the_flag_follows(self):
        seen = []
        out = strip_focus_reports("ab\x1b[Ocd\x1b[Ie", seen.append)
        self.assertEqual(out, "abcde")
        self.assertEqual(seen, [False, True])

    def test_other_escapes_pass_untouched(self):
        seen = []
        data = "\x1b[A\x1b[<0;10;5M\x1b[200~x\x1b[201~"  # up arrow, a mouse click, a bracketed paste
        self.assertEqual(strip_focus_reports(data, seen.append), data)
        self.assertEqual(seen, [])
        self.assertIs(strip_focus_reports("plain", seen.append), "plain")

    def test_the_screen_tracks_focus_through_the_parser(self):
        import os
        import tempfile

        from vision.ui import ChatScreen

        class Parser:
            def __init__(self):
                self.fed = []

            def feed(self, data):
                self.fed.append(data)

        with tempfile.TemporaryDirectory() as d:
            screen = ChatScreen(os.path.join(d, "history"), lambda: "")
            parser = Parser()
            screen.app.input = SimpleNamespace(vt100_parser=parser)
            self.assertTrue(screen._track_focus())
            self.assertTrue(screen.focused)
            self.assertTrue(screen.maybe_unfocused)  # no report yet: a notice is sent rather than missed
            parser.feed("hi\x1b[O")
            self.assertFalse(screen.focused)
            self.assertTrue(screen.maybe_unfocused)
            parser.feed("\x1b[Ithere")
            self.assertTrue(screen.focused)
            self.assertFalse(screen.maybe_unfocused)
            self.assertEqual(parser.fed, ["hi", "there"])


class DesktopNotifyTests(unittest.TestCase):
    def test_linux_uses_notify_send(self):
        calls = []
        with patch.object(notify.sys, "platform", "linux"), patch.object(notify.shutil, "which", lambda n: "/usr/bin/notify-send" if n == "notify-send" else None), \
                patch.object(notify.subprocess, "Popen", lambda cmd, **k: calls.append(cmd)):
            self.assertTrue(notify.desktop_notify("Vision", "reply ready · 2m 10s · Done."))
        self.assertEqual(calls, [["notify-send", "--app-name=Vision", "Vision", "reply ready · 2m 10s · Done."]])

    def test_mac_uses_osascript_with_quotes_escaped(self):
        calls = []
        with patch.object(notify.sys, "platform", "darwin"), patch.object(notify.shutil, "which", lambda n: f"/usr/bin/{n}"), \
                patch.object(notify.subprocess, "Popen", lambda cmd, **k: calls.append(cmd)):
            self.assertTrue(notify.desktop_notify("Vision", 'said "hi"'))
        self.assertEqual(calls[0][:2], ["osascript", "-e"])
        self.assertEqual(calls[0][2], 'display notification "said \\"hi\\"" with title "Vision"')

    def test_nothing_to_notify_with_is_quietly_false(self):
        with patch.object(notify.sys, "platform", "linux"), patch.object(notify.shutil, "which", lambda n: None):
            self.assertFalse(notify.desktop_notify("Vision", "x"))

    def test_summary_is_the_first_real_line_cut_short(self):
        self.assertEqual(notify.reply_summary("## Done\n\nThe tests pass now."), "Done")
        self.assertEqual(notify.reply_summary("```sh\nls\n```\nafter"), "ls")  # code is still words
        self.assertEqual(notify.reply_summary("- " + "x" * 80, limit=10), "xxxxxxxxx…")
        self.assertEqual(notify.reply_summary(""), "")


if __name__ == "__main__":
    unittest.main()
