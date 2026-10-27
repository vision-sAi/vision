"""`vision serve` as a background service, so the phone finds Vision whenever the computer is on:
a systemd user unit on Linux, a launchd agent on macOS. `vision service install | remove | status`,
and the phone step of the first run (vision/onboard.py) offers it.

The service runs this checkout's interpreter with the PATH of the shell that installed it, because
the provider CLIs (claude, codex, grok) usually live in ~/.local/bin or under a version manager, and
neither systemd nor launchd would find them there. No root: both are per-user."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from vision.config import STATE_DIR

UNIT = "vision.service"
LABEL = "com.vision.serve"
LOG = STATE_DIR / "serve.log"  # macOS; on Linux the journal has it (journalctl --user -u vision)
# Set in the unit: `vision serve` then waits for a server that is already up to go away and takes over
# (see wait_while), where a failed exit would have either manager start it again every few seconds.
ENV = "VISION_SERVICE"


class ServiceError(Exception):
    pass


def kind() -> str | None:
    """"systemd", "launchd", or None when this machine has neither for the user."""
    if sys.platform == "darwin":
        return "launchd" if shutil.which("launchctl") else None
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    return None


def unit_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / UNIT


def command() -> list[str]:
    """What the service runs: this interpreter's `vision serve`, with no QR (nobody is looking)."""
    return [sys.executable, "-m", "vision", "serve", "--no-qr"]


def systemd_unit(argv: list[str], path: str) -> str:
    pct = lambda text: text.replace("%", "%%")  # noqa: E731 - systemd expands % specifiers in both lines
    return f"""[Unit]
Description=Vision (vision serve, for the Vision Remote app)

[Service]
ExecStart={pct(" ".join(shlex.quote(a) for a in argv))}
WorkingDirectory=%h
Environment="PATH={pct(path)}"
Environment="{ENV}=1"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def launchd_plist(argv: list[str], path: str) -> str:
    args = "\n".join(f"    <string>{escape(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{escape(path)}</string>
    <key>{ENV}</key><string>1</string>
  </dict>
  <key>WorkingDirectory</key><string>{escape(str(Path.home()))}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{escape(str(LOG))}</string>
  <key>StandardErrorPath</key><string>{escape(str(LOG))}</string>
</dict>
</plist>
"""


def stay_awake(pid: int | None = None, popen: Callable[..., object] = subprocess.Popen) -> object | None:
    """macOS: keep the computer from sleeping while `vision serve` (pid, default this process) runs, or the
    phone loses it the moment the display has been off a few minutes. `caffeinate -s` holds a system-sleep
    assertion only while on AC power, so a Mac on battery still sleeps; `-w` makes caffeinate exit with
    the server, so nothing is left to clean up. Returns the caffeinate process, or None when not applicable."""
    if sys.platform != "darwin":
        return None
    exe = shutil.which("caffeinate") or ("/usr/bin/caffeinate" if os.path.exists("/usr/bin/caffeinate") else None)
    if exe is None:
        return None
    try:
        return popen([exe, "-s", "-w", str(pid or os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def wait_while(busy: Callable[[], bool], poll: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> None:
    """Sit quietly until `busy` says no: the service, started while another `vision serve` holds the port
    (one open in a terminal, or the last install's still stopping), serves once that one is gone."""
    while busy():
        sleep(poll)


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ServiceError(f"`{' '.join(cmd)}` could not run: {e}") from e
    if check and r.returncode != 0:
        raise ServiceError(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip() or f'exit status {r.returncode}'}")
    return r


def _domain() -> str:
    return f"gui/{os.getuid()}"


def installed() -> bool:
    return unit_path().exists()


def running() -> bool:
    k = kind()
    if k == "systemd":
        return _run("systemctl", "--user", "is-active", "--quiet", UNIT, check=False).returncode == 0
    if k == "launchd":
        return _run("launchctl", "print", f"{_domain()}/{LABEL}", check=False).returncode == 0
    return False


def pid() -> int | None:
    """The service's process, or None when it is not running (or there is no service manager)."""
    import re

    k = kind()
    if k == "systemd":
        out = _run("systemctl", "--user", "show", "--property=MainPID", "--value", UNIT, check=False).stdout.strip()
        return int(out) if out.isdigit() and int(out) > 0 else None
    if k == "launchd":
        m = re.search(r"^\s*pid = (\d+)", _run("launchctl", "print", f"{_domain()}/{LABEL}", check=False).stdout, re.M)
        return int(m.group(1)) if m else None
    return None


def waiting(server: dict | None) -> bool:
    """Whether the service is up but only waiting: another `vision serve` (one in a terminal) holds the
    port, and the service takes over when that one stops. `server` is remote.running_server()."""
    mine = pid()
    return bool(server) and running() and mine is not None and server.get("pid") != mine


def stop() -> None:
    """Stop the service for now; it stays installed (start() brings it back, and so does the next login)."""
    if kind() == "systemd":
        _run("systemctl", "--user", "stop", UNIT)
    elif kind() == "launchd":
        _run("launchctl", "bootout", f"{_domain()}/{LABEL}")


def start() -> None:
    if kind() == "systemd":
        _run("systemctl", "--user", "start", UNIT)
    elif kind() == "launchd":
        _run("launchctl", "bootstrap", _domain(), str(unit_path()))


def install() -> Path:
    """Write the unit and start it now and at every login. Returns where the unit is. Raises ServiceError."""
    k = kind()
    if k is None:
        raise ServiceError("no user service manager here (systemd on Linux, launchd on macOS); run `vision serve` in a terminal instead")
    from vision.brain import without_shims

    path, env_path = unit_path(), without_shims(os.environ.get("PATH", ""))  # installed from inside a turn, too
    path.parent.mkdir(parents=True, exist_ok=True)
    if k == "systemd":
        path.write_text(systemd_unit(command(), env_path), encoding="utf-8")
        _run("systemctl", "--user", "daemon-reload")
        _run("systemctl", "--user", "enable", UNIT)
        _run("systemctl", "--user", "restart", UNIT)
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _run("launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)  # an earlier install, if any
        path.write_text(launchd_plist(command(), env_path), encoding="utf-8")
        _run("launchctl", "bootstrap", _domain(), str(path))
    return path


def remove() -> bool:
    """Stop it and take the unit away. False when there was none."""
    k, path = kind(), unit_path()
    if not path.exists():
        return False
    if k == "systemd":
        _run("systemctl", "--user", "disable", "--now", UNIT, check=False)
    elif k == "launchd":
        _run("launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)
    path.unlink()
    if k == "systemd":
        _run("systemctl", "--user", "daemon-reload", check=False)
    return True


def logs_hint() -> str:
    """Where its output goes, as something to run."""
    return f"tail -f {LOG}" if sys.platform == "darwin" else "journalctl --user -u vision -f"


def lingers() -> bool | None:
    """systemd only: whether the service keeps running with nobody logged in (None: not systemd, or unknown)."""
    if kind() != "systemd":
        return None
    user = os.environ.get("USER") or ""
    r = _run("loginctl", "show-user", user, "--property=Linger", check=False) if user and shutil.which("loginctl") else None
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip().endswith("=yes")
