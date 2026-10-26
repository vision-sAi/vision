from __future__ import annotations

import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vision import planvoice
from vision.brain import Brain, Turn
from vision.config import Config

PLAN = "Move the cache out of the request handler. Then run its tests.\n\n1. Edit server.py\n2. Run pytest"


def worker_plan(choice_from) -> tuple[dict, dict]:
    """Drive a real worker Brain to its ExitPlanMode step; `choice_from(question)` answers the form.
    Returns the question it asked and what it told Claude Code."""
    with patch("vision.brain.find_claude", return_value="claude"):
        worker = Brain(Config().brain)
    worker.task_mode = True
    worker.cfg.mode = "plan"
    proc = SimpleNamespace(stdin=io.StringIO())
    asked = []

    def on_question(qs):
        asked.extend(qs)
        return choice_from(qs[0])

    worker._answer_control(proc, {"request_id": "q1", "request": {"tool_name": "ExitPlanMode", "input": {"plan": PLAN}}},
                           on_question, lambda _: None, Turn())
    return asked[0], json.loads(proc.stdin.getvalue())["response"]["response"]


class _Reply:
    """The turn's StreamingSpeaker: still speaking for `busy` checks after the flush."""

    def __init__(self, busy=0):
        self.busy, self.log = busy, []

    def flush(self):
        self.log.append("flush")

    @property
    def speaking(self):
        self.busy -= 1
        return self.busy >= 0


class _Say:
    def __init__(self, log):
        self.log = log

    def feed(self, text):
        self.log.append(("say", text))

    def finish(self):
        self.log.append("said")


class _Mic:
    def __init__(self, log, takes, closed=None, close_on=None):
        self.log, self.takes, self.closed, self.close_on = log, list(takes), closed, close_on

    def record_utterance(self, cancel=None, on_speech_start=None):
        self.log.append("listen")
        take = self.takes.pop(0)
        if isinstance(take, Exception):
            raise take
        if self.close_on == len(self.takes):
            self.closed.set()  # answered on the keys or the phone while this one was being heard
        return None if take is None else np.full(1600, 0.1, dtype=np.float32) if take else np.zeros(0, dtype=np.float32)


class _Ears:
    def __init__(self, words):
        self.words = list(words)

    def transcribe(self, audio):
        return self.words.pop(0)


class WorkerPlanTests(unittest.TestCase):
    """The question the real worker asks is the one the voice side recognises and summarises."""

    def test_the_workers_plan_question_is_spoken_as_its_first_sentence(self):
        question, _ = worker_plan(lambda q: None)
        self.assertIs(planvoice.plan_question([question]), question)
        self.assertEqual(planvoice.plan_summary(question),
                         f"Here's the plan: Move the cache out of the request handler. {planvoice.PROMPT}")

    def test_a_spoken_go_approves_and_spoken_changes_revise(self):
        _, told = worker_plan(lambda q: planvoice.plan_answer(q, "Yeah, go for it."))
        self.assertEqual(told["behavior"], "allow")
        _, told = worker_plan(lambda q: planvoice.plan_answer(q, "nah"))
        self.assertEqual(told["behavior"], "deny")
        self.assertNotIn("said:", told["message"])
        _, told = worker_plan(lambda q: planvoice.plan_answer(q, "keep the cache, just add a lock"))
        self.assertEqual(told["behavior"], "deny")
        self.assertIn("said: keep the cache, just add a lock", told["message"])


class ListenForAnswerTests(unittest.TestCase):
    """The terminal: the reply finishes, the summary is said, then the mic hears the answer."""

    Q = {"question": f"Carry out this plan?\n\n{PLAN}", "header": "Plan"}

    def _run(self, takes, words, busy=0, closed=None, close_on=None, talking=lambda: True):
        log, answers, notes = [], [], []
        closed = closed or threading.Event()
        reply = _Reply(busy)
        planvoice.listen_for_answer(self.Q, closed, reply, lambda: _Say(log), _Mic(log, takes, closed, close_on),
                                    _Ears(words), answers.append, lambda _: None, notes.append, talking)
        return log, answers, notes, reply

    def test_it_waits_for_the_reply_then_speaks_then_listens(self):
        with patch.object(planvoice.time, "sleep", lambda _: None):
            log, answers, _, reply = self._run([True], ["Go ahead."], busy=3)
        self.assertEqual(reply.log, ["flush"])
        self.assertLess(reply.busy, 0)  # it waited out the reply before saying anything
        self.assertEqual(log, [("say", planvoice.plan_summary(self.Q)), "said", "listen"])
        self.assertEqual(answers, [{self.Q["question"]: "Yes"}])

    def test_silence_and_empty_words_keep_it_listening(self):
        with patch.object(planvoice.time, "sleep", lambda _: None):
            log, answers, _, _ = self._run([None, False, True, True], ["", "change the tests first"])
        self.assertEqual(log.count("listen"), 4)
        self.assertEqual(answers, [{self.Q["question"]: "change the tests first"}])

    def test_answered_elsewhere_first_means_no_voice_answer(self):
        closed = threading.Event()
        closed.set()
        with patch.object(planvoice.time, "sleep", lambda _: None):
            log, answers, _, _ = self._run([True], ["go"], closed=closed)
        self.assertEqual((log, answers), ([], []))  # not even the summary: the form is gone
        closed = threading.Event()
        with patch.object(planvoice.time, "sleep", lambda _: None):
            log, answers, _, _ = self._run([True], ["go"], closed=closed, close_on=0)
        self.assertEqual(answers, [])  # heard, but the keys got there first

    def test_a_dead_mic_says_so_and_leaves_the_form(self):
        with patch.object(planvoice.time, "sleep", lambda _: None):
            _, answers, notes, _ = self._run([RuntimeError("no input device")], [])
        self.assertEqual(answers, [])
        self.assertIn("no input device", notes[0])

    def test_ending_the_conversation_stops_the_listening(self):
        with patch.object(planvoice.time, "sleep", lambda _: None):
            log, answers, _, _ = self._run([True], ["go"], talking=lambda: False)
        self.assertNotIn("listen", log)
        self.assertEqual(answers, [])


if __name__ == "__main__":
    unittest.main()
