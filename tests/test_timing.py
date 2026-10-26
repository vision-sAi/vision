import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vision.timing import VoiceTiming


class VoiceTimingTests(unittest.TestCase):
    def test_trace_has_durations_and_labels_without_provider_content(self):
        trace = VoiceTiming()
        trace.event("speech_end", replace=True)
        trace.event("speech_end", replace=True)
        trace.provider_event({"event": {"type": "content_block_start", "content_block": {
            "type": "tool_use", "name": "WebSearch", "id": "private-id", "input": "private-query"}}})
        trace.provider_event({"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "private-id", "content": "private-result"}]}})
        with tempfile.TemporaryDirectory() as tmp, patch("vision.timing.STATE_DIR", Path(tmp)):
            trace.write()
            trace.write()
            lines = (Path(tmp) / "voice-timing.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("private", lines[0])
        events = json.loads(lines[0])["events"]
        self.assertEqual([e["stage"] for e in events], ["speech_end", "web_start", "web_end"])
        self.assertTrue(all(e["ms"] >= 0 for e in events))

    def test_unwritable_trace_does_not_break_voice(self):
        with patch("pathlib.Path.open", side_effect=OSError), patch("pathlib.Path.mkdir"):
            VoiceTiming().write()

    def test_speaker_records_generation_and_playback_separately(self):
        from test_streaming_speaker import FakeSpeaker
        from vision.tts import StreamingSpeaker

        trace = VoiceTiming()
        source = FakeSpeaker()
        started = threading.Event()
        synth = source.synth_stream

        def stream(*args, **kwargs):
            started.set()
            yield from synth(*args, **kwargs)

        source.synth_stream = stream
        speaker = StreamingSpeaker(source, timing=trace)
        speaker.feed("Hello.")
        speaker.flush(tail="end")
        try:
            self.assertTrue(started.wait(1), "A complete short reply must start before finish()")
        finally:
            speaker.finish()
        self.assertEqual(speaker._chunks[0][2], 0, "Do not add a paragraph pause after the answer")
        stages = [e["stage"] for e in trace.events]
        self.assertEqual(sorted(stages), sorted(["speech_queued", "synthesis_start", "first_audio", "stream_open", "playback_estimated"]))
        # The device opens as the reply starts, no longer after its first piece of audio is made.
        self.assertLess(stages.index("stream_open"), stages.index("first_audio"))
        self.assertEqual([s for s in stages if s != "stream_open"], ["speech_queued", "synthesis_start", "first_audio", "playback_estimated"])
