"""Processes on Windows and on Unix: the few things the two do differently, kept in one place.

Windows has no process groups to signal, and `os.kill(pid, 0)` there is no probe: signal 0 is
CTRL_C_EVENT, and any other number terminates the process outright. So nothing outside this file
should reach for os.kill or os.killpg."""

from __future__ import annotations

import ntpath
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"

_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_ACCESS_DENIED = 5
_LOCK_BYTE = 1 << 40


def windows_pid_alive(pid: int) -> bool:
    """A process by that id is running; one we may not open is still running."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def windows_image(pid: int) -> str:
    """The program file of a running process, "" when that can't be told. A saved pid can have been
    reused by the time it is read back, so check this before ending one."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        return name.value if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def try_lock(fd: int, shared: bool = False) -> bool:
    """An advisory lock on an open file, False when another holder has it. The system drops it when
    the descriptor closes or the process dies. Windows' locks are mandatory, and would stop others
    reading what they cover, so there it sits on one byte far past the end of any real file. Windows
    has no shared lock: a reader's probe takes this one for the moment it holds it."""
    if not WINDOWS:
        import fcntl

        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    import msvcrt

    here = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    finally:
        os.lseek(fd, here, os.SEEK_SET)
    return True


def group_kwargs() -> dict:
    """Popen arguments that start a process apart from ours, so kill_tree can end it and its children."""
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}


def kill_tree(pid: int, force: bool = False) -> None:
    """End a process started with group_kwargs and whatever it started. OSError when it could not be.
    Windows has no polite version for a console program, so there it is always forced."""
    if not WINDOWS:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        return
    done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, text=True, errors="replace")
    if done.returncode:
        raise ProcessLookupError(done.stderr.strip() or f"taskkill could not end {pid}")


def replace_process(argv: list[str]) -> None:
    """Become `argv`. Windows' exec starts it and exits, handing the console back to the shell mid-chat,
    so there this stays and waits instead."""
    if not WINDOWS:
        os.execvp(argv[0], argv)
    sys.exit(subprocess.call([shutil.which(argv[0]) or argv[0], *argv[1:]]))


def native_cli(exe: str) -> str | None:
    """The program to run for a CLI found on PATH, None when only a batch wrapper could be found.
    npm installs a .cmd on Windows, and cmd.exe re-reads a batch file's arguments as a command line:
    the persona goes on the brains' command lines, so an `&` in it would run as a command, and it is
    longer than a batch line may be. Never run one; use the real program it wraps."""
    if not (WINDOWS and exe.lower().endswith((".cmd", ".bat"))):
        return exe
    wrapper = Path(exe)
    found = sorted((wrapper.parent / "node_modules").rglob(f"{wrapper.stem}.exe"))
    return str(found[0]) if found else None


def wrapper_note(exe: str) -> str:
    return f"{exe} is a batch wrapper, which can't safely carry Vision's prompts. Install the CLI's native Windows build."


def quote(arg: str) -> str:
    """`arg` quoted for this system's shell."""
    return subprocess.list2cmdline([arg]) if WINDOWS else shlex.quote(arg)


def user_shell(command: str) -> list[str]:
    """The command line that runs `command` in the user's own shell."""
    if not WINDOWS:
        return [os.environ.get("SHELL") or "/bin/sh", "-c", command]
    exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    # Windows PowerShell prints in the console's code page otherwise; what reads this expects UTF-8.
    utf8 = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    return [exe, "-NoProfile", "-NonInteractive", "-Command", utf8 + command]


def bash() -> str | None:
    """Where bash is, None if nowhere. On Windows that means Git Bash, the one Claude Code uses too:
    the `bash` in System32 is WSL's launcher, which runs commands in another machine's filesystem."""
    if not WINDOWS:
        return shutil.which("bash")
    found = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    if git := shutil.which("git"):
        found.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
    for root in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if base := os.environ.get(root):
            found.append(str(Path(base) / ("Programs" if root == "LOCALAPPDATA" else "") / "Git" / "bin" / "bash.exe"))
    found.append(shutil.which("bash"))
    system = ntpath.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    for path in found:
        if path and Path(path).is_file() and not path.lower().startswith(system):
            return path
    return None
