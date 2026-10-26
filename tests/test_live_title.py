import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from vision import sessions
from vision.sessions import LiveTitle


def _rec(**kw) -> bytes:
    return json.dumps(kw).encode() + b"\n"


class LiveTitleTests(unittest.TestCase):
    def test_claude_title_follows_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "-home-x"))
            path = os.path.join(d, "-home-x", "abc.jsonl")
            with patch.object(sessions, "CLAUDE_PROJECTS", d):
                t = LiveTitle()
                self.assertEqual(t.get("claude", None), "")
                self.assertEqual(t.get("claude", "abc", now=0), "")  # no file yet
                with open(path, "ab") as f:
                    f.write(_rec(type="user", entrypoint="sdk-cli", message={"role": "user", "content": "fix the   tab title please"}))
                    f.write(b'{"type":"assistant","message":{"content":[')  # a half-written line
                self.assertEqual(t.get("claude", "abc", now=1), "")  # within POLL: not re-read
                self.assertEqual(t.get("claude", "abc", now=3), "fix the tab title please")
                with open(path, "ab") as f:
                    f.write(b'{"type":"text"}]}}\n')
                    f.write(_rec(type="ai-title", aiTitle="Terminal tab title", sessionId="abc"))
                self.assertEqual(t.get("claude", "abc", now=6), "Terminal tab title")
                with open(path, "ab") as f:
                    f.write(_rec(type="ai-title", aiTitle="Tab title and spinner", sessionId="abc"))
                self.assertEqual(t.get("claude", "abc", now=9), "Tab title and spinner")
                # a new session starts over
                self.assertEqual(t.get("claude", "zzz", now=9), "")

    def test_worker_prompt_is_not_a_title(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "p"))
            with open(os.path.join(d, "p", "w1.jsonl"), "wb") as f:
                f.write(_rec(type="user", entrypoint="sdk-cli", message={"content": json.dumps({"type": "vision_task", "text": "x"})}))
            with patch.object(sessions, "CLAUDE_PROJECTS", d):
                self.assertEqual(LiveTitle().get("claude", "w1"), "")


class ProcTitleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "hide_cmdline works through /proc and prctl")
    def test_hide_cmdline_in_a_child(self):
        import subprocess, sys

        code = (
            "from vision.proctitle import hide_cmdline\n"
            "ok = hide_cmdline('vision')\n"
            "print(ok, repr(open('/proc/self/cmdline','rb').read()), open('/proc/self/comm').read().strip())\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["True", "b'\\x00'", "vision"])

    def test_cuda_names_a_blank_cmdline_by_comm(self):
        from vision.cuda import cmdline, holder_name

        with patch("builtins.open", side_effect=OSError):
            self.assertEqual(cmdline(1), [])
        with patch("vision.proctitle.process_name", return_value="vision"):
            with patch("vision.cuda.open", create=True) as op:
                op.return_value.__enter__ = lambda s: s
                op.return_value.__exit__ = lambda *a: None
                op.return_value.read.return_value = b"\x00"
                self.assertEqual(holder_name(cmdline(4242)), "vision")


if __name__ == "__main__":
    unittest.main()
