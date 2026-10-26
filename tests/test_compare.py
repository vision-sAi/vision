import io
import threading
import time
import unittest
from types import SimpleNamespace

from rich.console import Console

from vision import compare
from vision.compare import Comparison, CompareView, Pick, as_markdown, parse, picks_from_labels, silence
from vision.turnqueue import QueuedTurn


def _print(renderable, width: int) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=width, color_system=None).print(renderable)
    return buf.getvalue()


class ParseTests(unittest.TestCase):
    def test_models_with_efforts_then_the_prompt(self):
        picks, prompt, err = parse("opus:high gpt-6-astra:medium grok-4.6 which of these is right?", "medium")
        self.assertEqual(err, "")
        self.assertEqual(picks, [Pick("opus", "high"), Pick("gpt-6-astra", "medium"), Pick("grok-4.6", "medium")])
        self.assertEqual(prompt, "which of these is right?")

    def test_missing_effort_is_the_sessions_fitted_to_the_model(self):
        picks, prompt, err = parse("haiku sonnet", "high")
        self.assertEqual(err, "")
        self.assertEqual(picks, [Pick("haiku", ""), Pick("sonnet", "high")])  # Haiku has no effort setting
        self.assertEqual(prompt, "")

    def test_unsupported_effort_is_refused(self):
        picks, _, err = parse("grok-4.6:max opus", "medium")
        self.assertEqual(picks, [])
        self.assertIn("Grok 4.6 doesn't take effort max", err)

    def test_one_model_is_not_a_comparison(self):
        picks, _, err = parse("opus:high tell me", "medium")
        self.assertEqual(picks, [])
        self.assertIn("at least two models", err)

    def test_no_model_at_all(self):
        picks, prompt, err = parse("what is up", "medium")
        self.assertEqual((picks, prompt), ([], ""))
        self.assertIn("isn't a model", err)
        self.assertEqual(parse("", "medium"), ([], "", ""))

    def test_same_pick_twice_counts_once(self):
        picks, _, _ = parse("opus:high opus:high sonnet go", "medium")
        self.assertEqual(picks, [Pick("opus", "high"), Pick("sonnet", "medium")])

    def test_form_labels_become_picks(self):
        self.assertEqual(picks_from_labels("opus, gpt-6-astra", "high"), [Pick("opus", "high"), Pick("gpt-6-astra", "high")])
        self.assertEqual(picks_from_labels("opus", "high"), [])

    def test_labels(self):
        self.assertEqual(Pick("opus", "high").label(), "Opus 5 (high)")
        self.assertEqual(Pick("haiku", "").label(), "Haiku 4.5")
        self.assertEqual(Pick("opus", "high").spec(), "opus:high")
        self.assertEqual(compare.armed_label([Pick("opus", "high"), Pick("grok-4.6", "low")]), "Opus 5 (high) · Grok 4.6 (low)")


class FakeBrain:
    """Streams `reply` word by word; `fail` raises instead; `slow` waits for cancel()."""

    def __init__(self, reply: str, fail: str = "", slow: bool = False, tool: str = ""):
        self.reply, self.fail, self.slow, self.tool = reply, fail, slow, tool
        self.output_tokens = 0
        self.closed = False
        self.cancelled = threading.Event()
        self.prompts: list[str] = []

    def ask(self, prompt, on_text=None, on_status=None, **_):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError(self.fail)
        if self.tool:
            on_status(self.tool)
        for word in self.reply.split(" "):
            if self.slow and self.cancelled.wait(0.02):
                return SimpleNamespace(text=" ".join(self.reply.split(" ")[:1]), is_error=True, error="cancelled", usage=None)
            on_text(word + " ")
            self.output_tokens += 1
        return SimpleNamespace(text=self.reply, is_error=False, error="", usage={"output_tokens": self.output_tokens})

    def cancel(self):
        self.cancelled.set()

    def close(self):
        self.closed = True


class ComparisonTests(unittest.TestCase):
    picks = [Pick("opus", "high"), Pick("gpt-6-astra", "medium")]

    def test_every_pick_is_asked_at_once_and_answers_are_kept(self):
        brains = {}

        def make(pick):
            brains[pick.model] = FakeBrain(f"{pick.model} says hello", tool="Bash")
            return brains[pick.model]

        changes = []
        cmp = Comparison(self.picks, "hello?", make, on_change=lambda: changes.append(cmp.progress()))
        answers = cmp.run()
        self.assertEqual([a.text for a in answers], ["opus says hello", "gpt-6-astra says hello"])
        self.assertTrue(all(a.done and not a.error for a in answers))
        self.assertEqual([a.tokens for a in answers], [3, 3])
        self.assertTrue(all(b.closed for b in brains.values()))  # nothing kept alive afterwards
        self.assertTrue(all(p.startswith(compare.PREFACE) and p.endswith("hello?") for b in brains.values() for p in b.prompts))
        self.assertIn("Opus 5 (high): using Bash…", " ".join(changes))  # the live line followed the tool
        self.assertEqual(cmp.progress(), "Opus 5 (high): done · GPT-6 Astra (medium): done")

    def test_one_failing_brain_does_not_stop_the_other(self):
        def make(pick):
            return FakeBrain("fine", fail="no login" if pick.model == "opus" else "")

        answers = Comparison(self.picks, "q", make).run()
        self.assertEqual(answers[0].error, "no login")
        self.assertEqual(answers[1].text, "fine")
        self.assertIn("Opus 5 (high): failed", Comparison.progress(SimpleNamespace(answers=answers)))

    def test_cancel_stops_the_slow_ones(self):
        brains = []

        def make(pick):
            brains.append(FakeBrain("one two three four five six seven eight nine ten", slow=True))
            return brains[-1]

        cmp = Comparison(self.picks, "q", make)
        t = threading.Thread(target=cmp.run)
        t.start()
        time.sleep(0.05)
        cmp.cancel()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertTrue(cmp.cancelled)
        self.assertTrue(all(a.error == "cancelled" for a in cmp.answers))
        self.assertTrue(all(b.cancelled.is_set() for b in brains))

    def test_silence_turns_off_session_remembering(self):
        calls = []

        class B:
            def _remember_session(self):
                calls.append("remembered")

            def _save(self):
                calls.append("saved")

        b = B()
        silence(b)
        b._remember_session()
        b._save()
        self.assertEqual(calls, [])

    def test_queued_turn_carries_the_picks(self):
        self.assertEqual(QueuedTurn("x").compare, ())
        self.assertEqual(QueuedTurn("x", compare=tuple(self.picks)).compare, tuple(self.picks))


class ViewTests(unittest.TestCase):
    def answers(self):
        a = compare.Answer(Pick("opus", "high"), text="**Yes.** Opus thinks so.", tokens=1200, elapsed=4.2, done=True)
        b = compare.Answer(Pick("gpt-6-astra", "medium"), text="No. Astra disagrees.", tokens=40, elapsed=2.0, done=True)
        return [a, b]

    def test_wide_terminal_puts_the_answers_side_by_side(self):
        text = _print(CompareView(self.answers(), "is it?", untouched="Sonnet 5"), width=120)
        header = next(ln for ln in text.splitlines() if "Opus 5 (high)" in ln)
        self.assertIn("GPT-6 Astra (medium)", header)  # same row: columns
        footer = next(ln for ln in text.splitlines() if "4.2s" in ln)
        self.assertIn("↓1.2k", footer)
        self.assertIn("2.0s", footer)
        self.assertIn("Opus thinks so.", text)
        self.assertIn("nothing was sent to Sonnet 5", text)

    def test_narrow_terminal_stacks_them(self):
        text = _print(CompareView(self.answers(), "is it?"), width=60)
        lines = text.splitlines()
        opus = next(i for i, ln in enumerate(lines) if "Opus 5 (high)" in ln)
        astra = next(i for i, ln in enumerate(lines) if "GPT-6 Astra (medium)" in ln)
        self.assertLess(opus, astra)
        self.assertNotIn("GPT-6 Astra", lines[opus])
        self.assertIn("compared outside the conversation", text)

    def test_errors_and_cancels_show_in_the_cells(self):
        a = compare.Answer(Pick("opus", "high"), error="no login", elapsed=0.1, done=True)
        b = compare.Answer(Pick("grok-4.6", "low"), text="half an", error="cancelled", elapsed=1.0, done=True)
        text = _print(CompareView([a, b], "q"), width=120)
        self.assertIn("could not answer: no login", text)
        self.assertIn("cancelled", text)
        self.assertIn("half an", text)
        self.assertIn("failed", text)

    def test_markdown_for_the_phone(self):
        md = as_markdown(self.answers(), "is it?")
        self.assertTrue(md.startswith("**Compared:** is it?"))
        self.assertIn("## Opus 5 (high)\n\n**Yes.** Opus thinks so.\n\n_4.2s · ↓1.2k_", md)
        self.assertIn("## GPT-6 Astra (medium)", md)


if __name__ == "__main__":
    unittest.main()
