"""/copy: the fenced code blocks of a reply, and which reply the screen hands over."""
import os
import tempfile
import unittest

from vision.ui import ChatScreen, code_blocks


class CodeBlockTests(unittest.TestCase):
    def test_blocks_come_in_order_with_their_language(self):
        md = "Run this:\n\n```bash\nls -la\necho done\n```\n\nthen\n\n```\nplain\n```\n"
        self.assertEqual(code_blocks(md), [("bash", "ls -la\necho done"), ("", "plain")])

    def test_tilde_fences_and_info_strings(self):
        md = "~~~python title=x.py\nprint(1)\n~~~\n"
        self.assertEqual(code_blocks(md), [("python", "print(1)")])

    def test_a_fence_inside_a_list_item_loses_the_items_indent(self):
        md = "1. do it\n\n   ```sh\n   make\n     test\n   ```\n"
        self.assertEqual(code_blocks(md), [("sh", "make\n  test")])

    def test_a_longer_fence_can_hold_a_shorter_one(self):
        md = "````md\n```py\nx = 1\n```\n````\n"
        self.assertEqual(code_blocks(md), [("md", "```py\nx = 1\n```")])

    def test_no_blocks(self):
        self.assertEqual(code_blocks("just words, `inline` code"), [])


class LastReplyTests(unittest.TestCase):
    def test_the_latest_reply_with_text_is_copied(self):
        with tempfile.TemporaryDirectory() as d:
            screen = ChatScreen(os.path.join(d, "history"), lambda: "")
            self.assertEqual(screen.last_reply(), "")
            first = screen.start_reply()
            screen.update_reply(first, delta="one", force=True)
            screen.end_reply(first)
            second = screen.start_reply()
            screen.update_reply(second, delta="two\n\n```sh\nls\n```", force=True)
            screen.end_reply(second)
            self.assertTrue(screen.last_reply().startswith("two"))
            self.assertEqual(code_blocks(screen.last_reply()), [("sh", "ls")])
            empty = screen.start_reply()  # a reply that said nothing (an error, a cancel) is skipped
            screen.end_reply(empty, cancelled=True)
            self.assertTrue(screen.last_reply().startswith("two"))


if __name__ == "__main__":
    unittest.main()
