from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.document import Document

from vision import ui
from vision.ui import ChatScreen

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class ClipboardTests(unittest.TestCase):
    """What Ctrl-V reads off the system clipboard, through wl-paste or xclip."""

    def read(self, types: str, contents: dict):
        asked = []

        def clipboard(*cmd):
            asked.append(cmd)
            return types.encode() if cmd[-1] in ("--list-types", "-o") else contents.get(cmd[-1])

        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), patch.object(ui, "_clipboard", clipboard):
            return ui.clipboard_picture(), ui.clipboard_text(), asked

    def test_a_picture_comes_back_with_the_suffix_to_save_it_under(self):
        picture, text, asked = self.read("image/png\nimage/bmp\n", {"image/png": PNG})
        self.assertEqual(picture, (PNG, ".png"))
        self.assertEqual(text, "")
        self.assertEqual(asked[0][0], "wl-paste")

    def test_text_is_not_a_picture(self):
        picture, text, _ = self.read("text/plain\ntext/plain;charset=utf-8\n", {"text/plain;charset=utf-8": "héllo\r\nthere".encode()})
        self.assertIsNone(picture)
        self.assertEqual(text, "héllo\nthere")

    def test_with_no_display_nothing_is_read(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ui, "_clipboard", side_effect=AssertionError("asked")):
            self.assertIsNone(ui.clipboard_picture())
            self.assertEqual(ui.clipboard_text(), "")

    def test_a_tool_that_is_not_installed_reads_as_nothing(self):
        with patch("shutil.which", return_value=None):
            self.assertIsNone(ui._clipboard("wl-paste", "--list-types"))


class PasteTests(unittest.TestCase):
    """Ctrl-V in the chat: a picture is saved with the phone's uploads and attached; text is typed in."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.uploads = Path(self.dir.name) / "uploads"
        self.screen = ChatScreen(os.path.join(self.dir.name, "history"), lambda: "")
        patcher = patch.object(ui, "_image_numbers", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def draft(self, text: str) -> None:
        self.screen.area.buffer.set_document(Document(text, len(text)))

    def paste(self, picture=None, text=""):
        with patch.object(ui, "PASTE_DIR", self.uploads), patch.object(ui, "clipboard_picture", return_value=picture), \
                patch.object(ui, "clipboard_text", return_value=text):
            self.screen.paste()
        return self.screen.area.text

    def test_a_picture_is_saved_and_shows_as_image_n(self):
        self.draft("what is this")
        draft = self.paste(picture=(PNG, ".png"))
        saved = list(self.uploads.iterdir())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), PNG)
        self.assertRegex(saved[0].name, r"^\d{8}-\d{6}-[0-9a-f]{6}\.png$")  # named as the phone's uploads are
        self.assertEqual(draft, "what is this [Image #1] ")
        self.assertEqual(ui.expand_images(draft), f"what is this\n[Attached image: {saved[0]}]")
        self.assertEqual(self.screen._notice[0], "picture attached")

    def test_two_pictures_are_numbered_and_each_sent_on_its_own_line(self):
        self.paste(picture=(PNG, ".png"))
        draft = self.paste(picture=(PNG, ".jpg"))
        self.assertEqual(draft, "[Image #1] [Image #2] ")
        sent = ui.expand_images(draft).splitlines()
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(line.startswith("[Attached image: ") for line in sent))

    def test_a_pasted_picture_path_attaches_it(self):
        shot = Path(self.dir.name) / "Screenshot From 2026-10-21 18-06-40.png"
        shot.write_bytes(PNG)
        for pasted in (f"'{shot}'", str(shot), f"file://{str(shot).replace(' ', '%20')}\n", str(shot).replace(" ", "\\ ")):
            self.screen.area.buffer.reset()
            self.screen.paste_text(pasted)
            self.assertEqual(self.screen.area.text, "[Image #1] ", pasted)
        self.screen.paste_text("look at it")
        self.assertEqual(ui.expand_images(self.screen.area.text), f"[Attached image: {shot}]\nlook at it")

    def test_a_path_that_is_not_a_picture_is_typed_in(self):
        note = Path(self.dir.name) / "notes.txt"
        note.write_text("hi")
        for pasted in (str(note), "/no/such/picture.png", "see /no/such/picture.png"):
            self.screen.area.buffer.reset()
            self.screen.paste_text(pasted)
            self.assertEqual(self.screen.area.text, pasted)

    def test_an_unknown_number_is_left_as_typed(self):
        self.assertEqual(ui.expand_images("what is [Image #9]"), "what is [Image #9]")

    def test_the_transcript_shows_image_n_linked_to_the_file(self):
        rendered = ui.render_ansi(ui.user_grid("what is this\n[Attached image: /tmp/a b.png]"), 80)
        self.assertIn("[Image #1]", rendered)
        self.assertIn("file:///tmp/a b.png", rendered)
        self.assertNotIn("Attached image", rendered)
        plain = ui.Text.from_ansi(ui.render_ansi(ui.user_grid("what\n[Attached image: /a.png]\nand\n[Attached image: /b.png]\nok\nbye"), 80)).plain
        self.assertEqual([r.rstrip() for r in plain.splitlines()], ["▌ what [Image #2] and [Image #3] ok", "▌ bye"])  # inline, as typed; the user's own newline stays

    def test_text_is_typed_in_where_the_cursor_is(self):
        self.draft("say ")
        self.assertEqual(self.paste(text="hello"), "say hello")
        self.assertFalse(self.uploads.exists())

    def test_an_empty_clipboard_says_so_and_changes_nothing(self):
        self.assertEqual(self.paste(), "")
        self.assertIn("nothing to paste", self.screen._notice[0])


if __name__ == "__main__":
    unittest.main()
