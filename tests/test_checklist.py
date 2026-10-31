"""vision.checklist and its rows under the live line: the model's task list, Claude Code's way."""
import unittest
from types import SimpleNamespace

from vision.checklist import ACTIVE, DONE, PENDING, Checklist, checklist_of


def plain(rows) -> list[str]:
    return ["".join(f[1] for f in row).rstrip() for row in rows]


class ChecklistTests(unittest.TestCase):
    def test_todowrite_replaces_the_whole_list(self):
        cl = Checklist()
        cl.claude_call("TodoWrite", {"todos": [{"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
                                               {"content": "Fix it", "status": "in_progress", "activeForm": "Fixing it"}]})
        self.assertEqual([(t.text, t.status) for t in cl.items()], [("Read the code", DONE), ("Fix it", ACTIVE)])
        self.assertEqual(cl.active().active, "Fixing it")
        cl.claude_call("TodoWrite", {"todos": [{"content": "Fix it", "status": "completed"}]})
        self.assertEqual(cl.items(), [])  # all done: Claude Code drops a finished list

    def test_task_updates_change_and_delete(self):
        cl = Checklist()
        cl.claude_call("TaskCreate", {"subject": "A"}, {"task": {"id": "1"}})
        cl.claude_call("TaskCreate", {"subject": "B"}, {"task": {"id": "2"}})
        cl.claude_call("TaskUpdate", {"taskId": "2", "status": "in_progress", "subject": "B, renamed"})
        cl.claude_call("TaskUpdate", {"taskId": "1", "status": "deleted"})
        cl.claude_call("TaskUpdate", {"taskId": "9", "status": "completed"})  # one from before a resume: nothing to tick
        self.assertEqual([(t.id, t.text, t.status) for t in cl.items()], [("2", "B, renamed", ACTIVE)])

    def test_codex_and_acp_plans(self):
        cl = Checklist()
        cl.codex_plan([{"step": "Look", "status": "completed"}, {"step": "Patch", "status": "inProgress"}, {"step": "Test", "status": "pending"}])
        self.assertEqual([t.status for t in cl.items()], [DONE, ACTIVE, PENDING])
        cl.acp_plan([{"content": "Only", "status": "in_progress", "priority": "high"}])
        self.assertEqual([(t.text, t.status) for t in cl.items()], [("Only", ACTIVE)])

    def test_a_new_session_keeps_its_first_turns_list_and_a_switch_clears_it(self):
        brain = SimpleNamespace(session_id=None)
        checklist_of(brain).acp_plan([{"content": "X", "status": "pending"}])
        brain.session_id = "s1"  # the fresh session got its id after the turn
        self.assertEqual(len(checklist_of(brain).items()), 1)
        brain.session_id = None  # /new
        self.assertEqual(checklist_of(brain).items(), [])


class RowsTests(unittest.TestCase):
    def tasks(self, *statuses):
        cl = Checklist()
        cl.replace([(f"Task {i}", s, f"Doing task {i}") for i, s in enumerate(statuses, 1)])
        return cl.items()

    def test_short_list_shows_every_task(self):
        from vision.ui import checklist_rows

        self.assertEqual(plain(checklist_rows(self.tasks(DONE, ACTIVE, PENDING), 60)),
                         ["  ⎿  ✔ Task 1", "     ■ Task 2", "     □ Task 3"])

    def test_long_list_folds_the_finished_ones(self):
        from vision.ui import checklist_rows

        rows = plain(checklist_rows(self.tasks(DONE, ACTIVE, ACTIVE, PENDING, PENDING, PENDING, PENDING), 60))
        self.assertEqual(rows[0], "  ⎿  ■ Task 2")
        self.assertEqual(rows[-1], "     … +1 completed, +1 pending")
        self.assertEqual(len(rows), 6)

    def test_live_line_takes_the_active_task_and_keeps_thinking_in_brackets(self):
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.status = "thinking…"
        tasks = self.tasks(ACTIVE, PENDING)
        e.checklist_fn = lambda: tasks
        text = plain(e.lines(60))
        self.assertRegex(text[-3], r"^  . Doing task 1… \(0s · thinking · esc to stop\)$")
        self.assertEqual(text[-2:], ["  ⎿  ■ Task 1", "     □ Task 2"])

    def test_todo_calls_get_no_row_of_their_own(self):
        from vision.brain import ToolCall
        from vision.ui import _ReplyEntry

        e = _ReplyEntry(markdown=False)
        e.place(ToolCall(id="t1", name="TaskUpdate"))
        self.assertEqual(e.tools, {})

    def test_took(self):
        from vision.ui import took

        self.assertEqual(took(4.84), "4.8s")
        self.assertEqual(took(10.9, live=True), "10s")
        self.assertEqual(took(851), "14m 11s")
        self.assertEqual(took(3725), "1h 2m")


if __name__ == "__main__":
    unittest.main()
