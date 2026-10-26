"""Make the process look like `vision`, not `/…/.venv/bin/python -m vision`.

Terminals label their tabs with the foreground process. Ptyxis appends ` — <cmdline>` to the title
unless the command line is empty, and it turns every NUL in /proc/<pid>/cmdline into a space, so even
`claude` (one word plus its terminator) picks up a suffix. The only way to get a bare tab title is an
empty command line: overwrite argv[0] with NUL and set the last byte of the argv area to something
else, which flips the kernel's /proc reader into setproctitle(3) mode and reports just "". `comm`
is set to `vision` so ps/top show `[vision]`. Python has long since copied sys.argv, so nothing
inside the interpreter notices.
"""
from __future__ import annotations

import ctypes
import os
import sys

PR_SET_NAME = 15


def _arg_span() -> tuple[int, int]:
    """(arg_start, arg_end) of this process: fields 48 and 49 of /proc/self/stat."""
    with open("/proc/self/stat", encoding="utf-8") as f:
        stat = f.read()
    fields = stat[stat.rindex(")") + 2 :].split()  # past `pid (comm)`; comm may hold spaces
    return int(fields[45]), int(fields[46])


def hide_cmdline(name: str = "vision") -> bool:
    """Blank /proc/self/cmdline and name the thread `name`. True if it took; False (and nothing
    changed) off Linux or when the argv area could not be found."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        start, end = _arg_span()
        size = end - start
        if size < 2:
            return False
        area = (ctypes.c_char * size).from_address(start)
        area[0] = b"\0"
        area[size - 1] = b"v"
        ctypes.CDLL(None).prctl(PR_SET_NAME, name.encode(), 0, 0, 0)
    except (OSError, ValueError, IndexError, AttributeError):
        return False
    return True


def process_name(pid: int) -> str:
    """The kernel's short name for a process (what ps shows in brackets once cmdline is blank)."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


__all__ = ["hide_cmdline", "process_name"]
