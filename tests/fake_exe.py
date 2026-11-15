"""Stand-in executables for tests that need a real command to run (a fake CLI, a fake app-server)."""
from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

WINDOWS = os.name == "nt"


def python_command(path: str | Path, source: str) -> str:
    """Write `source` (Python) as a program at `path` and return what to execute. Elsewhere that is a
    script with a #! line and the executable bit; Windows runs neither a #! line nor a batch file Vision
    would accept (procs.native_cli), so there it is `path.exe`: the launcher pip's console scripts use,
    with this Python and the source appended as a zip, as an installed CLI's .exe would be."""
    path = Path(path)
    if WINDOWS:
        return str(_windows_exe(path.with_name(path.name + ".exe"), source))
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _windows_exe(exe: Path, source: str) -> Path:
    import distlib  # test-only (CI installs it): it carries the launchers

    arm = "ARM" in os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
    launcher = (Path(distlib.__file__).parent / ("t64-arm.exe" if arm else "t64.exe")).read_bytes()
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr("__main__.py", source)
    shebang = b'#!"' + sys.executable.encode("utf-8") + b'"\n'
    exe.write_bytes(launcher + shebang + body.getvalue())
    return exe
