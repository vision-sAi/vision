from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import time
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vision.brain import Brain, BrainError, ToolCall, Turn
from vision.codex import CodexBrain
from vision.config import Config, load_config
from vision.conversation import ClaudeConversation, SpeechStream, VoiceConversation, in_workspace, validate_response
from vision.delegation import task_result, validate_task


TASK = dict(objective="Fix the failing check", context="The user reported a failing test.",
            constraints=["Keep unrelated changes"], success_criteria=["The check passes"])
RESULT = dict(status="completed", summary="Fixed the check.", changes=["Updated the parser"],
              checks=["Parser tests: passed"], findings=[], question=None)


class FakeModel:
    usage = {"input_tokens": 10, "output_tokens": 5}

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.packets = []
        self.cancel = Mock()
        self.close = Mock()

    def complete(self, packet, cancel, on_speech=None):
        self.packets.append(copy.deepcopy(packet))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class StreamingFakeModel(FakeModel):
    """Streams each response's speech in pieces before returning it, as the CLI connection does."""

    def __init__(self, *responses, pieces=()):
        super().__init__(*responses)
        self.pieces = pieces  # (pieces of the first response's speech), (of the second), ...
        self.calls = 0

    def complete(self, packet, cancel, on_speech=None):
        for piece in self.pieces[self.calls] if self.calls < len(self.pieces) else ():
            on_speech(piece)
        self.calls += 1
        return super().complete(packet, cancel)


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.agent = SimpleNamespace(cfg=self.cfg.brain, provider="claude", session_id="typed-session", workdir="/tmp", ask=Mock())
        self.voice = VoiceConversation(self.cfg, self.agent)
        self.context = patch("vision.conversation.local_handoff", return_value="Earlier typed conversation")
        self.context.start()
        self.addCleanup(self.context.stop)
        self.memory = patch("vision.memory.facts", return_value=[])
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def test_small_talk_has_no_worker_and_only_speech_reaches_callbacks(self):
        self.voice.model = FakeModel(dict(speech="Yeah, I'm here. What's on your mind?", task=None))
        spoken, statuses = [], []
        with patch("vision.conversation.create_brain") as create:
            turn = self.voice.ask("You there?", on_text=spoken.append, on_status=statuses.append)
        create.assert_not_called()
        self.agent.ask.assert_not_called()
        self.assertEqual(spoken, [turn.text])
        self.assertEqual(statuses, [])
        self.assertEqual(turn.session_id, "typed-session")
        self.assertEqual(self.voice.model.packets[0]["coding_context"], "Earlier typed conversation")

    def test_streamed_speech_is_spoken_once_as_it_arrives(self):
        self.voice.model = StreamingFakeModel(
            dict(speech="  I'll check that.", task=TASK), dict(speech="Sorted. The parser tests pass now.", task=None),
            pieces=(("  I'll ", "check"), ("Sorted. The parser", " tests pass now.")))
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        spoken = []
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it", on_text=spoken.append)
        # The stream's leading whitespace is dropped, the rest of the acknowledgement follows what was
        # streamed, and the second response starts a new paragraph without repeating anything.
        self.assertEqual(spoken, ["I'll ", "check", " that.", "\n\nSorted. The parser", " tests pass now."])
        self.assertEqual(turn.text, "I'll check that.\n\nSorted. The parser tests pass now.")
        self.assertEqual(turn.usage, FakeModel.usage)

    def test_streamed_speech_differing_from_the_reply_is_not_said_twice(self):
        self.voice.model = StreamingFakeModel(dict(speech="Different words.", task=None), pieces=(("Something ", "else."),))
        spoken = []
        with patch("vision.conversation.create_brain"):
            turn = self.voice.ask("Hi", on_text=spoken.append)
        self.assertEqual("".join(spoken), "Something else.")
        self.assertEqual(turn.text, "Something else.")
        self.assertFalse(turn.is_error)

    def test_cancel_during_the_stream_stops_the_speech(self):
        self.voice.model = StreamingFakeModel(dict(speech="One two three.", task=None), pieces=(("One ", "two ", "three."),))
        spoken = []

        def on_text(delta):
            spoken.append(delta)
            self.voice.cancel()

        turn = self.voice.ask("Hi", on_text=on_text)
        self.assertEqual(spoken, ["One "])
        self.assertEqual(turn.error, "cancelled")

    def test_delegation_is_structured_silent_and_narrated_by_conversation(self):
        self.voice.model = FakeModel(dict(speech="I'll check that.", task=TASK), dict(speech="Sorted. The parser tests pass now.", task=None))
        worker = Mock()
        worker.ask.return_value = Turn(text="PRIVATE WORKER CHATTER", data=RESULT)
        on_question, on_agent, on_tool = Mock(), Mock(), Mock()
        states = []  # (kind, done) as each on_agent call saw the row (the same object is mutated as it goes)
        on_agent.side_effect = lambda r: states.append((r.kind, r.done))
        spoken = []
        with patch("vision.conversation.create_brain", return_value=worker) as create:
            turn = self.voice.ask("Fix it", on_text=spoken.append, on_question=on_question, on_agent=on_agent, on_tool=on_tool)
        create.assert_called_once_with(self.cfg.brain, voice_mode=False)
        self.assertTrue(worker.task_mode)
        self.assertEqual(json.loads(worker.ask.call_args.args[0]), {"type": "vision_task", "task": TASK})
        self.assertIs(worker.ask.call_args.kwargs["on_question"], on_question)
        runs = [c.args[0] for c in on_agent.call_args_list]  # the worker's own row: started, then done
        self.assertEqual(states, [("agent", False), ("agent", True)])
        self.assertIs(runs[0], runs[1])
        self.assertEqual(runs[0].label, "Fix the failing check")
        on_tool.assert_not_called()  # the worker's calls belong to its row, never the reply's tool line
        self.assertEqual("".join(spoken), "I'll check that.\n\nSorted. The parser tests pass now.")
        self.assertNotIn("PRIVATE", turn.text)
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-1], {"worker_result": RESULT})
        self.agent.ask.assert_not_called()

    def test_worker_tool_calls_are_steps_on_its_row_not_the_replys_tools(self):
        self.voice.model = FakeModel(dict(speech="On it.", task=TASK), dict(speech="Done.", task=None))
        worker = Mock()

        def ask(prompt, on_question=None, on_tool=None):
            on_tool(ToolCall("t1", "Bash"))
            on_tool(ToolCall("t1", "Bash", detail="cd ~/code/project && ls"))
            on_tool(ToolCall("t1", "Bash", detail="cd ~/code/project && ls", done=True))
            on_tool(ToolCall("t2", "Read", detail="stt.py"))
            return Turn(text="", data=RESULT)

        worker.ask.side_effect = ask
        on_agent, on_tool = Mock(), Mock()
        with patch("vision.conversation.create_brain", return_value=worker):
            self.voice.ask("Fix it", on_agent=on_agent, on_tool=on_tool)
        on_tool.assert_not_called()
        row = on_agent.call_args.args[0]
        self.assertEqual(row.steps, [("Bash", "cd ~/code/project && ls"), ("Read", "stt.py")])

    def test_named_model_and_effort_get_their_own_worker(self):
        task = {**TASK, "model": "Haiku", "effort": "LOW"}
        self.voice.model = FakeModel(dict(speech="On it.", task=task), dict(speech="Done.", task=None))
        worker = Mock()
        worker.cfg = SimpleNamespace(model="haiku", effort="")
        worker.output_tokens = 42
        worker.ask.return_value = Turn(text="", data=RESULT, usage={"input_tokens": 1000, "cache_read_input_tokens": 200, "output_tokens": 50}, tools_used=["Bash", "Read"])
        runs = []
        with patch("vision.conversation.create_brain", return_value=worker) as create:
            turn = self.voice.ask("get haiku to fix it, low effort", on_agent=runs.append)
        self.assertFalse(turn.is_error)
        cfg = create.call_args.args[0]
        self.assertEqual((cfg.model, cfg.effort), ("haiku", "low"))
        self.assertIsNot(cfg, self.cfg.brain)
        self.assertEqual(self.cfg.brain.model, "opus")  # the session's brain is untouched
        self.assertEqual(json.loads(worker.ask.call_args.args[0])["task"], TASK)  # the brain choice is not the worker's business
        run = runs[-1]
        self.assertTrue(run.done)
        self.assertEqual((run.model, run.effort, run.tool_uses), ("haiku", "", 2))
        self.assertEqual(run.tokens_fn(), 42)
        self.assertEqual(run.status, "0.0s · ↑1,200 ↓50")  # agent · model · effort · description · time · tokens
        self.assertGreaterEqual(run.started, 0)

    def test_unknown_model_fails_the_task_without_running_it(self):
        self.voice.model = FakeModel(dict(speech="Sure.", task={**TASK, "model": "gpt-9-ultra"}))
        with patch("vision.conversation.create_brain") as create:
            turn = self.voice.ask("use gpt-9-ultra")
        create.assert_not_called()
        self.assertTrue(turn.is_error)

    def test_task_model_and_effort_are_normalised(self):
        self.assertEqual(validate_task({**TASK, "model": "Grok", "effort": "Max"})["model"], "grok-4.6")
        self.assertEqual(validate_task({**TASK, "model": "grok", "effort": "Max"})["effort"], "max")
        self.assertEqual(validate_task({**TASK, "model": None, "effort": None}), {**TASK, "model": None, "effort": None})
        self.assertEqual(validate_task(dict(TASK)), TASK)
        with self.assertRaises(ValueError):
            validate_task({**TASK, "effort": "turbo"})
        with self.assertRaises(ValueError):
            validate_task({**TASK, "model": 7})

    def test_invalid_task_does_not_speak_claims_or_execute(self):
        self.voice.model = FakeModel(dict(speech="I've deleted it.", task={**TASK, "shell": "rm -rf"}))
        with patch("vision.conversation.create_brain") as create:
            turn = self.voice.ask("hello")
        create.assert_not_called()
        self.assertTrue(turn.is_error)
        self.assertNotIn("deleted", turn.text)

    def test_worker_failure_is_returned_to_model_for_honest_narration(self):
        self.voice.model = FakeModel(dict(speech="", task=TASK), dict(speech="The worker couldn't finish that.", task=None))
        worker = Mock()
        worker.ask.side_effect = RuntimeError("quota exceeded")
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        result = self.voice.model.packets[1]["turn"]["events"][-1]["worker_result"]
        self.assertEqual((result["status"], result["summary"]), ("failed", "quota exceeded"))
        self.assertNotIn("quota exceeded", turn.text)
        self.assertFalse(self.voice.model.packets[1]["delegation_allowed"])

    def test_duplicate_task_does_not_run_twice(self):
        self.voice.model = FakeModel(dict(speech="", task=TASK), dict(speech="I'll do it again.", task=TASK))
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        worker.ask.assert_called_once()
        self.assertIn("repeat", turn.error)
        self.assertNotIn("again", turn.text)

    def test_delegation_limit_does_not_execute_or_speak_an_extra_task(self):
        self.cfg.conversation.max_delegations = 1
        self.voice.model = FakeModel(dict(speech="", task=TASK), dict(speech="I'll do more.", task=TASK))
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        worker.ask.assert_called_once()
        self.assertTrue(turn.is_error)
        self.assertNotIn("do more", turn.text)
        self.assertFalse(self.voice.model.packets[1]["delegation_allowed"])

    def test_a_list_of_tasks_starts_one_agent_each_side_by_side(self):
        other = dict(TASK, objective="Find the largest file")
        self.voice.model = FakeModel(dict(speech="Two agents on it.", task=[TASK, other]),
                                     dict(speech="One's back.", task=None), dict(speech="Both done.", task=None))
        together = threading.Barrier(2, timeout=5)  # neither finishes unless both are at work at once
        objectives, constraints = [], {}

        def worker():
            w = Mock()

            def ask(prompt, on_question=None, on_tool=None):
                sent = json.loads(prompt)["task"]
                objectives.append(sent["objective"])
                constraints[sent["objective"]] = " ".join(sent["constraints"])
                together.wait()
                return Turn(data=RESULT)

            w.ask.side_effect = ask
            return w

        rows = []
        with patch("vision.conversation.create_brain", side_effect=lambda *a, **k: worker()):
            turn = self.voice.ask("Launch two agents", on_agent=lambda r: rows.append((r.id, r.label)))
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(sorted(objectives), ["Find the largest file", "Fix the failing check"])
        self.assertEqual({label for _, label in rows}, {"Fix the failing check", "Find the largest file"})
        self.assertEqual(len({rid for rid, _ in rows}), 2)  # a row each
        self.assertFalse(together.broken)  # both were at work at once
        results = [e["worker_result"] for e in self.voice.model.packets[-1]["turn"]["events"] if "worker_result" in e]
        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        # Each knew of the other from the start: neither was listening yet to be told.
        self.assertIn("Find the largest file", constraints["Fix the failing check"])
        self.assertIn("Fix the failing check", constraints["Find the largest file"])
        self.assertEqual(self.voice.model.packets[0]["agents_left"], 3)
        self.assertEqual(turn.text, "Two agents on it.\n\nOne's back.\n\nBoth done.")

    def test_tasks_past_the_limit_do_not_start_and_the_model_hears_at_once(self):
        self.cfg.conversation.max_delegations = 2
        b, c = dict(TASK, objective="Second job"), dict(TASK, objective="Third job")
        self.voice.model = FakeModel(dict(speech="Three on it.", task=[TASK, b, c]), dict(speech="Actually two, the limit's two.", task=None),
                                     dict(speech="One back.", task=None), dict(speech="Both done.", task=None))
        objectives = []
        worker = Mock()
        worker.ask.side_effect = lambda prompt, **_: objectives.append(json.loads(prompt)["task"]["objective"]) or Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Do all three")
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(sorted(objectives), ["Fix the failing check", "Second job"])
        events = self.voice.model.packets[1]["turn"]["events"]  # the very next call, before any result
        self.assertFalse(any("worker_result" in e for e in events))
        not_started = [e["not_started"] for e in events if "not_started" in e]
        self.assertEqual(len(not_started), 1)
        self.assertIn("Third job", not_started[0][0])
        self.assertIn("Actually two", turn.text)

    def test_the_same_task_twice_in_one_list_starts_once(self):
        self.voice.model = FakeModel(dict(speech="Two on it.", task=[TASK, dict(TASK)]), dict(speech="Just the one, actually.", task=None),
                                     dict(speech="Done.", task=None))
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        self.assertFalse(turn.is_error, turn.error)
        worker.ask.assert_called_once()
        self.assertIn({"not_started": ["Fix the failing check: listed twice, so it started once"]},
                      self.voice.model.packets[1]["turn"]["events"])

    def test_the_voice_models_scratch_folder_is_never_where_a_worker_looks(self):
        self.agent.workdir = "/home/u/project"
        scratch = f"{tempfile.gettempdir()}/vision-conversation-1ooear9z"
        task = dict(TASK, context=f"Working directory is {scratch}.", constraints=[f"Search {scratch}/src only"])
        self.voice.model = FakeModel(dict(speech="On it.", task=task), dict(speech="Done.", task=None))
        self.voice.model.scratch_dirs = lambda: [scratch]
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            self.voice.ask("Find the biggest file")
        sent = json.loads(worker.ask.call_args.args[0])["task"]
        self.assertEqual(sent["context"], "Working directory is /home/u/project.")
        self.assertEqual(sent["constraints"], ["Search /home/u/project/src only"])
        self.assertEqual(self.voice.model.packets[0]["workspace"], "/home/u/project")

    def test_only_the_live_scratch_folder_is_swapped_in_every_spelling(self):
        live = ["/var/folders/zz/T/vision-conversation-k2j3h4g5", "/private/var/folders/zz/T/vision-conversation-k2j3h4g5"]
        task = dict(TASK, context="Working directory is /private/var/folders/zz/T/vision-conversation-k2j3h4g5.",
                    constraints=["Delete /var/folders/zz/T/vision-conversation-old11111, a leftover",
                                 "Not /var/folders/zz/T/vision-conversation-k2j3h4g5x or /var/folders/zz/T/vision-conversation-k2j3h4g5.bak"])
        sent = in_workspace(task, "/Users/me/project", live)
        self.assertEqual(sent["context"], "Working directory is /Users/me/project.")
        self.assertEqual(sent["constraints"], task["constraints"])  # another folder, and longer names, stay as written
        self.assertIs(in_workspace(task, "/Users/me/project", []), task)
        lines = dict(TASK, context="Working directory:\n/var/folders/zz/T/vision-conversation-k2j3h4g5\tsrc")
        self.assertEqual(in_workspace(lines, "/Users/me/project", live)["context"], "Working directory:\n/Users/me/project\tsrc")

    def test_two_tasks_differing_only_in_brain_are_two_agents(self):
        self.voice.model = FakeModel(dict(speech="Opus and Haiku on it.", task=[dict(TASK, model="opus"), dict(TASK, model="haiku")]),
                                     dict(speech="One back.", task=None), dict(speech="Both back.", task=None))
        brains, constraints = [], []

        def worker(cfg, **_):
            w = Mock()
            w.cfg = SimpleNamespace(model=cfg.model, effort="")

            def ask(prompt, on_question=None, on_tool=None):
                brains.append(cfg.model)
                constraints.append(" ".join(json.loads(prompt)["task"]["constraints"]))
                return Turn(data=RESULT)

            w.ask.side_effect = ask
            return w

        rows = {}
        with patch("vision.conversation.create_brain", side_effect=worker):
            turn = self.voice.ask("Get Opus and Haiku both on it", on_agent=lambda r: rows.__setitem__(r.id, r.done))
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(sorted(brains), ["haiku", "opus"])
        self.assertEqual(list(rows.values()), [True, True])  # no row left spinning
        self.assertTrue(any("Fix the failing check (Haiku" in c for c in constraints))

    def test_a_task_list_is_validated_task_by_task(self):
        self.assertIsNone(validate_response(dict(speech="Hi.", task=[]))["task"])
        self.assertEqual(validate_response(dict(speech="", task=[TASK]))["task"], [TASK])
        with self.assertRaises(BrainError):
            validate_response(dict(speech="", task=[TASK, {**TASK, "shell": "rm -rf"}]))
        with self.assertRaises(BrainError):
            validate_response(dict(speech="", task=[TASK], search={"query": "news"}))

    def test_cancel_after_acknowledgement_prevents_dispatch(self):
        self.voice.model = FakeModel(dict(speech="I'll check.", task=TASK))
        with patch("vision.conversation.create_brain") as create:
            turn = self.voice.ask("Fix it", on_text=lambda _: self.voice.cancel())
        self.assertEqual(turn.error, "cancelled")
        create.assert_not_called()

    def test_cancel_while_worker_runs_prevents_narration(self):
        self.voice.model = FakeModel(dict(speech="", task=TASK))
        worker = Mock()
        def cancelled(*args, **kwargs):
            self.voice.cancel()
            return Turn(is_error=True, error="cancelled")
        worker.ask.side_effect = cancelled
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        worker.cancel.assert_called_once()
        self.assertEqual(turn.error, "cancelled")
        self.assertEqual(turn.text, "")
        self.assertEqual(len(self.voice.model.packets), 1)

    def test_new_session_resets_private_history_and_worker(self):
        self.voice.history.append({"user": "old"})
        self.voice._worker = Mock()
        self.voice.new_session()
        self.assertEqual(self.voice.history, [])
        self.assertIsNone(self.voice._worker)
        self.assertEqual(self.agent.session_id, "typed-session")

    def test_worker_changes_with_selected_provider(self):
        with patch("vision.conversation.create_brain", side_effect=[Mock(), Mock()]) as create:
            first = self.voice._task_worker()
            self.assertIs(self.voice._task_worker(), first)
            self.agent.provider = "codex"
            self.cfg.brain.model = "gpt-6-astra"
            self.assertIsNot(self.voice._task_worker(), first)
        self.assertEqual(create.call_count, 2)

    def test_named_worker_is_reused_while_asked_for(self):
        with patch("vision.conversation.create_brain", side_effect=[Mock(), Mock(), Mock()]) as create:
            default = self.voice._task_worker()
            named = self.voice._task_worker("haiku")
            self.assertIsNot(named, default)
            self.assertIs(self.voice._task_worker("haiku"), named)
            self.assertIsNot(self.voice._task_worker(), named)  # back to the session's brain: a fresh worker
        self.assertEqual(create.call_count, 3)
        self.assertEqual([c.args[0].model for c in create.call_args_list], ["opus", "haiku", "opus"])

    def test_a_message_sent_in_while_it_answers_is_answered_in_the_same_turn(self):
        sent = []

        class Model(FakeModel):
            def complete(model, packet, cancel, on_speech=None):
                if not sent:
                    sent.append(self.voice.steer("Actually, make it Tuesday."))
                return FakeModel.complete(model, packet, cancel)

        self.voice.model = Model(dict(speech="Monday works.", task=None), dict(speech="Tuesday it is.", task=None))
        turn = self.voice.ask("Book it for Monday")
        self.assertEqual(sent, [True])
        self.assertEqual(turn.text, "Monday works.\n\nTuesday it is.")
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-1],
                         {"user_interjection": {"text": "Actually, make it Tuesday.", "agent_heard": False}})
        self.assertFalse(self.voice.steer("too late"))  # the turn is over: the caller queues it

    def test_a_message_sent_in_while_the_worker_runs_reaches_it_and_comes_before_its_result(self):
        self.voice.model = FakeModel(dict(speech="On it.", task=TASK), dict(speech="Done, tests skipped.", task=None))
        worker = Mock()
        worker.steer.return_value = True
        sent = []

        def ask(prompt, on_question=None, on_tool=None):
            sent.append(self.voice.steer("Skip the tests."))
            return Turn(data=RESULT)

        worker.ask.side_effect = ask
        with patch("vision.conversation.create_brain", return_value=worker):
            self.voice.ask("Fix it")
        self.assertEqual(sent, [True])
        self.assertEqual(json.loads(worker.steer.call_args.args[0]), {"type": "vision_task_update", "message": "Skip the tests."})
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-2:],
                         [{"user_interjection": {"text": "Skip the tests.", "agent_heard": True}}, {"worker_result": RESULT}])

    def test_a_message_sent_in_while_the_worker_runs_is_answered_before_it_finishes(self):
        answered = threading.Event()

        class Model(FakeModel):
            def complete(model, packet, cancel, on_speech=None):
                if any("agent_running" in e for e in packet["turn"]["events"]) and not answered.is_set():
                    self.assertTrue(packet["delegation_allowed"])  # it may start more work alongside
                    answered.set()
                return FakeModel.complete(model, packet, cancel)

        self.voice.model = Model(dict(speech="On it.", task=TASK), dict(speech="Because Sol was picked.", task=None),
                                 dict(speech="Done.", task=None))
        worker = Mock()
        worker.steer.return_value = True

        def ask(prompt, on_question=None, on_tool=None):
            self.voice.steer("Why did I switch?")
            self.assertTrue(answered.wait(5))  # the aside is spoken while the agent is still working
            return Turn(data=RESULT)

        worker.ask.side_effect = ask
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        self.assertEqual(turn.text, "On it.\n\nBecause Sol was picked.\n\nDone.")
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-2:],
                         [{"user_interjection": {"text": "Why did I switch?", "agent_heard": True}},
                          {"agent_running": "Fix the failing check"}])
        self.assertEqual(self.voice.model.packets[2]["turn"]["events"][-1], {"worker_result": RESULT})

    def test_a_second_agent_launched_mid_run_works_alongside_the_first(self):
        other = dict(TASK, objective="Polish the chat list")
        first_going, narrated = threading.Event(), threading.Event()
        order = []

        class Model(FakeModel):
            def complete(model, packet, cancel, on_speech=None):
                if len(model.packets) == 1:  # the first agent is at work
                    self.assertTrue(first_going.wait(5))
                if len(model.packets) == 2:  # the second agent's result, told with the first still at work
                    narrated.set()
                return FakeModel.complete(model, packet, cancel)

        self.voice.model = Model(dict(speech="On it.", task=TASK), dict(speech="Starting another.", task=other),
                                 dict(speech="The list is done.", task=None), dict(speech="And the check.", task=None))

        steers, seen_constraints = [], []

        def worker():
            w = Mock()
            w.steer.side_effect = lambda text: steers.append(Mock(args=(text,))) or True

            def ask(prompt, on_question=None, on_tool=None):
                objective = json.loads(prompt)["task"]["objective"]
                seen_constraints.append(" ".join(json.loads(prompt)["task"]["constraints"]))
                if objective == TASK["objective"]:
                    self.voice.steer("Get another agent on the chat list.")
                    first_going.set()
                    self.assertTrue(narrated.wait(5))  # still running when the second's result is told
                    order.append("first")
                else:
                    order.append("second")
                return Turn(data=RESULT)

            w.ask.side_effect = ask
            return w

        rows = []
        with patch("vision.conversation.create_brain", side_effect=lambda *a, **k: worker()):
            turn = self.voice.ask("Fix it", on_agent=lambda r: rows.append(r.id))
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(order, ["second", "first"])
        prompts = [json.loads(c.args[0]) for c in steers]
        self.assertIn({"type": "vision_peer_started", "objective": "Polish the chat list"}, prompts)  # the first hears of the second
        self.assertIn("Fix the failing check", seen_constraints[-1])  # and the second knows the first is at work
        self.assertEqual(len(set(rows)), 2)  # two agent rows
        self.assertEqual(turn.text, "On it.\n\nStarting another.\n\nThe list is done.\n\nAnd the check.")
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-2:],
                         [{"user_interjection": {"text": "Get another agent on the chat list.", "agent_heard": True}},
                          {"agent_running": "Fix the failing check"}])
        self.assertEqual(self.voice.model.packets[2]["turn"]["events"][-2:],
                         [{"worker_result": RESULT}, {"agent_running": "Fix the failing check"}])

    def test_a_question_from_one_agent_is_answered_while_another_works(self):
        other = dict(TASK, objective="Polish the chat list")
        asked, answered = threading.Event(), threading.Event()
        NEEDS = dict(RESULT, status="needs_input", summary="Which colour?", question="Which colour?")

        self.voice.model = Model = FakeModel(dict(speech="On it.", task=TASK), dict(speech="Another.", task=other),
                                             dict(speech="Blue, done.", task=None), dict(speech="All sorted.", task=None))
        sent = []

        def worker():
            w = Mock()
            w.steer.return_value = True

            def ask(prompt, on_question=None, on_tool=None):
                msg = json.loads(prompt)
                if msg["type"] == "vision_task_answer":
                    sent.append(msg["answer"])
                    answered.set()
                    return Turn(data=RESULT)
                if msg["task"]["objective"] == TASK["objective"]:
                    self.voice.steer("And polish the chat list.")
                    self.assertTrue(asked.wait(5))  # the question has been put to the user
                    self.voice.steer("Blue.")  # the answer to the other agent's question
                    self.assertTrue(answered.wait(5))
                    return Turn(data=RESULT)
                return Turn(data=NEEDS)

            w.ask.side_effect = ask
            return w

        spoken = []

        def said(delta):
            spoken.append(delta)
            if "Which colour?" in delta:
                asked.set()

        with patch("vision.conversation.create_brain", side_effect=lambda *a, **k: worker()):
            turn = self.voice.ask("Fix it", on_text=said)
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(sent, ["Blue."])
        self.assertIn("Which colour?", "".join(spoken))
        events = self.voice.history[-1]["events"]
        self.assertIn({"answer_relayed": {"run_id": events[[i for i, e in enumerate(events) if "answer_relayed" in e][0]]["answer_relayed"]["run_id"], "answer": "Blue."}}, events)
        self.assertNotIn({"user_interjection": {"text": "Blue.", "agent_heard": False}}, events)

    def test_cancel_stops_every_agent_at_work(self):
        other = dict(TASK, objective="Polish the chat list")
        both = threading.Barrier(2, timeout=5)
        workers = []
        self.voice.model = FakeModel(dict(speech="On it.", task=TASK), dict(speech="Another.", task=other))

        def worker():
            w = Mock()
            w.steer.return_value = True
            stopped = threading.Event()
            w.cancel.side_effect = stopped.set

            def ask(prompt, on_question=None, on_tool=None):
                if json.loads(prompt)["task"]["objective"] == TASK["objective"]:
                    self.voice.steer("Another agent on the list, please.")
                    both.wait()
                    self.voice.cancel()
                else:
                    both.wait()
                self.assertTrue(stopped.wait(5))
                return Turn(is_error=True, error="cancelled")

            w.ask.side_effect = ask
            workers.append(w)
            return w

        with patch("vision.conversation.create_brain", side_effect=lambda *a, **k: worker()):
            turn = self.voice.ask("Fix it")
        self.assertEqual(turn.error, "cancelled")
        self.assertEqual(len(workers), 2)
        for w in workers:
            w.cancel.assert_called_once()

    def test_a_task_asked_for_just_before_a_message_came_in_waits_for_the_model_to_ask_again(self):
        sent = []

        class Model(FakeModel):
            def complete(model, packet, cancel, on_speech=None):
                if not sent:
                    sent.append(self.voice.steer("Only the parser, mind."))
                return FakeModel.complete(model, packet, cancel)

        narrow = dict(TASK, constraints=["Only touch the parser"])
        self.voice.model = Model(dict(speech="I'll check.", task=TASK), dict(speech="Just the parser.", task=narrow),
                                 dict(speech="Sorted.", task=None))
        worker = Mock()
        worker.ask.return_value = Turn(data=RESULT)
        with patch("vision.conversation.create_brain", return_value=worker):
            turn = self.voice.ask("Fix it")
        self.assertFalse(turn.is_error)
        worker.ask.assert_called_once()
        self.assertEqual(json.loads(worker.ask.call_args.args[0])["task"], narrow)
        self.assertEqual(self.voice.model.packets[1]["turn"]["events"][-2:],
                         [{"task_not_started": "the user sent a message before it began"},
                          {"user_interjection": {"text": "Only the parser, mind.", "agent_heard": False}}])

    def test_a_turn_takes_every_message_sent_in(self):
        results = []

        class Model(FakeModel):
            def complete(model, packet, cancel, on_speech=None):
                if not results:
                    results.extend(self.voice.steer(f"note {i}") for i in range(10))
                return FakeModel.complete(model, packet, cancel)

        self.voice.model = Model(dict(speech="Right.", task=None), dict(speech="Noted.", task=None))
        self.voice.ask("Hi")
        self.assertEqual(results, [True] * 10)  # no cap: none is left queued behind the turn
        self.assertEqual(len(self.voice.model.packets), 2)  # all heard together, answered once

    def test_nothing_is_taken_between_turns_or_after_a_cancel(self):
        self.assertFalse(self.voice.steer("nothing running"))
        self.voice.model = FakeModel(dict(speech="One.", task=None))
        sent = []

        def on_text(_):
            self.voice.cancel()
            sent.append(self.voice.steer("after the cancel"))

        self.voice.ask("Hi", on_text=on_text)
        self.assertEqual(sent, [False])


class ProjectCallTests(unittest.TestCase):
    """A call in a project's chat: the conversation model sees the project as data, and its tasks become
    threads (started, or told by name) that outlive the turn, which doesn't wait on them."""

    def setUp(self):
        self.cfg = Config()
        self.agent = SimpleNamespace(cfg=self.cfg.brain, provider="claude", session_id="s", workdir="/tmp", ask=Mock())
        self.voice = VoiceConversation(self.cfg, self.agent)
        for p in (patch("vision.conversation.local_handoff", return_value=""), patch("vision.memory.facts", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.started, self.told, self.closed = [], [], []
        threads = [{"id": "a1b2c3", "title": "Fix the tests", "state": "working", "latest": ""},
                   {"id": "d4e5f6", "title": "API docs", "state": "waiting on you", "latest": "Which style?"},
                   {"id": "778899", "title": "Fix the build", "state": "idle", "latest": ""}]
        self.voice.project = {
            "id": "p1", "title": "Calendar", "goal": "Add a calendar", "status": "<vision-project>…</vision-project>",
            "threads": threads,
            "start": lambda body, title: self.started.append((body, title)) or {"id": "n1", "title": title},
            "tell": lambda tid, text: self.told.append((tid, text)) or {"id": tid},
            "close": lambda tid: self.closed.append(tid) or {"id": tid}}

    def test_status_is_answered_from_the_packet(self):
        self.voice.model = FakeModel(dict(speech="Tests are still going, the docs one wants you.", task=None))
        self.voice.ask("how's it going?")
        packet = self.voice.model.packets[0]
        self.assertEqual(packet["project"]["title"], "Calendar")
        self.assertEqual(packet["project"]["threads"][1]["latest"], "Which style?")
        self.assertNotIn("start", packet["project"])  # data only: never the callables

    def test_a_new_task_starts_a_thread_and_the_turn_does_not_wait(self):
        self.voice.model = FakeModel(dict(speech="On it, starting a thread.", task=dict(TASK, thread=None)))
        with patch("vision.conversation.create_brain") as create:
            turn = self.voice.ask("fix the failing check in the background")
        create.assert_not_called()  # no agent of the turn's own
        self.assertEqual(len(self.started), 1)
        body, title = self.started[0]
        self.assertTrue(body.startswith("Fix the failing check"))
        self.assertIn("Done when: The check passes", body)
        self.assertEqual(title, "Fix the failing check")
        self.assertEqual(turn.text, "On it, starting a thread.")
        self.assertEqual(len(self.voice.model.packets), 1)  # one response: the turn ended there
        self.assertEqual(self.voice.history[-1]["events"][-1]["project_threads"][0]["started"], "Fix the failing check")

    def test_a_follow_up_goes_to_the_thread_named_and_close_closes_one(self):
        self.voice.model = FakeModel(dict(speech="Told it.", task=dict(TASK, objective="Skip the flaky one", thread="the tests thread"),
                                          close=["API docs"]))
        self.voice.ask("tell the tests thread to skip the flaky one, and close the docs one")
        self.assertEqual(self.told, [("a1b2c3", self.told[0][1])])
        self.assertTrue(self.told[0][1].startswith("Skip the flaky one"))
        self.assertEqual(self.closed, ["d4e5f6"])
        self.assertEqual(self.started, [])

    def test_an_ambiguous_name_goes_back_to_the_model_to_ask(self):
        self.voice.model = FakeModel(dict(speech="Telling it.", task=dict(TASK, thread="fix")),
                                     dict(speech="Which one, the tests or the build?", task=None))
        turn = self.voice.ask("tell the fix thread to stop")
        self.assertEqual(self.told, [])
        events = self.voice.model.packets[1]["turn"]["events"]
        self.assertIn("fits several threads", json.dumps(events))
        self.assertTrue(turn.text.endswith("Which one, the tests or the build?"))

    def test_find_thread(self):
        from vision.conversation import find_thread

        threads = self.voice.project["threads"] + [{"id": "aa0000", "title": "Old tests", "state": "resolved"}]
        self.assertEqual(find_thread(threads, "a1b2c3")[0]["title"], "Fix the tests")
        self.assertEqual(find_thread(threads, "d4e")[0]["title"], "API docs")  # the start of an id
        self.assertEqual(find_thread(threads, "api docs")[0]["id"], "d4e5f6")
        self.assertEqual(find_thread(threads, "the build thread")[0]["id"], "778899")
        self.assertEqual(find_thread(threads, "tests")[0]["id"], "a1b2c3")  # the closed one gives way to the live one
        self.assertIsNone(find_thread(threads, "fix")[0])
        self.assertIn("no thread called", find_thread(threads, "deploy")[1])

    def test_thread_and_close_are_checked_and_kept_beside_the_task(self):
        data = validate_response(dict(speech="ok", task=[dict(TASK, thread="a1"), dict(TASK, objective="Other", thread=None)],
                                      close=["b2"]))
        self.assertEqual(data["threads"], ["a1", None])
        self.assertNotIn("thread", data["task"][0])  # never handed on to a worker
        self.assertEqual(data["close"], ["b2"])
        self.assertEqual(validate_response(data)["threads"], ["a1", None])  # checking it again changes nothing
        with self.assertRaises(BrainError):
            validate_response(dict(speech="ok", task=dict(TASK, thread=5)))
        with self.assertRaises(BrainError):
            validate_response(dict(speech="ok", task=None, close="b2"))
        self.assertEqual(validate_response(dict(speech="", task=None, close=["b2"]))["close"], ["b2"])


class VoiceModelTests(unittest.TestCase):
    """The voice model: the chat's own pick when it is a Claude, Codex or Local model; [conversation].model
    (set only in the config) for chats on Grok."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.brain.model = "grok-4.6"  # a Grok chat: the voice is the config's fallback
        self.agent = SimpleNamespace(cfg=self.cfg.brain, provider="grok", session_id="s", workdir="/tmp", ask=Mock())
        self.voice = VoiceConversation(self.cfg, self.agent)
        self.voice.history.append({"id": "1", "user": "hello", "events": []})

    def test_claude_to_local_closes_the_old_model_and_turns_thinking_off(self):
        from vision.local import LocalConversation

        old = self.voice.model = Mock()
        self.cfg.brain.model = "local-model"
        self.voice.follow()
        old.close.assert_called_once()
        self.assertIsInstance(self.voice.model, LocalConversation)
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("local-model", "off"))
        self.assertEqual(len(self.voice.history), 1)  # the transcript carries over; every turn resends it

    def test_a_grok_chat_talks_through_the_fallback(self):
        self.assertIsInstance(self.voice.model, ClaudeConversation)
        self.assertEqual(self.cfg.conversation.model, "sonnet")

    def test_a_codex_chat_talks_through_its_own_model(self):
        from vision.codex_voice import CodexConversation

        self.cfg.brain.model = "gpt-5.6-luna"
        self.voice.follow()
        self.assertIsInstance(self.voice.model, CodexConversation)
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("gpt-5.6-luna", "low"))
        self.assertEqual(len(self.voice.history), 1)

    def test_a_claude_or_local_chat_talks_through_its_own_model(self):
        self.cfg.brain.model = "opus"
        self.voice.follow()
        self.assertIsInstance(self.voice.model, ClaudeConversation)
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("opus", "low"))
        self.cfg.brain.model = "local-model"
        self.voice.follow()
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("local-model", "off"))
        self.cfg.brain.model = "grok-4.6"  # can't talk: back to the fallback, at a Claude effort again
        self.voice.follow()
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("sonnet", "low"))
        self.assertEqual(len(self.voice.history), 1)

    def test_haiku_talks_with_no_effort_and_sonnet_gets_low_back(self):
        self.cfg.brain.model = "haiku"
        self.voice.follow()
        self.assertEqual((self.cfg.conversation.model, self.cfg.conversation.effort), ("haiku", ""))
        with patch("vision.conversation.find_claude", return_value="claude"):  # the command is built, not run
            self.assertNotIn("--effort", self.voice.model._command())
        self.cfg.brain.model = "sonnet"
        self.voice.follow()
        self.assertEqual(self.cfg.conversation.effort, "low")

    def test_follow_leaves_a_matching_model_running(self):
        old = self.voice.model = Mock()
        self.voice.follow()
        old.close.assert_not_called()

    def test_a_new_chat_starts_on_its_own_model(self):
        cfg = Config()
        cfg.brain.model = "haiku"
        voice = VoiceConversation(cfg, SimpleNamespace(cfg=cfg.brain, provider="claude", session_id=None, workdir="/tmp"))
        self.assertEqual((cfg.conversation.model, voice.fallback), ("haiku", "sonnet"))

    def test_turn_brain_follows_before_routing(self):
        from vision.cli import _turn_brain

        brain = SimpleNamespace(cfg=self.cfg.brain, provider="local")
        self.cfg.brain.model = "local-model"
        self.assertIs(_turn_brain(brain, self.voice, False, "typed in a call", talk=True), self.voice)
        self.assertEqual(self.cfg.conversation.model, "local-model")


class TransportTests(unittest.TestCase):
    def test_tools_and_customizations_are_disabled_and_no_agent_session_is_resumed(self):
        model = ClaudeConversation(Config())
        with patch("vision.conversation.find_claude", return_value="claude"):
            cmd = model._command()
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "")  # each web call is vetted (the weather guard)
        self.assertEqual(json.loads(cmd[cmd.index("--mcp-config") + 1]), {"mcpServers": {}})
        for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands", "--system-prompt"):
            self.assertIn(flag, cmd)
        for flag in ("--bare", "--resume", "--continue", "--dangerously-skip-permissions", "--append-system-prompt"):
            self.assertNotIn(flag, cmd)

    def test_web_off_leaves_the_voice_model_with_no_tools(self):
        cfg = Config()
        cfg.conversation.web = False
        with patch("vision.conversation.find_claude", return_value="claude"):
            cmd = ClaudeConversation(cfg)._command()
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "")
        self.assertIn("Your only tool is `weather`", cmd[cmd.index("--system-prompt") + 1])

    def model(self, mode="normal"):
        cfg = Config()
        model = ClaudeConversation(cfg)
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(tmp) / "packets.jsonl"
        script = """
import json, sys, time
count = 0
for line in sys.stdin:
    count += 1
    with open(sys.argv[1], 'a') as out:
        out.write(line)
    if sys.argv[2] == 'hang':
        time.sleep(30)
    if sys.argv[2] == 'crash':
        sys.exit(1)
    if sys.argv[2] == 'stderr':
        sys.stderr.write('noise' * 30000 + '\\n')
        sys.stderr.flush()
    print(json.dumps({'type':'assistant', 'message':{'content':[{'type':'text','text':'PRIVATE'}]}}), flush=True)
    if sys.argv[2] == 'stream':
        def ev(event):
            print(json.dumps({'type': 'stream_event', 'event': event}), flush=True)
        ev({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'WebSearch', 'input': {}}})
        ev({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"speech": "PRIVATE query"}'}})
        ev({'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}})
        ev({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'PRIVATE thoughts'}})
        ev({'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'name': 'StructuredOutput', 'input': {}}})
        ev({'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"speech": "rep'}})
        ev({'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': 'ly ' + str(count) + '", "task": null}'}})
    result = {'type':'result', 'result':'PRIVATE', 'structured_output':{'speech':f'reply {count}', 'task':None}}
    if sys.argv[2] == 'error':
        result = {'type':'result','is_error':True,'result':'Session limit reached'}
    if sys.argv[2] == 'invalid':
        result['structured_output'] = {'speech':'Do not speak this'}
    print(json.dumps(result), flush=True)
"""
        self.enterContext(patch.object(model, "_command", side_effect=lambda: [sys.executable, "-u", "-c", script, str(path), mode, cfg.conversation.model]))
        self.addCleanup(model.close)
        return model, path

    def packet(self, user="Hi", history=None, events=None):
        return {"history": history or [], "turn": {"user": user, "events": events or []},
                "memory": "Remember this", "coding_context": "Earlier typed work", "delegation_allowed": True}

    def test_connection_is_reused_and_only_new_context_is_sent(self):
        model, path = self.model()
        first = self.packet()
        result = model.complete(first, threading.Event())
        proc, cwd = model._proc, model._connection[1].name
        self.assertEqual(result, {"speech": "reply 1", "task": None})
        history = [{**first["turn"], "events": [{"assistant": result}]}]
        second = self.packet("Next", history)
        self.assertEqual(model.complete(second, threading.Event())["speech"], "reply 2")
        self.assertIs(model._proc, proc)
        sent = [json.loads(json.loads(line)["message"]["content"]) for line in path.read_text().splitlines()]
        self.assertEqual(sent[0], first)
        self.assertNotIn("history", sent[1])
        self.assertNotIn("memory", sent[1])
        self.assertEqual(sent[1]["turn"]["user"], "Next")
        model.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(Path(cwd).exists())

    def test_speech_streams_from_the_structured_reply_only(self):
        from vision.timing import VoiceTiming

        model, _ = self.model("stream")
        model.timing = VoiceTiming()
        pieces = []
        result = model.complete(self.packet(), threading.Event(), on_speech=pieces.append)
        self.assertEqual(pieces, ["rep", "ly 1"])
        self.assertEqual(result["speech"], "reply 1")
        self.assertIn("speech_stream", [e["stage"] for e in model.timing.events])
        self.assertEqual(model.complete(self.packet(), threading.Event())["speech"], "reply 2")  # no callback: fine

    def test_a_retried_structured_reply_is_not_spoken_twice(self):
        state: dict = {}

        def block(index, *parts):
            said = [ClaudeConversation._speech_piece({"type": "content_block_start", "index": index,
                                                       "content_block": {"type": "tool_use", "name": "StructuredOutput"}}, state)]
            for p in parts:
                said.append(ClaudeConversation._speech_piece({"type": "content_block_delta", "index": index,
                                                               "delta": {"type": "input_json_delta", "partial_json": p}}, state))
            return "".join(said)

        self.assertEqual(block(0, '{"speech": "Right, getting', ' Opus on it."'), "Right, getting Opus on it.")
        # The CLI rejected the first attempt; the model writes it again, then goes further.
        self.assertEqual(block(1, '{"speech": "Right, getting Opus', ' on it. Back soon."'), " Back soon.")
        # A retry that says something else is not said at all.
        self.assertEqual(block(2, '{"speech": "Something else entirely, and longer too."'), "")

    def test_warmup_starts_process_without_sending_a_model_request(self):
        model, path = self.model()
        model.warm_up()
        proc = model._proc
        self.assertIsNotNone(proc)
        self.assertFalse(path.exists())
        model.warm_up()
        model.complete(self.packet(), threading.Event())
        self.assertIs(model._proc, proc)
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_worker_continuation_only_sends_new_results(self):
        model, path = self.model()
        packet = self.packet()
        result = model.complete(packet, threading.Event())
        packet["turn"]["events"] = [{"assistant": result}, {"worker_result": RESULT}]
        model.complete(packet, threading.Event())
        sent = json.loads(json.loads(path.read_text().splitlines()[1])["message"]["content"])
        self.assertEqual(sent["turn"], {"events": [{"worker_result": RESULT}]})

    def test_changed_config_and_close_restart_with_full_context(self):
        model, path = self.model()
        packet = self.packet()
        model.complete(packet, threading.Event())
        proc = model._proc
        model.cfg.conversation.model = "haiku"
        model.complete(packet, threading.Event())
        self.assertIsNotNone(proc.poll())
        model.close()
        model.complete(packet, threading.Event())
        sent = [json.loads(json.loads(line)["message"]["content"]) for line in path.read_text().splitlines()]
        self.assertEqual(sent, [packet] * 3)

    def test_timeout_kills_model(self):
        model, _ = self.model("hang")
        model.cfg.conversation.timeout_s = 0.15
        with self.assertRaisesRegex(BrainError, "timed out"):
            model.complete(self.packet(), threading.Event())
        self.assertIsNone(model._proc)

    def test_cancel_unblocks_wait_and_next_request_rebuilds_context(self):
        model, path = self.model("hang")
        errors = []
        def run():
            try:
                model.complete(self.packet(), threading.Event())
            except BrainError as e:
                errors.append(str(e))
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        model.cancel()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(model._proc)
        self.assertIsNone(model._previous)
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_failure_is_not_retried_or_replaced_with_intermediate_text(self):
        for mode, message in [("invalid", "invalid response"), ("error", "Session limit reached"), ("crash", "closed")]:
            with self.subTest(mode=mode):
                model, path = self.model(mode)
                with self.assertRaisesRegex(BrainError, message):
                    model.complete(self.packet(), threading.Event())
                self.assertIsNone(model._proc)
                self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_stderr_is_drained_while_stdout_waits(self):
        model, _ = self.model("stderr")
        model.cfg.conversation.timeout_s = 2
        self.assertEqual(model.complete(self.packet(), threading.Event())["speech"], "reply 1")

    def test_repeated_prompt_is_new_turn_even_if_history_was_dropped(self):
        model, path = self.model()
        packet = self.packet()
        packet["turn"]["id"] = "one"
        model.complete(packet, threading.Event())
        packet["turn"]["id"] = "two"
        model.complete(packet, threading.Event())
        sent = json.loads(json.loads(path.read_text().splitlines()[1])["message"]["content"])
        self.assertEqual(sent["turn"]["user"], "Hi")

    def test_native_context_recycles_and_changed_context_is_forwarded(self):
        model, path = self.model()
        packet = self.packet()
        model.complete(packet, threading.Event())
        packet["memory"] = "Updated memory"
        packet["coding_context"] = "Updated typed work"
        model.complete(packet, threading.Event())
        model._calls = 20
        model.complete(packet, threading.Event())
        sent = [json.loads(json.loads(line)["message"]["content"]) for line in path.read_text().splitlines()]
        self.assertEqual(sent[1]["memory"], "Updated memory")
        self.assertEqual(sent[1]["coding_context"], "Updated typed work")
        self.assertEqual(sent[2], packet)

    def test_pruning_history_rebuilds_the_connection(self):
        model, path = self.model()
        packet = self.packet(history=[{"user": "old", "events": []}])
        model.complete(packet, threading.Event())
        packet["history"] = [{"user": "recent", "events": []}]
        result = model.complete(packet, threading.Event())
        self.assertEqual(result["speech"], "reply 1")
        sent = json.loads(json.loads(path.read_text().splitlines()[1])["message"]["content"])
        self.assertEqual(sent, packet)


class SpeechStreamTests(unittest.TestCase):
    def stream(self, text, size=1):
        """Feed `text` in pieces of `size` characters; returns (emitted pieces, parser)."""
        parser = SpeechStream()
        pieces = [parser.feed(text[i:i + size]) for i in range(0, len(text), size)]
        return [p for p in pieces if p], parser

    def test_speech_is_decoded_as_it_arrives_in_any_piece_size(self):
        reply = json.dumps({"speech": "Hello! \"Quoted\", back\\slash, caf\u00e9, tab\t, new\nline, \U0001F600 done.", "task": None})
        expected = json.loads(reply)["speech"]
        for size in (1, 2, 3, 7, len(reply)):
            with self.subTest(size=size):
                pieces, parser = self.stream(reply, size)
                self.assertEqual("".join(pieces), expected)
                self.assertTrue(parser.done)
        # Escaped form too: the model writes \uXXXX escapes, including surrogate pairs split across pieces.
        escaped = '{"speech": "caf\\u00e9 \\ud83d\\ude00!", "task": null}'
        pieces, _ = self.stream(escaped, 1)
        self.assertEqual("".join(pieces), "caf\u00e9 \U0001F600!")
        self.assertEqual(self.stream(escaped, len(escaped))[0], ["caf\u00e9 \U0001F600!"])

    def test_speech_key_is_found_after_a_task_and_never_inside_other_strings(self):
        task = {**TASK, "context": 'They said "speech": "not this", and {"speech": "nor this"}'}
        reply = json.dumps({"task": task, "speech": "This one."})
        for size in (1, 5, len(reply)):
            with self.subTest(size=size):
                pieces, parser = self.stream(reply, size)
                self.assertEqual("".join(pieces), "This one.")
                self.assertTrue(parser.done)

    def test_nothing_after_the_speech_string_is_emitted(self):
        parser = SpeechStream()
        self.assertEqual(parser.feed('{"speech": "Hi'), "Hi")
        self.assertEqual(parser.feed(' there.", "task": {"objective": "x"}}'), " there.")
        self.assertTrue(parser.done)
        self.assertEqual(parser.feed('"speech": "more"'), "")
        self.assertEqual(parser.text, "Hi there.")

    def test_incomplete_escapes_are_held_back(self):
        parser = SpeechStream()
        self.assertEqual(parser.feed('{"speech": "a\\'), "a")
        self.assertEqual(parser.feed('u00'), "")
        self.assertEqual(parser.feed('e9\\ud8'), "\u00e9")
        self.assertEqual(parser.feed('3d'), "")  # a high surrogate waits for its pair
        self.assertEqual(parser.feed('\\ude00"}'), "\U0001F600")
        self.assertTrue(parser.done)
        # A high surrogate left alone at the end of the string does not crash the reply.
        parser = SpeechStream()
        parser.feed('{"speech": "x\\ud83d')
        self.assertEqual(parser.feed('"}'), "?")

    def test_a_non_string_speech_value_emits_nothing(self):
        pieces, parser = self.stream('{"speech": null, "task": null, "other": "speech"}', 3)
        self.assertEqual(pieces, [])
        self.assertFalse(parser.done)


class ContractTests(unittest.TestCase):
    def test_malformed_and_plain_worker_results_cannot_claim_success(self):
        for turn in (Turn(text="All done!"), Turn(data={"status": "completed"}), Turn(data={**RESULT, "checks": "passed"})):
            result = task_result(turn)
            self.assertEqual(result["status"], "failed")
            self.assertIn("unconfirmed", result["summary"])

    def test_status_values_and_empty_objectives_are_rejected(self):
        with self.assertRaises(BrainError):
            validate_response(dict(speech="", task={**TASK, "objective": " "}))
        self.assertEqual(task_result(Turn(data={**RESULT, "status": "probably"}))["status"], "failed")

    def test_both_worker_drivers_preserve_permissions_and_typed_resume_target(self):
        cfg = Config()
        cfg.brain.mode = "plan"
        with patch("vision.brain.find_claude", return_value="claude"), patch("vision.codex.find_codex", return_value="codex"), patch("vision.memory.prompt_section", return_value=""):
            for cls in (Brain, CodexBrain):
                worker = cls(cfg.brain)
                worker.task_mode = True
                worker.session_id = "worker-id"
                cmd = worker._command()
                self.assertIn("silent task worker", " ".join(cmd))
                self.assertNotIn("lively South London", " ".join(cmd))
                if cls is Brain:
                    self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
                    self.assertIn("--allow-dangerously-skip-permissions", cmd)
                    self.assertNotIn("--dangerously-skip-permissions", cmd)
                    self.assertIn("--disallowedTools", cmd)
                    self.assertIn("--json-schema", cmd)
                else:
                    self.assertIn('sandbox_mode="read-only"', cmd)
                with patch("pathlib.Path.write_text") as write:
                    worker._remember_session()
                write.assert_not_called()

    def test_worker_plan_is_visible_in_question_even_without_text_callback(self):
        with patch("vision.brain.find_claude", return_value="claude"):
            worker = Brain(Config().brain)
        worker.task_mode = True
        worker.cfg.mode = "plan"
        proc = SimpleNamespace(stdin=io.StringIO())
        questions = []
        def answer(qs):
            questions.extend(qs)
            return {qs[0]["question"]: "Yes"}
        turn = Turn()
        worker._answer_control(proc, {"request_id": "q1", "request": {"tool_name": "ExitPlanMode", "input": {"plan": "Change parser.py and run its tests."}}}, answer, lambda _: None, turn)
        self.assertIn("Change parser.py", questions[0]["question"])
        self.assertTrue(turn.plan_approved)
        response = json.loads(proc.stdin.getvalue())["response"]["response"]
        self.assertEqual(response["behavior"], "allow")
        self.assertEqual(response["updatedPermissions"], [{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}])

    def test_approving_a_plan_says_so_at_once(self):
        for approval, told in (("session", ["auto"]), ("turn", [])):
            with patch("vision.brain.find_claude", return_value="claude"):
                worker = Brain(Config().brain)
            worker.cfg.mode, worker.cfg.plan_approval = "plan", approval
            seen = []
            worker.on_mode_change = lambda mode: seen.append((mode, worker.cfg.mode))
            worker._answer_control(SimpleNamespace(stdin=io.StringIO()), {"request_id": "q1", "request": {"tool_name": "ExitPlanMode", "input": {"plan": "Change parser.py."}}},
                                   lambda qs: {qs[0]["question"]: "Yes"}, lambda _: None, Turn())
            self.assertEqual(seen, [(m, "auto") for m in told])  # called after the switch, so a summary carries it

    def test_unapproved_plan_keeps_plan_mode(self):
        for choice in ("No", "Make it smaller"):
            with patch("vision.brain.find_claude", return_value="claude"):
                worker = Brain(Config().brain)
            worker.cfg.mode = "plan"
            proc = SimpleNamespace(stdin=io.StringIO())
            turn = Turn()
            worker._answer_control(proc, {"request_id": "q1", "request": {"tool_name": "ExitPlanMode", "input": {"plan": "Change parser.py."}}},
                                   lambda qs: {qs[0]["question"]: choice}, lambda _: None, turn)
            response = json.loads(proc.stdin.getvalue())["response"]["response"]
            self.assertEqual(response["behavior"], "deny")
            self.assertNotIn("updatedPermissions", response)
            self.assertEqual(worker.cfg.mode, "plan")
            self.assertFalse(turn.plan_approved)

    def test_worker_clarifications_go_back_as_data_instead_of_direct_user_questions(self):
        with patch("vision.brain.find_claude", return_value="claude"):
            worker = Brain(Config().brain)
        worker.task_mode = True
        proc = SimpleNamespace(stdin=io.StringIO())
        ask = Mock()
        worker._answer_control(proc, {"request_id": "q1", "request": {"tool_name": "AskUserQuestion", "input": {"questions": [{"question": "Which file?"}]}}}, ask, lambda _: None, Turn())
        ask.assert_not_called()
        response = json.loads(proc.stdin.getvalue())["response"]["response"]
        self.assertEqual(response["behavior"], "deny")
        self.assertIn("needs_input", response["message"])

    def test_worker_sessions_are_not_offered_as_typed_sessions(self):
        from vision.sessions import _claude_session, _codex_session

        task = json.dumps({"type": "vision_task", "task": TASK})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.jsonl"
            path.write_text(json.dumps({"type": "user", "entrypoint": "sdk-cli", "message": {"content": task}}) + "\n")
            self.assertIsNone(_claude_session(str(path)))
            path.write_text("\n".join(json.dumps(r) for r in [
                {"type": "session_meta", "payload": {"originator": "codex_exec", "id": "worker"}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"text": "You are Vision's silent task worker."}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": task}]}},
            ]) + "\n")
            self.assertIsNone(_codex_session(str(path)))

    def test_old_configs_get_conversation_defaults_and_new_settings_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for content, model in (('[brain]\nmodel="opus"\n', "sonnet"), ('[conversation]\nmodel="haiku"\neffort=""\nmax_delegations=2\n', "haiku")):
                path.write_text(content)
                with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                    cfg = load_config()
                self.assertEqual(cfg.conversation.model, model)
                self.assertFalse(cfg.conversation.timing)
            path.write_text('[conversation]\ntiming=true\n')
            with patch("vision.config.CONFIG_PATH", path), patch("vision.config.ensure_dirs"):
                self.assertTrue(load_config().conversation.timing)


if __name__ == "__main__":
    unittest.main()
