"""`vision voice check` (vision/voicecheck.py) with a fake voice, fake ears and scripted answers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_welcome import ScriptedIO
from vision import config, service, voicecheck

SPEAKERS = [("", "system default", ""), ("hdmi", "Monitor", "hdmi"), ("usb", "USB headset", "usb")]
MICS = [("", "system default", ""), ("usb", "USB headset", "usb")]


class FakeHands:
    """Says and hears what the test scripts: `heard` is what each listen returns, in order."""

    def __init__(self, heard=("testing one two three",), woke=True, say_error=None):
        self.heard, self.woke, self.say_error = list(heard), woke, say_error
        self.said, self.outputs, self.inputs, self.wakes, self.resets = [], [], [], 0, 0

    def say(self, text):
        if self.say_error:
            raise self.say_error
        self.said.append(text)

    def reset(self):
        self.resets += 1

    def hear(self):
        return self.heard.pop(0) if self.heard else ""

    def wake(self, timeout=0):
        self.wakes += 1
        return self.woke

    def use_output(self, spec):
        self.outputs.append(spec)

    def use_input(self, spec):
        self.inputs.append(spec)


class VoiceCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text('[voice]\noutput_device = ""\n\n[listen]\ninput_device = ""\n', encoding="utf-8")
        for p in (patch.object(config, "CONFIG_PATH", self.path), patch.object(config, "ensure_dirs"),
                  patch.object(config, "output_device_choices", return_value=SPEAKERS),
                  patch.object(config, "input_device_choices", return_value=MICS)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def check(self, io, hands):
        return voicecheck.check(io, config.Config(), hands)

    def test_everything_works_first_time(self):
        io, hands = ScriptedIO(confirms=[True]), FakeHands()
        self.assertTrue(self.check(io, hands))
        text = io.text()
        self.assertEqual(hands.said, [voicecheck.LINE])
        self.assertLess(text.index("◆ Can you hear Vision?"), text.index("◆ Can Vision hear you?"))
        self.assertLess(text.index("◆ Can Vision hear you?"), text.index("◆ The wake word"))
        self.assertIn("The voice works.", text)
        self.assertIn("Heard: “testing one two three”", text)
        self.assertIn("“Vision” wakes it.", text)
        self.assertIn("Voice is ready.", text)
        self.assertEqual([q for q in io.asked if isinstance(q, str)], ["Did you hear that?"])  # no device questions

    def test_no_sound_offers_the_other_speakers_and_saves_the_pick(self):
        io, hands = ScriptedIO(confirms=[False, True], picks=["usb"]), FakeHands()
        self.assertTrue(self.check(io, hands))
        self.assertEqual(io.asked[1], ("Which speakers should Vision use?", ["", "hdmi", "usb"]))
        self.assertEqual((hands.outputs, len(hands.said)), (["usb"], 2))  # said again on the new ones
        self.assertIn('output_device = "usb"', self.path.read_text())

    def test_still_no_sound_says_where_to_look_and_carries_on_to_the_microphone(self):
        io, hands = ScriptedIO(confirms=[False, False], picks=["hdmi"]), FakeHands()
        self.assertFalse(self.check(io, hands))
        self.assertIn("No sound yet. `vision doctor` lists the audio devices", io.text())
        self.assertIn("Heard: “testing one two three”", io.text())
        self.assertNotIn("Voice is ready.", io.text())

    def test_a_voice_that_will_not_start_says_why(self):
        io, hands = ScriptedIO(), FakeHands(say_error=RuntimeError("Qwen3-TTS model missing. Run `vision setup`."))
        self.assertFalse(self.check(io, hands))
        self.assertIn("The voice didn't start: Qwen3-TTS model missing.", io.text())

    def test_a_silent_microphone_offers_the_others_and_saves_the_pick(self):
        io, hands = ScriptedIO(confirms=[True], picks=["usb"]), FakeHands(heard=["", "hello there"])
        self.assertTrue(self.check(io, hands))
        self.assertIn("Didn't hear anything.", io.text())
        self.assertEqual(io.asked[1], ("Which microphone should Vision use?", ["", "usb"]))
        self.assertEqual(hands.inputs, ["usb"])
        self.assertIn('input_device = "usb"', self.path.read_text())
        self.assertIn("Heard: “hello there”", io.text())

    def test_no_microphone_at_all_fails_and_skips_the_wake_word(self):
        io, hands = ScriptedIO(confirms=[True], picks=["usb"]), FakeHands(heard=[])
        self.assertFalse(self.check(io, hands))
        self.assertIn("Still nothing. /mic in a chat picks the microphone", io.text())
        self.assertEqual(hands.wakes, 0)
        self.assertNotIn("◆ The wake word", io.text())

    def test_a_missed_wake_word_fails_nothing(self):
        io, hands = ScriptedIO(confirms=[True]), FakeHands(woke=False)
        self.assertTrue(self.check(io, hands))
        self.assertIn("Didn't catch it. It's optional", io.text())
        self.assertIn("Voice is ready.", io.text())

    def test_with_one_device_there_is_nothing_else_to_offer(self):
        with patch.object(config, "output_device_choices", return_value=SPEAKERS[:1]):
            io, hands = ScriptedIO(confirms=[False]), FakeHands()
            self.assertFalse(self.check(io, hands))
        self.assertEqual(len(hands.said), 1)
        self.assertEqual([q for q in io.asked if isinstance(q, tuple)], [])


BUSY = RuntimeError("voice is already loaded in another Vision (pid 4242 vision). Use that one, or close it and try again.")


class VoiceHeldElsewhereTests(VoiceCheckTests):
    """Only one Vision keeps the voice on the GPU: the check says who has it instead of blaming the speakers."""

    def setUp(self):
        super().setUp()
        self.service_pid = 4242
        for p in (patch.object(service, "pid", side_effect=lambda: self.service_pid),
                  patch.object(service, "stop"), patch.object(service, "start"),
                  patch.object(service, "kind", return_value="systemd")):
            p.start()
            self.addCleanup(p.stop)

    def held_until_paused(self):
        hands = FakeHands()
        real = hands.say
        hands.say = lambda text: real(text) if service.stop.called else (_ for _ in ()).throw(BUSY)
        return hands

    def test_the_background_service_is_paused_for_the_check_and_started_again(self):
        io, hands = ScriptedIO(confirms=[True, True]), self.held_until_paused()
        self.assertTrue(self.check(io, hands))
        self.assertEqual(io.asked[0], "Pause the service for this check? It starts again afterwards.")
        self.assertIn("The background service (`vision serve`, for your phone) has the voice loaded", io.text())
        service.stop.assert_called_once()
        self.assertEqual(hands.said, [voicecheck.LINE])  # spoken once the service was out of the way
        self.assertIn("The voice works.", io.text())
        service.start.assert_called_once()
        self.assertIn("The background service is running again.", io.text())
        self.assertGreaterEqual(hands.resets, 2)  # a fresh load after the pause, and the voice let go before the restart

    def test_the_service_comes_back_even_when_the_check_is_left_halfway(self):
        io, hands = ScriptedIO(confirms=[True]), self.held_until_paused()
        hands.hear = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        io.confirm = lambda question, default=True: True
        with self.assertRaises(KeyboardInterrupt):
            self.check(io, hands)
        service.start.assert_called_once()

    def test_a_no_to_pausing_does_not_ask_about_the_speakers(self):
        io, hands = ScriptedIO(confirms=[False]), self.held_until_paused()
        self.assertFalse(self.check(io, hands))
        service.stop.assert_not_called()
        service.start.assert_not_called()
        self.assertNotIn("Did you hear that?", io.asked)
        self.assertEqual([q for q in io.asked if isinstance(q, tuple)], [])  # and no list of other speakers
        self.assertIn("Your speakers were not tested.", io.text())
        self.assertIn("Heard: “testing one two three”", io.text())  # the microphone needs no voice: still checked

    def test_another_vision_holding_it_is_named_not_paused(self):
        self.service_pid = None  # no background service: it is a Vision in some terminal
        io, hands = ScriptedIO(), FakeHands(say_error=BUSY)
        self.assertFalse(self.check(io, hands))
        self.assertIn("Another Vision (pid 4242) has the voice loaded", io.text())
        self.assertEqual(io.asked, [])
        service.stop.assert_not_called()

    def test_the_real_voice_is_loaded_before_it_speaks_so_a_failure_is_heard_about(self):
        class Speaker:
            def __init__(self, cfg):
                self.spoke = False

            def _load(self):
                raise BUSY

            def say(self, text):  # the real one starts a player thread and returns: a failed load stays silent
                self.spoke = True

        with patch("vision.tts.Speaker", Speaker):
            hands = voicecheck.Hands(config.Config())
            with self.assertRaises(RuntimeError):
                hands.say("hello")
            self.assertFalse(hands._speaker.spoke)


if __name__ == "__main__":
    unittest.main()
