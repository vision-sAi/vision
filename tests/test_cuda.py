from vision.cuda import holder_name


def test_holder_name():
    assert holder_name([".venv/bin/python", "-m", "vision"]) == "vision"
    assert holder_name(["/usr/bin/python3", "/tmp/x/probe4.py"]) == "probe4.py"
    assert holder_name(["/usr/bin/python3", "-c", "print(1)"]) == "python3"
    assert holder_name(["/usr/bin/ptyxis"]) == "ptyxis"
    assert holder_name([]) == "?"


def test_gpu_claim_is_exclusive_across_processes(tmp_path):
    import subprocess
    import sys

    from vision.cuda import GpuBusy, GpuClaim

    lock = tmp_path / "voice.gpu.lock"
    a = GpuClaim(lock)
    a.acquire()
    assert a.held and lock.read_text().strip() == str(__import__("os").getpid())
    a.acquire()  # re-entrant for the same object
    # A second process cannot take it and learns who has it.
    code = (
        "import sys; from vision.cuda import GpuClaim, GpuBusy\n"
        f"c = GpuClaim(__import__('pathlib').Path({str(lock)!r}))\n"
        "try:\n    c.acquire()\nexcept GpuBusy as e:\n    print('busy', e.pid); sys.exit(3)\n"
        "print('got it'); sys.exit(0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 3, r.stdout + r.stderr
    assert r.stdout.split() == ["busy", str(__import__("os").getpid())]
    a.release()
    assert not a.held
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "got it", r.stdout + r.stderr
