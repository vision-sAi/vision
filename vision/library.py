"""The phone's Library: every picture a reply showed, and every photo, video and file sent from the phone.

Reply pictures are found by scanning the brains' session logs for `![caption](/abs/path.png)`; each log
is rescanned only when it changes (cache keyed on size + mtime), so a refresh after the first is cheap.
Uploads are the files in UPLOAD_DIR (a video's digest folder and json are not items of their own).

A project has a Library of its own: <project folder>/library.json indexes
what was sent from inside the project's chat (`source: "you"`) and what its threads produced (`source:
"thread"`: the paths a result lists as `artifacts`, and pictures its summary shows), each
{path, name, kind, source, thread?, branch?, caption, time}. Only the index entry of a thread's file is
ever deleted from the phone: the file belongs to the repo.
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
THUMB_DIR = Path.home() / ".cache" / "vision" / "thumbs"

_PICTURE = re.compile(rb'!\[([^\]\n"]{0,200})\]\((/[^)\s"\\]+\.(?:png|jpe?g|webp|gif))\)', re.I)
_logs: dict[str, tuple[int, float, list[tuple[str, str]]]] = {}
_lock = threading.Lock()


def _session_logs() -> list[str]:
    from vision.codex import CODEX_SESSIONS
    from vision.grok import GROK_SESSIONS
    from vision.local import SESSIONS_DIR
    from vision.sessions import CLAUDE_PROJECTS

    paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))
    paths += glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl"))
    paths += glob.glob(os.path.join(GROK_SESSIONS, "*", "*", "chat_history.jsonl"))
    paths += glob.glob(os.path.join(str(SESSIONS_DIR), "*.json"))
    return paths


def _pictures_in(path: str) -> list[tuple[str, str]]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    cached = _logs.get(path)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]
    found: list[tuple[str, str]] = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        data = b""
    if b"![" in data:
        seen = set()
        for m in _PICTURE.finditer(data):
            pic = m.group(2).decode("utf-8", "replace")
            if pic not in seen:
                seen.add(pic)
                found.append((pic, m.group(1).decode("utf-8", "replace").replace("\\n", " ").strip()))
    _logs[path] = (st.st_size, st.st_mtime, found)
    return found


def reply_pictures(allowed) -> list[dict]:
    """Pictures replies showed that still exist on disk (and that `allowed(Path)` lets the phone see)."""
    items: dict[str, dict] = {}
    with _lock:
        for log in _session_logs():
            for pic, caption in _pictures_in(log):
                if pic in items:
                    continue
                p = Path(pic)
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS or not allowed(p.resolve()):
                    continue
                items[pic] = {"kind": "image", "source": "vision", "path": pic, "name": p.name,
                              "caption": caption, "time": st.st_mtime, "size": st.st_size}
    return list(items.values())


def uploads(upload_dir: Path) -> list[dict]:
    out = []
    try:
        entries = list(os.scandir(upload_dir))
    except OSError:
        return out
    for e in entries:
        if not e.is_file() or e.name.endswith((".json", ".tmp")):
            continue
        ext = os.path.splitext(e.name)[1].lower()
        kind = "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "file"
        # "20260925-113458-e5d17a-report.pdf" → "report.pdf"; photos keep their stamped name
        name = re.sub(r"^\d{8}-\d{6}-[0-9a-f]{6}-", "", e.name) if kind == "file" else e.name
        st = e.stat()
        out.append({"kind": kind, "source": "you", "path": e.path, "name": name, "caption": "",
                    "time": st.st_mtime, "size": st.st_size})
    return out


def library(upload_dir: Path, allowed) -> list[dict]:
    items = reply_pictures(allowed) + uploads(upload_dir)
    items.sort(key=lambda i: i["time"], reverse=True)
    return items


def delete(path: str, upload_dir: Path, allowed) -> bool:
    """Delete a Library item from disk; only something the Library lists, so the phone can't reach any other
    file. A clip's digest (its json and frames folder) goes with it. False when the Library has no such item."""
    return bool(delete_many([path], upload_dir, allowed))


def delete_many(paths, upload_dir: Path, allowed) -> list[str]:
    """Delete several Library items with one listing (each listing reads every session log); the paths it
    actually deleted, skipping any the Library doesn't list."""
    listed = {i["path"]: i for i in library(upload_dir, allowed)}
    done = []
    for path in dict.fromkeys(paths):
        item = listed.get(path)
        if item is None:
            continue
        p = Path(path)
        p.unlink(missing_ok=True)
        if item["kind"] == "video":
            p.with_suffix(".json").unlink(missing_ok=True)
            shutil.rmtree(p.with_name(p.stem + "-frames"), ignore_errors=True)
        done.append(path)
    return done


# -- a project's Library ------------------------------------------------------------------------
PROJECT_INDEX = "library.json"
PROJECT_MAX = 500  # entries kept per project; the oldest drop off
_TEXT_PICTURE = re.compile(_PICTURE.pattern.decode(), re.I)
_index_lock = threading.Lock()


def _kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "file"


def project_entries(folder: Path) -> list[dict]:
    """The project's index as stored, oldest first."""
    try:
        data = json.loads((folder / PROJECT_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("path"), str)] if isinstance(data, list) else []


def _save_index(folder: Path, entries: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (PROJECT_INDEX + ".tmp")
    tmp.write_text(json.dumps(entries[-PROJECT_MAX:], indent=1), encoding="utf-8")
    os.replace(tmp, folder / PROJECT_INDEX)


def index_project(folder: Path, items: list[dict]) -> list[dict]:
    """Add items ({path, source, thread?, branch?, caption?}) to the project's index; a path already there
    is refreshed in place. Only files that exist are indexed. Returns what was added or refreshed."""
    added: list[dict] = []
    with _index_lock:
        entries = project_entries(folder)
        where = {e["path"]: i for i, e in enumerate(entries)}
        for item in items:
            path = os.path.abspath(os.path.expanduser(str(item.get("path") or "")))
            if not path or not os.path.isfile(path):
                continue
            entry = {"path": path, "name": os.path.basename(path), "kind": _kind(path), "source": item.get("source") or "thread",
                     "caption": str(item.get("caption") or "")[:200], "time": time.time()}
            for key in ("thread", "branch"):
                if item.get(key):
                    entry[key] = item[key]
            if path in where:
                entry["caption"] = entry["caption"] or entries[where[path]].get("caption") or ""  # a bare mention keeps it
                entries[where[path]] = entry
            else:
                where[path] = len(entries)
                entries.append(entry)
            added.append(entry)
        if added:
            _save_index(folder, entries)
    return added


def artifact_entry(entry: str) -> dict:
    """One `artifacts` entry, "/abs/path" or "/abs/path — what it shows" (a dash of any kind, spaced), as
    {path, caption}."""
    if os.path.isfile(os.path.expanduser(entry.strip())):  # a file whose own name holds a spaced dash
        return {"path": entry.strip(), "caption": ""}
    path, caption = (re.split(r"\s+[—–-]\s+", entry.strip(), maxsplit=1) + [""])[:2]
    return {"path": path.strip(), "caption": caption.strip()}


def index_thread_result(folder: Path, thread: dict, result: dict) -> list[dict]:
    """A thread's result into its project's Library: each file in `artifacts` (with what it shows, when the
    entry says), and every picture its summary or findings show (`![caption](/abs/path.png)`)."""
    items = [artifact_entry(p) for p in result.get("artifacts") or [] if isinstance(p, str)]
    for text in [result.get("summary") or "", *(result.get("findings") or [])]:
        items += [{"path": m.group(2), "caption": m.group(1)} for m in _TEXT_PICTURE.finditer(str(text))]
    for item in items:
        item.update(source="thread", thread=thread.get("id"), branch=thread.get("branch") or "")
    return index_project(folder, items)


def relocate_project(folder: Path, old_root: str, new_root: str) -> None:
    """A thread landed: its files in the worktree now live in the project's folder too, so the index
    points there (where the file exists) before the worktree goes."""
    old_root = old_root.rstrip(os.sep) + os.sep
    with _index_lock:
        entries = project_entries(folder)
        moved = False
        for e in entries:
            if e["path"].startswith(old_root):
                there = os.path.join(new_root, e["path"][len(old_root):])
                if os.path.isfile(there):
                    e["path"], e["branch"], moved = there, "", True
        if moved:
            _save_index(folder, entries)


def unindex_project(folder: Path, path: str) -> dict | None:
    """Take a path out of the project's index; the entry it had, None when it had none."""
    with _index_lock:
        entries = project_entries(folder)
        gone = next((e for e in entries if e["path"] == path), None)
        if gone is not None:
            _save_index(folder, [e for e in entries if e["path"] != path])
    return gone


def project_library(folder: Path, allowed, sessions: list[str] | None = None) -> list[dict]:
    """A project's Library for the phone, newest first: what its index lists that is still on disk (and that
    `allowed(Path)` lets the phone see), plus the pictures its chat's replies showed (`sessions`: those
    session log files)."""
    items: dict[str, dict] = {}
    for e in project_entries(folder):
        p = Path(e["path"])
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file() or not allowed(p.resolve()):
            continue
        name = re.sub(r"^\d{8}-\d{6}-[0-9a-f]{6}-", "", e["name"]) if e.get("kind") == "file" else e["name"]
        items[e["path"]] = {**e, "name": name, "time": e.get("time") or st.st_mtime, "size": st.st_size}
    with _lock:
        for log in sessions or []:
            for pic, caption in _pictures_in(log):
                p = Path(pic)
                if pic in items:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS and allowed(p.resolve()):
                    items[pic] = {"kind": "image", "source": "vision", "path": pic, "name": p.name, "caption": caption,
                                  "time": st.st_mtime, "size": st.st_size}
    return sorted(items.values(), key=lambda i: i["time"], reverse=True)


def thumbnail(path: Path, size: int = 360) -> bytes:
    """A JPEG no bigger than `size` on its long edge, cached on disk by path + mtime."""
    from PIL import Image, ImageOps

    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_mtime}|{st.st_size}|{size}".encode()).hexdigest()
    cached = THUMB_DIR / f"{key}.jpg"
    if cached.is_file():
        return cached.read_bytes()
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.convert("RGBA").split()[-1])
            im = bg
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    data = buf.getvalue()
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data
