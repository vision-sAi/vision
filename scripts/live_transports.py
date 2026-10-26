"""Live check of the Codex app-server and Grok ACP transports against the real CLIs.

Spends a little of each subscription (low effort, read-only prompts), so run it yourself:

    .venv/bin/python scripts/live_transports.py            # both
    .venv/bin/python scripts/live_transports.py grok       # one
    .venv/bin/python scripts/live_transports.py grok denied subagents   # only those steps

Every callback lands in ~/.local/state/vision/live-transports.jsonl with a timestamp; the terminal
gets a pass/fail line per step.
"""

import copy
import json
import sys
import threading
import time
from pathlib import Path

from vision.brain import agent_frame, create_brain, tool_frame
from vision.config import load_config
from vision.models import provider_default

LOG = Path.home() / ".local/state/vision/live-transports.jsonl"
REPO = str(Path(__file__).resolve().parent.parent)
TURN_S = 300

STEPS = [
    ("stream+tools", "List the files in the vision/ folder of this repo and tell me which one is biggest. One line.", None),
    ("steer", "Go through every .py file in vision/ one at a time and give each a one-sentence summary.",
     "Stop after three files and say STEERED at the end."),
    ("subagents", "This is a test of subagents, so you must not do the work yourself. Spawn two subagents with your "
                  "subagent tool (spawn_agent / spawn_subagent), in parallel: one runs `wc -l vision/brain.py`, the other "
                  "`wc -l vision/server.py`. Wait for both, then report the two numbers they gave you.", None),
    ("denied", "Run exactly this shell command and tell me what happened: sudo true", None),
]


def log(provider: str, step: str, kind: str, data) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "provider": provider, "step": step, "kind": kind, "data": data}, default=str) + "\n")


def run_step(brain, provider: str, step: str, prompt: str, steer_text: str | None) -> dict:
    seen = {"text": 0, "tools": 0, "agents": set(), "status": []}
    first_text = threading.Event()

    def on_text(d):
        seen["text"] += len(d)
        first_text.set()
        log(provider, step, "text", d)

    def on_status(s):
        seen["status"].append(s)
        log(provider, step, "status", s)

    def on_tool(c):
        seen["tools"] += 1
        log(provider, step, "tool", tool_frame(c))

    def on_agent(r):
        seen["agents"].add(getattr(r, "id", None) or id(r))
        log(provider, step, "agent", agent_frame(r))

    box = {}

    def go():
        try:
            box["turn"] = brain.ask(prompt, on_text=on_text, on_status=on_status, on_tool=on_tool, on_agent=on_agent)
        except Exception as e:  # noqa: BLE001 - a transport crash is exactly what this is looking for
            box["error"] = repr(e)

    start = time.time()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    steered = None
    if steer_text:
        first_text.wait(60)
        time.sleep(2)
        steered = brain.steer(steer_text)
        log(provider, step, "steer", {"accepted": steered, "text": steer_text})
    t.join(TURN_S)
    if t.is_alive():
        brain.cancel()
        t.join(10)
        box.setdefault("error", f"timed out after {TURN_S}s")
    turn = box.get("turn")
    result = {
        "secs": round(time.time() - start, 1),
        "text_chars": seen["text"],
        "tool_events": seen["tools"],
        "agents": len(seen["agents"]),
        "steer_accepted": steered,
        "error": box.get("error") or (turn.error if turn and turn.is_error else ""),
        "reply": (turn.text if turn else "")[-400:],
        "session": turn.session_id if turn else None,
        "model": turn.model if turn else None,
    }
    log(provider, step, "result", result)
    return result


def verdict(step: str, r: dict) -> str:
    if r["error"]:
        return "FAIL"
    checks = {
        "stream+tools": r["text_chars"] > 0 and r["tool_events"] > 0,
        "steer": bool(r["steer_accepted"]) and "STEERED" in r["reply"],
        "subagents": r["agents"] > 0,
        "denied": r["text_chars"] > 0,  # read the reply: it should say the command was refused
    }
    return "ok" if checks.get(step) else "CHECK"


def main() -> int:
    args = sys.argv[1:]
    providers = [a for a in args if a in ("codex", "grok")] or ["codex", "grok"]
    only = {a for a in args if a not in ("codex", "grok")}
    cfg = load_config()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for provider in providers:
        model = provider_default(provider)
        if not model:
            print(f"{provider}: no model list yet, skipped")
            continue
        bc = copy.deepcopy(cfg.brain)
        bc.model, bc.effort, bc.workdir = model, "low", REPO
        brain = create_brain(bc)
        print(f"\n{provider} ({model}, transport {getattr(getattr(bc, provider), 'transport', '?')})")
        for step, prompt, steer_text in STEPS:
            if only and step not in only:
                continue
            r = run_step(brain, provider, step, prompt, steer_text)
            print(f"  {verdict(step, r):5} {step:13} {r['secs']:6}s  text={r['text_chars']} tools={r['tool_events']} "
                  f"agents={r['agents']} steer={r['steer_accepted']} model={r['model']} {r['error'][:120]}")
            if step == "denied":
                print(f"        reply: {r['reply'][-200:]!r}")
    print(f"\nfull log: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
