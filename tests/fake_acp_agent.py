"""A stand-in for any ACP agent (Gemini CLI and the like) in tests/test_acp.py: the protocol's standard
slice only, nothing vendor-specific (compare tests/fake_grok_agent.py).

A prompt reading "run <command>" asks permission for that command first; "slow" waits for a cancel;
anything else answers with a read tool call and two text chunks. Every message received is appended to
$FAKE_ACP_LOG as JSON lines, with its argv first."""
import json
import os
import sys
import threading

log = open(os.environ["FAKE_ACP_LOG"], "a", encoding="utf-8")
log.write(json.dumps({"argv": sys.argv[1:], "env_mark": os.environ.get("FAKE_ACP_MARK")}) + "\n")
log.flush()
lock = threading.Lock()
answers: dict = {}
next_id = [500]
SID = "acp-session-0001"
KNOWN = {SID}
cancelled = threading.Event()


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
    update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}, total=321)


def ask_permission(command):
    next_id[0] += 1
    rid = next_id[0]
    event = answers[rid] = threading.Event()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": SID, "toolCall": {"toolCallId": "t1", "title": f"Run `{command}`", "kind": "execute", "rawInput": {"command": command}},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}, {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
    event.wait(10)
    return answers.pop(rid + 10**6, {})


def run_prompt(rid, text):
    cancelled.clear()
    instructed = "<instructions>" in text
    if instructed:
        text = text.rsplit("\n", 1)[-1]  # the user's words come last, after Vision's persona block
    if text.startswith("run "):
        command = text[4:]
        update({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": f"Run `{command}`", "kind": "execute", "rawInput": {"command": command}})
        outcome = ask_permission(command).get("outcome") or {}
        ok = outcome.get("optionId") == "allow"
        update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed" if ok else "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "ran fine\n" if ok else "the user said no"}}]})
        if not ok:
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})  # what a strict agent does with a refusal
            return
        chunk("Done: it ran.")
    elif text == "slow":
        chunk("Working…")
        cancelled.wait(10)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        return
    else:
        update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        update({"sessionUpdate": "tool_call", "toolCallId": "r1", "title": "Read README.md", "kind": "read", "rawInput": {"path": "README.md"}})
        update({"sessionUpdate": "tool_call_update", "toolCallId": "r1", "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "# Hi"}}]})
        chunk("Hello ")
        chunk("with instructions." if instructed else "from the agent.")
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps(msg) + "\n")
    log.flush()
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None and rid in answers:
        answers[rid + 10**6] = msg.get("result") or {}
        answers[rid].set()
    elif method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": SID}})
    elif method == "session/load":
        if params.get("sessionId") not in KNOWN:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "session not found"}})
        else:
            chunk("OLD REPLY FROM HISTORY")  # the replay: not part of the new turn
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        text = "".join(b.get("text", "") for b in params.get("prompt") or [])
        threading.Thread(target=run_prompt, args=(rid, text), daemon=True).start()
    elif method == "session/cancel":
        cancelled.set()
