"""The suite as CI runs it: each test named as it starts, and after HANG_S every thread's stack and an exit.
A hung job otherwise sits until GitHub cancels it hours later, having said nothing about where it stuck."""
import faulthandler
import sys
import unittest

HANG_S = 600  # the whole suite takes about two minutes

if sys.platform == "win32":  # as vision/__main__.py does: a pipe gets the code page otherwise, and "✓" fails to print
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
faulthandler.dump_traceback_later(HANG_S, exit=True, file=sys.stderr)
unittest.main(module=None, argv=["unittest", "discover", "-s", "tests", "-v"])
