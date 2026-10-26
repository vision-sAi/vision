"""A stand-in for `grok agent stdio` in tests/test_grok_acp.py: speaks the slice of ACP Vision uses,
with the shapes a real grok 1.0.41 sends (see vision/grok_acp.py).

A prompt reading "run <command>" asks permission for that command first and reports the outcome;
"slow" pauses mid-turn so a test can interject; "limit" fails like an exhausted free tier.
Everything it receives is appended to $FAKE_GROK_LOG as JSON lines, with its argv and GROK_SANDBOX."""
import json
import os
import sys
import threading
import time

log = open(os.environ["FAKE_GROK_LOG"], "a", encoding="utf-8")
log.write(json.dumps({"argv": sys.argv[1:], "sandbox": os.environ.get("GROK_SANDBOX")}) + "\n")
log.flush()
lock = threading.Lock()
interjections: list[str] = []
answers: dict = {}
next_id = [1000]
SID = "01a0e2cd-0000-7000-8000-000000000001"


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def update(u, total=None):
    params = {"sessionId": SID, "update": u}
    if total:
        params["_meta"] = {"totalTokens": total}
    send({"jsonrpc": "2.0", "method": "session/update", "params": params})


def chunk(text):
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}, total=1234)


def ask_permission(command):
    next_id[0] += 1
    rid = next_id[0]
    event = answers[rid] = threading.Event()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": SID, "toolCall": {"toolCallId": "c1", "title": "run_terminal_command", "kind": "execute", "rawInput": {"command": command},
                                       "_meta": {"x.ai/tool": {"name": "run_terminal_command", "kind": "execute"}}},
        "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}, {"optionId": "always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
    event.wait(10)
    return answers.pop(rid + 10**6, {})


def fallback(text):
    """What Grok does with an interject that reaches an idle session: runs it as a prompt."""
    send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": [], "runningPromptId": "interject-fallback-1", "runningText": text}})
    time.sleep(0.2)
    chunk(f" Late: {text}.")
    send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": []}})
    send({"jsonrpc": "2.0", "method": "_x.ai/session/prompt_complete", "params": {"sessionId": SID, "promptId": "interject-fallback-1", "stopReason": "end_turn"}})


running = [False]


def run_prompt(rid, text):
    running[0] = True
    send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": [], "runningPromptId": "p1", "runningText": text}})
    if text == "limit":
        send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": []}})
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32003, "message": "Rate limited", "data": "API error (status 429 Too Many Requests): free usage exhausted"}})
        return
    update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
    if text.startswith("run "):
        command = text[4:]
        update({"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "run_terminal_command", "rawInput": {"command": command, "description": "the command"},
                "_meta": {"x.ai/tool": {"name": "run_terminal_command", "kind": "execute", "label": "Run Command"}}})
        outcome = ask_permission(command).get("outcome") or {}
        ok = outcome.get("optionId") == "yes"
        update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed" if ok else "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "hi\n" if ok else "rejected"}}]})
        if not ok:  # A rejected permission ends the turn as cancelled without a reply.
            end_turn(rid, "cancelled")
            return
        chunk(f"permission {outcome.get('optionId') or outcome.get('outcome')}.")
    elif text == "late":  # the reply is over in Grok, the prompt's answer not yet sent: an interject now is a turn of its own
        chunk("Done.")
        send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": []}})
        running[0] = False
        update({"sessionUpdate": "tool_call", "toolCallId": "t9", "title": "todo_write", "rawInput": {}})
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t9", "status": "completed"})
        time.sleep(0.5)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return
    elif text == "slow":
        chunk("Working on it.")
        for _ in range(40):
            if interjections:
                break
            time.sleep(0.05)
        for said in interjections:
            chunk(f" Noted: {said}.")
    else:
        chunk("Hello ")
        chunk("there.")
    end_turn(rid, "end_turn")


def end_turn(rid, stop):
    send({"jsonrpc": "2.0", "method": "_x.ai/queue/changed", "params": {"sessionId": SID, "entries": []}})
    send({"jsonrpc": "2.0", "method": "_x.ai/session_notification", "params": {"sessionId": SID, "update": {
        "sessionUpdate": "turn_completed", "prompt_id": "p1", "stop_reason": stop,
        "usage": {"inputTokens": 100, "outputTokens": 7, "cachedReadTokens": 50, "cacheCreationTokens": 0, "reasoningTokens": 3}}}})
    send({"jsonrpc": "2.0", "method": "_x.ai/session/prompt_complete", "params": {"sessionId": SID, "promptId": "p1", "stopReason": stop}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": stop}})


for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps(msg) + "\n")
    log.flush()
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None and rid in answers:  # Vision's answer to a permission request
        answers[rid + 10**6] = msg.get("result") or {}
        answers[rid].set()
    elif method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "authMethods": [{"id": "cached_token"}]}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "method": "_x.ai/session/setup", "params": {"method": "session/new", "phase": "auth"}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": SID}})
    elif method == "session/load":
        if params.get("sessionId") != SID:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params", "data": "session not found"}})
        else:
            chunk("OLD REPLY FROM HISTORY")  # the replay: not part of the new turn
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        text = "".join(b.get("text", "") for b in params.get("prompt") or [])
        threading.Thread(target=run_prompt, args=(rid, text), daemon=True).start()
    elif method == "_x.ai/interject":
        if running[0]:
            interjections.append(params.get("text") or "")
            send({"jsonrpc": "2.0", "id": rid, "result": {"result": {"status": "interjected"}}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"result": {"status": "queued"}}})
            threading.Thread(target=fallback, args=(params.get("text") or "",), daemon=True).start()
    elif method == "session/cancel":
        pass
