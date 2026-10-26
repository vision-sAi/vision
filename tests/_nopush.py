"""Import this in any test that can reach push: it keeps the suite off the real phones. With a relay or a key in
the config push is ready, so a Hub in a test would otherwise read the real push_devices.json and
live_activity.json and send through the live relay. Patched for the whole process on first import;
test_push.py takes the real `send` back from REAL_SEND."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from vision import liveactivity, push

REAL_SEND = push.send
_DIR = tempfile.TemporaryDirectory(prefix="vision-nopush-")

patch.object(push, "DEVICES", Path(_DIR.name) / "push_devices.json").start()
patch.object(push, "send", lambda *a, **kw: []).start()
patch.object(liveactivity, "TOKENS", Path(_DIR.name) / "live_activity.json").start()
