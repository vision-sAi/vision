from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vision import agentlog, subagents
from vision.brain import AgentRun, Turn, agent_frame, clock, run_from_frame
from vision.subagents import AgentTracker, codex_item, grok_tool_call, grok_tool_update


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        for p in (patch.object(subagents, "TRACE_FILE", root / "trace.jsonl"), patch.object(agentlog, "AGENTS_DIR", root / "agents")):
            p.start()
            self.addCleanup(p.stop)


class CodexAgentTests(_Tmp):
    def test_collab_items_open_and_close_rows(self):
        turn, seen = Turn(), []
        subs = AgentTracker(turn, lambda r: seen.append((r.label, r.done, r.failed)), model="gpt-5.5", effort="high")
        spawn = {"id": "item_3", "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "root",
                 "receiver_thread_ids": ["child-1"], "prompt": "Probe the TTS queue", "model": "gpt-6-astra", "status": "in_progress"}
        self.assertTrue(codex_item(subs, spawn, finished=False))
        self.assertTrue(codex_item(subs, {**spawn, "status": "completed", "agents_states": {"child-1": {"status": "running"}}}, finished=True))
        wait = {"id": "item_5", "type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": ["child-1"],
                "agents_states": {"child-1": {"status": "completed", "message": "Queue drains in 40 ms"}}, "status": "completed"}
        self.assertTrue(codex_item(subs, wait, finished=True))
        self.assertFalse(codex_item(subs, {"type": "command_execution"}, finished=True))
        run = turn.agents[0]
        self.assertEqual((run.label, run.model, run.effort, run.done, run.failed, run.summary),
                         ("Probe the TTS queue", "gpt-6-astra", "high", True, False, "Queue drains in 40 ms"))
        self.assertEqual(len(turn.agents), 1)
        self.assertTrue((Path(self.dir.name) / "trace.jsonl").exists())  # raw events kept for checking the reader

    def test_sub_agent_activity_items(self):
        turn = Turn()
        subs = AgentTracker(turn, None)
        codex_item(subs, {"type": "sub_agent_activity", "id": "call_1", "kind": "started", "agent_thread_id": "t1", "agent_path": "/root/astra_pip"}, True)
        codex_item(subs, {"type": "sub_agent_activity", "id": "call_2", "kind": "interacted", "agent_thread_id": "t1"}, True)
        codex_item(subs, {"type": "sub_agent_activity", "id": "subagent-completed-x", "kind": "completed", "agent_thread_id": "t1"}, True)
        run = turn.agents[0]
        self.assertEqual((run.label, run.done, run.failed, run.steps), ("astra_pip", True, False, [("message", "")]))

    def test_unfinished_rows_are_cut_off_at_the_end(self):
        turn = Turn()
        subs = AgentTracker(turn, None)
        subs.start("a", "left running")
        subs.close("cut off when the turn ended")
        self.assertEqual((turn.agents[0].done, turn.agents[0].cut_off), (True, True))


class GrokAgentTests(_Tmp):
    def test_spawn_then_output_finishes_the_row(self):
        turn, calls = Turn(), {}
        subs = AgentTracker(turn, None, model="grok-4")
        self.assertTrue(grok_tool_call(subs, {"type": "tool_call", "toolCallId": "c1", "toolName": "spawn_subagent",
                                              "rawInput": {"description": "Map the tests", "prompt": "…", "background": True}}, calls))
        grok_tool_update(subs, {"type": "tool_call_update", "toolCallId": "c1", "status": "completed", "rawOutput": {"subagent_id": "sa-9"}}, calls)
        self.assertFalse(turn.agents[0].done)  # backgrounded: still at work
        grok_tool_call(subs, {"type": "tool_call", "toolCallId": "c2", "toolName": "get_command_or_subagent_output",
                              "rawInput": {"task_ids": ["sa-9"]}}, calls)
        grok_tool_update(subs, {"type": "tool_call_update", "toolCallId": "c2", "status": "completed",
                                "rawOutput": {"results": [{"task_id": "sa-9", "status": "completed", "output": "12 test files"}]}}, calls)
        run = turn.agents[0]
        self.assertEqual((run.label, run.model, run.done, run.failed, run.summary), ("Map the tests", "grok-4", True, False, "12 test files"))
        self.assertFalse(grok_tool_call(subs, {"type": "tool_call", "toolCallId": "c3", "toolName": "read_file"}, calls))


    def test_background_spawn_as_grok_really_reports_it(self):
        # Grok reports completed background work with a TaskOutput result.
        turn, calls = Turn(), {}
        subs = AgentTracker(turn, None, model="grok-4.7")
        grok_tool_call(subs, {"toolCallId": "c1", "toolName": "spawn_subagent",
                              "rawInput": {"description": "Count lines in vision/server.py", "prompt": "…", "background": True}}, calls)
        text = "Subagent started in background.\nsubagent_id: 01a0e89e-ff00\ndescription: Count lines in vision/server.py\n"
        grok_tool_update(subs, {"toolCallId": "c1", "status": "completed", "rawOutput": {"type": "Text", "text": text},
                                "content": [{"type": "content", "content": {"type": "text", "text": text}}]}, calls)
        self.assertFalse(turn.agents[0].done)
        grok_tool_call(subs, {"toolCallId": "c2", "toolName": "get_command_or_subagent_output", "rawInput": {"task_ids": ["01a0e89e-ff00"], "timeout_ms": 1}}, calls)
        output = "2043\n\n<subagent_meta>id=01a0e89e-ff00, tool_calls=1, turns=1, duration_ms=6444</subagent_meta>\n<worktree_path>/x</worktree_path>"
        grok_tool_update(subs, {"toolCallId": "c2", "status": "completed", "rawOutput": {"type": "TaskOutput", "Result": {
            "task_id": "01a0e89e-ff00", "status": "completed", "duration_secs": 6.4, "output": output}}}, calls)
        run = turn.agents[0]
        self.assertEqual((run.done, run.failed, run.summary, run.tool_uses), (True, False, "2043", 1))


class TraceTests(_Tmp):
    def test_trace_appends_and_trims_instead_of_rewriting_the_file_per_event(self):
        with patch.object(subagents, "TRACE_LINES", 10):
            for i in range(25):
                subagents.trace("codex", {"n": i})
            lines = subagents.TRACE_FILE.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 20)  # cut back to TRACE_LINES once every TRACE_LINES appends
        self.assertGreaterEqual(len(lines), 10)
        self.assertIn('"n": 24', lines[-1])


class AgentFrameTests(unittest.TestCase):
    def test_detail_snapshot_survives_reconnect_and_completion(self):
        run = AgentRun("a1", "Explore", "Map", prompt="Find every config entry point")
        run.steps = [("Read", "config.py"), ("Bash", "pytest"), ("Bash", "pytest")]
        frame = agent_frame(run)
        copy = run_from_frame(frame)
        run_from_frame(frame, copy)  # reconnect snapshots replace history, including repeated calls
        self.assertEqual(copy.steps, run.steps)
        self.assertEqual(copy.prompt, run.prompt)
        run.done, run.summary = True, "Found two entry points."
        run.details = ["Tests passed"]
        run_from_frame(agent_frame(run), copy)
        self.assertEqual((copy.steps, copy.summary, copy.details, copy.done),
                         (run.steps, run.summary, run.details, True))

    def test_old_frames_keep_the_available_steps(self):
        copy = run_from_frame({"id": "a1", "step": {"tool": "Read", "detail": "config.py"}})
        run_from_frame({"id": "a1", "done": True}, copy)
        self.assertEqual(copy.steps, [("Read", "config.py")])

    def test_a_frame_rebuilds_the_row_with_its_real_start(self):
        run = AgentRun("a1", "Explore", "Map the tests", model="", started=time.monotonic() - 125, tool_uses=4)
        frame = agent_frame(run)
        self.assertAlmostEqual(frame["started"], time.time() - 125, delta=1)
        copy = run_from_frame(frame)
        self.assertAlmostEqual(time.monotonic() - copy.started, 125, delta=1)
        self.assertEqual((copy.kind, copy.label, copy.tool_uses), ("Explore", "Map the tests", 4))

    def test_clock(self):
        self.assertEqual([clock(17.34), clock(17.3, live=True), clock(381.7), clock(3725)], ["17.3s", "17s", "6m 21s", "1h 02m"])


class AgentLogTests(_Tmp):
    def test_agent_details_survive_saved_history(self):
        run = AgentRun("a1", "Explore", "Map", prompt="Find the config loader", done=True,
                       steps=[("Read", "config.py")], summary="Found it", details=["Checked"])
        agentlog.record("claude", "s-detail", "Find config", "Done.", [agent_frame(run)])
        history = [{"role": "user", "text": "Find config"}, {"role": "assistant", "text": "Done."}]
        agentlog.attach(history, "claude", "s-detail")
        copy = run_from_frame(history[1]["agents"][0])
        self.assertEqual((copy.prompt, copy.steps, copy.summary, copy.details),
                         (run.prompt, run.steps, run.summary, run.details))

    def test_rows_come_back_with_their_reply(self):
        rows = [{"type": "agent", "id": "a1", "kind": "Explore", "label": "Map", "done": True, "status": "3 tools · 4.0s", "at": 12},
                {"type": "agent", "id": "a2", "kind": "Review", "label": "bugs", "done": False, "at": 12}]
        agentlog.record("claude", "s1", "Map the repo", "Sending two agents off. Done: it has 12 files.", rows)
        history = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello."},
                   {"role": "user", "text": "Map the repo"}, {"role": "assistant", "text": "Sending two agents off.  Done: it has 12 files."}]
        out = agentlog.attach(history, "claude", "s1")
        self.assertNotIn("agents", out[1])
        self.assertEqual([a["id"] for a in out[3]["agents"]], ["a1", "a2"])
        self.assertEqual((out[3]["agents"][1]["done"], out[3]["agents"][1]["cut_off"]), (True, True))  # never finished: saved as cut off

    def test_a_steered_turn_splits_into_parts(self):
        reply = "Looking at it now.Right, the other file then."
        agents = [{"id": "a1", "at": 5}, {"id": "a2", "at": 30}]
        rows = agentlog.turn_entries("check brain.py", reply, "", reply, agents, [(18, "use cli.py instead", 1)])
        self.assertEqual([(r["role"], r["text"]) for r in rows], [("user", "check brain.py"), ("assistant", "Looking at it now."),
                                                                  ("user", "use cli.py instead"), ("assistant", "Right, the other file then.")])
        self.assertEqual([[a["id"] for a in r.get("agents", [])] for r in rows if r["role"] == "assistant"], [["a1"], ["a2"]])
        self.assertEqual(rows[3]["agents"][0]["at"], 12)  # relative to its own part

    def test_tool_calls_go_to_the_part_they_ran_in(self):
        reply = "Looking at it now.Right, the other file then."
        tools = [{"type": "tool", "id": "t1", "name": "Read", "at": 0}, {"type": "tool", "id": "t2", "name": "Bash", "at": 44}]
        rows = agentlog.turn_entries("check brain.py", reply, "", reply, [], [(18, "use cli.py instead", 0)], tools)
        self.assertEqual([[t["id"] for t in r.get("tools", [])] for r in rows if r["role"] == "assistant"], [["t1"], ["t2"]])
        self.assertEqual(rows[3]["tools"][0], {"id": "t2", "name": "Bash", "at": 26})  # relative, and no wire `type`

    def test_tool_rows_survive_a_history_reload(self):
        tools = [{"type": "tool", "id": "t1", "name": "Bash", "detail": "git status", "at": 8,
                  "done": True, "output": "clean"}]
        entries = agentlog.turn_entries("check the repo", "Checking. All clean.", "", "Checking. All clean.", [], [], tools)
        agentlog.record_turn("claude", "s-tools", entries)
        history = [{"role": "user", "text": "check the repo"}, {"role": "assistant", "text": "Checking. All clean."}]
        agentlog.attach(history, "claude", "s-tools")
        self.assertEqual(history[1]["tools"], [{"id": "t1", "name": "Bash", "detail": "git status", "at": 8,
                                                 "done": True, "output": "clean"}])


if __name__ == "__main__":
    unittest.main()
