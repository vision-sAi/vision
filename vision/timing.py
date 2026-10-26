"""Opt-in voice latency traces. Only timings and fixed labels reach disk."""
from __future__ import annotations

import json
import threading
import time
import uuid

from vision.config import STATE_DIR


class VoiceTiming:
    def __init__(self):
        self.started = time.monotonic()
        self.events = []
        self._lock = threading.Lock()
        self._written = False
        self._tools = set()

    def event(self, name, *, cold=None, offset=0.0, replace=False):
        item = {"stage": name, "ms": round((time.monotonic() - self.started + offset) * 1000, 2)}
        if cold is not None:
            item["cold"] = cold
        with self._lock:
            if replace:
                self.events[:] = [e for e in self.events if e["stage"] != name]
            self.events.append(item)

    def provider_event(self, envelope):
        event = envelope.get("event", {})
        block = event.get("content_block", {})
        if event.get("type") == "content_block_start" and block.get("type") == "tool_use":
            if block.get("name") in ("WebSearch", "WebFetch"):
                self._tools.add(block.get("id"))
                self.event("web_start")
        if envelope.get("type") == "user":
            for block in envelope.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("tool_use_id") in self._tools:
                    self._tools.remove(block["tool_use_id"])
                    self.event("web_end")

    def write(self):
        with self._lock:
            if self._written:
                return
            self._written = True
            row = {"id": uuid.uuid4().hex, "events": list(self.events)}
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR / "voice-timing.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(row) + "\n")
        except OSError:
            pass  # Diagnostics must never interrupt a conversation.
