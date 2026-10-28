"""A stand-in for `vision serve` for the iPhone app's UI tests: it plays out a scene on cue and records what
the app sends back, with no model behind it.

    .venv/bin/python ios/fake_server.py --port 8799

The UI tests drive it over plain HTTP (the test runner can make requests; the app under test can't see them):
  POST /_scene {"chats": [snapshot…], "history": {chat: [item…]}, "status": {rows, problems}, "voices": {voice, voices},
                "projects": [project…], "threads": {project: [thread…]}, "memory": {project: [line…]},
                "library": {project: [item…]}, "thread_history": {thread: [{role, text}…]},
                "activity": {project: [event…]} (a project may carry scout, mood, leash, rules, approvals, checkin),
                "protocol": n or null, "update": "what to run", "shell": bool, "voice_on": bool, "read_aloud": bool,
                "widgets": {threads, usage, next} (what GET /widgets answers; None: a Vision from before it, 404)}
                                                                     reset: what the next hello and /health carry
  POST /_push  {frame}                                               send a frame to every connected app
  GET  /_frames                                                      every frame the app sent, oldest first
Everything else answers the way the app expects enough of for the screen to draw (history, uploads).
"""
from __future__ import annotations

import argparse
import asyncio
import io

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

app = FastAPI()
PROTOCOL = 1  # the app's VisionClient.protocolVersion: a scene that says nothing fits the app
scene: dict = {"chats": [], "history": {}, "status": None}
frames: list[dict] = []
sockets: set[WebSocket] = set()


def _jpeg() -> bytes:
    """A small solid picture for any upload the app asks for."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (120, 90), (220, 140, 60)).save(out, "JPEG")
    return out.getvalue()


@app.post("/_scene")
async def set_scene(request: Request) -> dict:
    body = await request.json()
    scene["chats"] = body.get("chats", [])
    scene["history"] = body.get("history", {})
    scene["status"] = body.get("status")  # None: a Vision from before /health reported it
    scene["folders"] = body.get("folders", {})  # path ("" = where the picker starts) → that folder's listing
    scene["voices"] = body.get("voices")  # what /voice answers; None: a Vision from before the voice could be picked
    scene["projects"] = body.get("projects", [])
    # project id → its threads, oldest first; each filled out to the real server's shape
    scene["threads"] = {pid: [_thread(pid, t) for t in ts] for pid, ts in (body.get("threads") or {}).items()}
    scene["memory"] = body.get("memory") or {}  # project id → its memory lines ("- 2026-10-11: fact")
    scene["library"] = body.get("library") or {}  # project id → its Library items
    scene["activity"] = body.get("activity") or {}  # project id → its activity events, newest first
    scene["thread_history"] = body.get("thread_history") or {}  # thread id → its conversation [{role, text}]
    scene["protocol"] = body.get("protocol", PROTOCOL)  # None: a Vision from before the number
    scene["update"] = body.get("update")  # what to run on the computer when a newer Vision is out
    scene["shell"] = body.get("shell", False)  # hello's "shell": the computer runs shell-mode commands
    scene["voice_on"] = body.get("voice_on")  # hello's "voice_on" (None: a Vision from before the voice switch)
    scene["read_aloud"] = body.get("read_aloud", False)  # hello's "read_aloud": /speak/pieces is there
    scene["widgets"] = body.get("widgets")  # what GET /widgets answers; None: a Vision from before the widgets
    frames.clear()
    return {"ok": True}


def _thread(pid: str, given: dict) -> dict:
    """A thread as the real server sends one (vision/projects.py), from the few fields a test names."""
    thread = {"id": "", "project": pid, "title": "", "objective": "", "state": "idle", "queued": False, "edits": True,
              "run_id": None, "provider": "claude", "model": "Opus", "effort": "high", "session_id": None,
              "workdir": "/home/me/code/vision", "folder": "~/code/vision", "branch": None, "worktree": None,
              "created": 0.0, "updated": 0.0, "activity": "", "tools": 0, "last_result": None, "question": None,
              "error": None, "note": None, "tokens_out": 0, "runs": [], "context": None}
    thread.update(given)
    return thread


def _threads(pid: str) -> list[dict]:
    return scene.setdefault("threads", {}).setdefault(pid, [])


def _counted(project: dict) -> dict:
    """A project with its threads counted by state, as the hello and `project` frames carry it."""
    counts = {s: 0 for s in ("working", "waiting_on_you", "ready_for_review", "landing", "idle", "resolved")}
    for t in (scene.get("threads") or {}).get(project["id"], []):
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    return {**project, "threads": counts}


async def _send_all(frame: dict) -> None:
    for ws in list(sockets):
        try:
            await ws.send_json(frame)
        except Exception:  # noqa: BLE001
            sockets.discard(ws)


async def _changed(thread: dict) -> None:
    """What the real server sends on every change to a thread: the thread (without runs) and its project's counts."""
    await _send_all({"type": "thread", "project": thread["project"],
                     "thread": {k: v for k, v in thread.items() if k not in ("runs", "context")}})
    project = next((p for p in scene.get("projects", []) if p["id"] == thread["project"]), None)
    if project is not None:
        await _send_all({"type": "project", "project": _counted(project)})


@app.post("/voice/power")
async def voice_power(request: Request) -> dict:
    body = await request.json()
    frames.append({"type": "_voice_power", **body})
    scene["voice_on"] = bool(body.get("on"))
    await _send_all({"type": "voice_power", "on": scene["voice_on"]})
    return {"on": scene["voice_on"]}


@app.post("/_push")
async def push(request: Request) -> dict:
    await _send_all(await request.json())
    return {"sent": len(sockets)}


@app.get("/_frames")
async def get_frames() -> list[dict]:
    return frames


@app.get("/health")
async def health() -> dict:
    h = {"version": "fake", "workdir": "/tmp", "chats": []}
    if scene["status"] is not None:
        h["status"] = scene["status"]
    return h


@app.get("/history")
async def history(chat: str = "", limit: int = 60) -> list[dict]:
    return scene["history"].get(chat, [])


@app.get("/search")
async def search(q: str = "", limit: int = 30) -> list[dict]:
    """vision/server.py Hub.search over the scene: chats whose message holds every word, newest message's text."""
    words = q.lower().split()
    frames.append({"type": "_search", "q": q})
    if len(q.strip()) < 2 or not words:
        return []
    hits = []
    for c in scene["chats"]:
        rows = scene["history"].get(c["chat"], [])
        row = next((r for r in reversed(rows) if all(w in str(r.get("text", "")).lower() for w in words)), None)
        if row:
            hits.append({"chat": c["chat"], "title": c.get("title", ""), "project": c.get("project", ""),
                         "role": row.get("role", ""), "snippet": " ".join(str(row["text"]).split())[:140]})
    return hits[:limit]


@app.get("/providers")
async def providers() -> dict:
    return {"providers": []}


@app.get("/folders")
async def folders(path: str = "") -> JSONResponse:
    listing = (scene.get("folders") or {}).get(path)
    return JSONResponse(listing) if listing is not None else JSONResponse({"detail": "no such folder"}, status_code=404)


MODELS = {"tabs": [{"tab": "Claude", "models": [{"id": "opus", "label": "Opus 5.5", "description": "Most capable"},
                                                {"id": "sonnet", "label": "Sonnet 5.5", "description": "Fast and capable"}]},
                   {"tab": "Codex", "models": [{"id": "gpt-5.5-codex", "label": "GPT-5.5 Codex", "description": "OpenAI's coding model"}]}],
          "efforts": [{"id": e, "label": e.capitalize(), "description": ""} for e in ("low", "medium", "high", "max")]}


@app.get("/models")
async def models(chat: str = "", model: str = "", effort: str = "") -> dict:
    """The model picker's list (the desktop app asks it; the phone's UI tests don't)."""
    current = model or next((c.get("model_id") for c in scene["chats"] if c.get("chat") == chat), None) or "opus"
    return {**MODELS, "current": current, "effort": effort or "high"}


@app.get("/voice/rate")
async def voice_rate() -> JSONResponse:
    return JSONResponse({"rate": 1.0}) if scene.get("voices") else JSONResponse({"detail": "not in the fake"}, status_code=404)


@app.get("/voice")
async def voice() -> JSONResponse:
    return JSONResponse(scene["voices"]) if scene.get("voices") else JSONResponse({"detail": "not in the fake"}, status_code=404)


def _silence(seconds: float) -> bytes:
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000)
        w.writeframes(bytes(2 * int(24000 * seconds)))
    return out.getvalue()


@app.get("/voice/sample")
async def voice_sample(voice: str = "") -> Response:
    """Three seconds of silence as that voice's sample; /voice then says which was asked for last ("sampled")."""
    if not scene.get("voices"):
        return JSONResponse({"detail": "not in the fake"}, status_code=404)
    scene["voices"]["sampled"] = voice
    return Response(_silence(3), media_type="audio/wav")


@app.get("/widgets")
async def widgets() -> JSONResponse:
    """The Home Screen widgets' answer, filled out from the scene's `widgets`; 404 as a Vision from before them."""
    import time

    given = scene.get("widgets")
    if given is None:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    frames.append({"type": "_widgets"})
    threads = {"working": 0, "needsYou": 0, "ready": 0, "rows": [], "projects": 0, **(given.get("threads") or {})}
    upcoming = given.get("upcoming") or ([given["next"]] if given.get("next") else [])
    return JSONResponse({"name": "fake", "at": time.time(), "usage_at": time.time(), "usage": given.get("usage") or [],
                         "next": given.get("next"), "upcoming": upcoming, "threads": threads,
                         "chats": given.get("chats") or [], "computer": given.get("computer"), "scouts": given.get("scouts") or []})


@app.post("/speak/pieces")
async def speak_pieces(request: Request) -> dict:
    """A reply cut at its sentences, as the real one (tts.read_aloud_pieces) roughly does; recorded as `_speak_pieces`."""
    import re

    text = (await request.json()).get("text") or ""
    frames.append({"type": "_speak_pieces", "text": text})
    return {"pieces": [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]}


@app.post("/speak")
async def speak(request: Request) -> Response:
    """Three seconds of silence per piece, recorded as `_speak`; with the scene's voice off, the real server's 503."""
    body = await request.json()
    frames.append({"type": "_speak", **body})
    if scene.get("voice_on") is False:
        return JSONResponse({"detail": "the voice is off on this computer"}, status_code=503)
    return Response(_silence(3), media_type="audio/wav")


@app.post("/voice")
async def set_voice(request: Request) -> JSONResponse:
    if not scene.get("voices"):
        return JSONResponse({"detail": "not in the fake"}, status_code=404)
    scene["voices"]["voice"] = (await request.json()).get("voice")
    return JSONResponse(scene["voices"])


@app.get("/uploads/{name}")
async def upload(name: str) -> Response:
    return Response(_jpeg(), media_type="image/jpeg")


@app.get("/projects")
async def projects_list() -> list[dict]:
    return [_counted(p) for p in scene.get("projects", [])]


@app.post("/projects")
async def projects_save(request: Request) -> dict:
    """A new project as the real server makes one, or an edit (`id`: the keys given change, settings merged);
    the app opens a new one's chat next."""
    body = await request.json()
    frames.append({"type": "_post_projects", **body})
    if body.get("id"):
        project = _project(body["id"])
        if project is None:
            return JSONResponse({"detail": "no such project"}, status_code=404)
        project.update({k: v for k, v in body.items() if k in ("goal", "title", "instructions")})
        if body.get("scout"):
            project["scout"] = {**(project.get("scout") or {}), **body["scout"]}
        settings = dict(body.get("settings") or {})
        rules = settings.pop("rules", None)
        project["settings"] = {**(project.get("settings") or {}), **settings}
        if "leash" in settings:
            project["leash"] = settings["leash"]
        for action, rule in (rules or {}).items():  # as the real server answers: each action's rule, and whether it was set
            for r in project.get("rules") or []:
                if r["action"] == action:
                    r["rule"], r["set"] = (r.get("default", "asks"), False) if rule == "default" else (rule, True)
        await _send_all({"type": "project", "project": _counted(project)})
        return _counted(project)
    project = {"id": f"p{len(scene.setdefault('projects', [])) + 1}", "title": body.get("title") or body.get("goal", "")[:60],
               "goal": body.get("goal", ""), "instructions": body.get("instructions", ""), "chat": "", "status": "active",
               "workdirs": [body["workdir"]] if body.get("workdir") else [], "folder": body.get("workdir", ""), "updated": 0,
               "scout": {"name": "Pip", "hue": 205, "trait": "antenna", **(body.get("scout") or {})}, "mood": "asleep",
               "leash": (body.get("settings") or {}).get("leash", "checkins"), "approvals": [], "rules": []}
    scene["projects"].insert(0, project)
    return project


@app.post("/projects/{pid}/approvals/{aid}")
async def approvals_decide(pid: str, aid: str, request: Request) -> JSONResponse:
    """A scout's request approved or denied: gone from its project's `approvals`, as the real server does."""
    body = await request.json()
    project = _project(pid)
    approval = next((a for a in (project or {}).get("approvals") or [] if a["id"] == aid), None)
    if approval is None:
        return JSONResponse({"detail": f"no approval {aid!r}"}, status_code=404)
    frames.append({"type": "_decide", "project": pid, "approval": aid, **body})
    project["approvals"] = [a for a in project["approvals"] if a["id"] != aid]
    decided = {**approval, "state": "approved" if body.get("decision") == "approve" else "denied",
               **({"edited": body["command"]} if body.get("command") else {})}
    await _send_all({"type": "approval", "project": pid, "approval": decided})
    await _send_all({"type": "project", "project": _counted(project)})
    return JSONResponse(decided)


@app.post("/projects/{pid}/checkin")
async def checkin_now(pid: str) -> JSONResponse:
    project = _project(pid)
    if project is None:
        return JSONResponse({"detail": "no such project"}, status_code=404)
    frames.append({"type": "_checkin", "project": pid})
    return JSONResponse({"chat": project.get("chat") or ""})


@app.post("/projects/{pid}/checkins")
async def checkins_set(pid: str, request: Request) -> JSONResponse:
    body = await request.json()
    project = _project(pid)
    if project is None:
        return JSONResponse({"detail": "no such project"}, status_code=404)
    frames.append({"type": "_checkins", "project": pid, **body})
    checkin = project.setdefault("checkin", {"schedule": None, "last": None, "running": None})
    checkin["schedule"] = None if body.get("repeat") == "off" else {"id": "k1", "enabled": True, "next_run": None, **body}
    await _send_all({"type": "project", "project": _counted(project)})
    return JSONResponse(_counted(project))


@app.get("/projects/{pid}/activity")
async def activity(pid: str) -> dict:
    return {"events": (scene.get("activity") or {}).get(pid, [])}


@app.delete("/projects/{pid}")
async def projects_archive(pid: str) -> JSONResponse:
    project = next((p for p in scene.get("projects", []) if p["id"] == pid), None)
    if project is None:
        return JSONResponse({"detail": "no such project"}, status_code=404)
    scene["projects"].remove(project)
    frames.append({"type": "_delete_project", "project": pid})
    return JSONResponse({**project, "status": "archived"})


@app.get("/projects/{pid}/threads")
async def threads_list(pid: str) -> list[dict]:
    return _threads(pid)


@app.get("/projects/{pid}/threads/{tid}")
async def threads_one(pid: str, tid: str) -> JSONResponse:
    thread = next((t for t in _threads(pid) if t["id"] == tid), None)
    return JSONResponse(thread) if thread else JSONResponse({"detail": "no such thread"}, status_code=404)


@app.post("/projects/{pid}/threads")
async def threads_start(pid: str, request: Request) -> dict:
    """A new thread, working straight away (nothing runs it in the fake)."""
    body = await request.json()
    frames.append({"type": "_post_thread", "project": pid, **body})
    threads = _threads(pid)
    objective = body.get("objective", "")
    thread = _thread(pid, {"id": f"t{len(threads) + 1}", "title": body.get("title") or objective[:60], "objective": objective,
                           "state": "working", "edits": body.get("edits", True), "activity": "starting",
                           "created": 1.0, "updated": 1.0})
    threads.append(thread)
    await _changed(thread)
    return thread


@app.get("/projects/{pid}/threads/{tid}/history")
async def threads_history(pid: str, tid: str) -> dict:
    return {"messages": (scene.get("thread_history") or {}).get(tid, [])}


@app.post("/projects/{pid}/threads/{tid}/{action}")
async def threads_act(pid: str, tid: str, action: str, request: Request) -> JSONResponse:
    """tell (and message, the phone's own), stop, resolve and land, as the real server answers them: the thread
    as it is now, or a 409 saying why not."""
    thread = next((t for t in _threads(pid) if t["id"] == tid), None)
    if thread is None:
        return JSONResponse({"detail": "no such thread"}, status_code=404)
    if action not in ("tell", "message", "stop", "resolve", "land"):
        return JSONResponse({"detail": "not in the fake"}, status_code=404)
    body = await request.json()
    frames.append({"type": f"_thread_{action}", "project": pid, "thread": tid, **body})
    if action in ("tell", "message") and thread["state"] == "resolved":
        # A closed thread stays closed, as on the machine: a new thread carries it on, and is what comes back.
        threads = _threads(pid)
        follow = _thread(pid, {"id": f"t{len(threads) + 1}", "title": thread["title"], "objective": body.get("text", ""),
                               "state": "working", "edits": thread["edits"], "activity": "reading your message",
                               "follows": tid, "created": 2.0, "updated": 2.0})
        threads.append(follow)
        thread["followed_by"] = follow["id"]
        await _changed(thread)
        await _changed(follow)
        return JSONResponse(follow)
    if action in ("tell", "message"):
        thread.update(state="working", queued=False, question=None, activity="reading your message")
    elif action == "stop":
        if thread["state"] != "working":
            return JSONResponse({"detail": "That thread isn't working."}, status_code=409)
        thread.update(state="idle", queued=False, activity="", note="Stopped from the phone.")
    elif action == "land":
        if not thread.get("branch") or thread["state"] not in ("ready_for_review", "idle"):
            return JSONResponse({"detail": "it has no finished branch to land"}, status_code=409)
        thread.update(state="resolved", activity="", note=f"landed {thread['branch']}")
    else:
        thread.update(state="resolved", queued=False, activity="")
    await _changed(thread)
    return JSONResponse(thread)


def _project(pid: str) -> dict | None:
    return next((p for p in scene.get("projects", []) if p["id"] == pid), None)


@app.get("/projects/{pid}/memory")
async def memory_list(pid: str) -> dict:
    return {"facts": scene.setdefault("memory", {}).setdefault(pid, [])}


@app.post("/projects/{pid}/memory")
async def memory_add(pid: str, request: Request) -> dict:
    body = await request.json()
    frames.append({"type": "_remember", "project": pid, **body})
    facts = scene.setdefault("memory", {}).setdefault(pid, [])
    facts.append(f"- 2026-10-11: {body.get('fact', '')}")
    return {"added": [facts[-1]], "facts": facts}


@app.delete("/projects/{pid}/memory/{number}")
async def memory_forget(pid: str, number: int) -> JSONResponse:
    facts = scene.setdefault("memory", {}).setdefault(pid, [])
    if not 1 <= number <= len(facts):
        return JSONResponse({"detail": f"there's no fact {number}"}, status_code=404)
    gone = facts.pop(number - 1)
    frames.append({"type": "_forget", "project": pid, "fact": gone})
    return JSONResponse({"forgot": gone, "facts": facts})


@app.post("/projects/{pid}/{action}")  # after every other POST under a project: it takes any last segment
async def projects_pause(pid: str, action: str) -> JSONResponse:
    """pause and resume."""
    project = _project(pid)
    if project is None or action not in ("pause", "resume"):
        return JSONResponse({"detail": "not in the fake"}, status_code=404)
    project["status"] = "paused" if action == "pause" else "active"
    frames.append({"type": f"_project_{action}", "project": pid})
    await _send_all({"type": "project", "project": _counted(project)})
    return JSONResponse(_counted(project))


@app.get("/library")
async def library(project: str = "") -> list[dict]:
    """A project's Library as the scene names it (the phone's own Library isn't in the fake)."""
    return (scene.get("library") or {}).get(project, []) if project else []


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def anything_else(path: str) -> JSONResponse:
    return JSONResponse({"detail": "not in the fake"}, status_code=404)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    sockets.add(websocket)
    try:
        hello = {"type": "hello", "chats": scene["chats"], "update": scene.get("update"),
                 "projects": [_counted(p) for p in scene.get("projects", [])]}
        if scene.get("shell"):
            hello["shell"] = True
        if scene.get("read_aloud"):
            hello["read_aloud"] = True
        if scene.get("voice_on") is not None:
            hello["voice_on"] = scene["voice_on"]
        if scene.get("protocol", PROTOCOL) is not None:
            hello["protocol"] = scene.get("protocol", PROTOCOL)
        await websocket.send_json(hello)
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") != "ping":
                frames.append(msg)
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(websocket)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
