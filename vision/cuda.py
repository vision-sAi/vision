"""Make pip-installed NVIDIA runtime libraries visible to CTranslate2 and ONNX Runtime.

The nvidia-*-cu12 wheels drop their .so files under site-packages/nvidia/*/lib, which is not
on the dynamic loader path. Loading them globally once, before the consumers import, is enough.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import site
from pathlib import Path

_LIBS = [
    "cuda_runtime/lib/libcudart.so.12",
    "cuda_nvrtc/lib/libnvrtc.so.12",
    "cublas/lib/libcublasLt.so.12",
    "cublas/lib/libcublas.so.12",
    "cudnn/lib/libcudnn.so.9",
    "cufft/lib/libcufft.so.11",
    "curand/lib/libcurand.so.10",
]
_done = False


def gpu_holders() -> list[str]:
    """Other processes using the GPU, as "pid 1234 vision (4.7 GB)", biggest first. [] when unknown."""
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        try:
            pid, mib = (x.strip() for x in line.split(","))
            pid, mib = int(pid), int(mib)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        argv = cmdline(pid)
        rows.append((mib, f"pid {pid} {holder_name(argv)} ({mib / 1024:.1f} GB)"))
    return [r for _, r in sorted(rows, reverse=True)]


class GpuBusy(RuntimeError):
    """Another process already holds the claim."""

    def __init__(self, pid: int, name: str):
        self.pid, self.name = pid, name
        super().__init__(f"already loaded in another Vision (pid {pid} {name}). Use that one, or close it and try again.")


class GpuClaim:
    """One process at a time may keep a GPU model loaded: an advisory lock on a file under the state
    directory. The kernel drops the lock when the holder exits (crash included), so a stale claim
    cannot outlive its process. `acquire()` raises GpuBusy naming the holder; `release()` lets go."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                pid = int(os.read(fd, 32).decode().strip() or 0)
            except ValueError:
                pid = 0
            os.close(fd)
            raise GpuBusy(pid, holder_name(cmdline(pid))) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)  # closing the descriptor releases the flock
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


def cmdline(pid: int) -> list[str]:
    try:
        argv = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        return []
    if not argv:  # a Vision that blanked its command line for the terminal tab (see proctitle.py)
        from vision.proctitle import process_name

        name = process_name(pid)
        return [name] if name else []
    return argv


def holder_name(argv: list[str]) -> str:
    """A short name for a process: "vision" for `python -m vision`, the script for `python foo.py`."""
    if not argv:
        return "?"
    name = os.path.basename(argv[0])
    if name.startswith("python") and len(argv) > 1:
        if argv[1] == "-m" and len(argv) > 2:
            return argv[2]
        if not argv[1].startswith("-"):
            return os.path.basename(argv[1])
    return name


def preload() -> None:
    global _done
    if _done:
        return
    roots = list(site.getsitepackages())
    if site.getusersitepackages():
        roots.append(site.getusersitepackages())
    for root in roots:
        for n in _LIBS:
            p = os.path.join(root, "nvidia", n)
            if os.path.exists(p):
                try:
                    ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
    _done = True
