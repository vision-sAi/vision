"""A desktop notification from the terminal chat: a long reply finished while you were in another
window. notify-send on Linux (libnotify, which every desktop ships), Notification Center on a Mac
through osascript, a toast through PowerShell on Windows. Nothing is raised: with none of them around
it simply does not notify."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# The words come in through the environment, so nothing in a reply is ever read as PowerShell. A toast
# needs an app id Windows knows: PowerShell's own, as Vision's terminal chat has no installed shortcut.
TOAST = """
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$null = $text.Item(0).AppendChild($xml.CreateTextNode($env:VISION_TOAST_TITLE))
$null = $text.Item(1).AppendChild($xml.CreateTextNode($env:VISION_TOAST_BODY))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def desktop_notify(title: str, body: str) -> bool:
    """Show `title` / `body` on the desktop. True when something was asked to show it."""
    cmd: list[str] | None = None
    env = None
    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", TOAST]
        env = {**os.environ, "VISION_TOAST_TITLE": title, "VISION_TOAST_BODY": body}
    elif sys.platform == "darwin" and shutil.which("osascript"):
        quote = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731 - AppleScript's string escapes
        cmd = ["osascript", "-e", f'display notification "{quote(body)}" with title "{quote(title)}"']
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=Vision", title, body]
    if cmd is None:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
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
