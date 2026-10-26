"""Which optional builds are installed and how to add one. Standard library only: `vision.__main__` uses it
to turn a missing dependency into an install hint before anything heavy has imported."""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # the checkout, when Vision runs from one
SETUP = ROOT / "scripts" / "setup.sh"


def _path(p: Path) -> str:
    """A path to paste into a shell: ~ for the home directory, quoted when it has to be."""
    home = Path.home()
    try:
        rel = p.relative_to(home)
    except ValueError:
        return shlex.quote(str(p))
    quoted = shlex.quote(str(rel))
    return f"~/{rel}" if quoted == str(rel) else shlex.quote(str(p))


def voice_backend() -> str | None:
    """Which voice build is installed: "mac" (voice-mac), "nvidia" (voice-nvidia), "cpu" (voice-cpu) or None.

    Read from package metadata, so it never imports torch and packages an earlier build left behind
    don't count: mlx-audio means the Mac build (a switch from voice-cpu leaves torch behind); otherwise
    PyPI's Linux torch requires the CUDA libraries, the CPU index's torch (and macOS's) doesn't.
    """
    from importlib.metadata import PackageNotFoundError, requires, version

    if sys.platform == "darwin":
        try:
            version("mlx-audio")
            return "mac"
        except PackageNotFoundError:
            pass
    try:
        v = version("torch")
    except PackageNotFoundError:
        return None
    if "+cpu" in v or not sys.platform.startswith("linux"):
        return "cpu"
    deps = requires("torch") or []
    return "nvidia" if any(d.startswith(("nvidia-", "cuda-toolkit")) for d in deps) else "cpu"


def voice_hardware() -> tuple[str, str] | None:
    """What this machine can run the voice on in real time: ("mac", chip) on Apple Silicon, ("nvidia",
    "RTX 4070 · 12 GB") with an NVIDIA card on Linux, or None (the voice would run on the CPU, several
    times slower than real time)."""
    import platform
    import shutil
    import subprocess

    if sys.platform == "darwin":
        return ("mac", "Apple Silicon") if platform.machine() == "arm64" else None
    if not sys.platform.startswith("linux") or not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    name, _, mem = out[0].partition(",")
    name = name.strip().removeprefix("NVIDIA ").removeprefix("GeForce ")
    return ("nvidia", f"{name} · {round(int(mem) / 1024)} GB" if mem.strip().isdigit() else name)


VOICE_MIN_VRAM_GB = 8  # under this the voice and Whisper don't both fit on the card
VOICE_DOWNLOAD = {"mac": "about 1 GB of packages and up to 10 GB of speech models",
                  "nvidia": "about 6 GB of packages and 10 GB of speech models"}


def voice_install_cmd(backend: str) -> list[str] | None:
    """The command that installs the voice build and its speech models with no questions asked, when
    Vision runs from a checkout (the installer's, or a clone); None for a bare pip install."""
    return [str(SETUP), "--voice", f"--{backend}", "--yes", "--no-doctor"] if SETUP.exists() else None


def setup_hint(backend: str = "") -> str:
    """The command that installs the voice, optionally for a given backend, runnable from any directory."""
    flag = {"cpu": " --cpu", "nvidia": " --nvidia", "mac": " --mac"}.get(backend, "")
    if SETUP.exists():
        return f"{_path(SETUP)} --voice{flag}"
    # Installed without a checkout: uv, not pip, because only uv reads the torch sources in pyproject.toml.
    if backend:
        return f"uv pip install 'vision[voice-{backend}]'"
    return (
        "uv pip install 'vision[voice-nvidia]' (NVIDIA GPU), 'vision[voice-mac]' (Apple Silicon) "
        "or 'vision[voice-cpu]' (anything else)"
    )


def sync_command(extra: str) -> str:
    """One extra added to the environment Vision is running from, with no questions asked (the guided
    setup runs it after a yes). In a checkout that is uv against the lock file; otherwise pip."""
    if SETUP.exists():
        import shutil

        local = Path.home() / ".local" / "bin" / "uv"  # where uv's installer puts it, on PATH or not
        uv = "uv" if shutil.which("uv") or not local.exists() else _path(local)
        return f"{uv} sync --project {_path(ROOT)} --frozen --inexact --extra {extra}"
    return f"{shlex.quote(sys.executable)} -m pip install 'vision[{extra}]'"


def extra_hint(extra: str) -> str:
    """The command that installs one optional extra ("voice", "serve", "weather")."""
    if extra == "voice":
        return "vision voice setup" if SETUP.exists() else setup_hint()
    if SETUP.exists():
        if extra == "serve":
            return f"{_path(SETUP)} --serve"
        return f"uv sync --project {_path(ROOT)} --frozen --inexact --extra {extra}"
    return f"pip install 'vision[{extra}]'"
