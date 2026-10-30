import asyncio
import json
import os
import shutil
import subprocess
import time
import unittest

import _nopush  # noqa: F401  (keeps the suite off the real phones)
from contextlib import ExitStack
from unittest.mock import patch

from vision import suggest, ui

CONVO = [{"role": "user", "text": "why does my loop only run once"},
         {"role": "assistant", "text": "The return is inside the loop. Want me to move it?"}]


def _ran(result: str, is_error: bool = False):
    out = json.dumps({"type": "result", "is_error": is_error, "result": result})
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")


class FakeClaude:
    """subprocess.Popen standing in for `claude`: it reads one stream-json message and answers it."""

    def __init__(self, answer, calls):
        self.answer, self.calls = answer, calls

    def __call__(self, cmd, **kw):
        proc = self

        class Proc:
            returncode = None

            def poll(self):
                return self.returncode

            def communicate(self, input=None, timeout=None):
                self.returncode = 0
                if input is None:
                    return "", ""
                proc.calls.append((cmd[0], cmd, {"input": json.loads(input)["message"]["content"], **kw}))
                if proc.answer is None:
                    return json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}) + "\n", ""
                return json.dumps({"type": "result", "is_error": False, "result": proc.answer}) + "\n", ""

            def kill(self):
                self.returncode = -9

        p = Proc()
        proc.started.append(p)
        return p

    started: list


class SuggestTests(unittest.TestCase):
    def setUp(self):
        suggest._failed.clear()
        self.addCleanup(suggest._failed.clear)
        self.addCleanup(suggest._drop_spare)

    def installed(self, *names):
        def find(name):
            if name not in names:
                raise FileNotFoundError(name)
            return name
        return patch("vision.clis.find_cli", side_effect=find)

    def fake(self, answers):
        """subprocess.run standing in for each CLI: `answers[exe]` is its guess, or None for a failed call."""
        calls = []

        def run(cmd, **kw):
            exe = cmd[0]
            calls.append((exe, cmd, kw))
            answer = answers.get(exe)
            if exe == "codex":
                if answer is None:
                    return subprocess.CompletedProcess(cmd, 1, "", "not signed in")
                with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as f:
                    f.write(answer)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if answer is None:
                return subprocess.CompletedProcess(cmd, 1, json.dumps({"type": "error", "message": "auth"}) + "\n", "")
            lines = [{"type": "text", "data": answer}, {"type": "end", "sessionId": "s"}]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(x) for x in lines), "")

        claude = FakeClaude(answers.get("claude"), calls)
        claude.started = self.started = []
        running = ExitStack()
        running.enter_context(patch("subprocess.run", side_effect=run))
        running.enter_context(patch("subprocess.Popen", side_effect=claude))
        return calls, running

    def test_the_guess_comes_from_a_toolless_haiku_call_on_the_conversation(self):
        calls, running = self.fake({"claude": "yes, move it"})
        with self.installed("claude", "codex"), running:
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
        self.assertEqual([c[0] for c in calls], ["claude"], "Claude first; Codex is not asked")
        _, cmd, kw = calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("User: why does my loop only run once", kw["input"])
        self.assertIn("Assistant: The return is inside the loop", kw["input"])

    def test_a_guess_takes_the_started_claude_and_starts_the_next(self):
        calls, running = self.fake({"claude": "yes, move it"})
        with self.installed("claude"), running:
            suggest.warm()
            self.assertEqual(len(self.started), 1, "warm starts one ahead")
            self.assertEqual(calls, [], "…which waits: nothing is asked yet")
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
            self.assertEqual(len(self.started), 2, "the guess used it and started the next")
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
            self.assertEqual(len(self.started), 3)
            self.assertEqual(len(calls), 2, "one process per guess: no guess sees another's conversation")
            suggest.warm()
            self.assertEqual(len(self.started), 3, "one is already waiting")

    def test_an_unused_spare_goes_after_a_while_instead_of_idling_for_good(self):
        calls, running = self.fake({"claude": "yes, move it"})
        with self.installed("claude"), running, patch.object(suggest, "SPARE_MAX_AGE", 0.05):
            suggest.warm()
            self.assertIsNotNone(suggest._spare)
            for _ in range(100):
                if suggest._spare is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(suggest._spare, "no turn came, so the waiting claude was let go")
            self.assertIsNotNone(self.started[0].poll(), "…and its process ended")
            self.assertEqual(calls, [], "it was never asked anything")
            suggest.warm()
            self.assertEqual(len(self.started), 2, "the next turn starts a fresh one")

    def test_a_taken_spare_is_not_let_go_mid_guess(self):
        calls, running = self.fake({"claude": "yes, move it"})
        with self.installed("claude"), running:
            suggest.warm()
            taken = suggest._take_spare(("claude", suggest.PROMPT, None))
            self.assertIsNotNone(taken)
            self.addCleanup(taken.drop)
            self.assertTrue(taken.timer.finished.is_set(), "its expiry is called off once a guess has it")

    def test_a_failing_claude_starts_no_spare(self):
        calls, running = self.fake({"claude": None, "codex": "yes, move it"})
        with self.installed("claude", "codex"), running:
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
            suggest.warm()
        self.assertEqual(len(self.started), 1, "only the one that failed; warm leaves a failing Claude alone")

    def test_without_claude_a_small_codex_model_guesses_and_leaves_no_session(self):
        calls, running = self.fake({"codex": "yes, move it"})
        from vision.models import ModelInfo

        listed = [ModelInfo("codex", "gpt-6-sol", "", "", ()), ModelInfo("codex", "gpt-6-luna", "", "", ()),
                  ModelInfo("codex", "gpt-5.6-luna", "", "", ())]
        with self.installed("codex", "grok"), running, patch("vision.models._codex_models_from_cache", return_value=listed):
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
        self.assertEqual([c[0] for c in calls], ["codex"])
        _, cmd, kw = calls[0]
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-luna", "the newest Luna Codex lists")
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", cmd)
        self.assertIn("Assistant: The return is inside the loop", kw["input"])

    def test_with_only_grok_grok_guesses(self):
        calls, running = self.fake({"grok": "yes, move it"})
        with self.installed("grok"), running:
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
        _, cmd, _ = calls[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], suggest.MODELS["grok"])
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_a_signed_out_claude_hands_over_and_is_passed_over_after(self):
        calls, running = self.fake({"claude": None, "codex": "yes, move it"})
        with self.installed("claude", "codex"), running:
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
            self.assertEqual(suggest.suggest(CONVO), "yes, move it")
        self.assertEqual([c[0] for c in calls], ["claude", "codex", "codex"], "Claude isn't retried on every reply")

    def test_a_provider_switched_off_in_vision_is_not_asked(self):
        calls, running = self.fake({"claude": "x", "codex": "yes, move it"})
        with self.installed("claude", "codex"), running:
            self.assertEqual(suggest.suggest(CONVO, providers=["codex"]), "yes, move it")
        self.assertEqual([c[0] for c in calls], ["codex"])

    def test_no_reply_yet_or_no_provider_gives_nothing(self):
        calls, running = self.fake({"claude": "x"})
        with self.installed("claude"), running:
            self.assertEqual(suggest.suggest(CONVO[:1]), "")
        self.assertEqual(calls, [])
        with self.installed(), running:
            self.assertEqual(suggest.suggest(CONVO), "")
        calls, running = self.fake({})
        with self.installed("claude", "codex", "grok"), running:
            self.assertEqual(suggest.suggest(CONVO), "")
        self.assertEqual(len(calls), 3, "each tried once, none could")

    def test_only_a_short_single_line_message_is_offered(self):
        self.assertEqual(suggest.clean(' "run the tests" '), "run the tests")
        self.assertEqual(suggest.clean("Nothing."), "")
        self.assertEqual(suggest.clean("one\ntwo"), "")
        self.assertEqual(suggest.clean("x" * 200), "")
        self.assertEqual(suggest.clean("<br>"), "")  # a model's way of sending nothing
        self.assertEqual(suggest.clean("..."), "")

    def test_a_long_reply_is_cut_to_its_end(self):
        rows = suggest._rows([{"role": "assistant", "text": "a" * 5000 + "Shall I?"}])
        self.assertTrue(rows.endswith("Shall I?"))
        self.assertLess(len(rows), suggest.ROW_CHARS + 20)


class ChatSuggestionTests(unittest.TestCase):
    def _chat(self):
        from vision.config import Config
        from vision.server import Hub

        class Brain:
            provider = "claude"
            workdir = "/tmp"

            def __init__(self, cfg, session_id=None):
                self.cfg, self.session_id, self.handoff = cfg, session_id, None

            def resolved_model(self):
                return self.cfg.model

        cfg = Config()
        creating = patch("vision.brain.create_brain", side_effect=lambda config, **kw: Brain(config, kw.get("session_id")))
        creating.start()
        self.addCleanup(creating.stop)
        hub = Hub(cfg, Brain(cfg.brain), "token", log=lambda *_: None, open_initial=False, journal_enabled=False)
        posted = []
        hub.post = posted.append
        chat = hub.open_chat()
        chat._transcript = list(CONVO)
        return chat, posted

    def test_the_guess_goes_to_the_phone_and_into_the_chat_snapshot(self):
        chat, posted = self._chat()
        with patch("vision.suggest.suggest", return_value="yes, move it"):
            asyncio.run(chat._suggest())
        self.assertIn({"type": "suggestion", "text": "yes, move it", "chat": chat.id}, posted)
        self.assertEqual(chat.summary()["suggestion"], "yes, move it")

    def test_a_guess_that_arrives_after_a_new_message_is_dropped(self):
        chat, posted = self._chat()

        def late(rows, *_):
            chat.updated += 1  # the user sent something while Haiku thought
            return "yes, move it"

        with patch("vision.suggest.suggest", side_effect=late):
            asyncio.run(chat._suggest())
        self.assertFalse([e for e in posted if e.get("type") == "suggestion"])
        self.assertEqual(chat.suggestion, "")

    def test_a_new_conversation_drops_the_old_ones_guess(self):
        chat, posted = self._chat()
        chat.suggestion = "yes, move it"
        chat.reset_history()
        self.assertEqual(chat.summary()["suggestion"], "")

        chat._transcript = list(CONVO)

        def late(rows, *_):
            chat.reset_history()  # the conversation changed while Haiku thought
            return "yes, move it"

        with patch("vision.suggest.suggest", side_effect=late):
            asyncio.run(chat._suggest())
        self.assertEqual(chat.suggestion, "")
        self.assertFalse([e for e in posted if e.get("type") == "suggestion"])

    def test_a_failed_reply_gets_no_guess(self):
        chat, _ = self._chat()
        chat._transcript[-1] = {"role": "assistant", "text": "", "error": "rate limited"}
        with patch("vision.suggest.suggest") as guess:
            asyncio.run(chat._suggest())
        guess.assert_not_called()


class TerminalSuggestionTests(unittest.TestCase):
    """The terminal's box: the guess grey while it is empty, Tab takes it, Enter sends it."""

    def setUp(self):
        import os
        import tempfile

        from vision.ui import ChatScreen

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.screen = ChatScreen(os.path.join(self.dir.name, "history"), lambda: "")
        self.sent = []
        self.screen.on_submit = self.sent.append
        self.screen.suggestion = "yes, move it"

    def press(self, name):
        from prompt_toolkit.keys import Keys

        keys = {"tab": Keys.ControlI, "enter": Keys.ControlM}[name]
        binding = next(b for b in self.screen.app.key_bindings.get_bindings_for_keys((keys,)) if b.filter())
        binding.handler(None)

    def drawn(self):
        from prompt_toolkit.layout.processors import TransformationInput

        processor = next(p for p in self.screen.area.control.input_processors if isinstance(p, ui.Suggestion))
        doc = self.screen.area.buffer.document
        ti = TransformationInput(self.screen.area.control, doc, 0, lambda i: i, [("", doc.text)], 80, 5)
        return "".join(t for _, t, *_ in processor.apply_transformation(ti).fragments)

    def test_the_guess_is_grey_only_in_an_empty_idle_box(self):
        from prompt_toolkit.document import Document

        self.assertEqual(self.drawn(), "yes, move it")
        self.screen.busy = True
        self.assertEqual(self.drawn(), "")
        self.screen.busy = False
        self.screen.area.buffer.set_document(Document("no", 2))
        self.assertEqual(self.drawn(), "no")

    def test_enter_sends_the_guess_and_clears_it(self):
        self.press("enter")
        self.assertEqual(self.sent, ["yes, move it"])
        self.assertEqual(self.screen.suggestion, "")

    def test_tab_puts_the_guess_in_the_box_to_change(self):
        self.press("tab")
        self.assertEqual(self.screen.area.text, "yes, move it")
        self.assertEqual(self.sent, [])

    def test_with_no_guess_enter_on_an_empty_box_sends_nothing(self):
        self.screen.suggestion = ""
        self.press("enter")
        self.assertEqual(self.sent, [])


class JoinedSuggestionTests(unittest.TestCase):
    def test_the_servers_guess_reaches_a_joined_terminal(self):
        from vision.config import BrainConfig
        from vision.remote import RemoteBrain

        got = []
        rb = RemoteBrain({"url": "http://x", "token": "t"}, {"chat": "c1"}, BrainConfig(), on_suggestion=got.append)
        rb._dispatch({"type": "suggestion", "chat": "c1", "text": "run the tests"})
        rb._dispatch({"type": "suggestion", "chat": "other", "text": "not ours"})
        rb._dispatch({"type": "chat", "chat": "c1", "busy": True, "suggestion": ""})
        self.assertEqual(got, ["run the tests", ""])


# The reply that drew "Does it still jump when the drawer opens?": the build was already on the phone,
# so the user's next word is what they saw there, not a question back.
HANDED_OVER = [
    {"role": "user", "text": "the drawer still jumps at the edges when it closes, fix it and put it on my phone"},
    {"role": "assistant", "text": (
        "The shrink is back and the edge jump is gone. The new build is installed and running on your phone.\n\n"
        "The jump came from SwiftUI, not UIKit as I'd thought: anything under a scaleEffect loses the screen's "
        "safe area. When the scale got back to exactly 1, it snapped out to the screen edges.\n\n"
        "Fix: the chat now lays out on the full screen outside the scale, and gets the status bar and home "
        "indicator insets back as padding inside it. The change is in SideDrawer.swift.\n\n"
        "Simulator: on a close, the card stays full height the whole way, and the header moves at most a couple "
        "of points as it lands. I've removed the debug hook and shut down the simulator and fake server.")},
]

# Vision asked what the user wants; only the user knows, so there is nothing to guess.
OPEN_QUESTION = [
    {"role": "user", "text": "Hello"},
    {"role": "assistant", "text": "Hey. What are we working on?"},
]


@unittest.skipUnless(os.environ.get("VISION_LIVE") and shutil.which("claude"),
                     "asks the real Haiku: set VISION_LIVE=1 with Claude Code signed in")
class LiveGuessTests(unittest.TestCase):
    def test_after_a_hand_over_the_guess_is_the_users_verdict_not_a_question(self):
        answer = suggest.one_shot(suggest.ask(suggest._rows(HANDED_OVER)), ("claude",))
        self.assertIsNotNone(answer, "Haiku never answered (signed out, or `claude` couldn't run)")
        guess = suggest.clean(answer)
        self.assertFalse(guess.rstrip().endswith("?"), f"a question back to the assistant: {guess!r}")

    def test_an_open_question_from_the_assistant_gets_no_guess(self):
        answer = suggest.one_shot(suggest.ask(suggest._rows(OPEN_QUESTION)), ("claude",))
        self.assertIsNotNone(answer, "Haiku never answered (signed out, or `claude` couldn't run)")
        self.assertEqual(suggest.clean(answer), "", f"filler guessed where only the user knows: {answer!r}")


if __name__ == "__main__":
    unittest.main()
