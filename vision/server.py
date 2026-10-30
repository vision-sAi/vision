"""Remote access: `vision serve` puts the brain, ears and voice behind HTTP + WebSocket for the iOS app.

One user, several chats. Each chat is an independent conversation with its own agent session, model
and transcript, and chats run at the same time (like Claude Code sessions in the Claude app); the
ears and the voice are shared. The server listens on the LAN by default so the phone can reach it
over the same Wi-Fi; Tailscale Serve takes it to the user's other devices anywhere (a public tunnel,
Tailscale Funnel or Cloudflare Tunnel, to the whole internet). Every
request carries the bearer token from ~/.config/vision/remote_token in its Authorization header (never in
the URL, where proxies would log it); the QR code printed at start-up carries the URL and the token so
the app can pair with one scan.

WebSocket protocol (JSON, one object per frame). Every frame about a chat carries `chat` (its id);
a phone → machine frame without one goes to the most recently active chat.
  phone → machine   message {chat, text, speak, voice, images?: [path…]} (photos, videos and files from /upload) · answer {chat, answers} · cancel {chat}
                   new {model?, effort?, workdir?, project?} (workdir: the folder the chat works in, as GET /folders lists them;
                     project: open that project's chat, or switch to it when it is already open) · close {chat} · resume {session_id, provider?} · model {chat, model, effort} · ping
                   looking (the app came to the front: look for a newer Vision now, as connecting does)
                   mode {chat, mode} ("auto" or "plan", as /mode in the terminal; refused while the chat is replying)
                   hush {chat?} (a call hung up: no more speech for that chat's reply, every reply with no chat; the text carries on)
                   open_terminal {chat} (a terminal window here running `vision --join <chat>`; one per chat:
                     a chat that already has one gets it brought forward, on Hyprland, and a toast)
  machine → phone   hello {version, workdir, chats: [chat…], projects: [project…]}
                   chat {chat, title, model, effort, mode, session_id, busy, waiting, questions, partial, user_text, elapsed_ms, project, …}
                     (sent whenever a chat's state changes; `opened: true` on the reply to new/resume;
                     `elapsed_ms`: how long the running reply has taken, so a phone opening mid-reply times it from there;
                     `project`: the id of the project whose chat this is, "" for an ordinary chat)
                   project {project: {id, title, goal, instructions, workdirs, folder, chat, status, settings, threads,
                     tokens_out, scout, mood, leash, …}} (a project made or changed: projects.py; `threads`: how many
                     threads are in each state; `tokens_out`: what its threads have written; `scout` {name, hue, trait}
                     and `mood` its buddy avatar, sent again when the mood changes; `leash` its autonomy as
                     on_call | checkins | autonomous) · project_removed {project} (archived; …/resume brings it back)
                   activity {project, event: {at, kind, text, thread?}} (a line in the project's activity log)
                   approval {project, approval: {id, target, title, tool, input, command, rule, action, state, …}} (a scout
                     asked leave to run something, or the user decided: projects.py approvals)
                   checkin {project, chat, state: started|done, label, started, ended?, summary?, needs_you?, error?}
                     (a scout's check-in began or ended; GET /history has a finished one as a {role: "checkin"} row)
                   thread {project, thread: {id, title, objective, state, queued, paused, activity, tools, model, effort,
                     folder, branch, last_result, question, error, note, told, …}} (a project thread changed: runs.py;
                     its run log comes from GET /projects/{id}/threads/{tid}, its conversation from …/history)
                   shell {text, running} when a phone's `!` command starts, shell_output {text, output} with what it has
                     printed so far while it runs (the end of it, every quarter second at most), shell {text, output, code} once done
                   chat_closed {chat, moved_to?} · start {text, speak, note?} · delta {text} · status {tool}
                     (`note`: the turn is a project's threads reporting back to its coordinator: show that line as a
                     note, never `text` as the user's words; GET /history has it as a {role: "note"} row too)
                   agent {id, kind, label, done, tools, step?, status?}
                   audio {seq, wav} (base64 WAV per sentence) · audio_end
                   question {questions} · done {text, error, session_id, duration_ms} · note {text}
                   error {text} · toast {text} (to the asker only; a scheduled run toasts everyone) · pong
                   scheduled (the task list changed or a task ran: refetch GET /scheduled)
POST /speak {text, spoken?} is `text` as a WAV in the voice; POST /speak/pieces {text} cuts a finished reply into
the pieces the phone's read-aloud button asks /speak for one by one (or says itself when the voice is off).
GET /widgets is what the phone's Home Screen widgets show: {name, at, threads {working, needsYou, ready, rows, projects},
usage [{provider, label, windows}], usage_at, next {id, title, kind, at, repeat} | None, upcoming [next…] (three),
chats [{chat, title, busy, last, updated}] (the three last spoken in), computer {name, cpu, memory, disk, uptime,
voice_on, update}}.
GET /folders?path= lists a folder's subfolders for the phone's new-chat picker (no path: the default
folder, with the recent ones); a chat's summary says where it works (`workdir`, and `folder` with ~).
GET /history rows are {role, text, error?, agents?, tools?, duration_ms?}: a reply finished while this
process has been up keeps how long its turn took (the phone's footer, like the terminal's).
GET /search?q=&limit= finds what was said in the open chats: [{chat, title, project, role, snippet}], one per
chat whose message (or title) holds every word of q, newest chat first, the snippet around the first hit.
Projects (projects.py, runs.py): /projects (list, make or edit), /projects/adopt (POST {chat} or {session_id,
provider?}: an open chat becomes a project's, as it is), /projects/{id} (one; DELETE archives),
…/open, …/pause, …/resume, …/delete (a retired one, for good), …/memory (GET, POST {fact}, DELETE …/memory/{n}), …/activity (GET ?limit=&since=:
the events, newest first), …/checkin (POST: check in now), …/checkins (POST the check-in schedule), …/approvals
(GET; POST …/approvals/{aid} {decision: approve|deny, command?}), …/threads (list, POST starts
one {objective, title?, edits?, after?}), …/threads/{tid} (one, with its run log) and its …/tell, …/message
(the user's own, from the phone), …/stop, …/resolve, …/land, …/diff (what landing brings in; ?path= for one
file's patch) and …/history; GET /library?project= is a
project's Library and DELETE /library?path=&project= takes an item out of it.
Events are broadcast to every connected client, so a phone that reconnects mid-reply picks it up.
Message frames received while a chat is busy are queued on that chat and run in arrival order.

Chats carry `source`: "server" for one this process runs, "terminal" for a `vision` chat open in a
terminal on this machine (link.py). Terminal chats are found on disk, driven over their socket
and leave the list when the terminal quits; `close` quits that terminal (as /quit typed there would).
Closing the last chat leaves none open; the phone's next message (or +) opens one.

The other way round, a terminal `vision` can join one of this process's chats (remote.py): it dials
the same WebSocket (`/ws?client=terminal&chat=<id>&pid=<its pid>`) and follows the frames like a
phone; the chat and pid say which chat already has a window here. While running, this process leaves ~/.local/state/vision/serve.json so a terminal on this machine finds it.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path

# Module-level on purpose: with `from __future__ import annotations` FastAPI resolves the endpoint
# type hints by name, so Request/WebSocket/UploadFile have to be importable from this module's globals.
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from vision import __version__
from vision import agentlog
from vision import selfupdate
from vision.brain import agent_frame, context_figure, tool_frame, warm_soon
from vision.config import CONFIG_DIR, STATE_DIR, Config, save_brain_defaults, saved_brain_defaults
from vision.sessions import claude_history
from vision.planvoice import plan_answer, plan_question, plan_summary
from vision.projects import APPROVAL_PREFIX, CHECKIN_PREFIX, REPORT_PREFIX
from vision.reply import status_label
from vision.tts import SAMPLE_RATE, Speaker, StreamingSpeaker, log_voice_line

UPLOAD_DIR = Path.home() / ".local" / "share" / "vision" / "uploads"


# What a window needs from the desktop session, when this process was started outside it.
DESKTOP_VARS = ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
                "DBUS_SESSION_BUS_ADDRESS", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK")


def desktop_env() -> dict[str, str]:
    """This process's environment, with the desktop session's display in it when it has none of its own.
    The background service starts at login, before there is a desktop, and a restart in place keeps that
    environment; the desktop hands its variables to the user's systemd once it is up (KDE and GNOME do,
    and a compositor set up to), so that is asked."""
    env = dict(os.environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    exe = shutil.which("systemctl")
    if not exe:
        return env
    try:
        r = subprocess.run([exe, "--user", "show-environment"], capture_output=True, text=True, timeout=3, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired):
        return env
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, sep, value = line.partition("=")
        if sep and name in DESKTOP_VARS and name not in env:
            env[name] = value
    return env


def open_terminal(chat_id: str, workdir: str) -> str:
    """Open a terminal window on this machine with `vision --join <chat>` in it (the phone's
    "Open on computer"). Needs a desktop session on this machine (desktop_env finds it for a server
    started outside one). Returns the terminal used; raises RuntimeError when none is found."""
    vision = shutil.which("vision") or "vision"
    cmd = f"{vision} --join {chat_id}"
    cwd = workdir if workdir and os.path.isdir(workdir) else os.path.expanduser("~")
    if sys.platform == "darwin":
        return _open_mac_terminal(f"cd {shlex.quote(cwd)} && {cmd}")
    env = desktop_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no desktop session was found on this machine, so there is nowhere to open a window")
    candidates = [
        (env.get("TERMINAL"), lambda t: [t, "-e", "sh", "-c", cmd]),
        ("ptyxis", lambda t: [t, "--new-window", "-d", cwd, "-x", cmd]),
        ("gnome-terminal", lambda t: [t, f"--working-directory={cwd}", "--", "sh", "-c", cmd]),
        ("kgx", lambda t: [t, f"--working-directory={cwd}", "-e", cmd]),
        ("konsole", lambda t: [t, "--workdir", cwd, "-e", "sh", "-c", cmd]),
        ("kitty", lambda t: [t, "-d", cwd, "sh", "-c", cmd]),
        ("alacritty", lambda t: [t, "--working-directory", cwd, "-e", "sh", "-c", cmd]),
        ("foot", lambda t: [t, "-D", cwd, "sh", "-c", cmd]),
        ("xterm", lambda t: [t, "-e", "sh", "-c", cmd]),
    ]
    for name, argv in candidates:
        if not name or not (path := shutil.which(name)):
            continue
        subprocess.Popen(argv(path), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return os.path.basename(name)
    raise RuntimeError("no terminal emulator found (set $TERMINAL)")


# The command comes in as argv, so nothing in it needs AppleScript quoting.
MAC_TERMINAL_SCRIPTS = {
    "iTerm": 'on run argv\ntell application "iTerm"\nactivate\ncreate window with default profile command '
             '"/bin/zsh -lc " & quoted form of (item 1 of argv)\nend tell\nend run',
    "Terminal": 'on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run',
}


def _open_mac_terminal(shell_cmd: str) -> str:
    """macOS has no DISPLAY to look for: a server in the user's login session can drive a terminal
    app over Apple Events. iTerm when it is installed, Terminal otherwise. The first time, macOS asks
    on the Mac whether Vision may control that app."""
    app = "iTerm" if any(os.path.isdir(p) for p in ("/Applications/iTerm.app", os.path.expanduser("~/Applications/iTerm.app"))) else "Terminal"
    try:
        r = subprocess.run(["osascript", "-e", MAC_TERMINAL_SCRIPTS[app], shell_cmd],
                           capture_output=True, text=True, timeout=15, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{app} did not open a window ({e})") from e
    if r.returncode != 0:
        if "-1743" in r.stderr:
            raise RuntimeError(f"macOS is not letting Vision control {app}; allow it in System Settings > Privacy & Security > Automation")
        raise RuntimeError(f"{app} did not open a window: {r.stderr.strip() or 'osascript failed'}")
    return app


def _parent_pid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat.rsplit(")", 1)[1].split()[1])  # the name in (…) can hold spaces; ppid follows the state
    except (OSError, ValueError, IndexError):
        return 0


def focus_terminal(pid: int) -> bool:
    """Bring forward the window a `vision` process (`pid`) runs in: "Open on computer" for a chat that
    already has one. Hyprland only, whose hyprctl gives each window's pid: the terminal emulator's, an
    ancestor of `vision` (through `sh -c`). Elsewhere, or when the window can't be told apart, nothing."""
    exe = shutil.which("hyprctl")
    env = desktop_env() if exe else {}
    if not exe or not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return False
    try:
        r = subprocess.run([exe, "clients", "-j"], capture_output=True, text=True, timeout=3, encoding="utf-8", env=env)
        clients = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    by_pid: dict[int, list[dict]] = {}
    for c in clients if isinstance(clients, list) else []:
        by_pid.setdefault(c.get("pid"), []).append(c)
    while pid > 1 and pid not in by_pid:
        pid = _parent_pid(pid)
    windows = by_pid.get(pid, [])
    if len(windows) > 1:  # one process drawing several windows (a single-instance terminal): Vision's own title
        from vision.ui import TITLE_SPINNER

        titles = [(c.get("title") or "").lstrip(TITLE_SPINNER + " ") for c in windows]
        windows = [c for c, t in zip(windows, titles) if t == "Vision" or t.startswith("Vision - ")]
    if len(windows) != 1:
        return False
    try:
        r = subprocess.run([exe, "dispatch", "focuswindow", f"address:{windows[0]['address']}"],
                           capture_output=True, text=True, timeout=3, encoding="utf-8", env=env)
    except (OSError, KeyError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "ok"


IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".webp": "image/webp", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v"}
MAX_FILE_BYTES = 100 * 1024 * 1024   # any other file from the phone's Files picker; brains open it with their tools
MAX_VIDEO_FRAMES = 24     # what a brain is handed for one clip, however long
SCENE_THRESHOLD = 0.04    # ffmpeg scene score that counts as "the screen changed" (screen recordings change little)


def drop_uploads(paths) -> None:
    """Delete uploads a message took with it (a queued message removed before it ran), as the Library's
    own delete does: only files it lists, a clip's digest along with it."""
    from vision.library import delete

    for p in paths or []:
        if isinstance(p, str):
            try:
                delete(p, UPLOAD_DIR, image_path_allowed)
            except OSError:
                pass


def with_attachments(text: str, images) -> str:
    """Append the uploaded photos' paths so the brain can open them with its file tools. A video is
    handed over as its frames (brains can read images, not video) plus what was said in it."""
    paths = [p for p in (images or []) if isinstance(p, str) and p.startswith(str(UPLOAD_DIR)) and os.path.isfile(p)]
    if not paths:
        return text
    lines: list[str] = []
    videos = images = files = False
    for p in paths:
        suffix = Path(p).suffix.lower()
        if suffix not in IMAGE_TYPES and suffix not in VIDEO_TYPES:
            files = True
            lines.append(f"[Attached file: {p}]")
            continue
        digest = video_digest(p)
        if digest is None:
            images = True
            lines.append(f"[Attached image: {p}]")
            continue
        videos = True
        frames = digest.get("frames") or []
        lines.append(f"[Attached video: {p}]")
        lines.append(f"[Video length {clock(digest.get('duration', 0))}; {len(frames)} frames in time order, open them to watch it]")
        lines.extend(f"[Frame {clock(t)}: {f}]" for t, f in frames)
        if said := (digest.get("transcript") or "").strip():
            lines.append(f"[Video audio, transcribed: {' '.join(said.split())}]")
    note = "\n".join(lines)
    nudge = "\n".join(line for wanted, line in [
        (images, "Look at the attached image(s)."),
        (videos, "Watch the attached video(s) through their frames."),
        (files, "Read the attached file(s)."),
    ] if wanted)
    return f"{text}\n\n{note}" if text else f"{note}\n\n{nudge}"


def image_path_allowed(path: Path) -> bool:
    """Reply pictures come from the home folder or the temp folders, never the rest of the system. On macOS
    the user's temp folder ($TMPDIR) is not /tmp but /private/var/folders/…, private to that user."""
    import tempfile

    roots = (Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
    return any(path.is_relative_to(root.resolve()) for root in roots)


def clock(seconds: float) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def video_digest(path: str | Path) -> dict | None:
    """What digest_video stored for an uploaded clip, or None when the path isn't a digested video."""
    path = Path(path)
    if path.suffix.lower() not in VIDEO_TYPES:
        return None
    try:
        return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ffmpeg_exe() -> str:
    """The ffmpeg to run: the static one imageio-ffmpeg ships (the serve extra, so every machine has it),
    else one on the PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001  (not installed, or no binary for this platform)
        if exe := shutil.which("ffmpeg"):
            return exe
        raise RuntimeError("ffmpeg is not available: reinstall Vision with the serve extra, or install ffmpeg") from None


def probe_video(ffmpeg: str, path: Path) -> tuple[float, set[str]]:
    """A clip's length in seconds and the kinds of track in it ({"video", "audio"}), read from the header
    `ffmpeg -i` prints (imageio-ffmpeg has no ffprobe)."""
    head = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    if m := re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", head):
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    else:
        duration = 0.0
    kinds = {k.lower() for k in re.findall(r"Stream #\d+:\d+[^\n]*?: (Video|Audio):", head)}
    return duration, kinds


def digest_video(path: Path, transcribe=None) -> dict:
    """Turn an uploaded clip into what a brain can take in: JPEG frames where the picture changes
    (plus evenly spaced ones so a static stretch isn't one frame), capped at MAX_VIDEO_FRAMES, and the
    soundtrack through Whisper. Stored beside the clip as <clip>.json; frames in <clip>-frames/."""
    ffmpeg = ffmpeg_exe()
    duration, kinds = probe_video(ffmpeg, path)
    if "video" not in kinds:
        raise RuntimeError("no video track in that file")
    frames_dir = path.with_name(path.stem + "-frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    fit = "scale='if(gt(iw,ih),min(1568,iw),-2)':'if(gt(iw,ih),-2,min(1568,ih))'"  # long edge ≤ 1568 px, as photos

    # Scene changes (and the first frame); showinfo logs each kept frame's time.
    run = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(path),
         "-vf", f"select='eq(n,0)+gt(scene,{SCENE_THRESHOLD})',showinfo,{fit}", "-fps_mode", "vfr", "-q:v", "3",
         str(frames_dir / "s%04d.jpg")],
        capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read the video: {run.stderr.strip().splitlines()[-1] if run.stderr.strip() else run.returncode}")
    times = [float(line.split("pts_time:")[1].split()[0]) for line in run.stderr.splitlines() if "pts_time:" in line]
    scene = sorted(zip(times, sorted(frames_dir.glob("s*.jpg"))))

    # Evenly spaced frames fill the gaps a scene pass leaves (one every ~4 s, up to 8).
    even = []
    count = min(8, int(duration // 4))
    for k in range(1, count):
        t = duration * k / count
        if any(abs(t - s) < 1.5 for s, _ in scene):
            continue
        out = frames_dir / f"e{k:04d}.jpg"
        subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                        "-frames:v", "1", "-vf", fit, "-q:v", "3", str(out)], capture_output=True)
        if out.is_file():
            even.append((t, out))

    frames = sorted(scene + even)
    if len(frames) > MAX_VIDEO_FRAMES:  # keep the first and last, spread the rest evenly
        step = (len(frames) - 1) / (MAX_VIDEO_FRAMES - 1)
        keep = {round(i * step) for i in range(MAX_VIDEO_FRAMES)}
        frames = [f for i, f in enumerate(frames) if i in keep]
    kept = set()
    named = []
    for i, (t, f) in enumerate(frames):
        final = frames_dir / f"{i:02d}.jpg"
        f.rename(final)
        kept.add(final)
        named.append((round(t, 2), str(final)))
    for stray in frames_dir.glob("*.jpg"):
        if stray not in kept:
            stray.unlink()

    transcript = ""
    if "audio" in kinds and transcribe is not None:
        wav = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                              "-ar", "16000", "-f", "wav", "pipe:1"], capture_output=True).stdout
        if wav:
            transcript = (transcribe(wav) or "").strip()
    digest = {"duration": round(duration, 2), "frames": named, "transcript": transcript}
    path.with_suffix(".json").write_text(json.dumps(digest), encoding="utf-8")
    return digest

TOKEN_FILE = CONFIG_DIR / "remote_token"
QUESTION_TIMEOUT_S = 900  # how long a turn waits for the phone to answer an AskUserQuestion form


# ---------------------------------------------------------------- folders (where a chat works)
FOLDERS_MAX = 500  # subfolders listed for one folder; a bigger one is browsed by its first that many
RECENT_FOLDERS = 8


def tilde(path: str) -> str:
    """A path as the phone shows it: the home directory written as ~."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def project_view(project: dict, threads: dict | None = None) -> dict:
    """A project as the phone and the CLI get it: with its first folder written as ~ (`folder`), how
    many of its threads are in each state (`threads`, runs.py), and its autonomy as the scout's leash."""
    from vision.projects import ACTIONS, clean_settings, effective_rules

    dirs = project.get("workdirs") or []
    settings = clean_settings({}, project.get("settings"))
    rules = effective_rules(project)
    return {**project, "settings": settings, "folder": tilde(dirs[0]) if dirs else "", "threads": threads or {},
            "leash": settings["leash"],
            "rules": [{"action": a, "label": spec["label"], "rule": rules[a], "set": a in settings["rules"]} for a, spec in ACTIONS.items()]}


def thread_view(thread: dict, full: bool = True) -> dict:
    """A project thread as the phone and the CLI get it: its folder written as ~; the run log and the
    context only in `full` (the `thread` frames leave them out, as every tool call sends one)."""
    view = {k: v for k, v in thread.items() if k != "pending"}
    view["folder"] = tilde(thread.get("workdir") or "")
    if not full:
        view.pop("runs", None)
        view.pop("context", None)
        view.pop("brief", None)
    return view


def usable_folder(path: str) -> str:
    """The absolute path of a folder a chat can work in (~ expanded). ValueError says why not."""
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(target):
        raise ValueError(f"not a folder: {tilde(target)}" if os.path.exists(target) else f"no such folder: {tilde(target)}")
    if not os.access(target, os.R_OK | os.X_OK):
        raise ValueError(f"Vision can't open {tilde(target)}")
    return target


def restored_folder(state: dict) -> tuple[str, str]:
    """The folder a chat brought back by a restart worked in (recover()'s state), or "" for the server's own:
    the one its journal saved, else for older journals the one its Claude conversation started in; never a
    folder that is gone. With it, a line for the chat when its folder is gone and it moved to the server's."""
    from vision.sessions import claude_session_cwd

    folder = state.get("workdir") or claude_session_cwd(state.get("session_id") or "")
    if not folder:
        return "", ""
    try:
        return usable_folder(folder), ""
    except ValueError as e:
        return "", f"{e}; this chat works in the default folder instead."


def list_folder(path: str) -> dict:
    """One folder for the phone's picker: itself, its parent ("" at the top) and its subfolders, hidden
    ones left out, by name. ValueError when it is not a folder Vision can open."""
    target = usable_folder(path)
    names = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                try:
                    if not entry.name.startswith(".") and entry.is_dir():
                        names.append(entry.name)
                except OSError:  # a dangling link, a mount that went away
                    continue
    except OSError as e:
        raise ValueError(f"Vision can't read {tilde(target)}: {e.strerror or e}") from None
    names.sort(key=str.lower)
    parent = os.path.dirname(target)
    return {"path": target, "label": tilde(target), "parent": "" if parent == target else parent,
            "folders": [{"name": n, "path": os.path.join(target, n)} for n in names[:FOLDERS_MAX]],
            "more": max(0, len(names) - FOLDERS_MAX)}


def recent_folders(default: str, chats, sessions) -> list[dict]:
    """Where work has been happening, newest first and each once: the default folder, the open chats'
    and the earlier conversations' (those that still exist), for the top of the phone's picker."""
    seen, out = set(), []
    for path in [default, *(c.workdir for c in chats), *(s.cwd for s in sessions)]:
        path = os.path.abspath(os.path.expanduser(path)) if path else ""
        if path and path not in seen and os.path.isdir(path):
            seen.add(path)
            out.append({"path": path, "label": tilde(path)})
        if len(out) >= RECENT_FOLDERS:
            break
    return out


# ---------------------------------------------------------------- token
def load_token(regenerate: bool = False) -> str:
    """The shared secret the app must present. Created on first use, 0600, rotated with --new-token."""
    if not regenerate:
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return tok


def bearer_token(headers) -> str | None:
    """The token a request presents: `Authorization: Bearer <token>` and nothing else. One in the URL
    is ignored on purpose: query strings end up in proxy logs and browser history."""
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- audio helpers
def wav_bytes(audio, sample_rate: int = SAMPLE_RATE) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


VOICE_OFF = "The voice is off on the computer. Turn it on in Settings, or with /voice on in a terminal."


class VoiceOff(RuntimeError):
    """[voice] enabled is false: nothing speaks or listens, and nothing is loaded onto the GPU for it."""

    def __init__(self):
        super().__init__(VOICE_OFF)


class WireSpeaker(StreamingSpeaker):
    """StreamingSpeaker that ships synthesised audio to a sink instead of the sound card."""

    # The phone plays each WAV the moment it lands, with nothing queued but what was sent, so this side
    # keeps it fed. Pieces go out a second long (fewer, fatter WAV messages) while the phone has
    # LOW_LEAD_S or more still to play; with less, each piece goes as soon as it is made; with nothing
    # (the reply's opening, or after a pause) PREBUFFER_S is gathered first. The opening used to be one
    # 0.32 s piece with a full second behind it, which took ~0.65 s to make: the phone went quiet after
    # the first word. A whole second up front instead held the first sound back by most of a second.
    MIN_PIECE_S = 1.0
    PREBUFFER_S = 0.6
    LOW_LEAD_S = 1.0
    GAP_LOG_S = 0.03  # a mid-sentence wait longer than this is logged to voice.log as a phone gap
    _until = 0.0  # when (monotonic) the phone runs out of what it has been sent, going by send times
    _midway = False  # the last piece sent was not its chunk's last: the next one carries on mid-sentence

    def _min_piece_s(self, first: bool, spoken: int) -> float:
        lead = self._until - time.monotonic()
        if lead <= 0:
            return self.PREBUFFER_S
        return StreamingSpeaker.MIN_PIECE_S if lead < self.LOW_LEAD_S else self.MIN_PIECE_S

    def __init__(self, speaker: Speaker, sink):
        self.sink = sink
        self.seq = 0
        self.hushed = False
        super().__init__(speaker)

    def hush(self) -> None:
        """Nobody is listening any more (the phone hung up): say nothing more of this reply. Unlike
        stop() it leaves the shared speaker alone, so another chat's speech carries on. The audio
        queue is left as it is: the play loop drops what is in it, and draining here could take the
        synth thread's end marker with it, leaving finish() waiting on a play loop that never ends."""
        self.hushed = True

    def feed(self, text: str) -> None:
        if not self.hushed:
            super().feed(text)

    def flush(self, tail: str = "paragraph") -> None:
        if not self.hushed:
            super().flush(tail)

    def _play_loop(self) -> None:  # runs on its own thread, in sentence order
        while True:
            item = self._audio_q.get()
            if item is None:
                return
            audio, _raw, last = item
            if audio is None or self.hushed or self.speaker._stop.is_set():
                self._midway = self._midway and not last
                continue
            now = time.monotonic()
            if self._midway and now - self._until > self.GAP_LOG_S:
                # The phone's counterpart of the local speaker's `underflow` timing event: the phone reports none.
                log_voice_line("phone-gap", f"~{(now - self._until) * 1000:.0f} ms mid-sentence before piece {self.seq + 1}")
            self._until = max(self._until, now) + audio.size / SAMPLE_RATE
            self._midway = not last
            self.seq += 1
            self.sink(self.seq, wav_bytes(audio))


# ---------------------------------------------------------------- chats
class Chat:
    """One independent conversation: its own brain (agent session), model, transcript and turn state.

    Several chats run at once, like Claude Code sessions in the Claude app: each owns a copy of the
    config (so /model on one never touches another) and drives its own agent process.
    """

    def __init__(self, hub: "Hub", brain, cfg: Config, title: str = "", chat_id: str | None = None,
                 restored: dict | None = None):
        from vision.conversation import VoiceConversation

        self.hub = hub
        self.id = chat_id or secrets.token_hex(6)
        self.cfg = cfg
        self.project = ""  # the id of the project this chat is the chat of (projects.py), "" for none
        self._project_prompt = ""  # …and what its brain is told about it every turn
        self.auto_steps = 0  # thread reports run here since the user last said anything (projects.AUTO_STEPS)
        self._call_until = 0.0  # a call is taken to be on until then: a while after the last thing said on one
        self._switch_after: tuple | None = None  # (model, effort) to switch to once the reply under way is done
        self._own_rules = (list(brain.cfg.ask_tools or []), list(brain.cfg.denied_tools or []))  # before a project's are added
        self.brain = brain
        self._noted = (brain.cfg.model, brain.cfg.effort)  # the model the chat last ran on: a switch is said at the next turn
        self.conversation = VoiceConversation(cfg, agent=brain)
        self.title = title
        self.created = time.time()
        self.updated = self.created
        self.busy = False
        self._pending: deque[tuple[str, bool, bool, bool]] = deque()
        self.closed = False   # closed on a phone: what was queued behind the reply never runs
        self._turn_task: asyncio.Task | None = None
        self.partial = ""
        self.user_text = ""
        self.suggestion = ""  # the message the user would likely send next (suggest.py), "" for none
        self.shell_notes: list[str] = []  # shell-mode commands (brain.shell_note) from the phone or a joined terminal, for the brain's next typed turn
        self._shell = None  # the phone's shell-mode command while it runs (a Popen; True before it has started)
        self._spoken = False  # the last turn came from a call: no suggestion after it
        self._started = 0.0  # when the running reply part began (summary's elapsed_ms), moved on by time spent waiting on a question
        self._asked = 0.0  # when the open question was asked: elapsed_ms holds still from here until it is answered
        self._parts: list[int] = []  # ms each earlier part of this turn's reply took (a steered message starts the next)
        self._question: Future | None = None
        self._questions: list[dict] = []
        self._wire: WireSpeaker | None = None
        self._lock = threading.Lock()
        # A remote transcript includes typed and spoken turns, never the private worker's JSON.
        # Kept for the lifetime of the chat so reconnecting phones recover voice-only turns too.
        self._transcript: list[dict] | None = None
        self.history_id = secrets.token_hex(12)
        # This turn's subagent rows as agent frames (with `at`, where in the reply each started): in the
        # summary while busy, so a phone that (re)connects mid-turn rebuilds them with their real timers.
        self.agents: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}  # …and its tool calls as tool frames, the same way
        self._steers: list[tuple] = []  # (reply chars so far, text, agent rows so far) of messages sent into this turn
        # Sent into the turn but not yet taken in by Claude (it takes one at its next step and says so,
        # see Brain.on_steer_taken): still listed as queued, so the phone keeps the bubble waiting.
        self._sent: list[str] = []
        self._driver = None  # what answers this turn (the brain, or the conversation model on a call)
        self.journal = None
        self._journal_has_base = bool(restored and restored["has_base"])
        if hub.journal_enabled:
            from vision.turnjournal import ChatJournal

            self.journal = ChatJournal(self.id)
            if not self.journal.claim():
                raise RuntimeError(f"chat {self.id} is already open in another Vision process")
            if restored is None:
                self.journal.append("chat", title=title, model=cfg.brain.model, effort=cfg.brain.effort,
                                    session_id=brain.session_id, workdir=brain.workdir)
        if restored is not None:
            self._transcript = restored["history"] if restored["has_base"] or restored["history"] else None
            self.created, self.updated = restored["created"], restored["updated"]
            if not self.title:
                self.title = next((m["text"][:48] for m in self._transcript or []
                                   if m.get("role") == "user" and m.get("text")), "")
            # A first turn can crash before the provider gives Vision a session id. Carry the
            # recovered transcript into the next fresh provider thread.
            if not brain.session_id and self._transcript:
                from vision.turnjournal import recovery_context

                brain.handoff = recovery_context(self._transcript, restored["recovery_rows"])

    # -- the bits of the brain the hub reads (LinkedChat has the same, from the terminal's summary)
    source = "server"

    @property
    def brain(self):
        return self._brain

    @brain.setter
    def brain(self, brain) -> None:
        brain.chat_id = self.id  # its shell gets VISION_CHAT (brain_env): `vision remind` comes back here
        brain.project_prompt = self._project_prompt  # a model switch keeps the project in the new brain's prompt
        old = getattr(self, "_brain", None)
        if old is not None and getattr(old, "allowed_once", None):
            brain.allowed_once = old.allowed_once  # what the user approved carries over a model switch
        self._brain = brain
        self._apply_rules()

    def _apply_rules(self) -> None:
        """The project's rules (projects.rule_patterns) on the brain's own permission rules: ask first and never
        come on top of the user's [brain] ones, and go again when the chat stops being a project's."""
        from vision.projects import rule_patterns

        project = self.hub.projects.get(self.project) if self.project and getattr(self.hub, "projects", None) else None
        ask, deny = rule_patterns(project, "coordinator") if project else ([], [])
        own_ask, own_deny = getattr(self, "_own_rules", ([], []))
        cfg = self._brain.cfg
        cfg.ask_tools = list(dict.fromkeys(own_ask + ask))
        cfg.denied_tools = list(dict.fromkeys(own_deny + deny))

    def set_project(self, project: dict | None) -> None:
        """Make this the chat of `project` (None: no project's any more). The goal and instructions go
        into the brain's system prompt from the next turn (a kept Claude process restarts for it)."""
        from vision.projects import prompt_section

        self.project = project["id"] if project else ""
        self._project_prompt = prompt_section(project)
        self._brain.project_prompt = self._project_prompt
        self._apply_rules()

    async def run_shell(self, command: str) -> None:
        """Shell mode from the phone: run `command` in this chat's folder (shellmode.run), as a `shell` row in
        the transcript, and keep what it printed for the brain's next typed turn. One at a time."""
        from vision import shellmode
        from vision.brain import shell_note

        if self._shell is not None:
            self.post({"type": "toast", "text": "A command is still running; stop it first."})
            return
        self._shell = True
        await asyncio.get_running_loop().run_in_executor(None, self.history)
        row = {"role": "shell", "text": command, "running": True}
        with self._lock:
            self._transcript.append(row)
        self.updated = time.time()
        self.post({"type": "shell", "text": command, "running": True})
        self.hub.post(self.summary())
        self.log(f"! {command[:80]}")
        def show(text: str) -> None:
            # What it has printed so far, on the phones now and in /history for one that opens meanwhile.
            shown = shellmode.tail(text)
            with self._lock:
                if row.get("running"):
                    row["output"] = shown
            self.post({"type": "shell_output", "text": command, "output": shown})

        live = shellmode.Live(show)
        try:
            output, code = await asyncio.to_thread(shellmode.run, command, self.workdir,
                                                   lambda proc: setattr(self, "_shell", proc), live.add)
        finally:
            live.close()
            self._shell = None
        shown = shellmode.tail(output)
        with self._lock:
            row.pop("running", None)
            row.update(output=shown, code=code)
        self.shell_notes.append(shell_note([(command, output, code)]))
        self.updated = time.time()
        self.post({"type": "shell", "text": command, "output": shown, "code": code})
        if not self.closed:
            self.hub.post(self.summary())

    def stop_shell(self) -> bool:
        from vision import shellmode

        return shellmode.stop(self._shell if self._shell is not True else None)

    def remind(self, text: str) -> None:
        """A reminder falling due: a line in this chat, now and when it is next opened. No model runs."""
        self.history()
        with self._lock:
            self._transcript.append({"role": "assistant", "text": text})
        self.updated = time.time()
        self.post({"type": "note", "text": text})

    @property
    def provider(self) -> str:
        return self.brain.provider

    @property
    def session_id(self) -> str | None:
        return self.brain.session_id

    @property
    def workdir(self) -> str:
        return self.brain.workdir

    @property
    def model_id(self) -> str:
        return self.brain.cfg.model

    @property
    def effort(self) -> str:
        return self.brain.cfg.effort or ""

    # -- snapshot for the phone
    def summary(self) -> dict:
        from vision.models import model_label

        return {
            "type": "chat",
            "chat": self.id,
            "source": self.source,
            "title": self.title,
            "provider": self.brain.provider,
            "model": model_label(self.brain.resolved_model()) or self.brain.cfg.model,
            "model_id": self.brain.cfg.model,
            "effort": self.brain.cfg.effort or "",
            "mode": self.brain.cfg.mode,  # auto or plan; approving a plan turns a plan chat to auto, as in the terminal
            "workdir": self.workdir,
            "folder": tilde(self.workdir),  # the same, as the phone shows it
            # spoken turns are answered by the conversation model, not the brain above
            "voice_model": model_label(self.cfg.conversation.model) or self.cfg.conversation.model,
            "voice_model_id": self.cfg.conversation.model,
            "voice_effort": self.cfg.conversation.effort or "",
            "session_id": self.brain.session_id,
            "context": context_figure(self.brain),
            "history_id": self.history_id,
            "busy": self.busy,
            "waiting": self._question is not None and not self._question.done(),
            "questions": self._questions if self._question is not None and not self._question.done() else [],
            "partial": self.partial if self.busy else "",
            "user_text": self.user_text if self.busy else "",
            # how long the running reply has taken so far, for a phone that opens mid-reply (its timer goes on from here)
            "elapsed_ms": int(((self._asked or time.time()) - self._started) * 1000) if self.busy and self._started else 0,
            "queued": self._sent + [item[0] for item in self._pending],  # a phone opened cold gets its queued bubbles back
            "shell_running": self._shell is not None,  # a shell-mode command is running: the phone's stop button stops it
            "suggestion": "" if self.busy else self.suggestion,
            "agents": list(self.agents.values()) if self.busy else [],
            "tools": list(self.tools.values()) if self.busy else [],
            "project": self.project,
            "pinned": self.hub.is_pinned(self.id),
            "created": self.created,
            "updated": self.updated,
        }

    def post(self, ev: dict) -> None:
        ev["chat"] = self.id
        self.hub.post(ev)

    def log(self, msg: str) -> None:
        self.hub.log(f"[{self.id[:4]}] {msg}" if len(self.hub.chats) > 1 else msg)

    # -- transcript
    def reset_history(self) -> None:
        self._transcript = None
        self.history_id = secrets.token_hex(12)
        self.suggestion = ""  # a guess for the conversation that was

    def history(self, limit: int = 60) -> list[dict]:
        from vision.sessions import session_history

        with self._lock:
            if self._transcript is None:
                self._transcript = session_history(self.brain.provider, self.brain.session_id or "")
            if not self.title:
                first = next((m["text"] for m in self._transcript if m.get("role") == "user" and m.get("text")), "")
                self.title = _title_from(first)
            rows = list(self._transcript[-max(1, min(limit, 200)):])
        if self.project and any(r.get("role") == "checkin" for r in rows):
            from vision.projects import checkins, fold_checkins

            rows = fold_checkins(rows, checkins(self.hub.projects.root / self.project))  # one card per check-in
        return rows

    # -- turn control
    def queue(self, text: str, speak: bool, voice: bool, talk: bool = False) -> None:
        """`voice`: spoken; `talk`: typed during a call. Both go to the conversation model, so one
        model answers the whole call."""
        if self.closed:
            return
        if (voice or talk) and not text.startswith(REPORT_PREFIX):  # said by the user, not a spoken thread report
            self._call_until = time.time() + self.CALL_GRACE_S
        if (self.busy and not (voice or talk or speak) and hasattr(self._driver, "take_untaken_steers")
                and not self.hub.restarting and self.steer(text)):
            return  # Claude at work: into its own queue, taken at its next step (as Claude Code does)
        if self.journal:
            self.journal.append("queued", text=text)
        if self.busy or self.hub.restarting:
            # A restart already under way: the journal carries it over and the new process runs it.
            self._pending.append((text, speak, voice, talk))
            self.hub.post(self.summary())  # every phone shows the queue as it stands, not just the sender
            return
        self.busy = True  # reserve the turn before yielding to a scheduled task
        self._turn_task = asyncio.create_task(self.run_turn(text, speak=speak, voice=voice, talk=talk))

    def resume_queued(self, texts: list[str]) -> None:
        """Run messages that were queued when a graceful restart took the last process down (their
        `queued` records are already in the journal, so they are not journalled again)."""
        self._pending.extend((text, False, False, False) for text in texts if text)
        if not self.busy and self._pending:
            text, speak, voice, talk = self._pending.popleft()
            self.busy = True
            self._turn_task = asyncio.create_task(self.run_turn(text, speak, voice, talk))

    def answer(self, answers) -> None:
        fut = self._question
        if fut is not None and not fut.done():
            fut.set_result(answers if isinstance(answers, dict) and answers else None)

    def unqueue(self, text: str) -> bool:
        """Take a queued message back out (the first one reading `text`); False if it already ran."""
        for i, item in enumerate(self._pending):
            if item[0] == text:
                if self.journal:
                    self.journal.append("unqueued", text=text)
                del self._pending[i]
                self.hub.post(self.summary())
                return True
        return False

    def steer(self, text: str) -> bool:
        """Send `text` into the running turn now instead of after it (Claude takes it at its next step).
        A copy still waiting in the queue is taken out. False when this turn's brain can't take one
        (Codex, Grok, a local model, nothing running): the message stays queued."""
        steer = getattr(self._driver, "steer", None)
        if not self.busy or steer is None:
            return False
        if text in self._sent:
            return True  # in already, waiting for Claude's next step
        if self.journal:
            self.journal.append("steer_attempt", text=text)
        if not steer(text):
            if self.journal:
                self.journal.append("steer_failed", text=text)
            return False
        for i, item in enumerate(self._pending):
            if item[0] == text:
                del self._pending[i]
                break
        if hasattr(self._driver, "take_untaken_steers"):
            # Claude holds it until its next step: the reply splits when it takes it in, not now.
            self._sent.append(text)
            self.hub.post(self.summary())
            return True
        self._steer_landed(text)
        return True

    def _steer_landed(self, text: str) -> None:
        """A steered message reached the model: it sits in the reply where it went in."""
        if self.journal:
            self.journal.append("steered", text=text)
            self.journal.append("unqueued", text=text)
        if text in self._sent:
            self._sent.remove(text)
        self._steers.append((len(self.partial), text, len(self.agents)))
        _split_timer(self)
        self.log(f"» {text[:80]}{'…' if len(text) > 80 else ''}")
        self.post({"type": "steered", "text": text})

    def talks_over(self) -> bool:
        """A call's turn is running (the conversation model, often waiting on agents it launched): what
        is said on the call goes into it and is answered now, rather than queuing behind the agents."""
        return self.busy and self._driver is self.conversation

    CALL_GRACE_S = 180.0  # how long after the last thing said on a call it is still taken to be on

    def on_call(self) -> bool:
        """Someone is on a call with this chat: something was said on one lately and it hasn't hung up."""
        return time.time() < self._call_until

    def hush(self) -> None:
        self._call_until = 0.0  # hung up
        if self._wire:
            self._wire.hush()

    def shut(self) -> None:
        """Closed: drop the queue before the running reply is stopped, so the stop can't start the next one."""
        self.closed = True
        self._pending.clear()

    def cancel(self) -> None:
        fut = self._question
        if fut is not None and not fut.done():
            fut.set_result(None)
        if self._wire:
            self._wire.stop()
        self.conversation.cancel()
        self.brain.cancel()

    def set_mode(self, mode: str) -> None:
        """auto or plan for this chat from now on, as /mode does in the terminal: plan is read-only until
        a plan is approved. Kept with the chat's journal, so a restart does not hand a plan chat back in auto."""
        if mode == self.brain.cfg.mode:
            return
        self.cfg.brain.mode = self.brain.cfg.mode = mode
        warm_soon(self.brain)  # the mode is on the Claude command line: its process boots while the next message is typed
        if self.journal:
            self.journal.append("mode", mode=mode)
        self.hub.post(self.summary())

    def _mode_switched(self, mode: str) -> None:
        """Approving a plan turned the chat to auto mid-turn: journalled, so a restart keeps it, and on
        every phone now rather than at the end of the turn."""
        if self.journal:
            self.journal.append("mode", mode=mode)
        if not self.closed:
            self.hub.post(self.summary())

    def switch_when_free(self, model: str, effort: str | None) -> None:
        """Switch model now, or once the reply under way is done (a project's coordinator model changed)."""
        if self.busy:
            self._switch_after = (model, effort)
        else:
            asyncio.ensure_future(self.switch_model(model, effort))

    async def switch_model(self, model: str, effort: str | None) -> None:
        from vision.cli import _effort_word, _model_label, _switch_model

        if not model:
            return
        from vision.models import provider_for
        from vision.providers import unavailable_reason

        loop = asyncio.get_running_loop()
        why = await loop.run_in_executor(None, unavailable_reason, provider_for(model), self.cfg)  # it may probe a server
        if why:
            self.post({"type": "error", "text": why})
            return
        model_changed = model != self.cfg.brain.model
        self.busy = True
        with self._lock:
            history = list(self._transcript or [])
        try:
            self.brain, _detail = await loop.run_in_executor(
                None, lambda: _switch_model(self.cfg, self.brain, model, voice_mode=False, effort=effort or None,
                                            history=history)
            )
            warm_soon(self.brain)  # the process for the new pick boots while the next message is being typed
            self.conversation.agent = self.brain
            if self.journal:
                self.journal.append("model", model=self.cfg.brain.model, effort=self.cfg.brain.effort,
                                    session_id=self.brain.session_id)
            self.conversation.follow()  # a call on this chat talks through the new pick
            # The chat's note waits for the next message (flicking through models shouldn't fill it);
            # a terminal joined to this chat says it now, as the answer to its /model.
            effort_word = _effort_word(self.cfg.brain.effort)
            if model_changed:
                text = f"Switched to {_model_label(self.brain)} ({effort_word})"
            else:
                text = f"Effort set to {effort_word}"
            self.post({"type": "switched", "text": text})
        except Exception as e:  # noqa: BLE001
            self.post({"type": "error", "text": f"model switch failed: {e}"})
        finally:
            self.busy = False
            self.hub.post(self.summary())
            # A message may have arrived during the handoff. It belongs after the switch, on the new brain.
            if self._pending and not self.hub.restarting:
                text, speak, voice, talk = self._pending.popleft()
                self.busy = True
                self._turn_task = asyncio.create_task(self.run_turn(text, speak, voice, talk))
            elif self.project and not self.closed:
                self.hub.report_soon(self.project, delay=0.5)
            self.hub.maybe_restart()

    def _note_switch(self) -> None:
        """Above a turn: one line if the model or effort changed since the last turn, however many
        switches it took (and none if it was switched back)."""
        from vision.cli import _effort_word, _model_label

        now = (self.brain.cfg.model, self.brain.cfg.effort)
        if now == self._noted:
            return
        effort_word = _effort_word(now[1])
        if now[0] != self._noted[0]:
            text = f"Switched to {_model_label(self.brain)} ({effort_word})"
        else:
            text = f"Effort set to {effort_word}"
        self._noted = now
        self.post({"type": "note", "text": text, "switch": True})  # a joined terminal had it as `switched`

    async def run_turn(self, text: str, speak: bool, voice: bool, talk: bool = False) -> None:
        """Run this turn and every follow-up queued while it is active, in arrival order."""
        try:
            while True:
                await self._run_one_turn(text, speak, voice, talk)
                if not self._pending or self.hub.restarting or self.closed:
                    return
                text, speak, voice, talk = self._pending.popleft()
        finally:
            self.busy = False
            if self._turn_task is asyncio.current_task():
                self._turn_task = None
            if not self.closed:  # a snapshot of a closed chat would put it back on every phone
                self.hub.post(self.summary())
            self.hub.maybe_restart()
            if self._switch_after and not self.closed and not self.hub.restarting:
                (model, effort), self._switch_after = self._switch_after, None
                asyncio.ensure_future(self.switch_model(model, effort))
            elif self.project and not self.closed:
                self.hub.report_soon(self.project, delay=0.5)  # threads that stopped while it was replying
            if not self.closed and not self.hub.restarting and not self._spoken and self.hub.cfg.remote.suggestions:
                asyncio.ensure_future(self._suggest())

    async def _suggest(self) -> None:
        """Guess the user's next message and offer it to the phone, unless something was said meanwhile."""
        from vision.suggest import suggest

        updated, conversation, rows = self.updated, self.history_id, self.history(8)
        if not rows or rows[-1].get("role") != "assistant" or rows[-1].get("error"):
            return
        text = await asyncio.get_running_loop().run_in_executor(None, suggest, rows, self.hub.cfg.providers.enabled)
        if text and not self.busy and not self.closed and self.updated == updated and self.history_id == conversation:
            self.suggestion = text
            self.post({"type": "suggestion", "text": text})

    async def _run_one_turn(self, text: str, speak: bool, voice: bool, talk: bool = False) -> None:
        from vision.projects import checkin_note, report_note

        if self.hub.cfg.remote.suggestions:
            from vision.suggest import warm  # its guess finds Claude Code started, not starting

            asyncio.get_running_loop().run_in_executor(None, warm, self.hub.cfg.providers.enabled)

        loop = asyncio.get_running_loop()
        self.busy, self.partial, self.user_text = True, "", text
        self.suggestion, self._spoken = "", bool(voice or talk)
        self.agents, self.tools, self._steers = {}, {}, []
        idle = time.time() - self.updated
        self.updated = time.time()
        note = report_note(text)  # a project thread's report: shown as a note, never as the user's words
        if note is None:
            note = checkin_note(text)  # …and a check-in, the same way
        if note is None and text.startswith(APPROVAL_PREFIX):
            note = text[len(APPROVAL_PREFIX):].split("\n", 1)[0].strip()  # …and what the user decided about a request
        if note is None:
            self.auto_steps = 0
        if not self.title:
            self.title = _title_from(text)
        await loop.run_in_executor(None, self.history)  # seed from the typed session before it advances
        if self.journal:
            if not self._journal_has_base:
                self.journal.append("base", history=self._transcript or [])
                self._journal_has_base = True
            self.journal.append("start", text=text)
        self.log(f"› {(note or text)[:80]}{'…' if len(note or text) > 80 else ''}")
        started = self._started = time.time()
        self._parts = []
        self._note_switch()
        self.post({"type": "start", "text": text, "speak": speak, **({"note": note} if note is not None else {})})
        self.hub.post(self.summary())

        wire = None
        if speak:
            try:
                sp = await loop.run_in_executor(None, self.hub.speaker)
                wire = WireSpeaker(sp, sink=lambda seq, wav: self.post({"type": "audio", "seq": seq, "wav": base64.b64encode(wav).decode()}))
                if voice:
                    from vision.cli import _arm_filler

                    _arm_filler(wire, sp, self.hub.cfg.voice)  # "Hmm." if the first words are late
            except Exception as e:  # noqa: BLE001
                self.post({"type": "note", "text": f"voice unavailable: {e}"})
        self._wire = wire

        def on_text(delta: str) -> None:
            if self.journal:
                self.journal.append("delta", text=delta)
            self.partial += delta
            self.post({"type": "delta", "text": delta})
            if wire:
                wire.feed(delta)

        def on_status(tool: str) -> None:
            if wire and tool:
                wire.flush()  # the text block is over: say its last sentence while the tool runs
            self.post({"type": "status", "tool": tool, "label": status_label(tool)})

        def on_agent(run) -> None:
            # One frame per change; `step` is the newest tool call (absent when it just started or finished).
            frame = agent_frame(run)
            frame["at"] = self.agents.get(run.id, {}).get("at", len(self.partial))
            if self.journal:
                from vision.turnjournal import compact_agent_frame

                self.journal.append("agent", frame=compact_agent_frame(frame, self.agents.get(run.id)))
            self.agents[run.id] = frame
            self.post(dict(frame))

        def on_tool(call) -> None:
            # The main conversation's tool calls, drawn as rows by the phone and a terminal following this chat.
            frame = tool_frame(call)
            frame["at"] = self.tools.get(call.id, {}).get("at", len(self.partial))
            if self.journal:
                self.journal.append("tool", frame=frame)
            self.tools[call.id] = frame
            self.post(dict(frame))

        def on_question(questions: list[dict]) -> dict[str, str] | None:
            nonlocal started
            fut: Future = Future()
            self._asked = time.time()
            if self.journal:
                self.journal.append("question", questions=questions)
            self._questions = questions
            self._question = fut
            self.post({"type": "question", "questions": questions})
            self.hub.post(self.summary())
            plan = plan_question(questions)
            if plan and wire:  # a voice reply: say what the plan is, so a spoken "go" can answer it
                wire.flush()
                wire.feed(plan_summary(plan))
                wire.flush()
            answers = None
            try:
                answers = fut.result(timeout=QUESTION_TIMEOUT_S)
                return answers
            except Exception:  # noqa: BLE001  (timeout → the model carries on without answers)
                return None
            finally:
                if self.journal:
                    self.journal.append("answered", questions=questions, answers=answers)
                self._question = None
                self._questions = []
                # The wait isn't the turn's time: its clock carries on from where it stopped.
                waited = time.time() - self._asked
                started += waited
                self._started += waited
                self._asked = 0.0
                self.post({"type": "answered", "questions": questions, "answers": answers})
                if not self.closed:  # closed while it waited: no snapshot to put it back on every phone
                    self.hub.post(self.summary())

        from vision.cli import _turn_brain

        driver = self._driver = _turn_brain(self.brain, self.conversation, voice, text, talk=talk)
        if self.shell_notes and driver is self.brain:
            self.brain.shell, self.shell_notes = "\n\n".join(self.shell_notes), []  # inject_handoff puts them ahead of the message
        self.brain.on_mode_change = self._mode_switched
        # A check-in runs while the user is away: a call that asks first files an approval request instead of a form.
        self.brain.on_approval = ((lambda tool, args, rule: self.hub.request_approval(self.project, "coordinator", tool, args, rule))
                                  if self.project and text.startswith(CHECKIN_PREFIX) else None)
        self.brain.on_steer_taken = self._steer_landed
        prompt = text
        if self.project:
            # The coordinator sees the project as it is now: its threads, memory and Library. The brain gets
            # it ahead of the message (history views take it off again); the conversation model on a call
            # gets it as data, with the threads it can address.
            recap = ""
            if driver is self.brain and self.brain.session_id:
                from vision.projects import coordinator_fresh, recap as recap_of

                # A heavy or cold session starts afresh: the status block carries the project, and a short
                # recap what was said, so a report doesn't reread the whole project's history. The phone's
                # transcript is this chat's own and stays as it was.
                if why := coordinator_fresh((getattr(self.brain, "context", None) or (0, 0))[0], idle):
                    recap = recap_of(self.history(200))  # loads the transcript when this process hasn't yet
                    self.brain.new_session()
                    self.log(f"  fresh session: {why}")
            status = await loop.run_in_executor(None, lambda: self.hub.project_status(self.project, recap))
            if driver is self.brain:
                prompt = f"{status}\n\n{text}" if status else text
            else:
                self.conversation.project = self.hub.voice_project(self.project, status)
        else:
            self.conversation.project = None
        try:
            turn = await loop.run_in_executor(
                None, lambda: driver.ask(prompt, on_text=on_text, on_status=on_status, on_question=on_question, on_agent=on_agent, on_tool=on_tool)
            )
            error = turn.error if turn.is_error else ""
            reply, session_id, model = turn.text, turn.session_id, turn.model
        except Exception as e:  # noqa: BLE001
            error, reply, session_id, model = str(e), self.partial, self.brain.session_id, None
        if wire:
            try:
                await loop.run_in_executor(None, wire.finish)  # flush the last sentence
            except Exception:  # noqa: BLE001
                pass
            self.post({"type": "audio_end"})
        self._wire = None
        self._driver = None
        self.brain.on_steer_taken = None
        untaken = self.brain.take_untaken_steers() if hasattr(self.brain, "take_untaken_steers") else []
        self._sent = []
        for t in reversed(untaken):  # the turn ended before Claude took them in: they run next instead
            if self.journal:
                self.journal.append("steer_failed", text=t)
                self.journal.append("queued", text=t)
            self._pending.appendleft((t, False, False, False))
        if self.journal:
            self.journal.append("done", text=reply, error=error,
                                session_id=session_id or self.brain.session_id)
        entries = _turn_entries(text, reply, error, self.partial, list(self.agents.values()), self._steers, list(self.tools.values()))
        took = int((time.time() - self._started) * 1000)  # the last part: an earlier one's time is in _parts
        _timed(entries, took, self._parts)
        with self._lock:
            if self._transcript is None:
                self._transcript = []
            self._transcript.extend(entries)
            self._transcript = self._transcript[-200:]
        _record_agents(self.brain.provider, session_id or self.brain.session_id, entries)
        self.updated = time.time()
        self.post({
            "type": "done",
            "text": reply,
            "error": error,
            "busy": bool(self._pending),
            "session_id": session_id or self.brain.session_id,
            "history_id": self.history_id,
            "model": model,
            "duration_ms": took,
        })
        self.log(f"  {'✗ ' + error if error else '✓'} {time.time() - started:.1f} s")
        if self.project and text.startswith(CHECKIN_PREFIX):
            self.hub.checkin_done(self, text, reply, error)


def _timed(entries: list[dict], duration_ms: int, parts: list[int] = ()) -> None:
    """Note how long each reply row took, so the phone's footers survive a /history refetch. A reply
    split by messages sent into it times each part on its own (`parts`: the earlier ones, then
    `duration_ms` for the last); when the split could not be placed, only the last row is timed."""
    replies = [row for row in entries if row.get("role") == "assistant"]
    if not replies:
        return
    times = [*parts, duration_ms] if len(replies) == len(parts) + 1 else [duration_ms]
    for row, ms in zip(replies[-len(times):], times):
        row["duration_ms"] = ms


def _split_timer(chat) -> None:
    """A message went into the running reply: the part before it is over and its time kept, and the
    next part (the reply to that message) times itself from now, not from the start of the turn.
    A question still open goes on holding the clock still, from now."""
    now = time.time()
    chat._parts.append(int(((chat._asked or now) - chat._started) * 1000))
    chat._started = now
    if chat._asked:
        chat._asked = now


_assistant_entry = agentlog.assistant_entry
_turn_entries = agentlog.turn_entries
_record_agents = agentlog.record_turn


def _title_from(text: str, limit: int = 60) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- linked (terminal) chats
class LinkedChat:
    """A chat that lives in a terminal `vision` process (see link.py), driven over its Unix socket.

    Same face as Chat for the hub and the phone; the terminal runs the turns and streams every
    frame back, this end adds the chat id, keeps the transcript and does the speaking when the
    phone asked for a voice reply (the terminal need not have its voice loaded).
    """

    source = "terminal"

    def __init__(self, hub: "Hub", info: dict):
        self.hub = hub
        self.id = secrets.token_hex(6)
        self.pid = int(info["pid"])
        self.created = float(info.get("started") or time.time())
        self.updated = self.created
        self.state: dict = {}  # the terminal's latest summary
        self.title = ""
        self.busy = False
        self.partial = ""
        self.user_text = ""
        self._questions: list[dict] = []
        self.history_id = secrets.token_hex(12)
        self._transcript: list[dict] | None = None
        self._lock = threading.Lock()
        self._wire: WireSpeaker | None = None
        self._speak: deque[bool] = deque()  # speak flag per message this end sent, in order
        self._started = 0.0
        self._asked = 0.0  # as Chat._asked: the terminal's open question holds elapsed_ms still
        self._parts: list[int] = []  # as Chat._parts
        self.link = None
        self.agents: dict[str, dict] = {}  # this turn's agent rows, as Chat.agents (from the terminal's frames)
        self.tools: dict[str, dict] = {}  # …and its tool calls, as Chat.tools
        self._steers: list[tuple] = []

    # -- what the hub reads
    @property
    def provider(self) -> str:
        return self.state.get("provider", "")

    @property
    def session_id(self) -> str | None:
        return self.state.get("session_id") or None

    @property
    def workdir(self) -> str:
        return self.state.get("workdir", "")

    @property
    def model_id(self) -> str:
        return self.state.get("model_id", "")

    @property
    def effort(self) -> str:
        return self.state.get("effort", "")

    @property
    def waiting(self) -> bool:
        return bool(self.state.get("waiting"))

    def summary(self) -> dict:
        s = {k: v for k, v in self.state.items() if k not in ("type", "chat")}
        s.update({
            "type": "chat",
            "chat": self.id,
            "source": self.source,
            "title": self.title or s.get("title", ""),
            "history_id": self.history_id,
            "pinned": self.hub.is_pinned(self.id),
            "busy": self.busy,
            "agents": list(self.agents.values()) if self.busy else [],
            "tools": list(self.tools.values()) if self.busy else [],
            "waiting": self.waiting,
            "questions": self._questions if self.waiting else [],
            "partial": self.partial if self.busy else "",
            "user_text": self.user_text if self.busy else "",
            "elapsed_ms": int(((self._asked or time.time()) - self._started) * 1000) if self.busy and self._started else 0,
            "folder": tilde(self.workdir),
            "created": self.created,
            "updated": self.updated,
        })
        return s

    def post(self, ev: dict) -> None:
        ev["chat"] = self.id
        self.hub.post(ev)

    def log(self, msg: str) -> None:
        self.hub.log(f"[{self.id[:4]} terminal] {msg}")

    # -- transcript (seeded from the session on disk, then kept up from the terminal's done frames)
    def reset_history(self) -> None:
        with self._lock:
            self._transcript = None
        self.history_id = secrets.token_hex(12)

    def history(self, limit: int = 60) -> list[dict]:
        from vision.sessions import session_history

        with self._lock:
            if self._transcript is None:
                self._transcript = session_history(self.provider, self.session_id or "") if self.session_id else []
            if not self.title:
                first = next((m["text"] for m in self._transcript if m.get("role") == "user" and m.get("text")), "")
                self.title = _title_from(first)
            return list(self._transcript[-max(1, min(limit, 200)):])

    # -- phone → terminal
    def queue(self, text: str, speak: bool, voice: bool, talk: bool = False) -> None:
        self._speak.append(speak)
        self.link.send({"type": "message", "text": text, "speak": speak, "voice": voice, "talk": talk})

    def remind(self, text: str) -> None:
        """A reminder falling due: a note in the terminal window and on the phones showing it."""
        self.link.send({"type": "note", "text": text})
        self.post({"type": "note", "text": text})

    def answer(self, answers) -> None:
        self.link.send({"type": "answer", "answers": answers if isinstance(answers, dict) and answers else None})

    def unqueue(self, text: str) -> bool:
        if not self.link:
            return False
        self.link.send({"type": "unqueue", "text": text})
        return True

    def steer(self, text: str) -> bool:
        """The terminal runs the turn: it sends the message in (and says `steered`) or leaves it queued."""
        if not self.link:
            return False
        self.link.send({"type": "steer", "text": text})
        return True

    def hush(self) -> None:
        if self._wire:
            self._wire.hush()

    def cancel(self) -> None:
        if self._wire:
            self._wire.stop()
        if self.link:
            self.link.send({"type": "cancel"})

    async def switch_model(self, model: str, effort: str | None) -> None:
        if model:
            self.link.send({"type": "model", "model": model, "effort": effort or None})

    def set_mode(self, mode: str) -> None:
        self.link.send({"type": "mode", "mode": mode})  # the terminal switches, and its next summary says so

    def quit(self) -> None:
        """Close from the phone: the terminal exits; its link dropping removes the chat from the list."""
        if self.link:
            self.link.send({"type": "quit"})

    def detach(self) -> None:
        if self._wire:
            self._wire.stop()
        if self.link:
            self.link.close()

    # -- terminal → phone
    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "chat":
            before = (self.provider, self.session_id)
            self.state = ev
            if (self.provider, self.session_id) != before:
                # /new or /resume in the terminal: another conversation now, so the old title goes too
                # (a fresh chat getting its first id mid-turn is the same conversation: its title stays)
                if before[1]:
                    self.title = ""
                if self._transcript is not None:
                    self.reset_history()
            if ev.get("title"):
                self.title = ev["title"]
            self.busy = bool(ev.get("busy"))
            self._questions = ev.get("questions") or []
            self.hub.post(self.summary())
            self.hub.dedupe()
            return
        if kind == "question":
            self._questions = ev.get("questions") or []
            self._asked = self._asked or time.time()
            plan = plan_question(self._questions)
            if plan and self._wire:
                self._wire.flush()
                self._wire.feed(plan_summary(plan))
                self._wire.flush()
        elif kind == "answered":
            self._questions = []
            if self._asked:
                self._started += time.time() - self._asked
                self._asked = 0.0
        if kind == "start":
            self.busy, self.partial, self.user_text = True, "", ev.get("text", "")
            self.agents, self.tools, self._steers = {}, {}, []
            self.updated = self._started = time.time()
            self._asked, self._parts = 0.0, []
            if not self.title and self.user_text:
                self.title = _title_from(self.user_text)
            threading.Thread(target=self._seed, daemon=True).start()
            self.log(f"› {self.user_text[:80]}{'…' if len(self.user_text) > 80 else ''}")
            speak = bool(ev.get("speak"))
            if speak:
                try:
                    sp = self.hub.speaker()
                    self._wire = WireSpeaker(sp, sink=lambda seq, wav: self.post({"type": "audio", "seq": seq, "wav": base64.b64encode(wav).decode()}))
                    if ev.get("voice"):
                        from vision.cli import _arm_filler

                        _arm_filler(self._wire, sp, self.hub.cfg.voice)
                except Exception as e:  # noqa: BLE001
                    self.post({"type": "note", "text": f"voice unavailable: {e}"})
            self.post(ev)
            self.hub.post(self.summary())
            return
        if kind == "delta":
            self.partial += ev.get("text", "")
            self.post(ev)
            if self._wire:
                self._wire.feed(ev.get("text", ""))
            return
        if kind == "status":
            if self._wire and ev.get("tool"):
                self._wire.flush()
            ev.setdefault("label", status_label(ev.get("tool", "")))
            self.post(ev)
            return
        if kind == "done":
            wire, self._wire = self._wire, None
            if wire:
                try:
                    wire.finish()
                except Exception:  # noqa: BLE001
                    pass
                self.post({"type": "audio_end"})
            reply = ev.get("text") or self.partial
            # (the terminal keeps the agent rows with the conversation itself: vision.agentlog)
            entries = _turn_entries(self.user_text, reply, ev.get("error") or "", self.partial, list(self.agents.values()), self._steers,
                                    list(self.tools.values()))
            if self._parts or "duration_ms" not in ev:  # the terminal times the whole turn, not its last part
                ev["duration_ms"] = int((time.time() - self._started) * 1000)
            _timed(entries, ev["duration_ms"], self._parts)
            with self._lock:
                if self._transcript is None:
                    self._transcript = []
                self._transcript.extend(entries)
                self._transcript = self._transcript[-200:]
            self.updated = time.time()
            self.busy = bool(ev.get("busy"))
            ev.setdefault("history_id", self.history_id)
            self.post(ev)
            self.log(f"  {'✗ ' + ev['error'] if ev.get('error') else '✓'} {(time.time() - self._started):.1f} s")
            self.hub.post(self.summary())
            return
        if kind == "agent" and ev.get("id"):
            frame = {k: v for k, v in ev.items() if k != "chat"}
            frame["at"] = self.agents.get(ev["id"], {}).get("at", frame.get("at", len(self.partial)))
            self.agents[ev["id"]] = frame
        elif kind == "tool" and ev.get("id"):
            frame = {k: v for k, v in ev.items() if k != "chat"}
            frame["at"] = self.tools.get(ev["id"], {}).get("at", frame.get("at", len(self.partial)))
            self.tools[ev["id"]] = frame
            ev = {**ev, "at": frame["at"]}
        elif kind == "steered":
            self._steers.append((len(self.partial), ev.get("text") or "", len(self.agents)))
            _split_timer(self)
        self.post(ev)  # agent, tool, steered, question, note, error: straight through

    def _seed(self) -> None:
        try:
            self.history()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- hub
# What the phone and the server need from each other, as one number (hello "protocol"). Raise it,
# here and in the app (VisionClient.protocolVersion), in the change that makes one side need
# something new from the other: an app that is ahead says the computer's Vision is out of date, and
# one that is behind says the app is. A change the other side can simply not know about leaves it alone.
# 1 is everything up to the number itself, so a Vision that sends none counts as 1.
PROTOCOL = 1


class Hub:
    """Owns the chats, the speech models and the connected sockets."""

    # What to run on the computer when a newer Vision is out (selfupdate.how), else None: hello's
    # "update". Only `vision serve` looks (watch_updates); a hub made anywhere else never reaches for the network.
    update_how: str | None = None
    # The threads' Lock Screen card (liveactivity.Feed), made in __init__; None on a hub made without it.
    live = None

    def __init__(self, cfg: Config, brain, token: str, log=print, follow_defaults: bool = False,
                 open_initial: bool = True, journal_enabled: bool = False):
        self.cfg = cfg
        # New chats re-read the saved default model/effort, so /default elsewhere applies without a
        # restart. Off when `vision serve --model/--effort` pinned them for this run.
        self.follow_defaults = follow_defaults
        self.journal_enabled = journal_enabled
        self.token = token
        self.log = log
        self.clients: set = set()
        self.phones: set = set()  # the clients that are the app, not a terminal following a chat (/health counts them)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.events: asyncio.Queue | None = None
        self._speaker: Speaker | None = None
        self._stt = None
        self._stt_error = ""  # why speech recognition failed to load, for /health
        self._lock = threading.Lock()
        self.chats: dict[str, Chat | LinkedChat] = {}
        self.links: dict[int, LinkedChat] = {}  # terminal chats by pid (see link.py)
        self.restart_wanted = ""  # who asked for a graceful restart (see request_restart); "" = none
        self.restarting = False  # every chat went quiet and the new process is on its way
        self.exec_restart = None  # what replaces this process (serve sets it); None = just log
        self._resume: list[tuple[Chat, list[str]]] = []  # messages a graceful restart carried over, run at startup
        # The other way round: terminal windows following one of this process's chats (remote.py), by
        # chat id, each socket with its process's pid, and when a window was launched for a chat that
        # has not dialled in yet. "Open on computer" opens one window per chat.
        self.mirrors: dict[str, dict] = {}
        self._opening: dict[str, float] = {}
        self._dedupe_lock = threading.Lock()
        from vision.scheduled import Schedule

        self.schedule = Schedule()
        self._push_done: dict[str, tuple[str, int]] = {}  # chat id → (task title, replies due before the task's own)
        from vision.projects import ProjectStore

        self.projects = ProjectStore()
        self.pinned: set[str] = load_pinned()
        self._project_opening: dict[str, asyncio.Future] = {}  # project id → its chat on the way (one per project)
        self.threads: dict = {}  # project id → its ProjectThreads (runs.py), made when first needed
        self._moods: dict[str, str] = {}  # project id → the mood its scout was last shown in
        self._checkins: dict[str, float] = {}  # chat id → when the check-in it's running started
        self._own_rules = (list(cfg.brain.ask_tools or []), list(cfg.brain.denied_tools or []))  # [brain]'s, for threads
        from vision.liveactivity import Feed

        self.live = Feed(lambda: self.cfg.push, log=self.log)  # the threads' Lock Screen card
        from vision.sleepwatch import SleepWatch

        self.sleep = SleepWatch()  # when this computer was suspended: agents and replies pause with it
        if journal_enabled:
            self.restore_chats()
        # Only an explicit --new or --continue opens a chat at startup.
        if open_initial:
            self.open_chat(brain, copy.deepcopy(cfg))

    # -- chats
    def picked_cfg(self, model, effort) -> Config | None:
        """A new chat's config on a model picked for it alone (the phone's picker with no chat open):
        the defaults otherwise, the effort fitted to the model. None when nothing (known) was picked."""
        from vision.models import coerce_effort, find

        model = str(model or "")
        if not model or not find(model):
            return None
        cfg = copy.deepcopy(self.cfg)
        if self.follow_defaults and (saved := saved_brain_defaults()):
            cfg.brain.model, cfg.brain.effort = saved
        cfg.brain.effort, _ = coerce_effort(model, str(effort or cfg.brain.effort or ""))
        cfg.brain.model = model
        return cfg

    def _prepare_chat(self, cfg: Config | None = None, workdir: str = "") -> tuple:
        """The slow part of opening a chat, kept off the event loop: the saved defaults (a config
        read), the provider check (it may probe a server for a couple of seconds) and the brain.
        Returns (brain, cfg, note); open_chat registers them."""
        from vision.brain import create_brain
        from vision.models import coerce_effort, usable_model
        from vision.providers import settle

        if cfg is None:
            cfg = copy.deepcopy(self.cfg)
            if self.follow_defaults:
                saved = saved_brain_defaults()
                if saved is None:
                    self.log("config.toml unreadable; new chat uses the defaults from start-up")
                else:
                    cfg.brain.model, cfg.brain.effort = saved
        cfg.brain.model, retired = usable_model(cfg.brain.model)  # a saved model since retired
        if retired:
            cfg.brain.effort, _ = coerce_effort(cfg.brain.model, cfg.brain.effort)
            self.log(retired)
        moved = settle(cfg)  # its provider isn't set up here, or is switched off
        if moved:
            retired = f"{retired} {moved}".strip()
            self.log(moved)
        if workdir:
            cfg.brain.workdir = workdir  # the folder picked for this chat on the phone; "" keeps the server's own
        return create_brain(cfg.brain, voice_mode=False, continue_session=False), cfg, retired

    def open_chat(self, brain=None, cfg: Config | None = None, title: str = "",
                  chat_id: str | None = None, restored: dict | None = None, note: str = "") -> Chat:
        """A chat on `brain`, or on one made here (see _prepare_chat; the async callers run that part
        in the executor first, so a phone's new chat never stalls every phone's stream)."""
        if brain is None:
            brain, cfg, note = self._prepare_chat(cfg)
        elif cfg is None:
            cfg = copy.deepcopy(self.cfg)
        chat = Chat(self, brain, cfg, title=title, chat_id=chat_id, restored=restored)
        self.chats[chat.id] = chat
        if note:
            chat.post({"type": "note", "text": note})
        if restored is None:
            # A chat someone just opened gets its first message within seconds: the provider's
            # process boots meanwhile (Brain.warm). Not the chats a restart brings back: many at
            # once, most never spoken to again.
            warm_soon(brain)
        return chat

    def restore_chats(self) -> None:
        from vision.brain import create_brain
        from vision.turnjournal import recover, saved_chats

        graceful = take_restart_marker()  # the last process restarted on purpose: its queued messages run
        for chat_id, records in saved_chats():
            try:
                state = recover(records, requeue=graceful)
                cfg = copy.deepcopy(self.cfg)
                cfg.brain.model = state["model"] or cfg.brain.model
                cfg.brain.effort = state["effort"] or cfg.brain.effort
                cfg.brain.mode = state.get("mode") or cfg.brain.mode
                from vision.models import provider_for

                sid = state["session_id"]
                from vision.providers import cap

                if state["interrupted"] and not cap(provider_for(cfg.brain.model), "keeps_partial_turns", True):
                    sid = None  # its saved model context ends at the last completed turn
                folder, moved = restored_folder(state)
                if folder:
                    cfg.brain.workdir = folder  # back in the folder it worked in, not the server's own
                brain = create_brain(cfg.brain, voice_mode=False, session_id=sid)
                chat = self.open_chat(brain, cfg, title=state["title"], chat_id=chat_id, restored=state)
                if moved:
                    chat.log(moved)
                    chat.remind(moved)  # a line in the chat for when it is next opened: no phone is listening yet
                if project := self.projects.by_chat(chat_id):
                    chat.set_project(project)  # a project only remembers its chat's id: the tag comes back here
                if graceful and state["pending"]:
                    self._resume.append((chat, list(state["pending"])))
                elif state["pending"] and chat.journal:
                    # Recovery showed these as lost. Retire them durably so a later
                    # graceful restart cannot silently run them after the user retries.
                    for text in state["pending"]:
                        chat.journal.append("unqueued", text=text)
                        chat.journal.append("steer_failed", text=text)
            except Exception as e:  # noqa: BLE001
                self.log(f"could not restore chat {chat_id}: {e}")

    # -- graceful restart
    def busy_chats(self) -> list:
        """The server's own chats still replying, or with messages waiting to run."""
        return [c for c in self.chats.values() if isinstance(c, Chat) and (c.busy or c._pending)]

    def request_restart(self, by: str) -> int:
        """Restart once every chat is quiet: nothing is cut off mid-reply. Returns how many chats it
        is waiting on (0: it restarts now). Asking again while one is pending changes nothing."""
        waiting = len(self.busy_chats())
        if not self.restart_wanted:
            self.restart_wanted = by or "someone"
            self.log(f"restart asked for ({self.restart_wanted})" + (f"; waiting on {waiting} chat{'s' * (waiting != 1)} to finish" if waiting else ""))
            if waiting:
                self.post({"type": "toast", "text": "Vision restarts once the current replies finish."})
        self.maybe_restart()
        return waiting

    def maybe_restart(self) -> None:
        """Restart now if one was asked for and nothing is running. From here on a new message is
        held (Chat.queue) and journalled, and the next process runs it (restore_chats)."""
        if not self.restart_wanted or self.restarting or self.busy_chats():
            return
        if self.exec_restart is None:
            return
        self.restarting = True
        self.log("restarting…")
        self.post({"type": "toast", "text": "Restarting Vision…"})
        write_restart_marker()
        if self.loop is not None:
            self.loop.call_later(0.4, self.exec_restart)  # let the toast reach the phone first
        else:
            self.exec_restart()

    def close_chat(self, chat: Chat, already_cancelled: bool = False) -> None:
        if isinstance(chat, Chat):
            chat.shut()
        if isinstance(chat, Chat) and chat.journal:
            chat.journal.append("close")
        if not already_cancelled:
            chat.cancel()
        if isinstance(chat, Chat):
            close = getattr(chat.brain, "close", None)
            if close is not None:
                close()  # a process kept between turns goes with the chat
        if isinstance(chat, Chat) and chat.journal:
            chat.journal.release()
        self.chats.pop(chat.id, None)
        if chat.id in self.pinned:
            self.pin(chat.id, False, announce=False)  # a closed chat is gone from every list: its pin goes with it
        # No replacement: the phone shows an empty screen and opens a chat with its next message.

    # -- search: what was said in the open chats, for an app's search box
    SEARCH_LIMIT = 30

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[dict]:
        """The open chats where every word of `query` turns up in one message (or in the title), newest
        chat first, each with the message that matched as a snippet around its first hit. Scout and
        project chats are searched too. Case is ignored; a chat with nothing matching is left out."""
        words = [w for w in query.lower().split() if w][:8]
        if not words or len(query.strip()) < 2:
            return []
        hits = []
        for chat in sorted(list(self.chats.values()), key=lambda c: c.updated, reverse=True):
            title = chat.title or ""
            found = None
            try:
                rows = chat.history(200)
            except Exception:  # noqa: BLE001  - a transcript that can't be read is not a search error
                rows = []
            for row in reversed(rows):  # the most recent mention first
                text = str(row.get("text") or "")
                low = text.lower()
                if all(w in low for w in words):
                    found = {"role": row.get("role") or "", "snippet": snippet(text, words[0])}
                    break
            if found is None and all(w in title.lower() for w in words):
                found = {"role": "title", "snippet": ""}
            if found:
                hits.append({"chat": chat.id, "title": title, "project": getattr(chat, "project", "") or "", **found})
                if len(hits) >= limit:
                    break
        return hits

    # -- pinned chats: kept at the top of every app's list, the same on each
    def is_pinned(self, chat_id: str) -> bool:
        return chat_id in self.pinned

    def pin(self, chat_id: str, on: bool, announce: bool = True) -> None:
        with self._lock:
            if on == (chat_id in self.pinned):
                return
            (self.pinned.add if on else self.pinned.discard)(chat_id)
            save_pinned(self.pinned)
        chat = self.chats.get(chat_id)
        if announce and chat is not None:
            self.post(chat.summary())

    # -- terminal chats (link.py): found on disk, listed while their process lives
    LINK_POLL = 2.0

    def _attach_link(self, info: dict) -> None:
        from vision.link import LinkClient

        pid = int(info["pid"])
        if pid in self.links:
            return
        chat = LinkedChat(self, info)
        client = LinkClient(info, on_event=chat.on_event, on_close=lambda: self._drop_link(pid))
        try:
            client.connect()
        except OSError:
            return  # not listening yet, or gone; next sweep decides
        chat.link = client
        self.links[pid] = chat
        self.chats[chat.id] = chat
        self.log(f"terminal chat joined (pid {pid})")

    def _drop_link(self, pid: int) -> None:
        chat = self.links.pop(pid, None)
        if chat is None:
            return
        self.chats.pop(chat.id, None)
        self.post({"type": "chat_closed", "chat": chat.id})
        self.log(f"terminal chat left (pid {pid})")

    def sweep_links(self) -> None:
        from vision.link import list_links

        seen = set()
        for info in list_links():
            seen.add(int(info["pid"]))
            self._attach_link(info)
        for pid in list(self.links):
            if pid not in seen:
                self.links[pid].detach()  # descriptor gone: the terminal is closing
        self.dedupe()  # a copy that was mid-turn when its terminal showed up closes once idle

    def dedupe(self) -> None:
        """One conversation, one chat. When a terminal holds the same session as a chat this server
        runs (the one `vision serve` continued at start-up, or one resumed on both sides), the terminal
        keeps it and the server's copy closes as soon as it is idle; phones follow to the terminal row."""
        owners = {(c.provider, c.session_id): c for c in list(self.links.values()) if c.session_id}
        if not owners:
            return
        with self._dedupe_lock:  # runs on link threads and the sweep
            for chat in list(self.chats.values()):
                if chat.source != "server":
                    continue
                owner = owners.get((chat.provider, chat.session_id))
                asking = chat._question is not None and not chat._question.done()
                if owner is None or chat.busy or chat._pending or asking:
                    continue
                self.close_chat(chat)
                self.post({"type": "chat_closed", "chat": chat.id, "moved_to": owner.id})
                self.log(f"[{chat.id[:4]}] closed: its conversation is open in a terminal (pid {owner.pid})")

    async def _watch_links(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(None, self.sweep_links)
            except Exception as e:  # noqa: BLE001
                self.log(f"link sweep failed: {e}")
            await asyncio.sleep(self.LINK_POLL)

    # -- terminal windows following a chat (remote.py); only touched on the event loop
    OPEN_GRACE = 15.0  # seconds a launched window has to dial in before another tap may open a second

    def mirror_joined(self, chat_id: str, ws, pid: int | None) -> None:
        self.mirrors.setdefault(chat_id, {})[ws] = pid
        self._opening.pop(chat_id, None)

    def mirror_left(self, chat_id: str, ws) -> None:
        windows = self.mirrors.get(chat_id, {})
        windows.pop(ws, None)
        if not windows:
            self.mirrors.pop(chat_id, None)

    def opening(self, chat_id: str) -> bool:
        """A window was launched for this chat moments ago and is still starting up."""
        return time.monotonic() - self._opening.get(chat_id, float("-inf")) < self.OPEN_GRACE

    # -- scheduled tasks (scheduled.py): each run opens a fresh chat and sends the task's prompt
    SCHEDULE_POLL = 20.0

    def task_chat(self, task: dict):
        """The chat a task was set from, while it is still open (see scheduled.py: `chat`)."""
        chat = self.chats.get(task.get("chat") or "")
        return None if chat is None or getattr(chat, "closed", False) else chat

    def scout_from_task(self, task: dict) -> dict:
        """A scout whose goal is a scheduled task's prompt, running on its schedule: what a timed job
        that isn't tied to a chat became. Returns the project; ValueError when the task doesn't make one."""
        workdir = ""
        if task.get("workdir"):
            try:
                workdir = usable_folder(task["workdir"])
            except ValueError:
                pass  # gone since: the default folder, as the task would have run in
        if not workdir:
            workdir = os.path.abspath(os.path.expanduser(self.cfg.brain.workdir)) if self.cfg.brain.workdir else os.getcwd()
        project = self.projects.upsert({"goal": str(task.get("prompt") or ""), "title": str(task.get("title") or ""),
                                        "workdirs": [workdir]})
        name = (project.get("scout") or {}).get("name") or project.get("title") or "Scout"
        fields = {k: task[k] for k in ("repeat", "time", "date", "weekday", "every_minutes", "enabled") if k in task}
        try:
            self.schedule.upsert({"kind": "checkin", "project": project["id"], "prompt": "",
                                  "title": f"{name}'s status report", **fields})
        except (TypeError, ValueError):
            self.projects.delete(project["id"])
            raise
        return project

    def run_task(self, task: dict, brain=None, cfg: Config | None = None, note: str = ""):
        """Fire a task; answers with the chat it ran in (None: a reminder whose chat has gone)."""
        home = self.task_chat(task)
        title = task.get("title") or "task"
        if task.get("kind") == "reminder":
            return self.remind(task, home)
        if home is not None:
            # Back in the chat it was set from, behind whatever that chat is doing. The push carries
            # this turn's reply, not the one already under way.
            chat = home
            self._push_done[chat.id] = (title, int(chat.busy) + len(getattr(chat, "_pending", ())))
            chat.queue(f"[Scheduled: {title}] {task['prompt']}", speak=False, voice=False)
        else:
            chat = self.open_chat(brain, cfg, title=task.get("title") or "", note=note)
            if chat.journal:
                chat.journal.append("scheduled", task_id=task["id"])
            chat.updated = time.time()
            self.post(chat.summary())
            self._push_done[chat.id] = (title, 0)
            chat.queue(task["prompt"], speak=False, voice=False)
        self.schedule.ran(task["id"], chat.id)
        self.post({"type": "toast", "text": f"Scheduled: {title} started"})
        self.post({"type": "scheduled"})
        self.log(f"scheduled task {task['id']} → chat {chat.id[:4]}")
        return chat

    def remind(self, task: dict, chat=None):
        """A reminder: the words back to the user, wherever they are. No chat is opened for one."""
        text = f"Reminder: {task['prompt']}"
        if chat is not None:
            chat.remind(text)
        self.schedule.ran(task["id"], chat.id if chat is not None else "")
        self.post({"type": "toast", "text": text})
        self.post({"type": "scheduled"})
        self.push("Reminder", task["prompt"], chat.id if chat is not None else "")
        if shutil.which("notify-send"):  # and on this machine's desktop
            subprocess.Popen(["notify-send", "--app-name=Vision", "Reminder", task["prompt"]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log(f"reminder {task['id']}: {task['prompt'][:60]!r}")
        return chat

    def push(self, title: str, body: str, chat: str = "", project: str = "", thread: str = "", category: str = "",
             approval: str = "") -> None:
        """Notify the phones (push.py), off the loop; quietly nothing when push is not set up. With
        `project` and `thread`, a tap opens that project thread; `category` (with `approval`) gives it
        buttons (push.message)."""
        from vision import push

        if not push.ready(self.cfg.push):
            return

        def go() -> None:
            try:
                failed = push.send(self.cfg.push, title, body, chat, project=project, thread=thread, category=category,
                                   approval=approval)
            except Exception as e:  # noqa: BLE001
                failed = [str(e)]
            for line in failed:
                self.log(f"push failed: {line}")

        threading.Thread(target=go, daemon=True).start()

    async def run_task_off_loop(self, task: dict, why: str = "scheduled"):
        """run_task with its brain made in the executor: a scheduled start never stalls the phones. A
        check-in runs in its project's chat instead (`why`: "now" when the user asked for it)."""
        if task.get("kind") == "checkin":
            chat = await self.checkin(str(task.get("project") or ""), why=why, missed=task.get("missed"))
            self.schedule.ran(task["id"], chat.id if chat is not None else "")
            self.post({"type": "scheduled"})
            return chat
        if task.get("kind") == "reminder" or self.task_chat(task) is not None:
            return self.run_task(task)
        workdir, moved = "", ""
        if task.get("workdir"):
            try:
                workdir = usable_folder(task["workdir"])
            except ValueError as e:
                moved = f"{e}; this run works in the default folder instead."
        brain, cfg, note = await asyncio.get_running_loop().run_in_executor(None, self._prepare_chat, None, workdir)
        return self.run_task(task, brain, cfg, f"{note} {moved}".strip())

    SLEEP_POLL = 10.0
    MOOD_POLL = 60.0

    async def _watch_moods(self) -> None:
        """Moods that change with time alone (still_waiting, proud wearing off) reach the apps."""
        while True:
            await asyncio.sleep(self.MOOD_POLL)
            try:
                self.moods_changed()
            except Exception as e:  # noqa: BLE001
                self.log(f"mood check failed: {e}")

    async def _watch_sleep(self) -> None:
        """Tell the apps when this computer wakes from a suspend, so they can say why work paused."""
        while True:
            await asyncio.sleep(self.SLEEP_POLL)
            gap = self.sleep.tick()
            if gap:
                self.log(f"woke after {(gap[1] - gap[0]) / 60:.0f} min asleep")
                self.post({"type": "slept", "from": round(gap[0], 1), "to": round(gap[1], 1)})

    def fold_tasks_into_scouts(self) -> None:
        """Scheduled tasks from before scouts took them over (on their own, not set from a chat) become
        scouts on the same schedule, and an on-call scout (a leash the apps no longer offer) becomes one
        that asks first with its schedule off, which is what on call did. Once, as the server starts serving."""
        from vision.projects import clean_settings

        for project in self.projects.list(include_archived=True):
            settings = clean_settings({}, project.get("settings"))
            if settings["leash"] == "on_call":
                self.projects.upsert({"id": project["id"], "settings": {**settings, "leash": "checkins"}})
                if (task := self.schedule.for_project(project["id"])) is not None:
                    self.schedule.upsert({"id": task["id"], "enabled": False})
                self.log(f"project {project['id']}: on call is now its schedule off")
        for task in self.schedule.list():
            if task.get("kind") == "task" and not task.get("chat"):
                try:
                    project = self.scout_from_task(task)
                    self.schedule.delete(task["id"])
                    self.log(f"scheduled task {task['id']} is now scout {project['id']}")
                except (TypeError, ValueError, KeyError) as e:
                    self.log(f"scheduled task {task['id']} stays a task: {e}")

    async def _watch_schedule(self) -> None:
        self.fold_tasks_into_scouts()
        while True:
            try:
                for task in self.schedule.due():
                    await self.run_task_off_loop(task)
            except Exception as e:  # noqa: BLE001
                self.log(f"scheduled tasks failed: {e}")
            await asyncio.sleep(self.SCHEDULE_POLL)

    def chat(self, cid: str | None) -> Chat | None:
        if cid:
            return self.chats.get(cid)
        # No id (an older app build): the most recently active chat.
        return max(self.chats.values(), key=lambda c: c.updated, default=None)

    # -- auth
    def authorized(self, token: str | None) -> bool:
        return bool(token) and secrets.compare_digest(token, self.token)

    # -- models (lazy, thread-safe)
    def speaker(self) -> Speaker:
        if not self.cfg.voice.enabled:
            raise VoiceOff()
        with self._lock:
            fresh = self._speaker is None
            if fresh:
                self._speaker = Speaker(self.cfg.voice)
        self._speaker._load()
        if not self.cfg.voice.enabled:  # switched off while it loaded: let it go again
            self.voice_power(False)
            raise VoiceOff()
        if fresh and self.cfg.voice.filler:
            # Its "Hmm." clips, made once and cached on disk; a turn before they are ready just stays quiet.
            sp, phrases = self._speaker, self.cfg.voice.filler_phrases
            threading.Thread(target=lambda: sp.prepare_fillers(phrases), daemon=True).start()
        return self._speaker

    def stt(self):
        if not self.cfg.voice.enabled:
            raise VoiceOff()
        with self._lock:
            if self._stt is None:
                from vision.stt import Transcriber

                self._stt = Transcriber(self.cfg.listen)
        stt = self._stt
        stt.warm_up()
        if not self.cfg.voice.enabled:  # switched off while it loaded
            self.voice_power(False)
            raise VoiceOff()
        return stt

    def voice_power(self, on: bool, save: bool = False) -> None:
        """The voice switch (/voice on|off, the phone's Settings): on loads the voice and the ears in the background;
        off unloads both, so the GPU is free, and refuses to load them again until it is back on. `save`: write
        [voice] enabled to config.toml, which terminals on this machine read too. Blocks while unloading."""
        if save:
            from vision.config import save_voice_enabled

            save_voice_enabled(on)
        was, self.cfg.voice.enabled = self.cfg.voice.enabled, on
        if on:
            if not was:
                self.log("voice on")
                self.warm_up(titles=False)
        else:
            with self._lock:
                gone = [r for r in (self._stt, self._speaker) if r is not None]  # Whisper first, as a terminal does
                self._stt = self._speaker = None
            for resource in gone:
                try:
                    resource.close()
                except Exception as e:  # noqa: BLE001
                    self.log(f"voice did not unload cleanly: {e}")
            if was:
                self.log("voice off: the voice and speech recognition are unloaded")
        if was != on:
            self.post({"type": "voice_power", "on": on})

    def status(self) -> dict:
        """What the server runs for speech, as rows and problems the phone shows as they are (vision.status)."""
        from vision.status import server_status

        return server_status(self.cfg.voice, self.cfg.listen, self._speaker, self._stt, self._stt_error)

    def warm_up(self, titles: bool = True) -> None:
        """Load the ears and the voice in the background so the first spoken turn is quick (not while the
        voice is off), and the continued chats' titles (`titles`)."""
        def ears():
            try:
                stt = self.stt()
                self._stt_error = ""
                self.log(f"ears ready: {stt.device}")
            except Exception as e:  # noqa: BLE001
                self._stt_error = str(e)
                self.log(f"ears unavailable: {e}")

        def voice():
            try:
                sp = self.speaker()
                self.log(f"voice ready: {sp.voice} on {sp.device}")
            except Exception as e:  # noqa: BLE001
                self.log(f"voice unavailable: {e}")

        def load_titles():
            # The continued session's title comes from its transcript on disk.
            for chat in list(self.chats.values()):
                try:
                    chat.history(1)
                except Exception:  # noqa: BLE001
                    continue
                if chat.title:
                    self.post(chat.summary())

        if self.cfg.voice.enabled:
            threading.Thread(target=ears, daemon=True).start()
            threading.Thread(target=voice, daemon=True).start()
        else:
            self.log("voice off: not loading the voice or speech recognition (/voice on, or the phone's Settings)")
        if titles:
            threading.Thread(target=load_titles, daemon=True).start()

    # -- snapshot for a (re)connecting client
    def hello(self) -> dict:
        chats = sorted(self.chats.values(), key=lambda c: c.created)
        return {
            "type": "hello",
            "version": __version__,
            "protocol": PROTOCOL,
            "update": self.update_how,
            "threads_live": self.live and self.live.last,  # the Lock Screen card as it stands; None before any thread changed
            "name": machine_name(),
            "shell": True,  # runs the phone's shell-mode commands (`shell` frames): the app offers `!` only then
            "read_aloud": True,  # POST /speak/pieces: the app puts a read-aloud button under a reply only then
            "widgets": True,  # GET /widgets: the Home Screen widgets have something to ask
            "looking": True,  # takes `looking` frames: an app sends one only then (an older Vision calls it unknown)
            "voice_on": self.cfg.voice.enabled,  # the voice switch; a `voice_power` frame when it changes
            "workdir": chats[0].workdir if chats else "",
            "asleep": self.sleep.recent() if getattr(self, "sleep", None) else [],  # suspends in the last day
            "chats": [c.summary() for c in chats],
            "projects": [self.view_project(p) for p in self.projects.list()],
        }

    # -- project threads (runs.py): work that runs outside any turn
    def project_threads(self, project: dict):
        """The project's threads, loaded from its folder the first time they're asked for."""
        from vision.runs import ProjectThreads

        pid = project["id"]
        with self._lock:
            pt = self.threads.get(pid)
            if pt is None:
                pt = self.threads[pid] = ProjectThreads(
                    self.cfg, project, folder=self.projects.root / pid, base=lambda: self.thread_base(pid),
                    on_change=lambda t: self.thread_changed(pid, t),
                    on_stop=lambda t: self.thread_stopped(pid, t))
                pt.on_activity = lambda e: self.post({"type": "activity", "project": pid, "event": e})
                pt.rules = lambda: self.thread_rules(pid)
                pt.on_approval = lambda tid, tool, args, rule: self.request_approval(pid, tid, tool, args, rule)
        return pt

    def thread_base(self, pid: str):
        """The brain config a project's threads copy: its chat's, as it is now (a /model there reaches the
        next thread), else a new chat's."""
        project = self.projects.get(pid) or {}
        chat = self.project_chat(project)
        if chat is not None:
            return chat.brain.cfg
        cfg = copy.deepcopy(self.cfg)
        if self.follow_defaults and (saved := saved_brain_defaults()) is not None:
            cfg.brain.model, cfg.brain.effort = saved
        return cfg.brain

    GLANCE_USAGE_S = 300  # the widgets' usage is read again after this long (each read is a CLI call per provider)

    def glance(self) -> dict:
        """What the phone's Home Screen widgets show (GET /widgets), in one small answer: the project threads
        as the Lock Screen card counts them, each shown row with its project so a tap opens it; each provider's
        usage windows (providers that can't say are left out); and the next reminder or task to run. Called
        from the executor: the first usage read of a while runs the providers' CLIs."""
        from vision.liveactivity import ROWS, SHOWN, summary
        from vision.usage import usage_all

        threads = []
        for project in self.projects.list():
            pt = self.threads.get(project["id"])
            if pt is None and (self.projects.root / project["id"] / "threads.json").exists():
                pt = self.project_threads(project)
            for t in pt.list() if pt is not None else []:
                threads.append({**t, "project": project["id"], "project_title": project.get("title") or ""})
        counts = summary(threads)
        shown = sorted((t for t in threads if t.get("state") in SHOWN),
                       key=lambda t: (SHOWN.index(t["state"]), -(t.get("updated") or 0)))
        rows = [{"title": str(t.get("title") or "Thread")[:60], "state": t["state"], "project": t["project"],
                 "project_title": t["project_title"]} for t in shown[:ROWS]]
        now = time.time()
        cached = getattr(self, "_glance_usage", None)
        if cached is None or now - cached[0] > self.GLANCE_USAGE_S:
            usage = [{"provider": r["provider"], "label": r.get("label") or r["provider"],
                      "windows": [{"label": w.get("label") or "", "used_percent": w.get("used_percent"),
                                   "resets_at": w.get("resets_at")} for w in r.get("windows") or []]}
                     for r in usage_all(self.cfg, None) if not r.get("error") and r.get("windows")]
            cached = self._glance_usage = (now, usage)
        upcoming = [{"id": t["id"], "title": t.get("title") or t["prompt"][:60], "kind": t.get("kind") or "task",
                     "at": t["next_run"], "repeat": t.get("repeat") or "once"}
                    for t in self.schedule.list() if t.get("enabled") and t.get("next_run")][:3]
        return {
            "name": machine_name(),
            "at": now,
            "threads": {"working": counts["working"], "needsYou": counts["needsYou"], "ready": counts["ready"],
                        "rows": rows, "projects": len(self.projects.list())},
            "usage": cached[1],
            "usage_at": cached[0],
            "next": upcoming[0] if upcoming else None,
            "upcoming": upcoming,
            "chats": self._glance_chats(),
            "computer": self._glance_computer(),
            "scouts": self._glance_scouts(),
        }

    def _glance_scouts(self) -> list[dict]:
        """The Scouts widget: each scout's buddy (name, hue, trait, mood), how many things need the user, and one
        line on where it stands; those that need the user first."""
        from vision.projects import approvals, clean_settings, mood

        out = []
        for project in self.projects.list():
            pt = self.threads.get(project["id"])
            threads = pt.list() if pt is not None else []
            pending = approvals(self.projects.root / project["id"], pending_only=True)
            states = [t.get("state") for t in threads]
            needs = states.count("waiting_on_you") + states.count("ready_for_review") + len(pending)
            leash = clean_settings({}, project.get("settings"))["leash"]
            task = self.schedule.for_project(project["id"]) if hasattr(self.schedule, "for_project") else None
            line = (f"{needs} need{'s' if needs == 1 else ''} you" if needs else "paused" if project.get("status") == "paused"
                    else "working" if "working" in states else "when asked" if leash == "on_call" else "resting")
            scout = project.get("scout") or {}
            out.append({"id": project["id"], "name": scout.get("name") or project.get("title") or "Scout",
                        "hue": scout.get("hue", 205), "trait": scout.get("trait") or "antenna",
                        "mood": mood(project, threads, pending=pending), "needs": needs, "line": line,
                        "next_checkin": (task or {}).get("next_run") if leash != "on_call" else None})
        return sorted(out, key=lambda x: (-x["needs"], x["name"].casefold()))

    def _glance_chats(self, count: int = 3) -> list[dict]:
        """The Recent Chats widget: the chats last spoken in, each with the opening of its latest reply (as
        plain words), or "replying…" while one is coming."""
        from vision.tts import speechify

        out = []
        for chat in sorted(self.chats.values(), key=lambda c: c.updated, reverse=True)[:count]:
            last = ""
            if not chat.busy:
                try:
                    reply = next((m["text"] for m in reversed(chat.history(6)) if m.get("role") == "assistant" and m.get("text")), "")
                except Exception:  # noqa: BLE001  (a session file gone: the chat still lists)
                    reply = ""
                last = " ".join(speechify(reply, code="(code)").split())[:120]
            out.append({"chat": chat.id, "title": chat.title or "New chat", "busy": bool(chat.busy), "last": last,
                        "updated": chat.updated})
        return out

    def _glance_computer(self) -> dict:
        """The Computer widget: how busy this machine is (load per core, memory and disk in use, as percents;
        None where the platform doesn't say), how long it has been up, the voice switch, and an update."""
        load = memory = uptime = None
        try:
            load = round(100 * os.getloadavg()[0] / (os.cpu_count() or 1))
        except OSError:
            pass
        try:
            info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
            total, free = (int(info[k].split()[0]) for k in ("MemTotal", "MemAvailable"))
            memory = round(100 * (total - free) / total)
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            pass
        try:
            disk = shutil.disk_usage(Path.home())
            disk_pct = round(100 * disk.used / disk.total)
        except OSError:
            disk_pct = None
        return {"name": machine_name(), "cpu": load, "memory": memory, "disk": disk_pct, "uptime": uptime,
                "voice_on": self.cfg.voice.enabled, "update": self.update_how}

    def view_project(self, project: dict) -> dict:
        pt = self.threads.get(project["id"])
        if pt is None and (self.projects.root / project["id"] / "threads.json").exists():
            pt = self.project_threads(project)
        view = project_view(project, pt.counts() if pt is not None else None)
        view["tokens_out"] = pt.tokens_out() if pt is not None else 0
        pending = self.pending_approvals(project["id"])
        view["approvals"] = pending
        view["mood"] = self._moods[project["id"]] = self.mood_of(project, pt, pending)
        view["checkin"] = self.checkin_view(project)
        return view

    def pending_approvals(self, pid: str) -> list[dict]:
        from vision.projects import approvals

        return approvals(self.projects.root / pid, pending_only=True)

    def mood_of(self, project: dict, pt=None, pending: list[dict] | None = None) -> str:
        from vision.projects import mood

        pt = pt if pt is not None else self.threads.get(project["id"])
        pending = self.pending_approvals(project["id"]) if pending is None else pending
        return mood(project, pt.list() if pt is not None else [], pending=pending)

    # -- a scout's rules and approvals (projects.py): what asks first becomes a request the user decides
    def thread_rules(self, pid: str) -> tuple[list[str], list[str]]:
        """(ask, deny) permission rules for the project's threads: [brain]'s own, plus the project's."""
        from vision.projects import rule_patterns

        ask, deny = rule_patterns(self.projects.get(pid), "thread")
        own_ask, own_deny = self._own_rules
        return list(dict.fromkeys(own_ask + ask)), list(dict.fromkeys(own_deny + deny))

    def request_approval(self, pid: str, target: str, tool: str, args: dict, rule: str | None) -> str | None:
        """File an approval request for a call that asks first (from a thread's worker, or the coordinator during
        a check-in): kept in approvals.json, an `approval` frame, a line of activity, and a push unless notify is
        none. Called from worker threads. Returns the approval's id."""
        from vision.projects import add_approval, log_activity

        project = self.projects.get(pid)
        if project is None:
            return None
        folder = self.projects.root / pid
        pt = self.threads.get(pid)
        thread = pt.store.get(target) if pt is not None and target != "coordinator" else None
        name = (project.get("scout") or {}).get("name") or project.get("title") or "Your scout"
        who = (thread or {}).get("title") or name
        approval, new = add_approval(folder, target=target, tool=tool, tool_input=args, rule=rule, title=who)
        if not new:
            return approval["id"]
        self.log(f"project {pid}: approval {approval['id']} asked by {target}: {approval['command'][:80]}")
        event = log_activity(folder, "approval", f"Asks to run: {approval['command']}", thread=target if thread else "")
        self.post({"type": "activity", "project": pid, "event": event})
        self.post({"type": "approval", "project": pid, "approval": approval})
        self.post_project(project)
        if ((project.get("settings") or {}).get("notify") or "needs_you") != "none":
            self.push(f"{name} asks to run", approval["command"][:180], project.get("chat") or "", project=pid,
                      thread=target if thread else "", category="approval", approval=approval["id"])
        return approval["id"]

    def decide(self, pid: str, aid: str, approve: bool, command: str = "") -> dict:
        """The user approved (as it was, or with `command` edited) or denied a request: the caller is let through
        once and told to go ahead, or told it was declined if it's waiting on the answer. KeyError: no such
        approval; ValueError: decided already."""
        from vision.projects import approved_call, decide_approval, log_activity

        project = self.projects.get(pid)
        if project is None:
            raise KeyError(pid)
        folder = self.projects.root / pid
        approval = decide_approval(folder, aid, approve, command)
        call = approved_call(approval)
        target = approval["target"]
        pt = self.project_threads(project) if target != "coordinator" else None
        thread = pt.store.get(target) if pt is not None else None
        if approve:
            told = f"The user approved: {call}" + (" (as they edited it)" if approval.get("edited") else "")
            tell = f"{told}. Run exactly that now, then carry on with the work."
        else:
            told = f"The user declined: {approval['command']}"
            tell = f"{told}. Don't run it or work around it; carry on without it, or say what you'd do instead."
        if thread is not None and thread["state"] != "resolved":
            if approve:
                pt.allow(target, call)
            if approve or thread["state"] == "waiting_on_you":
                try:
                    pt.tell(target, tell)
                except (KeyError, ValueError) as e:
                    self.log(f"project {pid}: couldn't tell thread {target} about approval {aid}: {e}")
        elif (approve or target == "coordinator") and self.loop is not None and not self.loop.is_closed():
            # The coordinator (or a closed thread's work, which it would pick up): told in its chat, on the loop.
            done = asyncio.run_coroutine_threadsafe(
                self._tell_coordinator(project, call if approve else "", f"{APPROVAL_PREFIX}{told}\n{tell}"), self.loop)
            done.add_done_callback(lambda f: not f.cancelled() and f.exception() and self.log(f"project {pid}: approval {aid} not passed on: {f.exception()}"))
        event = log_activity(folder, "approved" if approve else "denied", f"{'Approved' if approve else 'Declined'}: {call}",
                             thread=target if thread else "")
        self.post({"type": "activity", "project": pid, "event": event})
        self.post({"type": "approval", "project": pid, "approval": approval})
        self.post_project(project)
        return approval

    async def _tell_coordinator(self, project: dict, call: str, text: str) -> None:
        """What the user decided, to the project's chat (opened if it isn't), with `call` let through once."""
        try:
            chat = await self.open_project_chat(project)
        except Exception as e:  # noqa: BLE001
            self.log(f"project {project['id']}: couldn't open its chat for an approval: {e}")
            return
        if call:
            if not isinstance(getattr(chat.brain, "allowed_once", None), list):
                chat.brain.allowed_once = []  # a brain Vision doesn't gate (the rules only reach it in its prompt)
            chat.brain.allowed_once.append(call)
        chat.queue(text, speak=False, voice=False)

    def checkin_view(self, project: dict) -> dict:
        """A scout's check-ins as the apps show them: its schedule (None: no schedule), when the next is
        due, the last one ({started, ended, needs_you}) and whether one is running now."""
        from vision.projects import checkins

        task = self.schedule.for_project(project["id"]) if getattr(self, "schedule", None) else None
        records = checkins(self.projects.root / project["id"])
        schedule = None
        if task is not None:
            schedule = {k: task.get(k) for k in ("id", "repeat", "time", "weekday", "every_minutes", "enabled", "next_run")}
        running = next((started for cid, started in self._checkins.items() if project.get("chat") == cid), None)
        return {"schedule": schedule, "last": records[-1] if records else None, "running": running}

    # -- check-ins: a scout's turn on its own, on its schedule
    CHECKIN_GRACE_S = 5 * 60  # added to the project's `length` before a check-in that runs on is stopped

    async def checkin(self, pid: str, why: str = "now", missed: float | None = None):
        """A check-in turn in the project's chat (opened if it isn't): what changed since the last one, then
        work and a report. None when it doesn't run: no such project, archived or paused, already checking
        in, or (scheduled) a scout on call, which only works when it's given something."""
        from vision.projects import checkin_label, checkin_message, checkins, log_activity
        from vision.runs import LENGTHS

        project = self.projects.get(pid)
        if project is None or project.get("status") in ("archived", "paused"):
            self.log(f"project {pid}: check-in skipped ({'gone' if project is None else project['status']})")
            return None
        if why == "scheduled" and (project.get("settings") or {}).get("leash", (project.get("settings") or {}).get("autonomy")) in ("on_call", "ask"):
            self.log(f"project {pid}: check-in skipped (on call)")
            return None
        chat = self.project_chat(project)
        if chat is None:
            try:
                chat = await self.open_project_chat(project)
            except Exception as e:  # noqa: BLE001
                self.log(f"project {pid}: couldn't open its chat for a check-in: {e}")
                return None
        if time.time() - self._checkins.get(chat.id, 0.0) < 3 * 3600:
            return chat  # one at a time (one that never got to run stops counting after a while)
        folder = self.projects.root / pid
        records = checkins(folder)
        since = float(records[-1]["started"]) if records else float(project.get("created") or 0) or None
        started = time.time()
        changes = await asyncio.get_running_loop().run_in_executor(None, self.checkin_changes, project, since)
        asleep = sum(max(0.0, min(b, started) - max(a, since or started)) for a, b in self.sleep.gaps) if since else 0.0
        self._checkins[chat.id] = started
        chat.auto_steps += 1  # not the user's word: it counts towards the coordinator's limit, as a report does
        label = checkin_label(started)
        self.log(f"project {pid}: {label.lower()} ({why})")
        event = log_activity(folder, "checkin", f"Woke for its {label.lower()}")
        self.post({"type": "activity", "project": pid, "event": event})
        self.post({"type": "checkin", "project": pid, "chat": chat.id, "state": "started", "label": label, "started": round(started, 1)})
        self.post_project(project)
        chat.queue(checkin_message(started, since, changes, missed=missed, asleep_s=asleep, why=why), speak=False, voice=False)
        # A check-in runs no longer than one go of a thread may (the project's `length`), then it's stopped.
        limit = LENGTHS.get((project.get("settings") or {}).get("length") or "normal", LENGTHS["normal"])[1][0] * 60
        if self.loop is not None:
            self.loop.call_later(limit + self.CHECKIN_GRACE_S, self._checkin_overrun, chat.id, started)
        return chat

    def checkin_changes(self, project: dict, since: float | None) -> list[str]:
        """What a check-in is told changed since the last: the threads' activity and new commits in the
        project's folder (on any branch, so the threads' own count too)."""
        from vision.projects import activity

        lines = [e["text"] for e in reversed(activity(self.projects.root / project["id"], limit=15, since=since or 0))
                 if e.get("kind") != "checkin"]
        dirs = project.get("workdirs") or []
        if dirs and since:
            try:
                out = subprocess.run(["git", "-C", dirs[0], "log", "--all", f"--since=@{int(since)}", "--format=%h %s", "-n", "12"],
                                     capture_output=True, text=True, timeout=10)
                commits = [ln for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []
            except (OSError, subprocess.SubprocessError):
                commits = []
            if commits:
                lines.append(f"{len(commits)}{'+' if len(commits) == 12 else ''} new commit{'s' if len(commits) != 1 else ''} in "
                             f"{tilde(dirs[0])}: " + "; ".join(commits))
        return lines

    def _checkin_overrun(self, chat_id: str, started: float) -> None:
        """A check-in still going past its limit is stopped; its report says so."""
        if self._checkins.get(chat_id) != started:
            return
        chat = self.chats.get(chat_id)
        if chat is not None and chat.busy:
            self.log(f"chat {chat_id[:4]}: check-in ran past its limit, stopping it")
            threading.Thread(target=chat.cancel, daemon=True).start()

    def checkin_done(self, chat, text: str, reply: str, error: str) -> None:
        """A check-in turn ended: kept (checkins.jsonl), a `checkin` frame and a line of activity, and one
        push by the project's notify setting: always with `all`, only when something needs the user with
        `needs_you`."""
        from vision.projects import SUMMARY_CHARS, checkin_of, log_activity, record_checkin

        info = checkin_of(text) or {}
        started = float(info.get("started") or self._checkins.get(chat.id) or time.time())
        self._checkins.pop(chat.id, None)
        project = self.projects.get(chat.project)
        if project is None:
            return
        pid = project["id"]
        pt = self.threads.get(pid)
        counts = pt.counts() if pt is not None else {}
        needs = int(counts.get("waiting_on_you", 0)) + int(counts.get("ready_for_review", 0)) + len(self.pending_approvals(pid))
        ended = time.time()
        folder = self.projects.root / pid
        record = {"started": round(started, 1), "ended": round(ended, 1), "needs_you": needs, "chat": chat.id,
                  **({"error": error} if error else {})}
        try:
            record_checkin(folder, record)
        except OSError as e:
            self.log(f"project {pid}: couldn't keep the check-in: {e}")
        name = (project.get("scout") or {}).get("name") or project.get("title") or "Your scout"
        words = f"{needs} need{'s' if needs == 1 else ''} you" if needs else "nothing needs you"
        event = log_activity(folder, "checkin_done", f"Status report: {words}" if not error else f"Status report stopped: {error}")
        self.post({"type": "activity", "project": pid, "event": event})
        self.post({"type": "checkin", "project": pid, "chat": chat.id, "state": "done", "label": info.get("label") or "Status report",
                   "started": round(started, 1), "ended": round(ended, 1), "summary": (reply or "")[:SUMMARY_CHARS],
                   "needs_you": needs, **({"error": error} if error else {})})
        self.post_project(project)
        notify = (project.get("settings") or {}).get("notify") or "needs_you"
        if notify == "all" or (notify == "needs_you" and (needs or error)):
            first = next((ln.strip() for ln in (reply or "").splitlines() if ln.strip()), "")
            self.push(f"{name} sent a status report" + (f" · {words}" if needs else ""), error or first[:180] or words, chat.id, project=pid)

    def moods_changed(self) -> None:
        """Projects whose scout's mood is no longer the one the apps were shown (a thread changed, or time
        passed: needs_you turns still_waiting, proud wears off) go out again as `project` frames."""
        for project in self.projects.list():
            now = self.mood_of(project)
            if self._moods.get(project["id"], now) != now:
                self.post_project(project)

    def project_status(self, pid: str, recap: str = "") -> str:
        """The block a turn in the project's chat starts with (projects.status_block): its threads as they
        are now, the project memory and the newest of its Library (`recap`: a fresh session's catch-up).
        Threads long finished without changes are closed first (ProjectThreads.sweep), so the coordinator
        starts fresh ones rather than reuse them."""
        from vision.brain import Brain
        from vision.library import project_entries
        from vision.projects import memory_lines, status_block, usage_warning

        project = self.projects.get(pid)
        if project is None:
            return ""
        folder = self.projects.root / pid
        pt = self.threads.get(pid) or (self.project_threads(project) if (folder / "threads.json").exists() else None)
        if pt is not None and pt.sweep():
            self.post_project(project)
        threads = pt.list() if pt is not None else []
        return status_block(project, threads, memory_lines(folder), project_entries(folder),
                            warning=usage_warning(Brain.cached_usage(), threads), recap=recap)

    def thread_changed(self, pid: str, thread: dict) -> None:
        self.post({"type": "thread", "project": pid, "thread": thread_view(thread, full=False)})
        if (project := self.projects.get(pid)) is not None and (pt := self.threads.get(pid)) is not None:
            if self._moods.get(pid) != self.mood_of(project, pt):
                self.post_project(project)
        # The Lock Screen card (liveactivity.py): to the phones when what it shows changed, and to Apple.
        state = self.live and self.live.changed([t for pt in list(self.threads.values()) for t in pt.list()])
        if state is not None:
            self.post({"type": "threads_live", "state": state})

    def thread_stopped(self, pid: str, thread: dict) -> None:
        """A thread stopped (called from its worker's thread): a push by the project's notify setting
        (projects.should_push), and a report turn for the project's coordinator a moment later, so
        threads stopping together come in one turn."""
        from vision.projects import push_kind, should_push, stop_line

        project = self.projects.get(pid)
        if project is None or thread["state"] == "resolved":
            return
        self.post_project(project)
        kind = push_kind(thread)
        pt = self.threads.get(pid)
        if should_push((project.get("settings") or {}).get("notify", "needs_you"), kind, thread.get("last_stop") or ""):
            ready = thread["state"] == "ready_for_review" and bool(thread.get("branch"))
            self.push(project["title"], stop_line(thread)[0], project.get("chat") or "", project=pid, thread=thread["id"],
                      **({"category": "land"} if ready else {}))  # finished with a branch: Land on the notification
        if pt is not None and pt.store.get(thread["id"]) is not None:
            pt.store.update(thread["id"], last_stop=kind)
        # A question or a failure reaches the coordinator straight away; plain finishes wait a little, so
        # threads finishing near each other cost the coordinator one turn, not one each.
        self.report_soon(pid, delay=self.REPORT_BATCH_S if kind == "done" else self.REPORT_URGENT_S)

    def voice_project(self, pid: str, status: str) -> dict | None:
        """The project as the conversation model on a call gets it (VoiceConversation.project): the threads
        it can address by name, the status block as data, and what starting, telling and closing one does."""
        from vision.projects import thread_state_words

        project = self.projects.get(pid)
        if project is None:
            return None
        pt = self.project_threads(project)
        listed = pt.list()
        shown = [t for t in listed if t["state"] != "resolved"] + [t for t in listed if t["state"] == "resolved"][-5:]

        def latest(t: dict) -> str:
            r = t.get("last_result") or {}
            return str(t.get("question") or t.get("error") or r.get("summary") or "")[:200]

        def start(body: str, title: str) -> dict:
            t = pt.start(body, title=title)
            self.log(f"project {pid}: thread {t['id']} started on a call {t['title']!r}")
            self.post_project(self.projects.get(pid) or project)
            return t

        def tell(tid: str, text: str) -> dict:
            t = pt.tell(tid, text)
            self.post_project(self.projects.get(pid) or project)
            return t

        def close(tid: str) -> dict:
            t = pt.resolve(tid)
            self.post_project(self.projects.get(pid) or project)
            return t

        return {"id": pid, "title": project["title"], "goal": project["goal"], "status": status,
                "threads": [{"id": t["id"], "title": t["title"], "state": thread_state_words(t), "latest": latest(t)} for t in shown],
                "start": start, "tell": tell, "close": close}

    REPORT_BATCH_S = 90.0  # threads finishing within this of the first report in one turn
    REPORT_URGENT_S = 3.0  # …and one that needs the user or failed, after only this

    def report_soon(self, pid: str, delay: float | None = None) -> None:
        """Bring the coordinator the threads that stopped since it last heard, after a short wait, from any thread."""
        if self.loop is None or self.loop.is_closed():
            return
        wait = self.REPORT_BATCH_S if delay is None else delay

        def schedule() -> None:
            self.loop.call_later(wait, lambda: asyncio.ensure_future(self.report(pid)))
        self.loop.call_soon_threadsafe(schedule)

    async def report(self, pid: str) -> None:
        """A report turn in the project's chat for every thread that stopped unreported (its chat opened if it
        has gone). A chat that's busy gets it when its replies are done (Chat.run_turn calls back)."""
        from vision.projects import report_message

        project = self.projects.get(pid)
        pt = self.threads.get(pid)
        if project is None or pt is None or project.get("status") == "archived" or self.restarting:
            return
        due = pt.unreported()
        if not due:
            return
        chat = self.project_chat(project)
        if chat is None:
            try:
                chat = await self.open_project_chat(project)
            except Exception as e:  # noqa: BLE001
                self.log(f"project {pid}: couldn't open its chat for a report: {e}")
                return
        if chat.busy or chat._pending or chat.closed:
            return  # after the reply under way (run_turn's end asks again)
        pt.mark_reported(t["id"] for t in due)
        chat.auto_steps += 1
        spoken = chat.on_call()  # the user is on a call with this chat: the report is said, in a line, by the call's model
        self.log(f"project {pid}: reporting {len(due)} thread{'s' if len(due) != 1 else ''} to its chat"
                 + (" (on a call)" if spoken else ""))
        chat.queue(report_message(due, chat.auto_steps), speak=spoken, voice=spoken)

    def restore_threads(self) -> None:
        """At start-up: threads that were working when Vision last stopped pick their sessions back up, and
        reports owed to a project's coordinator are made."""
        for project in self.projects.list():
            if (self.projects.root / project["id"] / "threads.json").exists():
                pt = self.project_threads(project)
                if any(t["state"] == "working" for t in pt.list()):
                    self.log(f"project {project['id']}: resuming its threads")
                    pt.restore()
                if pt.unreported():
                    self.report_soon(project["id"], delay=5.0)

    # -- projects (projects.py): a goal, a folder and instructions around one chat
    def project_chat(self, project: dict):
        """The project's chat while it is open here."""
        chat = self.chats.get(project.get("chat") or "")
        return chat if isinstance(chat, Chat) and not chat.closed else None

    def post_project(self, project: dict) -> None:
        if project.get("status") == "archived":
            self.post({"type": "project_removed", "project": project["id"]})
        else:
            self.post({"type": "project", "project": self.view_project(project)})

    def project_saved(self, project: dict, before: dict | None = None) -> None:
        """A project made or edited (`before`: as it was): its open chat takes the new goal and instructions
        (and title), and every phone hears of it. Paused: its threads stop where they are, to carry on at
        resume. Archived (retired): its working threads stop and its chat stays its own, so bringing it back
        (resume) finds it as it was. A new coordinator model reaches the open chat as a /model would."""
        was = (before or {}).get("status")
        now = project.get("status")
        if (pt := self.threads.get(project["id"])) is not None or now != was == "paused":
            pt = pt or self.project_threads(project)
            pt.project = project
            if now == "archived":
                for t in pt.list():
                    if t["state"] == "working":
                        pt.stop(t["id"])
            elif now == "paused" and was != "paused":
                self.log(f"project {project['id']}: paused")
                threading.Thread(target=pt.pause, daemon=True).start()  # cancelling workers waits on them
            elif was == "paused" and now == "active":
                self.log(f"project {project['id']}: resumed")
                pt.resume()
            elif was == "archived" and now == "active":
                self.log(f"project {project['id']}: brought back")
        chat = self.project_chat(project)
        if chat is not None:
            chat.set_project(project)
            if now != "archived":
                chat.title = project["title"]
                old, new = (before or {}).get("settings") or {}, project.get("settings") or {}
                if before is not None and (old.get("coordinator_model"), old.get("coordinator_effort")) != \
                        (new.get("coordinator_model"), new.get("coordinator_effort")) and new.get("coordinator_model"):
                    chat.switch_when_free(new["coordinator_model"], new.get("coordinator_effort") or None)
            self.post(chat.summary())
        self.post_project(project)

    def delete_project(self, project: dict) -> list[str]:
        """A retired project gone for good: its threads stopped and their worktrees removed (one with work
        that's on no other branch is kept), its check-ins, its chat and its folder. The work folder itself
        is never touched. Returns a line for each worktree kept."""
        from vision.runs import remove_worktree

        pid = project["id"]
        pt = self.threads.get(pid)
        if pt is None and (self.projects.root / pid / "threads.json").exists():
            pt = self.project_threads(project)
        kept = []
        for t in pt.list() if pt is not None else []:
            if t["state"] == "working":
                pt.stop(t["id"])
            if t.get("worktree") and (why := remove_worktree(t["worktree"], t.get("branch") or "")):
                kept.append(f"{tilde(t['worktree'])} kept: {why}")
        with self._lock:
            self.threads.pop(pid, None)
        self._moods.pop(pid, None)
        if (task := self.schedule.for_project(pid)) is not None:
            self.schedule.delete(task["id"])
            self.post({"type": "scheduled"})
        if (chat := self.project_chat(project)) is not None:
            chat.shut()
            chat.cancel()
            self.close_chat(chat, already_cancelled=True)
            self.post({"type": "chat_closed", "chat": chat.id})
        self.projects.delete(pid)
        self.log(f"project deleted: {pid} {project.get('title')!r}" + "".join(f"; {k}" for k in kept))
        self.post({"type": "project_removed", "project": pid})
        return kept

    async def open_project_chat(self, project: dict) -> Chat:
        """The project's chat: the open one, or a new one in the project's folder (the first; the server's
        own when it has gone), remembered on the project. Two phones asking at once get the same chat."""
        project = self.projects.get(project["id"]) or project  # as it is now: its chat may have changed since it was read
        if (chat := self.project_chat(project)) is not None:
            return chat
        pending = self._project_opening.get(project["id"])
        if pending is not None:
            return await asyncio.shield(pending)
        fut = asyncio.get_running_loop().create_future()
        self._project_opening[project["id"]] = fut
        try:
            workdir, note = "", ""
            for folder in project.get("workdirs") or []:
                try:
                    workdir = usable_folder(folder)
                    break
                except ValueError as e:
                    note = f"{e}; this chat works in the default folder instead."
            settings = project.get("settings") or {}
            # The coordinator's own model when the project names one; else a new chat's.
            picked = self.picked_cfg(settings.get("coordinator_model"), settings.get("coordinator_effort"))
            brain, cfg, retired = await asyncio.get_running_loop().run_in_executor(None, lambda: self._prepare_chat(picked, workdir))
            chat = self.open_chat(brain, cfg, title=project["title"], note=" ".join(x for x in (retired, note) if x))
            chat.set_project(project)
            saved = self.projects.set_chat(project["id"], chat.id) or project
            self.log(f"project {project['id']} → chat {chat.id[:4]}")
            self.post(chat.summary())
            self.post_project(saved)
            fut.set_result(chat)
            return chat
        except Exception as e:
            fut.set_exception(e)
            fut.exception()  # read here, so a failure nobody else awaited isn't logged as never retrieved
            raise
        finally:
            if not fut.done():
                fut.cancel()
            self._project_opening.pop(project["id"], None)

    def watch_updates(self) -> None:
        """Ask the git remote now, every few minutes and whenever an app comes into view whether a newer
        Vision is out, and tell the phones as soon as the answer changes, not only on their next connect."""
        def changed(check: selfupdate.Check) -> None:
            self.update_how = selfupdate.how() if check.available else None
            if check.available:
                self.log(check.status())
            self.post({"type": "update", "update": self.update_how})

        selfupdate.watch(changed)

    # -- event fan-out (events queued from any thread, sent in order from the loop)
    def post(self, ev: dict) -> None:
        if ev.get("type") == "done" and ev.get("chat") in self._push_done:
            # A scheduled task's reply is in: tell the phones, which may have the app shut.
            title, before = self._push_done[ev["chat"]]
            if before > 0:
                self._push_done[ev["chat"]] = (title, before - 1)
            else:
                del self._push_done[ev["chat"]]
                self.push(title, ev.get("error") or ev.get("text") or "Done.", ev["chat"])
        if self.loop and self.events:
            self.loop.call_soon_threadsafe(self.events.put_nowait, ev)

    async def broadcast(self, ev: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(ev)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def pump(self) -> None:
        assert self.events
        while True:
            ev = await self.events.get()
            await self.broadcast(ev)

    # -- incoming frames
    async def handle(self, ws, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ping":
            await ws.send_json({"type": "pong"})
            return
        if kind == "looking":
            selfupdate.nudge()
            return
        if kind == "new" and msg.get("project"):
            # A project's chat: the open one, or a new one in the project's folder.
            project = self.projects.get(str(msg["project"]))  # a retired one's too: its chat is still its own
            if project is None:
                await ws.send_json({"type": "error", "text": "That project is gone."})
                return
            try:
                chat = await self.open_project_chat(project)
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "text": f"could not open the project's chat: {e}"})
                return
            await ws.send_json({**chat.summary(), "opened": True})
            return
        if kind == "new":
            # `model`/`effort`: what the phone picked with no chat open. It is for this chat only; the
            # saved default (Settings, /default) is left alone. The config read, the provider probe and
            # the brain happen in the executor: on the loop they held every phone's stream meanwhile.
            workdir = str(msg.get("workdir") or "").strip()
            if workdir:
                try:
                    workdir = usable_folder(workdir)
                except ValueError as e:
                    await ws.send_json({"type": "error", "text": str(e)})
                    return
            prepared = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._prepare_chat(self.picked_cfg(msg.get("model"), msg.get("effort")), workdir))
            chat = self.open_chat(*prepared[:2], note=prepared[2])
            await ws.send_json({**chat.summary(), "opened": True})  # the asker switches to it
            self.post(chat.summary())                                # everyone else just lists it
            return
        if kind == "resume":
            await self.resume(ws, msg)
            return
        if kind == "hush":
            # The phone hung up a call: the chat it was listening to stops sending speech (the text
            # carries on). Other chats keep talking to whoever is on a call with them. An older app
            # names no chat: then every reply goes quiet, as before.
            if "chat" in msg:
                hushed = [c for c in (self.chat(msg.get("chat")),) if c is not None]
            else:
                hushed = list(self.chats.values())
            for chat in hushed:
                chat.hush()
            return
        if kind == "restart" or (kind == "message" and (msg.get("text") or "").strip().lower() == "/restart"):
            self.request_restart("phone")  # it tells every phone what happens next
            return
        chat = self.chat(msg.get("chat"))
        if chat is None:
            await ws.send_json({"type": "error", "text": "that chat is gone"})
            return
        plan = plan_question(chat._questions) if kind == "message" and (msg.get("text") or "").strip() and not msg.get("images") else None
        if plan:
            # A plan waiting for approval: what the user says next (by voice, usually) is the answer.
            chat.answer(plan_answer(plan, msg["text"]))
        elif kind == "message":
            if isinstance(chat, Chat) and chat.project and msg.get("images"):
                # Sent inside a project: it joins the project's Library too.
                from vision.library import index_project

                sent = [{"path": p, "source": "you"} for p in msg["images"] if isinstance(p, str) and p.startswith(str(UPLOAD_DIR))]
                await asyncio.to_thread(index_project, self.projects.root / chat.project, sent)
            text = with_attachments((msg.get("text") or "").strip(), msg.get("images"))
            if msg.get("shell") and isinstance(chat, Chat):
                chat.shell_notes.append(str(msg["shell"]))  # a joined terminal's `!` commands, for the brain
            # `now`: send it into the running turn (Claude takes it at its next step); else it queues.
            # Said (or typed) on a call while the call's own turn runs, it goes in too: talking to Vision
            # while an agent works shouldn't wait for the agent. A terminal chat decides that itself.
            spoken = bool(msg.get("voice") or msg.get("talk")) and isinstance(chat, Chat) and chat.talks_over()
            if text and not ((msg.get("now") or spoken) and chat.busy and await asyncio.to_thread(chat.steer, text)):
                chat.queue(text, bool(msg.get("speak")), bool(msg.get("voice")), bool(msg.get("talk")))
        elif kind == "unqueue":
            # A queued message removed (or taken back to edit) on the phone: it never runs.
            text = with_attachments((msg.get("text") or "").strip(), msg.get("images"))
            if text and chat.unqueue(text) and isinstance(chat, Chat) and not msg.get("keep"):
                # It never ran, so nothing will read what it carried: those uploads go too, rather
                # than linger in the Library. (A terminal chat decides on its own; its files stay.)
                # `keep`: taken back to edit, its uploads went back into the phone's composer.
                await asyncio.to_thread(drop_uploads, msg.get("images"))
        elif kind == "steer":
            # A queued message the phone wants sent into the running turn now.
            text = with_attachments((msg.get("text") or "").strip(), msg.get("images"))  # as it was queued
            if text and not await asyncio.to_thread(chat.steer, text):
                await ws.send_json({"type": "toast", "text": "This model can't take a message mid-reply, so it stays queued."})
        elif kind == "answer":
            chat.answer(msg.get("answers"))
        elif kind == "shell":
            command = (msg.get("text") or "").strip()
            if not isinstance(chat, Chat):
                await ws.send_json({"type": "toast", "text": "This chat is open in a terminal: use ! there."})
            elif command:
                asyncio.ensure_future(chat.run_shell(command))
        elif kind == "shell_stop":
            if isinstance(chat, Chat):
                await asyncio.to_thread(chat.stop_shell)
        elif kind == "cancel":
            await asyncio.to_thread(chat.cancel)
        elif kind == "close":
            if chat.source == "terminal":
                chat.quit()  # the terminal exits as if /quit were typed; its link going away removes it from the list
                return
            if isinstance(chat, Chat):
                chat.shut()  # first: stopping the reply must not start what was queued behind it
            await asyncio.to_thread(chat.cancel)
            self.close_chat(chat, already_cancelled=True)
            self.post({"type": "chat_closed", "chat": chat.id})
        elif kind == "pin":
            self.pin(chat.id, bool(msg.get("pinned", True)))
        elif kind == "model":
            if chat.busy:
                await ws.send_json({"type": "error", "text": "Vision is still replying; switch models once it has finished."})
                return
            await chat.switch_model(msg.get("model") or "", msg.get("effort"))
        elif kind == "mode":
            from vision.config import MODES

            mode = str(msg.get("mode") or "").lower()
            if mode not in MODES:
                await ws.send_json({"type": "error", "text": "The mode is auto or plan."})
            elif chat.busy:
                await ws.send_json({"type": "error", "text": "Vision is still replying; switch modes once it has finished."})
            else:
                chat.set_mode(mode)
        elif kind == "open_terminal":
            # An app (the phone, the desktop) asks for this chat in a terminal window here: `vision --join <chat>` (remote.py
            # attaches it; this process keeps running the conversation, the window mirrors it).
            if chat.source == "terminal":
                await ws.send_json({"type": "toast", "text": "That chat is already open in a terminal."})
                return
            windows = self.mirrors.get(chat.id)
            if windows:
                pids = [pid for pid in windows.values() if pid]
                shown = bool(pids) and await asyncio.to_thread(focus_terminal, pids[-1])
                text = "Already open on the computer; brought it to the front." if shown else "That chat is already open on the computer."
                await ws.send_json({"type": "toast", "text": text})
                return
            if self.opening(chat.id):
                await ws.send_json({"type": "toast", "text": "That chat is already opening on the computer."})
                return
            self._opening[chat.id] = time.monotonic()  # before the await: a second tap lands while it runs
            try:
                term = await asyncio.to_thread(open_terminal, chat.id, chat.workdir)
            except Exception as e:  # noqa: BLE001
                self._opening.pop(chat.id, None)
                await ws.send_json({"type": "error", "text": f"could not open a terminal: {e}"})
                return
            chat.log(f"opened in a terminal ({term}) from an app")
            await ws.send_json({"type": "toast", "text": "Opened in a terminal on the computer."})
        else:
            await ws.send_json({"type": "error", "text": f"unknown frame type {kind!r}"})

    async def find_session(self, sid: str, provider: str | None = None):
        """An earlier conversation by its id or the start of one (sessions.SessionInfo), `provider`'s
        when given; None when there is none. The listing reads and parses files: off the loop."""
        from vision.sessions import find_any_session, find_session

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, find_session, provider, sid) if provider else None
        if info is None:
            prefer = self.cfg.brain.provider if hasattr(self.cfg.brain, "provider") else None
            info = await loop.run_in_executor(None, lambda: find_any_session(sid, prefer=prefer))
        return info

    def chat_of_session(self, info):
        """The chat here that holds that conversation, if one does."""
        return next((c for c in self.chats.values() if c.provider == info.provider and c.session_id == info.id), None)

    async def open_session(self, info) -> tuple[Chat, str]:
        """An earlier conversation taken up as a chat of this server's, with a note on how (resumed in
        place, continued in its directory, or carried over from another provider's transcript)."""
        from vision.brain import create_brain
        from vision.sessions import session_history

        loop = asyncio.get_running_loop()

        def build():
            from vision.cli import _apply_session

            cfg = copy.deepcopy(self.cfg)
            brain = create_brain(cfg.brain, voice_mode=False, continue_session=False)
            detail = _apply_session(brain, info, cfg.brain)  # a Claude conversation is continued in its directory
            return cfg, brain, detail

        cfg, brain, detail = await loop.run_in_executor(None, build)
        chat = self.open_chat(brain, cfg, title=info.title)
        if info.provider != brain.provider:
            chat._transcript = await loop.run_in_executor(None, session_history, info.provider, info.id)
        chat.updated = time.time()
        return chat, detail

    async def resume(self, ws, msg: dict) -> None:
        """Open an earlier conversation as its own chat (or switch to the chat that already has it)."""
        sid = msg.get("session_id") or ""
        if not sid:
            return
        info = await self.find_session(sid, (msg.get("provider") or "").strip().lower() or None)
        if info is None:
            await ws.send_json({"type": "error", "text": f"no conversation starts with “{sid}”"})
            return
        if (chat := self.chat_of_session(info)) is not None:
            await ws.send_json({**chat.summary(), "opened": True})
            return
        try:
            chat, detail = await self.open_session(info)
        except Exception as e:  # noqa: BLE001
            await ws.send_json({"type": "error", "text": f"could not open that conversation: {e}"})
            return
        await ws.send_json({**chat.summary(), "opened": True})
        self.post(chat.summary())
        chat.post({"type": "note", "text": detail})

    def adopt_chat(self, chat: Chat, body: dict) -> dict:
        """Make an open chat a project's, as it is: it keeps its conversation and folder and is the
        project's coordinator from its next turn. The goal is `body`'s, else what the user first asked
        for in the chat; the title the chat's. Raises ValueError when the chat is a project's already
        or there is nothing to make a goal of. Reads the chat's history: call it off the loop."""
        from vision.projects import GOAL_CHARS

        if (owner := self.projects.by_chat(chat.id)) is not None:
            raise ValueError(f"this chat is already the project “{owner.get('title') or owner['id']}”")
        goal = str(body.get("goal") or "").strip()
        if not goal:
            chat.history(limit=1)  # loads the transcript (and a title) when the chat was opened cold
            first = next((m["text"] for m in chat._transcript or [] if m.get("role") == "user" and m.get("text")), "")
            goal = " ".join(first.split())
            if len(goal) > GOAL_CHARS:
                goal = goal[:GOAL_CHARS].rsplit(" ", 1)[0] + "…"
        if not goal:
            raise ValueError("say what the project is for: nothing has been asked in this chat yet")
        body = {k: v for k, v in body.items() if k in ("instructions", "settings")}
        project = self.projects.upsert({**body, "goal": goal, "title": chat.title or "", "workdirs": [chat.workdir]})
        project = self.projects.set_chat(project["id"], chat.id) or project
        chat.set_project(project)
        chat.title = project["title"]
        self.log(f"project {project['id']} {project['title']!r} made of chat {chat.id[:4]}")
        self.post(chat.summary())
        self.post_project(project)
        return project

    def cancel(self) -> None:
        for chat in list(self.chats.values()):
            if chat.source == "terminal":
                chat.detach()
            else:
                chat.cancel()
        for pt in list(self.threads.values()):  # threads stay `working`: the next start resumes them
            pt.suspend()

    # -- speech helpers (thread pool)
    def transcribe_bytes(self, data: bytes) -> str:
        import numpy as np
        import soundfile as sf

        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return self.stt().transcribe(np.ascontiguousarray(audio), sr)

    def speak_bytes(self, text: str, spoken: bool = False) -> bytes:
        from vision.tts import speechify

        sp = self.speaker()
        return wav_bytes(sp.synth(text if spoken else speechify(text)))


# ---------------------------------------------------------------- app
def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        hub.events = asyncio.Queue()
        asyncio.create_task(hub.pump())
        asyncio.create_task(hub._watch_links())
        asyncio.create_task(hub._watch_schedule())
        asyncio.create_task(hub._watch_sleep())
        asyncio.create_task(hub._watch_moods())
        for chat, texts in hub._resume:
            chat.resume_queued(texts)
        hub._resume.clear()
        hub.restore_threads()
        hub.warm_up()
        yield
        # The voice transport stays alive between requests; reap it when the server stops.
        await asyncio.to_thread(hub.cancel)

    # No docs pages and no /openapi.json: that one would hand the whole route list to anyone who finds
    # the port, token or not.
    app = FastAPI(title="Vision Remote", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def bearer(request: Request) -> None:
        if not hub.authorized(bearer_token(request.headers)):
            raise HTTPException(status_code=401, detail="bad token")

    @app.post("/restart", dependencies=[Depends(bearer)])
    async def restart() -> dict:
        """`vision restart`: restart once every chat is quiet (see Hub.request_restart)."""
        return {"waiting": hub.request_restart("vision restart")}

    @app.get("/health", dependencies=[Depends(bearer)])
    async def health() -> dict:
        h = hub.hello()
        h["type"] = "health"
        h["status"] = await asyncio.to_thread(hub.status)
        h["phones"] = len(getattr(hub, "phones", ()))  # how the guided setup knows the scan worked
        return h

    def chat_or_404(cid: str | None) -> Chat:
        chat = hub.chat(cid)
        if chat is None:
            raise HTTPException(status_code=404, detail="no such chat")
        return chat

    @app.get("/chats", dependencies=[Depends(bearer)])
    async def chats() -> list[dict]:
        return hub.hello()["chats"]

    @app.get("/search", dependencies=[Depends(bearer)])
    async def search(q: str = "", limit: int = Hub.SEARCH_LIMIT) -> list[dict]:
        # Reading a transcript can mean reading its session file: in the executor, not on the loop.
        return await asyncio.get_running_loop().run_in_executor(None, hub.search, q, max(1, min(limit, 100)))

    @app.get("/history", dependencies=[Depends(bearer)])
    async def history(limit: int = 60, chat: str | None = None) -> list[dict]:
        c = chat_or_404(chat)
        return await asyncio.get_running_loop().run_in_executor(None, c.history, limit)

    @app.get("/models", dependencies=[Depends(bearer)])
    async def models(chat: str | None = None, model: str | None = None, effort: str | None = None) -> dict:
        c = hub.chat(chat)
        if c is None and chat:
            raise HTTPException(status_code=404, detail="no such chat")
        # The provider tabs probe servers and the defaults read the config: in the executor, not on the loop.
        return await asyncio.get_running_loop().run_in_executor(None, models_payload, c, model, effort)

    def models_payload(c, model: str | None, effort: str | None) -> dict:
        from vision.models import coerce_effort, effort_choices, find
        from vision.providers import model_tabs

        if c is None:
            # `model`/`effort`: the phone's pick for the next chat (see `new`), with that model's levels.
            if picked := hub.picked_cfg(model, effort):
                return defaults_payload(picked.brain.model, picked.brain.effort)
            model, effort = (saved_brain_defaults() or (hub.cfg.brain.model, hub.cfg.brain.effort)) if hub.follow_defaults else (hub.cfg.brain.model, hub.cfg.brain.effort)
            return defaults_payload(model, effort)
        tabs = [
            {"tab": tab, "models": [{"id": v, "label": label, "description": desc} for v, label, desc in entries]}
            for tab, entries, _ in model_tabs(hub.cfg, setup_rows=False)  # enabled + set up: the phone can't install
        ]
        # `model`: one being looked at in the picker, so its own levels show before the switch is made.
        shown = model if model and find(model) else c.model_id
        efforts = [{"id": v, "label": label, "description": desc} for v, label, desc in effort_choices(shown)]
        effort = c.effort if shown == c.model_id else coerce_effort(shown, c.effort)[0]
        return {"tabs": tabs, "efforts": efforts, "current": c.model_id, "effort": effort}

    def defaults_payload(model: str, effort: str) -> dict:
        from vision.models import effort_choices
        from vision.providers import model_tabs

        tabs = [
            {"tab": tab, "models": [{"id": v, "label": label, "description": desc} for v, label, desc in entries]}
            for tab, entries, _ in model_tabs(hub.cfg, setup_rows=False)  # enabled + set up: the phone can't install
        ]
        efforts = [{"id": v, "label": label, "description": desc} for v, label, desc in effort_choices(model)]
        return {"tabs": tabs, "efforts": efforts, "current": model, "effort": effort}

    @app.get("/defaults", dependencies=[Depends(bearer)])
    async def get_defaults() -> dict:
        """The model and effort a new chat starts on: config.toml's [brain], as /default saves it."""
        def read() -> dict:
            model, effort = saved_brain_defaults() or (hub.cfg.brain.model, hub.cfg.brain.effort)
            return defaults_payload(model, effort)

        return await asyncio.get_running_loop().run_in_executor(None, read)

    @app.post("/defaults", dependencies=[Depends(bearer)])
    async def set_defaults(body: dict) -> dict:
        """Save a new default, like /default in the terminal. Open chats keep their own model."""
        from vision.models import coerce_effort, find

        model = str(body.get("model") or "")
        if not find(model):
            raise HTTPException(status_code=400, detail=f"unknown model {model!r}")
        effort, _ = coerce_effort(model, str(body.get("effort") or ""))
        try:
            save_brain_defaults(model, effort)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"could not save config.toml: {e}")
        # Also this server's own fallback, so new chats follow it even when a flag pinned the start-up model.
        hub.cfg.brain.model, hub.cfg.brain.effort = model, effort
        hub.log(f"default saved from the phone → {model} · {effort or 'effort off'}")
        return defaults_payload(model, effort)

    @app.exception_handler(VoiceOff)
    async def voice_off(request: Request, exc: VoiceOff) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.post("/voice/power", dependencies=[Depends(bearer)])
    async def set_voice_power(body: dict) -> dict:
        """The voice switch: {"on": false} unloads the voice and speech recognition and keeps them off (saved
        as [voice] enabled), {"on": true} loads them again. The phone's Settings and /voice on|off use it."""
        on = body.get("on")
        if not isinstance(on, bool):
            raise HTTPException(status_code=400, detail="on must be true or false")
        try:
            await asyncio.to_thread(hub.voice_power, on, True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"could not switch the voice: {e}")
        return {"on": hub.cfg.voice.enabled}

    @app.get("/voice/rate", dependencies=[Depends(bearer)])
    async def get_voice_rate() -> dict:
        """How fast the voice speaks: [voice] rate, 1.0 as spoken, 0.5 to 2.0."""
        return {"rate": hub.cfg.voice.rate}

    @app.post("/voice/rate", dependencies=[Depends(bearer)])
    async def set_voice_rate(body: dict) -> dict:
        """Save [voice] rate in config.toml and speak at it from the next sentence on (the voice reads it live)."""
        from vision.config import save_config_value

        try:
            rate = round(min(2.0, max(0.5, float(body.get("rate")))), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="rate must be a number from 0.5 to 2.0") from None
        try:
            save_config_value("voice", "rate", repr(rate))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"could not save config.toml: {e}")
        hub.cfg.voice.rate = rate
        hub.log(f"voice rate saved from the phone → {rate:g}×")
        return {"rate": rate}

    def voices_payload() -> dict:
        from vision.config import voice_choices, voice_dir

        # Only what can speak now: a built-in design that was never made needs `vision voice design` first.
        # "sample": there is a clip of it to hear before choosing it (GET /voice/sample).
        voices = [{"id": n, "note": note, "sample": (voice_dir(n) / "ref.wav").is_file()}
                  for n, note in voice_choices(hub.cfg.voice) if not note.startswith("built-in design, not made")]
        current = hub.cfg.voice.voice
        if current not in [v["id"] for v in voices]:
            voices.insert(0, {"id": current, "note": "", "sample": False})  # a path to a clip, or a voice since removed
        return {"voice": current, "voices": voices, "on": hub.cfg.voice.enabled}

    @app.get("/voice", dependencies=[Depends(bearer)])
    async def get_voice() -> dict:
        """The voice Vision speaks in ([voice] voice) and the ones to choose from, as `vision voices` lists them."""
        return voices_payload()

    @app.get("/voice/sample", dependencies=[Depends(bearer)])
    async def voice_sample(voice: str = "") -> FileResponse:
        """A voice to hear before choosing it: the clip it was cloned from or designed as (its ref.wav).
        Nothing is spoken afresh, so it needs no GPU and leaves the voice in use alone."""
        from vision.config import voice_dir

        if not any(v["id"] == voice and v["sample"] for v in voices_payload()["voices"]):
            raise HTTPException(status_code=404, detail=f"no sample of {voice!r}")
        return FileResponse(voice_dir(voice) / "ref.wav", media_type="audio/wav")

    @app.post("/voice", dependencies=[Depends(bearer)])
    async def set_voice(body: dict) -> dict:
        """Save [voice] voice in config.toml, like /voice in the terminal, and speak in it from the next reply on."""
        from vision.config import save_voice_default

        name = str(body.get("voice") or "")
        if name not in [v["id"] for v in voices_payload()["voices"]]:
            raise HTTPException(status_code=400, detail=f"unknown voice {name!r}")
        sp = hub._speaker

        def switch():
            if sp is None:
                return  # not loaded yet: it starts in the saved voice
            sp.set_voice(name)
            if sp._loaded and hub.cfg.voice.filler:
                sp.prepare_fillers(hub.cfg.voice.filler_phrases)

        try:
            await asyncio.get_running_loop().run_in_executor(None, switch)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"could not switch the voice: {e}")
        try:
            save_voice_default(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"could not save config.toml: {e}")
        hub.cfg.voice.voice = name
        hub.log(f"voice saved from the phone → {name}")
        return voices_payload()

    @app.get("/providers", dependencies=[Depends(bearer)])
    async def providers_list() -> dict:
        """Every provider the machine knows, with how to draw it (icon, tint) and whether it is on and ready."""
        from vision.providers import REGISTRY

        rows = await asyncio.get_running_loop().run_in_executor(None, lambda: [p.describe(hub.cfg) for p in REGISTRY.values()])
        return {"providers": rows}

    @app.get("/sessions", dependencies=[Depends(bearer)])
    async def sessions() -> list[dict]:
        from vision.models import model_label
        from vision.sessions import list_all_sessions
        from vision.turnjournal import session_brains

        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, list_all_sessions)
        brains = await loop.run_in_executor(None, session_brains)  # the model and effort each one last ran on
        rows.sort(key=lambda s: s.last_active, reverse=True)  # every provider merged, newest first
        open_ids = {(c.provider, c.session_id): c.id for c in hub.chats.values()}
        return [
            {
                "id": s.id,
                "title": s.title,
                "age": s.age(),
                "updated": s.last_active,
                "provider": s.provider,
                "model": model_label(brains[s.id][0]) if s.id in brains else None,
                "effort": (brains[s.id][1] or None) if s.id in brains else None,
                "current": (s.provider, s.id) in open_ids,
                "chat": open_ids.get((s.provider, s.id)),
            }
            for s in rows
        ]

    @app.get("/folders", dependencies=[Depends(bearer)])
    async def folders(path: str = "") -> dict:
        """A folder to start a chat in, for the phone's picker: its subfolders, and, at the start
        (no `path`), the default folder with the recent ones."""
        from vision.sessions import list_all_sessions

        default = os.path.abspath(os.path.expanduser(hub.cfg.brain.workdir)) if hub.cfg.brain.workdir else os.getcwd()
        loop = asyncio.get_running_loop()
        try:
            listing = await loop.run_in_executor(None, list_folder, path or default)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        if not path:
            sessions = await loop.run_in_executor(None, list_all_sessions)
            sessions.sort(key=lambda s: s.last_active, reverse=True)
            chats = sorted(hub.chats.values(), key=lambda c: c.updated, reverse=True)
            listing["recent"] = recent_folders(default, chats, sessions)
        listing["home"] = os.path.expanduser("~")
        return listing

    @app.get("/usage", dependencies=[Depends(bearer)])
    async def usage(provider: str = "all", chat: str | None = None) -> dict:
        """Subscription usage for the phone's Usage page: every provider, or one. The on-screen chat's
        brain answers for its own provider; the others get a throwaway brain, as `/usage all` does."""
        from vision.usage import PROVIDERS, usage_all, usage_data

        c = hub.chat(chat)
        brain = getattr(c, "brain", None)  # a terminal-linked chat has no brain of its own here
        cfg = getattr(c, "cfg", None) or hub.cfg
        loop = asyncio.get_running_loop()
        if provider == "all":
            rows = await loop.run_in_executor(None, usage_all, cfg, brain)
        elif provider in PROVIDERS:
            rows = [await loop.run_in_executor(None, usage_data, cfg, brain, provider)]
        else:
            raise HTTPException(status_code=400, detail=f"provider must be all, {', '.join(PROVIDERS)}")
        return {"providers": rows, "at": time.time()}

    @app.post("/usage/reset", dependencies=[Depends(bearer)])
    async def use_reset(body: dict) -> dict:
        """Spend one of a provider's banked resets ({"provider", "id" (optional), "chat"}), then read its usage again:
        {"ok", "outcome", "message", "provider": the fresh usage row}."""
        from vision.usage import use_banked, usage_data

        from vision.providers import names

        provider = str(body.get("provider") or "")
        if provider not in names(banked_resets=True):
            raise HTTPException(status_code=400, detail=f"provider must be {' or '.join(names(banked_resets=True))}")
        c = hub.chat(body.get("chat"))
        brain = getattr(c, "brain", None)
        cfg = getattr(c, "cfg", None) or hub.cfg
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, use_banked, provider, body.get("id") or None)
        hub.log(f"banked {provider} reset from the phone → {result['outcome']}")
        result["provider"] = await loop.run_in_executor(None, usage_data, cfg, brain, provider)
        return result

    def project_sessions(project: dict) -> list[str]:
        """The session log of the project's chat, where its replies' pictures are found."""
        import glob as _glob

        from vision.sessions import CLAUDE_PROJECTS

        chat = hub.project_chat(project)
        sid = chat.session_id if chat is not None else ""
        if not sid or chat.provider != "claude":
            return []
        return _glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{sid}.jsonl"))

    @app.get("/library", dependencies=[Depends(bearer)])
    async def library_items(project: str = "") -> list[dict]:
        """Pictures replies showed and everything sent from the phone, newest first. `project`: that
        project's Library instead (what was sent in its chat, what its threads produced, its replies' pictures)."""
        from vision.library import library, project_library

        if project:
            p = project_or_404(project)
            return await asyncio.to_thread(project_library, hub.projects.root / p["id"], image_path_allowed, project_sessions(p))
        return await asyncio.to_thread(library, UPLOAD_DIR, image_path_allowed)

    @app.delete("/library", dependencies=[Depends(bearer)])
    async def library_delete(path: str, project: str = "") -> dict:
        """Delete one Library item (a reply's picture or something sent from the phone) from the machine.
        With `project`: out of that project's Library; a thread's file only leaves the index (it belongs
        to the repo), something sent from the phone is deleted too."""
        from vision.library import delete, unindex_project

        if project:
            p = project_or_404(project)
            gone = await asyncio.to_thread(unindex_project, hub.projects.root / p["id"], path)
            if gone is None:
                raise HTTPException(status_code=404, detail="not in the project's library")
            if gone.get("source") == "you":
                await asyncio.to_thread(delete, path, UPLOAD_DIR, image_path_allowed)
            hub.log(f"project {p['id']}: library item removed from the phone: {path}")
            return {"ok": True}
        try:
            found = await asyncio.to_thread(delete, path, UPLOAD_DIR, image_path_allowed)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"could not delete it: {e}")
        if not found:
            raise HTTPException(status_code=404, detail="not in the library")
        hub.log(f"library item deleted from the phone: {path}")
        return {"ok": True}

    @app.post("/library/delete", dependencies=[Depends(bearer)])
    async def library_delete_many(body: dict) -> dict:
        """Delete several Library items at once (the phone's Select mode); returns the paths that went."""
        from vision.library import delete_many

        paths = [p for p in body.get("paths") or [] if isinstance(p, str)]
        try:
            done = await asyncio.to_thread(delete_many, paths, UPLOAD_DIR, image_path_allowed)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"could not delete them: {e}")
        hub.log(f"{len(done)} library item{'s' if len(done) != 1 else ''} deleted from the phone")
        return {"deleted": done}

    @app.get("/thumb", dependencies=[Depends(bearer)])
    async def thumb(path: str, size: int = 360) -> Response:
        """A small JPEG of a library picture (a video's first frame), for the Library grid."""
        from vision.library import thumbnail

        try:
            real = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="no such picture")
        if real.suffix.lower() in VIDEO_TYPES and real.parent == UPLOAD_DIR.resolve():
            frames = (video_digest(real) or {}).get("frames") or []
            real = Path(frames[0][1]) if frames and os.path.isfile(frames[0][1]) else real
        if not real.is_file() or real.suffix.lower() not in IMAGE_TYPES or not image_path_allowed(real):
            raise HTTPException(status_code=404, detail="no such picture")
        try:
            data = await asyncio.to_thread(thumbnail, real, max(64, min(size, 1024)))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"could not read that picture: {e}")
        return Response(content=data, media_type="image/jpeg")

    @app.get("/scheduled", dependencies=[Depends(bearer)])
    async def scheduled_list() -> list[dict]:
        return hub.schedule.list()

    @app.post("/scheduled", dependencies=[Depends(bearer)])
    async def scheduled_save(body: dict) -> dict:
        """Add a task, or edit one (`id` set): {title, prompt, repeat, time, date?, weekday?, enabled,
        kind?, chat?, workdir?}. `pids` (the `vision remind` command's ancestors) names a terminal chat instead
        of `chat`: the one whose process is among them."""
        pids = body.pop("pids", None)
        if not body.get("chat") and isinstance(pids, list):
            body["chat"] = next((hub.links[p].id for p in pids if p in hub.links), "")
        if not body.get("id") and (body.get("kind") or "task") == "task" and not body.get("chat"):
            # A timed job on its own is a scout now: it keeps one chat and remembers its last run.
            from vision.scheduled import clean as scheduled_clean

            try:
                if body.get("workdir"):
                    body["workdir"] = usable_folder(str(body["workdir"]))
                project = hub.scout_from_task(scheduled_clean(body))
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e))
            hub.log(f"scheduled job made scout {project['id']}: {project['title']!r}")
            hub.project_saved(project)
            hub.post({"type": "scheduled"})
            return hub.schedule.for_project(project["id"]) or {}
        try:
            if body.get("workdir"):
                body["workdir"] = usable_folder(str(body["workdir"]))
            task = hub.schedule.upsert(body)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        hub.log(f"scheduled {task['kind']} saved: {task['title']!r} ({task['repeat']} {task['time']})")
        hub.post({"type": "scheduled"})
        if not body.get("id") and task.get("next_run") and (home := hub.task_chat(task)) is not None:
            # Set from a chat: that chat says so, as it has no list of its own to show it in.
            from datetime import datetime

            when = datetime.fromtimestamp(task["next_run"]).strftime("%a %H:%M")
            what = "Reminder" if task["kind"] == "reminder" else "Scheduled"
            home.post({"type": "note", "text": f"{what} for {when}{'' if task['repeat'] == 'once' else ', ' + task['repeat']}: {task['title']}"})
        return task

    @app.delete("/scheduled/{tid}", dependencies=[Depends(bearer)])
    async def scheduled_delete(tid: str) -> dict:
        if not hub.schedule.delete(tid):
            raise HTTPException(status_code=404, detail="no such task")
        hub.post({"type": "scheduled"})
        return {"ok": True}

    @app.post("/scheduled/{tid}/run", dependencies=[Depends(bearer)])
    async def scheduled_run(tid: str) -> dict:
        """Run a task now, whatever its schedule; answers with the chat it opened."""
        task = hub.schedule.get(tid)
        if task is None:
            raise HTTPException(status_code=404, detail="no such task")
        chat = await hub.run_task_off_loop(task, why="now")
        return {"chat": chat.id} if chat is not None else {}

    # -- projects (projects.py)
    def project_or_404(ref: str) -> dict:
        try:
            project = hub.projects.find(ref)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if project is None:
            raise HTTPException(status_code=404, detail=f"no project {ref!r}")
        return project

    @app.get("/projects", dependencies=[Depends(bearer)])
    async def projects_list(all: bool = False, chat: str = "") -> list[dict]:  # noqa: A002  (the query parameter's name)
        """Every project, latest activity first; archived ones too with `all`. `chat`: only the project
        whose chat that is (how `vision thread` run from a project chat's shell finds its project)."""
        if chat:
            return [hub.view_project(p)] if (p := hub.projects.by_chat(chat)) else []
        return [hub.view_project(p) for p in hub.projects.list(include_archived=all)]

    @app.get("/projects/{ref}", dependencies=[Depends(bearer)])
    async def projects_get(ref: str) -> dict:
        """One project, by its id or the start of one."""
        return hub.view_project(project_or_404(ref))

    @app.post("/projects", dependencies=[Depends(bearer)])
    async def projects_save(body: dict) -> dict:
        """Make a project, or edit one (`id` set): {goal, title?, instructions?, workdir? | workdirs?,
        status?, settings?}. A new project with no folder works in the server's default one."""
        body = {k: v for k, v in body.items() if k != "chat"}  # the chat is the server's to set
        dirs = [body["workdir"]] if body.get("workdir") else body.get("workdirs")
        if dirs is None and not body.get("id"):
            dirs = [os.path.abspath(os.path.expanduser(hub.cfg.brain.workdir)) if hub.cfg.brain.workdir else os.getcwd()]
        if dirs is not None:
            if not isinstance(dirs, list):
                raise HTTPException(status_code=400, detail="workdirs must be a list of folders")
            try:
                body["workdirs"] = [usable_folder(str(d)) for d in dirs if str(d).strip()]
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            body.pop("workdir", None)
        before = hub.projects.get(str(body["id"])) if body.get("id") else None
        try:
            project = hub.projects.upsert(body)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such project") from None
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        hub.log(f"project saved: {project['id']} {project['title']!r}")
        hub.project_saved(project, before)
        return hub.view_project(project)

    @app.post("/projects/adopt", dependencies=[Depends(bearer)])
    async def projects_adopt(body: dict) -> dict:
        """Make a chat a project's, as it is (the terminal's /project): {chat} for one this server runs,
        or {session_id, provider?} for a conversation held elsewhere (a terminal's own), which is opened
        here first. {goal?, instructions?, settings?}: the goal defaults to what the user first asked
        for in the chat, the title to the chat's. Returns {project, chat} (the chat, to join)."""
        if cid := body.get("chat"):
            chat = hub.chat(str(cid))
            if chat is None:
                raise HTTPException(status_code=404, detail="that chat is gone")
            if not isinstance(chat, Chat):
                raise HTTPException(status_code=409, detail="that chat runs in a terminal: send its session_id instead")
        elif sid := body.get("session_id"):
            info = await hub.find_session(str(sid), (str(body.get("provider") or "")).strip().lower() or None)
            if info is None:
                raise HTTPException(status_code=404, detail=f"no conversation starts with “{sid}”")
            chat = hub.chat_of_session(info)
            if chat is None:
                try:
                    chat, _ = await hub.open_session(info)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(status_code=500, detail=f"could not open that conversation: {e}") from None
        else:
            raise HTTPException(status_code=400, detail="say which chat: {chat} or {session_id}")
        try:
            project = await asyncio.get_running_loop().run_in_executor(None, hub.adopt_chat, chat, body)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return {"project": hub.view_project(project), "chat": chat.summary()}

    @app.post("/projects/{ref}/pause", dependencies=[Depends(bearer)])
    async def projects_pause(ref: str) -> dict:
        """Pause a project: its threads stop where they are and nothing new starts until it's resumed."""
        before = project_or_404(ref)
        if before.get("status") == "archived":
            raise HTTPException(status_code=409, detail="that project is archived")
        project = hub.projects.upsert({"id": before["id"], "status": "paused"})
        hub.project_saved(project, before)
        return hub.view_project(project)

    @app.post("/projects/{ref}/resume", dependencies=[Depends(bearer)])
    async def projects_resume(ref: str) -> dict:
        """Resume a paused project (its paused threads carry on in their own sessions), or bring back a
        retired one, with its chat and check-in schedule as they were."""
        before = project_or_404(ref)
        project = hub.projects.upsert({"id": before["id"], "status": "active"})
        hub.project_saved(project, before)
        return hub.view_project(project)

    # -- the project memory (projects.py: memory.md)
    @app.get("/projects/{ref}/memory", dependencies=[Depends(bearer)])
    async def project_memory(ref: str) -> dict:
        """The project memory's facts, oldest first, numbered from 1 (what `forget` takes)."""
        from vision.projects import memory_lines

        project = project_or_404(ref)
        return {"facts": memory_lines(hub.projects.root / project["id"])}

    @app.post("/projects/{ref}/memory", dependencies=[Depends(bearer)])
    async def project_remember(ref: str, body: dict) -> dict:
        """Add facts {facts: [str]} or {fact: str} to the project memory."""
        from vision.projects import memory_lines, remember

        project = project_or_404(ref)
        facts = body.get("facts") if isinstance(body.get("facts"), list) else [body.get("fact")]
        facts = [str(f) for f in facts if isinstance(f, str) and f.strip()]
        if not facts:
            raise HTTPException(status_code=400, detail="say what to remember")
        folder = hub.projects.root / project["id"]
        added = await run_off_loop(lambda: remember(folder, facts, source=str(body.get("source") or "")))
        return {"added": added, "facts": memory_lines(folder)}

    @app.delete("/projects/{ref}/memory/{number}", dependencies=[Depends(bearer)])
    async def project_forget(ref: str, number: int) -> dict:
        from vision.projects import forget, memory_lines

        folder = hub.projects.root / project_or_404(ref)["id"]
        try:
            gone = await run_off_loop(forget, folder, number)
        except IndexError:
            raise HTTPException(status_code=404, detail=f"there's no fact {number}") from None
        return {"forgot": gone, "facts": memory_lines(folder)}

    @app.get("/projects/{ref}/activity", dependencies=[Depends(bearer)])
    async def project_activity(ref: str, limit: int = 100, since: float = 0) -> dict:
        """What the project's threads did (projects.activity), newest first: at most `limit` events,
        none from before `since` (a Unix time)."""
        from vision.projects import activity

        folder = hub.projects.root / project_or_404(ref)["id"]
        return {"events": await run_off_loop(activity, folder, max(1, min(limit, 500)), since)}

    @app.get("/projects/{ref}/approvals", dependencies=[Depends(bearer)])
    async def project_approvals(ref: str, all: bool = False) -> list[dict]:  # noqa: A002
        """What the scout asked leave to do: waiting ones, or with `all` the decided ones too (oldest first)."""
        from vision.projects import approvals

        return approvals(hub.projects.root / project_or_404(ref)["id"], pending_only=not all)

    @app.post("/projects/{ref}/approvals/{aid}", dependencies=[Depends(bearer)])
    async def project_decide(ref: str, aid: str, body: dict) -> dict:
        """{decision: approve | deny, command?}: `command` approves an edited shell command instead."""
        project = project_or_404(ref)
        decision = str(body.get("decision") or "").lower()
        if decision not in ("approve", "deny"):
            raise HTTPException(status_code=400, detail="decision must be approve or deny")
        try:
            return await run_off_loop(lambda: hub.decide(project["id"], aid, decision == "approve", str(body.get("command") or "")))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no approval {aid!r}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.post("/projects/{ref}/checkin", dependencies=[Depends(bearer)])
    async def project_checkin(ref: str) -> dict:
        """Check in now (the apps' Check in now): a check-in turn in the project's chat. Answers {chat}."""
        project = project_or_404(ref)
        if project.get("status") in ("archived", "paused"):
            raise HTTPException(status_code=409, detail=f"that project is {project['status']}")
        chat = await hub.checkin(project["id"], why="now")
        if chat is None:
            raise HTTPException(status_code=409, detail="it couldn't send a status report now")
        return {"chat": chat.id}

    @app.post("/projects/{ref}/checkins", dependencies=[Depends(bearer)])
    async def project_checkins(ref: str, body: dict) -> dict:
        """Set a scout's check-in schedule: {repeat: daily|weekdays|weekly|every|off, time?, weekday?,
        every_minutes?, enabled?}; `off` removes it. Answers the project, with its `checkin`."""
        project = project_or_404(ref)
        old = hub.schedule.for_project(project["id"])
        if str(body.get("repeat") or "") == "off":
            if old is not None:
                hub.schedule.delete(old["id"])
        else:
            name = (project.get("scout") or {}).get("name") or project.get("title") or "Scout"
            fields = {k: body[k] for k in ("repeat", "time", "weekday", "every_minutes", "enabled") if k in body}
            try:
                hub.schedule.upsert({**({"id": old["id"]} if old else {}), "kind": "checkin", "project": project["id"],
                                     "title": f"{name}'s status report", "prompt": "", **fields})
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
        hub.post({"type": "scheduled"})
        hub.post_project(project)
        return hub.view_project(project)

    @app.post("/projects/{ref}/open", dependencies=[Depends(bearer)])
    async def projects_open(ref: str) -> dict:
        """The project's chat, opened if it isn't (`vision project open` joins it next)."""
        project = project_or_404(ref)
        chat = await hub.open_project_chat(project)
        return chat.summary()

    @app.post("/projects/{ref}/delete", dependencies=[Depends(bearer)])
    async def projects_delete(ref: str) -> dict:
        """Delete a retired project for good (retire it first: DELETE /projects/{ref}): its chat, threads,
        memory and check-ins. Its work folder is left alone; `kept` lists worktrees with work in them."""
        project = project_or_404(ref)
        if project.get("status") != "archived":
            raise HTTPException(status_code=409, detail="retire it first")
        kept = await asyncio.to_thread(hub.delete_project, project)
        return {"deleted": project["id"], "kept": kept}

    @app.delete("/projects/{ref}", dependencies=[Depends(bearer)])
    async def projects_archive(ref: str) -> dict:
        """Archive (retire) a project: its threads stop, it leaves the lists (GET /projects?all=1 still has
        it) and its check-ins are skipped, as a paused one's are. Its chat stays its own and nothing on disk is
        deleted, so POST …/resume brings it back as it was."""
        before = project_or_404(ref)
        project = hub.projects.archive(before["id"])
        hub.log(f"project archived: {project['id']} {project['title']!r}")
        hub.project_saved(project, before)
        return hub.view_project(project)

    # -- project threads (runs.py)
    def thread_or_404(pt, tid: str) -> dict:
        try:
            thread = pt.find(tid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {tid!r}")
        return thread

    def run_off_loop(fn, *args):  # a worktree to make, a git check: never on the event loop
        return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    @app.get("/projects/{ref}/threads", dependencies=[Depends(bearer)])
    async def threads_list(ref: str) -> list[dict]:
        """A project's threads, oldest first, each with its run log."""
        return [thread_view(t) for t in hub.project_threads(project_or_404(ref)).list()]

    @app.get("/projects/{ref}/threads/{tid}", dependencies=[Depends(bearer)])
    async def threads_get(ref: str, tid: str) -> dict:
        return thread_view(thread_or_404(hub.project_threads(project_or_404(ref)), tid))

    @app.post("/projects/{ref}/threads", dependencies=[Depends(bearer)])
    async def threads_start(ref: str, body: dict) -> dict:
        """Start a thread: {objective, title?, edits? (default true: a worktree of its own), context?,
        after?: [thread ids whose results it builds on]}."""
        project = project_or_404(ref)
        pt = hub.project_threads(project)
        uses = body.get("after") or []
        if isinstance(uses, str):
            uses = [u.strip() for u in uses.split(",")]
        try:
            thread = await run_off_loop(lambda: pt.start(str(body.get("objective") or ""), title=str(body.get("title") or ""),
                                                         edits=body.get("edits") is not False, context=str(body.get("context") or ""),
                                                         uses=[str(u) for u in uses if str(u).strip()]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        hub.log(f"project {project['id']}: thread {thread['id']} started {thread['title']!r}")
        hub.post_project(project)
        return thread_view(thread)

    @app.post("/projects/{ref}/threads/{tid}/tell", dependencies=[Depends(bearer)])
    async def threads_tell(ref: str, tid: str, body: dict) -> dict:
        """A message to one thread {text, direct?}: it steers it while it works, answers its question, or
        gives it more work once it has stopped (a closed one stays closed: a new thread follows it, and is
        what comes back). `direct`: the user's own, from the
        phone's thread view or a terminal, so the coordinator hears of it too."""
        project = project_or_404(ref)
        pt = hub.project_threads(project)
        thread = thread_or_404(pt, tid)
        text = str(body.get("text") or "")
        try:
            thread = await run_off_loop(pt.tell, thread["id"], text)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        if body.get("direct") and text.strip():
            pt.store.update(thread["id"], told=text.strip()[:300])  # shown in the coordinator's status block
            if (chat := hub.project_chat(project)) is not None:
                chat.remind(f"You told “{thread['title']}”: {text.strip()[:200]}")
        hub.post_project(project)
        return thread_view(pt.store.get(thread["id"]) or thread)

    @app.post("/projects/{ref}/threads/{tid}/message", dependencies=[Depends(bearer)])
    async def threads_message(ref: str, tid: str, body: dict) -> dict:
        """The phone's thread view sending {text}: `tell`, from the user directly."""
        return await threads_tell(ref, tid, {**body, "direct": True})

    @app.post("/projects/{ref}/threads/{tid}/land", dependencies=[Depends(bearer)])
    async def threads_land(ref: str, tid: str) -> dict:
        """Merge a finished thread's branch into the project folder's current branch and close it. 409 with
        why when it can't (uncommitted changes, a conflict: everything is left as it was)."""
        project = project_or_404(ref)
        pt = hub.project_threads(project)
        thread = thread_or_404(pt, tid)
        try:
            thread = await run_off_loop(pt.land, thread["id"])
        except ValueError as e:
            hub.post_project(project)
            raise HTTPException(status_code=409, detail=str(e)) from None
        hub.log(f"project {project['id']}: thread {thread['id']} landed")
        hub.post_project(project)
        return thread_view(thread)

    @app.get("/projects/{ref}/threads/{tid}/diff", dependencies=[Depends(bearer)])
    async def threads_diff(ref: str, tid: str, path: str = "") -> dict:
        """What landing a thread would bring in: {base, commits, files: [{path, added, removed}], more}, and
        with ?path= that file's patch ({patch, cut}). 409 when it has no branch of its own."""
        from vision.runs import branch_diff

        project = project_or_404(ref)
        thread = thread_or_404(hub.project_threads(project), tid)
        dirs = project.get("workdirs") or []
        if not thread.get("branch") or not dirs:
            raise HTTPException(status_code=409, detail="it has no branch of its own (it worked in the project's folder)")
        try:
            return await run_off_loop(branch_diff, dirs[0], thread["branch"], path)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.get("/projects/{ref}/threads/{tid}/history", dependencies=[Depends(bearer)])
    async def threads_history(ref: str, tid: str) -> dict:
        """A thread's conversation, read from its provider's own store (nothing is copied): the tasks and
        messages Vision gave it and the results it returned, in plain words. Empty when the store can't
        be read (the run log then tells its story)."""
        from vision.runs import thread_history

        thread = thread_or_404(hub.project_threads(project_or_404(ref)), tid)
        return {"messages": await run_off_loop(thread_history, thread)}

    @app.post("/projects/{ref}/threads/{tid}/stop", dependencies=[Depends(bearer)])
    async def threads_stop(ref: str, tid: str) -> dict:
        pt = hub.project_threads(project_or_404(ref))
        return thread_view(await run_off_loop(pt.stop, thread_or_404(pt, tid)["id"]))

    @app.post("/projects/{ref}/threads/{tid}/resolve", dependencies=[Depends(bearer)])
    async def threads_resolve(ref: str, tid: str) -> dict:
        """Close a thread; its worktree goes unless something in it isn't on another branch yet."""
        project = project_or_404(ref)
        pt = hub.project_threads(project)
        thread = await run_off_loop(pt.resolve, thread_or_404(pt, tid)["id"])
        hub.post_project(project)
        return thread_view(thread)

    # -- Vision's memory (memory.py), for the phone's Memory screen
    @app.get("/memory", dependencies=[Depends(bearer)])
    async def memory_list(topic: str = "", q: str = "") -> dict:
        """The core's facts and the topic list; `topic` gives one topic's facts instead, `q` every
        line holding those words. A fact is {line, date, text} (search hits add `topic`, "" = core);
        `updated` is when a file last changed."""
        from vision import memory

        def changed(name: str) -> float | None:
            try:
                return memory._path(name).stat().st_mtime
            except OSError:
                return None

        try:
            if q.strip():
                return {"facts": [memory.entry(ln, name) for name, ln in memory.search(q)], "topics": []}
            listed = [] if topic else [{"name": n, "about": about, "count": count, "updated": changed(n)}
                                       for n, about, count in memory.topics()]
            return {"facts": [memory.entry(ln) for ln in memory.facts(topic)], "topics": listed, "updated": changed(topic)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/memory", dependencies=[Depends(bearer)])
    async def memory_add(body: dict) -> dict:
        """Remember a fact: {fact, topic?} ("" or missing = the core)."""
        from vision import memory

        try:
            return memory.entry(memory.remember(str(body.get("fact") or ""), str(body.get("topic") or "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/memory/forget", dependencies=[Depends(bearer)])
    async def memory_forget(body: dict) -> dict:
        """Drop one fact: {line (as listed), topic?}."""
        from vision import memory

        try:
            gone = memory.drop(str(body.get("line") or ""), str(body.get("topic") or ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not gone:
            raise HTTPException(status_code=404, detail="no such fact")
        return {"ok": True}

    @app.post("/push/register", dependencies=[Depends(bearer)])
    async def push_register(body: dict) -> dict:
        """The app's device token for notifications, and the key it wants them sealed with:
        {token (hex), key (base64, 32 bytes), sandbox?, name?}."""
        from vision import push

        try:
            dev = push.register(body)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "ready": push.ready(hub.cfg.push), "name": dev["name"]}

    @app.post("/push/activity", dependencies=[Depends(bearer)])
    async def push_activity(body: dict) -> dict:
        """A Live Activity token from the app, for the threads' Lock Screen card (liveactivity.py):
        {kind: "start" | "card", token (hex), sandbox?}."""
        from vision import liveactivity

        try:
            liveactivity.register(body)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.delete("/push/register/{token}", dependencies=[Depends(bearer)])
    async def push_unregister(token: str) -> dict:
        from vision import push

        return {"ok": push.unregister(token)}

    @app.post("/push/test", dependencies=[Depends(bearer)])
    async def push_test() -> dict:
        """Send a notification to every registered phone now; `failed` names the ones that missed it."""
        from vision import push

        try:
            failed = await asyncio.to_thread(push.send, hub.cfg.push, "Vision", "Push is working.")
        except push.PushError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"devices": len(push.devices()), "failed": failed}

    @app.post("/transcribe", dependencies=[Depends(bearer)])
    async def transcribe(file: UploadFile = File(...), preview: bool = False) -> dict:
        """`preview=1` is the phone's live words: the utterance so far, sent every second or so while
        you talk. Only served when Whisper is on the GPU (409 otherwise, the phone falls back to its own),
        and not logged."""
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty audio")
        if preview and not (await asyncio.get_running_loop().run_in_executor(None, hub.stt)).can_preview_live:
            raise HTTPException(status_code=409, detail="live preview needs Whisper on the GPU")  # the load, if any, ran in the executor
        try:
            text = await asyncio.get_running_loop().run_in_executor(None, hub.transcribe_bytes, data)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"could not decode audio: {e}")
        if preview:
            return {"text": text}
        hub.log(f"heard: {text!r}")
        return {"text": text}

    @app.post("/upload", dependencies=[Depends(bearer)])
    async def upload(file: UploadFile = File(...)) -> dict:
        """A photo, video or any other file from the phone, saved where every brain can read it; the message
        frame then names the path. A video is digested here (frames + transcript), so a bad clip fails the
        upload. Other files keep their name after the stamp, so the brain (and the phone's chip) sees it."""
        name = os.path.basename(file.filename or "")
        ext = os.path.splitext(name)[1].lower() or ".jpg"
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        if ext not in IMAGE_TYPES and ext not in VIDEO_TYPES:
            safe = re.sub(r"[^\w.\- ]", "_", name).strip(" .")[-120:] or "file"
            path = UPLOAD_DIR / f"{stamp}-{safe}"
        else:
            path = UPLOAD_DIR / f"{stamp}{ext}"

        def save() -> int:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            size = 0
            try:
                with path.open("xb") as out:
                    while chunk := file.file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(status_code=413, detail=f"uploads are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB")
                        out.write(chunk)
                if not size:
                    raise HTTPException(status_code=400, detail="empty file")
                return size
            except BaseException:
                path.unlink(missing_ok=True)
                raise

        size = await asyncio.to_thread(save)
        if ext not in IMAGE_TYPES and ext not in VIDEO_TYPES:
            hub.log(f"file: {path.name} ({size // 1024} KB)")
            return {"path": str(path)}
        if ext in IMAGE_TYPES:
            hub.log(f"photo: {path.name} ({size // 1024} KB)")
            return {"path": str(path)}
        started = time.monotonic()
        try:
            digest = await asyncio.get_running_loop().run_in_executor(
                None, digest_video, path, getattr(hub, "transcribe_bytes", None))
        except Exception as e:  # noqa: BLE001
            path.unlink(missing_ok=True)
            shutil.rmtree(path.with_name(path.stem + "-frames"), ignore_errors=True)
            raise HTTPException(status_code=422, detail=f"could not read that video: {e}")
        hub.log(f"video: {path.name} ({size // 1024} KB, {clock(digest['duration'])}, "
                f"{len(digest['frames'])} frames, {len(digest['transcript'])} chars heard, {time.monotonic() - started:.1f} s)")
        return {"path": str(path), "frames": str(len(digest["frames"])), "duration": str(digest["duration"])}

    @app.get("/uploads/{name}", dependencies=[Depends(bearer)])
    async def uploaded(name: str) -> Response:
        """A photo sent earlier, by file name; the phone re-fetches thumbnails when it reloads a transcript.
        For a video that thumbnail is its first frame."""
        path = UPLOAD_DIR / os.path.basename(name)
        if path.parent != UPLOAD_DIR or not path.is_file():
            raise HTTPException(status_code=404, detail="no such photo")
        if path.suffix.lower() in VIDEO_TYPES:
            frames = (video_digest(path) or {}).get("frames") or []
            if not frames or not os.path.isfile(frames[0][1]):
                raise HTTPException(status_code=404, detail="no thumbnail for that video")
            path = Path(frames[0][1])
        data = await asyncio.get_running_loop().run_in_executor(None, path.read_bytes)
        return Response(content=data, media_type=IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream"))

    @app.get("/image", dependencies=[Depends(bearer)])
    async def reply_image(path: str) -> FileResponse:
        """A picture a reply shows (`![caption](/abs/path.png)`), for the phone to draw inline."""
        try:
            real = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="no such image")
        if not real.is_file() or real.suffix.lower() not in IMAGE_TYPES or not image_path_allowed(real):
            raise HTTPException(status_code=404, detail="no such image")
        return FileResponse(real, media_type=IMAGE_TYPES[real.suffix.lower()])

    @app.get("/videos/{name}", dependencies=[Depends(bearer)])
    async def uploaded_video(name: str) -> FileResponse:
        """A video sent earlier, for the phone to play back; byte ranges supported, as AVPlayer asks for them."""
        path = UPLOAD_DIR / os.path.basename(name)
        if path.parent != UPLOAD_DIR or path.suffix.lower() not in VIDEO_TYPES or not path.is_file():
            raise HTTPException(status_code=404, detail="no such video")
        return FileResponse(path, media_type="video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4")

    @app.post("/speak", dependencies=[Depends(bearer)])
    async def speak(request: Request):
        """`text` as a WAV in the voice. `spoken`: it is ready to say already (a terminal borrowing the voice
        sends what its own StreamingSpeaker made of the reply), so it isn't put through speechify again."""
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="nothing to say")
        wav = await asyncio.get_running_loop().run_in_executor(None, hub.speak_bytes, text, bool(body.get("spoken")))
        return Response(content=wav, media_type="audio/wav")

    @app.get("/widgets", dependencies=[Depends(bearer)])
    async def widgets() -> dict:
        """The phone's Home Screen widgets: the project threads, usage and what is next on the schedule, in one
        answer (Hub.glance). Widgets ask every quarter of an hour or so, and whenever the app has been used."""
        return await asyncio.to_thread(hub.glance)

    @app.post("/speak/pieces", dependencies=[Depends(bearer)])
    async def speak_pieces(body: dict) -> dict:
        """A finished reply cut into speakable pieces for the phone's read-aloud button (tts.read_aloud_pieces).
        The phone asks POST /speak for each with `spoken`, or says them in its own voice when this one is off;
        nothing here needs the GPU."""
        from vision.tts import read_aloud_pieces

        return {"pieces": read_aloud_pieces(str(body.get("text") or ""))}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        token = bearer_token(websocket.headers)
        # Accept first either way: a bad token then gets a clean close code (4401) the phone can recognise,
        # rather than a generic handshake failure it would keep retrying.
        await websocket.accept()
        if not hub.authorized(token):
            await websocket.close(code=4401, reason="bad token")
            return
        hub.clients.add(websocket)
        who = "terminal" if websocket.query_params.get("client") == "terminal" else "phone"
        # A terminal window following one chat (remote.py) says which, and its pid: one window per chat.
        mirror = websocket.query_params.get("chat") if who == "terminal" else None
        if mirror:
            pid = websocket.query_params.get("pid") or ""
            hub.mirror_joined(mirror, websocket, int(pid) if pid.isdigit() else None)
        if who == "phone":
            hub.phones.add(websocket)
            selfupdate.nudge()  # an app opening is when a newer Vision is worth knowing about
        hub.log(f"{who} connected ({len(hub.clients)} client{'s' if len(hub.clients) != 1 else ''})")
        try:
            await websocket.send_json(hub.hello())
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    await websocket.send_json({"type": "error", "text": "frames must be JSON objects"})
                    continue
                if isinstance(msg, dict):
                    await hub.handle(websocket, msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            hub.log(f"socket error: {e}")
        finally:
            hub.clients.discard(websocket)
            hub.phones.discard(websocket)
            if mirror:
                hub.mirror_left(mirror, websocket)
            hub.log(f"{who} disconnected")

    @app.exception_handler(404)
    async def _404(request: Request, exc) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    return app


# ---------------------------------------------------------------- start-up
def tailscale_url(port: int) -> str | None:
    """https://<machine>.<tailnet>.ts.net if Tailscale Serve/Funnel is publishing this port.

    Tailscale merely being up is not enough: without a serve config the https:// name answers nothing,
    and a QR pointing there would send the phone to a dead address.
    """
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5, encoding="utf-8")
        name = json.loads(r.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
        r = subprocess.run([exe, "serve", "status", "--json"], capture_output=True, text=True, timeout=5, encoding="utf-8")
        published = f":{port}" in r.stdout  # a proxy handler like "http://127.0.0.1:8765"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return f"https://{name}" if name and published else None


def lan_ip() -> str | None:
    """The address this machine has on its default route (what a phone on the same Wi-Fi dials)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; the kernel just picks the source address
            return s.getsockname()[0]
    except OSError:
        return None


def machine_name() -> str:
    """What this computer calls itself, for the phone's list of Visions ("" when it has no name)."""
    import socket

    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return ""


def pairing_payload(url: str, token: str, local: str | None = None, name: str = "") -> str:
    """What the QR carries. `local`: the address on this Wi-Fi, when `url` is another one (Tailscale, a
    tunnel), so the phone can fall back to it at home with that switched off. `name`: what the computer
    calls itself, which is what the phone lists the connection under. An older app ignores both."""
    body = {"url": url, "token": token}
    if local and local != url:
        body["local"] = local
    if name:
        body["name"] = name
    return json.dumps(body)


def qr_lines(payload: str) -> list[str]:
    """The pairing QR as terminal lines (half-block characters, two modules per row)."""
    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(payload)
    qr.make(fit=True)
    m = qr.get_matrix()
    if len(m) % 2:
        m.append([False] * len(m[0]))
    blocks = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return ["".join(blocks[(top, bot)] for top, bot in zip(m[y], m[y + 1])) for y in range(0, len(m), 2)]


def pairing_info(cfg: Config, host: str | None, port: int | None, public_url: str | None, regenerate_token: bool = False) -> dict:
    """Where the phone should connect and the token it should present (what the QR code carries)."""
    host = host or cfg.remote.host
    port = port or cfg.remote.port
    token = load_token(regenerate_token)
    # Bound to every interface: the phone dials the LAN address, not 0.0.0.0.
    reach = (lan_ip() or "127.0.0.1") if host in ("0.0.0.0", "") else host
    local = f"http://{reach}:{port}"
    url = public_url or cfg.remote.public_url or tailscale_url(port) or local
    name = machine_name()
    return {"host": host, "port": port, "local": local, "url": url, "token": token, "name": name,
            "payload": pairing_payload(url, token, local, name)}


def snippet(text: str, word: str, width: int = 140) -> str:
    """`text` on one line, cut to about `width` characters around the first `word` (lower case), with an
    ellipsis where it was cut."""
    flat = " ".join(text.split())
    at = flat.lower().find(word)
    if len(flat) <= width or at < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, at - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    if start:
        start = flat.find(" ", start) + 1 or start  # whole words at the cut
    return ("…" if start else "") + flat[start:end].rstrip() + ("…" if end < len(flat) else "")


PINNED = STATE_DIR / "pinned.json"  # the chats pinned to the top of the list, by id (Hub.pin)


def load_pinned() -> set[str]:
    try:
        return {str(c) for c in json.loads(PINNED.read_text())}
    except (OSError, ValueError, TypeError):
        return set()


def save_pinned(pinned: set[str]) -> None:
    try:
        PINNED.parent.mkdir(parents=True, exist_ok=True)
        tmp = PINNED.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(pinned)))
        os.replace(tmp, PINNED)
    except OSError:
        pass  # a pin that doesn't survive a restart is no reason to fail the tap

RESTART_MARKER = STATE_DIR / "restart.json"  # a graceful restart is under way (see Hub.maybe_restart)


def write_restart_marker() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RESTART_MARKER.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def take_restart_marker() -> bool:
    """True once after a graceful restart (a marker less than five minutes old), then gone."""
    try:
        at = json.loads(RESTART_MARKER.read_text(encoding="utf-8")).get("at") or 0
        RESTART_MARKER.unlink()
    except (OSError, ValueError, AttributeError):
        return False
    return time.time() - at < 300


RESTART_DROPPED = {"--new", "--continue", "-c", "--new-token"}  # one-off start-up flags a restart must not repeat


def restart_argv(argv: list[str]) -> list[str]:
    """The command line that brings `vision serve` back: the same interpreter and options, less the
    one-off ones (chats come back from the journal; the pairing token stays)."""
    return [sys.executable, "-m", "vision", *(a for a in argv[1:] if a not in RESTART_DROPPED)]


def watch_keys(hub: "Hub", on_clear, on_pair=lambda: None):
    """`r` asks for a graceful restart, `c` clears the window (on_clear redraws the one-line banner)
    and `p` shows the pairing code again (on_pair), read a key at a time off a terminal. Returns what
    puts the terminal back as it was (a no-op when stdin is not a terminal)."""
    if not sys.stdin.isatty():
        return lambda: None
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)  # keys arrive one at a time and are not echoed; Ctrl-C still stops the server

    def restore() -> None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:
            pass

    def loop() -> None:
        while True:
            try:
                key = os.read(fd, 1).decode(errors="ignore").lower()
            except OSError:
                return
            if not key:
                return
            if key == "r":
                if hub.loop is not None:
                    hub.loop.call_soon_threadsafe(hub.request_restart, "r pressed")
            elif key == "c":
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
                on_clear()
            elif key == "p":
                on_pair()

    threading.Thread(target=loop, daemon=True, name="serve-keys").start()
    return restore


def serve(cfg: Config, brain, info: dict, log=print, follow_defaults: bool = True,
          open_initial: bool = False, on_clear=None, on_pair=None) -> None:
    import uvicorn

    from vision.remote import remove_serve_descriptor, write_serve_descriptor

    hub = Hub(cfg, brain, info["token"], log=log, follow_defaults=follow_defaults,
              open_initial=open_initial, journal_enabled=True)
    hub.watch_updates()
    restore_keys = watch_keys(hub, on_clear or (lambda: None), on_pair or (lambda: None))

    def exec_restart() -> None:
        """Become a fresh `vision serve` on the new code: same terminal, same port (the listening
        socket is not inherited), chats back from the journal. The phone just reconnects."""
        hub.cancel()
        restore_keys()
        remove_serve_descriptor()
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, restart_argv(sys.argv))

    hub.exec_restart = exec_restart
    try:
        write_serve_descriptor(info["host"], info["port"])
    except OSError as e:
        log(f"terminals cannot find this server (serve.json): {e}")
    try:
        uvicorn.run(create_app(hub), host=info["host"], port=info["port"], log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)
    finally:
        restore_keys()
        remove_serve_descriptor()
