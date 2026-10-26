"""Compare voice transports without recording a microphone or playing audio.

Run from the repo: .venv/bin/python scripts/bench_conversation.py --transport persistent
Uses the configured Claude subscription and performs live web searches. Only timing data
is printed/saved. These measurements exclude STT, synthesis and speaker latency.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision.config import load_config
from vision.conversation import ClaudeConversation
from vision.timing import VoiceTiming


class OneShot(ClaudeConversation):
    """Match the pre-persistence JSON transport for reproducible comparisons."""
    def complete(self, packet, cancel):
        import subprocess
        import tempfile
        from vision.brain import BrainError, brain_env
        from vision.conversation import validate_response

        command = self._command()
        command[command.index("--output-format") + 1] = "json"
        i = command.index("--input-format")
        del command[i:i + 2]
        command.remove("--verbose")
        command.remove("--include-partial-messages")
        with tempfile.TemporaryDirectory(prefix="vision-benchmark-") as cwd:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, cwd=cwd, env=brain_env("claude"), encoding="utf-8") as proc:
                try:
                    out, _ = proc.communicate(json.dumps(packet), timeout=self.cfg.conversation.timeout_s)
                except BaseException:
                    proc.kill()
                    proc.wait()
                    raise
        envelope = json.loads(out)
        if proc.returncode or envelope.get("is_error"):
            raise BrainError("Benchmark conversation failed; check Claude authentication and connectivity.")
        data = envelope.get("structured_output")
        return validate_response(data if data is not None else json.loads(envelope.get("result", "")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("one-shot", "persistent"), default="persistent")
    parser.add_argument("--baseline-module", help="Load an unchanged conversation.py for the baseline")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    cfg = load_config()
    cls = OneShot if args.transport == "one-shot" else ClaudeConversation
    if args.baseline_module:
        spec = importlib.util.spec_from_file_location("baseline", args.baseline_module)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = module.ClaudeConversation
    model = cls(cfg)
    rows = []
    history = []
    try:
        for kind in ("chat", "web"):
            # A cold request per category, then count warm requests.
            model.cancel()
            history.clear()
            for i in range(args.count + 1):
                prompt = ("Explain why the sky looks blue in one short sentence." if kind == "chat" else
                          "Check the current weather in New York City on the web and give me one short sentence. "
                          "Do a fresh lookup for this request.")
                packet = dict(history=history, turn={"user": prompt, "events": []},
                              coding_context="", memory="", worker_mode=cfg.brain.mode, delegation_allowed=False)
                timing = VoiceTiming()
                model.timing = timing
                start = time.monotonic()
                result = model.complete(packet, threading.Event())
                if result["task"] is not None:
                    raise RuntimeError("Unexpected delegation in benchmark")
                row = dict(category=kind, cold=i == 0, seconds=round(time.monotonic() - start, 3),
                           events=timing.events)
                rows.append(row)
                print(json.dumps(row), flush=True)
                history.append({"user": prompt, "events": [{"assistant": result}]})
        summary = {kind: {"median_s": round(statistics.median(r["seconds"] for r in rows if r["category"] == kind and not r["cold"]), 3),
                          "slowest_s": max(r["seconds"] for r in rows if r["category"] == kind and not r["cold"])}
                   for kind in ("chat", "web")}
        print(json.dumps({"summary": summary}), flush=True)
    finally:
        model.cancel()
        if args.output:
            args.output.write_text(json.dumps({"transport": args.transport, "rows": rows}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
