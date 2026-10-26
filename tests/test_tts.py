import unittest
from unittest.mock import patch

from vision.tts import select_reply_voice


PHOENIX_VOICES = [
    "phoenix-conversational",
    "phoenix-expressive",
    "phoenix-reassuring",
    "phoenix-london",
]


class VoiceStyleSelectionTests(unittest.TestCase):
    def test_keeps_non_phoenix_voice(self):
        self.assertEqual(select_reply_voice("narrator", "Nice one!", PHOENIX_VOICES), "narrator")

    def test_uses_expressive_reference_for_excited_reply(self):
        self.assertEqual(
            select_reply_voice("phoenix-conversational", "Perfect! We got it!", PHOENIX_VOICES),
            "phoenix-expressive",
        )

    def test_reassurance_beats_exclamation(self):
        self.assertEqual(
            select_reply_voice("phoenix-conversational", "Don't worry! We'll sort it!", PHOENIX_VOICES),
            "phoenix-reassuring",
        )

    def test_code_exclamation_is_not_excitement(self):
        self.assertEqual(
            select_reply_voice("phoenix-conversational", "Change it to a != b and rerun.", PHOENIX_VOICES),
            "phoenix-conversational",
        )

    def test_apology_is_reassuring(self):
        self.assertEqual(
            select_reply_voice("phoenix-conversational", "Sorry, my mistake. Fixed now.", PHOENIX_VOICES),
            "phoenix-reassuring",
        )

    def test_falls_back_when_a_variant_is_not_installed(self):
        self.assertEqual(
            select_reply_voice("phoenix-conversational", "Nice one!", ["phoenix-conversational"]),
            "phoenix-conversational",
        )



class VoiceHeldByTheServiceTests(unittest.TestCase):
    """Only one Vision keeps the voice on the GPU: when it is the background service, the error says to connect to it."""

    def held(self, service_pid):
        from vision import service, tts
        from vision.cuda import GpuBusy

        with patch.object(service, "pid", return_value=service_pid):
            return tts._held(GpuBusy(4242, "vision"))

    def test_the_background_service_is_named_with_the_command_that_connects_to_it(self):
        from vision import voicecheck

        said = self.held(4242)
        self.assertIn("the background service", said)
        self.assertIn("`vision pair` shows its pairing code", said)
        self.assertNotIn("close it", said)  # a service has no window to close
        self.assertEqual(voicecheck.voice_holder(RuntimeError(said)), 4242)  # `vision voice check` still finds the holder

    def test_another_vision_in_a_window_keeps_the_old_words(self):
        for service_pid in (None, 99):
            self.assertEqual(self.held(service_pid), "voice is already loaded in another Vision (pid 4242 vision). "
                                                     "Use that one, or close it and try again.")

    def test_no_service_manager_to_ask_is_not_an_error(self):
        from vision import service, tts
        from vision.cuda import GpuBusy

        with patch.object(service, "pid", side_effect=OSError("no systemctl")):
            self.assertIn("Use that one, or close it", tts._held(GpuBusy(4242, "vision")))



class ReadAloudPiecesTests(unittest.TestCase):
    """A finished reply cut up for the phone's read-aloud button (tts.read_aloud_pieces)."""

    def pieces(self, text):
        from vision.tts import read_aloud_pieces

        return read_aloud_pieces(text)

    def test_code_is_skipped_and_formatting_is_not_read_out(self):
        text = ("## The fix\n\nThe `return` on line twelve is **inside** the loop, so it exits after the first pass.\n\n"
                "```python\nfor x in y:\n    return x\n```\n\nMove it one level out and see [the docs](https://example.com/docs).")
        said = " ".join(self.pieces(text))
        self.assertEqual(said, "The fix. The return on line twelve is inside the loop, so it exits after the first pass. "
                               "Code skipped. Move it one level out and see the docs.")
        self.assertNotIn("on screen", said, "nothing was just put on screen: the reply is being read back")

    def test_each_piece_is_about_a_sentence_and_short_ones_join_the_next(self):
        text = ("OK. The build failed on the linker step, which usually means a stale object file. "
                "Clean the build folder and run it again. That should do it.")
        self.assertEqual(self.pieces(text), [
            "OK. The build failed on the linker step, which usually means a stale object file.",
            "Clean the build folder and run it again. That should do it.",
        ])

    def test_a_long_run_is_cut_at_a_clause(self):
        text = ", ".join(f"item number {n}" for n in range(60)) + "."
        pieces = self.pieces(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 300 for p in pieces), [len(p) for p in pieces])
        self.assertEqual(" ".join(pieces), text, "nothing lost at the cuts")

    def test_a_reply_with_nothing_to_say_has_no_pieces(self):
        self.assertEqual(self.pieces(""), [])
        self.assertEqual(self.pieces("| a | b |"), [])


if __name__ == "__main__":
    unittest.main()
