"""A desktop notification from the terminal chat: a long reply finished while you were in another
window. notify-send on Linux (libnotify, which every desktop ships), Notification Center on a Mac
through osascript. Nothing is raised: with neither around it simply does not notify."""
from __future__ import annotations

import shutil
import subprocess
import sys


def desktop_notify(title: str, body: str) -> bool:
    """Show `title` / `body` on the desktop. True when something was asked to show it."""
    cmd: list[str] | None = None
    if sys.platform == "darwin" and shutil.which("osascript"):
        quote = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731 - AppleScript's string escapes
        cmd = ["osascript", "-e", f'display notification "{quote(body)}" with title "{quote(title)}"']
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=Vision", title, body]
    if cmd is None:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def reply_summary(text: str, limit: int = 60) -> str:
    """The first words of a reply for the notification line: one line, no markdown furniture."""
    for line in (text or "").splitlines():
        raw = line.strip()
        if raw.startswith(("```", "~~~")):
            continue  # a fence line, not words
        line = raw.lstrip("#*-> ").strip("`").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""
