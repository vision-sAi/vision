"""The suite as CI runs it: each test named as it starts, and after HANG_S every thread's stack and an exit.
A hung job otherwise sits until GitHub cancels it hours later, having said nothing about where it stuck."""
import faulthandler
import sys
import unittest

HANG_S = 600  # the whole suite takes about two minutes

faulthandler.dump_traceback_later(HANG_S, exit=True, file=sys.stderr)
unittest.main(module=None, argv=["unittest", "discover", "-s", "tests", "-v"])
