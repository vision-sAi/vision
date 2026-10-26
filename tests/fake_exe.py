"""Stand-in executables for tests that need a real command to run (a fake CLI, a fake app-server)."""
from __future__ import annotations

import stat
from pathlib import Path


def python_command(path: str | Path, source: str) -> str:
    """Write `source` (Python) as an executable script at `path` (#! line, executable bit) and return
    what to execute."""
    path = Path(path)
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)
