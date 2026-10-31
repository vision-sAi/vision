"""Screenshots of the terminal chat, for checking its look without a terminal.

    python scripts/cli_shots.py [scene …]     (all scenes without arguments)

Builds a real ChatScreen headlessly (a pipe for input, an output that writes nowhere, a fixed size),
fills it with one scene, lets prompt_toolkit draw it, then copies the renderer's last screen cell by
cell into an HTML page (each cell an inline box, half blocks as two-colour gradients) and has a
headless Firefox photograph that. Out: scripts/cli-shots/out/<scene>.png (and the .html beside it).
The scenes use made-up data: no brain, no server and no network.
"""
from __future__ import annotations

import html
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["COLORTERM"] = "truecolor"  # before vision.ui is imported: it picks the colour depth from this
os.environ["PROMPT_TOOLKIT_COLOR_DEPTH"] = "DEPTH_24_BIT"
os.environ["TERM"] = "xterm-256color"  # rich draws a "dumb" terminal (a tool's shell) 80 wide, whatever it is told
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output.vt100 import Vt100_Output  # noqa: E402

from vision import ui  # noqa: E402
from vision.brain import AgentRun, ToolCall  # noqa: E402
from vision.buddy import Buddy  # noqa: E402
from vision.theme import TEXT  # noqa: E402

OUT = ROOT / "scripts" / "cli-shots" / "out"
BG = "#15131b"  # the terminal's own background and text, as the mock-ups take them
FG = TEXT
ANSI = {"ansiblack": "#000000", "ansired": "#ff7a8a", "ansigreen": "#5be3a0", "ansiyellow": "#ffc46b", "ansiblue": "#5b8cff",
        "ansimagenta": "#8b6cff", "ansicyan": "#56b6c2", "ansigray": "#eeeaf8", "ansibrightblack": "#5f5a72",
        "ansibrightred": "#ff7a8a", "ansibrightgreen": "#5be3a0", "ansibrightyellow": "#ffc46b", "ansibrightblue": "#78a0ff",
        "ansibrightmagenta": "#a58bff", "ansibrightcyan": "#56b6c2", "ansiwhite": "#ffffff"}
SETTLE_S = 1.5  # how long the screen runs before it is copied: the buddy slides in, the pacer reveals the reply


class _Nowhere(io.StringIO):
    def fileno(self):  # prompt_toolkit asks; there is no terminal behind this
        raise io.UnsupportedOperation


# ---------------------------------------------------------------- scenes
CODE = """for path in UPLOAD_DIR.iterdir():
    if time.time() - path.stat().st_mtime < 60:
        continue  # still being read
    path.unlink(missing_ok=True)"""

SCOUTS = [
    {"id": "a", "scout": {"name": "Atlas", "hue": 205, "trait": "antenna"}, "mood": "needs_you", "status": "active",
     "threads": {"waiting_on_you": 1}, "approvals": []},
    {"id": "b", "scout": {"name": "Juniper", "hue": 28, "trait": "sprout"}, "mood": "focused", "status": "active", "threads": {}, "approvals": []},
    {"id": "c", "scout": {"name": "Moss", "hue": 140, "trait": "curl"}, "mood": "proud", "status": "active", "threads": {}, "approvals": []},
    {"id": "d", "scout": {"name": "Wren", "hue": 270, "trait": "ears"}, "mood": "asleep", "status": "active", "threads": {}, "approvals": []},
]


def _edit(cid: str, path: str) -> ToolCall:
    return ToolCall(cid, "Edit", path, done=True, output="ok", diff=[{"old": 40, "new": 40, "lines": [
        " for path in UPLOAD_DIR.iterdir():", "-    path.unlink()", "+    if time.time() - path.stat().st_mtime < 60:", "+        continue"]}])


def scene_chat(s: ui.ChatScreen) -> None:
    s.title_fn = lambda: "Fix the flaky upload test"
    s.folder_fn = lambda: "~/code/vision"
    s.add(ui.user_grid("why does test_upload fail about one run in five?"), gap_before=True)
    text = ("I'll look at the test and the cleanup code first.\n\n"
            "Found it. `drop_uploads` runs on a timer, and when the test is slow the file is gone before the assertion reads it. "
            "I made it skip anything younger than a minute:\n\n"
            f"```python\n{CODE}\n```\n\n"
            "The test passed 50 runs in a row after that. Want me to commit it?")
    agent = AgentRun("g1", "Explore", "Find who deletes uploads", steps=[("Grep", "unlink"), ("Read", "vision/uploads.py")], done=True,
                     tool_uses=2, duration_ms=8200)
    at = text.index("Found it.")
    calls = [(at, ToolCall("1", "Read", "tests/test_upload.py", done=True, output="x\n" * 120)),
             (at, ToolCall("2", "Grep", "drop_uploads", done=True, output="3 matches")),
             (at, agent),
             (text.index("```"), _edit("3", "vision/uploads.py")),
             (text.index("```"), ToolCall("4", "Bash", "pytest tests/test_upload.py --count 50", done=True, output="50 passed"))]
    e = s.add_past_reply(text, calls)
    e.footer = "Worked for 48.4s · done 12:41 PM"
    s.add(ui.notice_grid("checkpoint saved"))
    s.add(ui.notice_grid("the quota is nearly used up: 9% left of the weekly window", "warn"))
    s.suggestion = "Run the whole suite to be sure"
    s.scouts_fn = lambda: SCOUTS[:2]


def scene_working(s: ui.ChatScreen) -> None:
    from vision.turnqueue import QueuedTurn, TurnQueue

    s.title_fn = lambda: "Frigate camera dropouts"
    s.folder_fn = lambda: "~"
    s.add(ui.user_grid("the driveway camera keeps dropping out overnight, can you look at frigate's logs on offsite"), gap_before=True)
    e = s.start_reply()
    s.update_reply(e, "Checking Frigate's logs on offsite for the driveway camera.\n\n")
    s.update_reply(e, tool=ToolCall("t1", "Bash", "ssh offsite pct exec 101 -- docker logs frigate --since 12h", done=True, output="…"))
    s.update_reply(e, tool=ToolCall("t2", "Grep", "driveway.*(error|timeout)", done=True, output="38 matches"))
    run = AgentRun("a1", "Explore", "Compare the dropouts with the router's DHCP leases", steps=[("Read", "/etc/dnsmasq.d/leases")],
                   model="claude-opus-5", effort="high", started=time.monotonic() - 41)
    s.update_reply(e, agent=run)
    s.update_reply(e, "The dropouts line up with 02:00 every night: the camera's DHCP lease renews and it comes back on a new")
    e.tokens_fn = lambda: 4200
    s.turns = TurnQueue()
    s.turns.put(QueuedTurn("also check the backyard one while you're there", shown=True))
    s.can_steer_fn = lambda: True
    s.placeholder = "Ask a follow-up"
    s.mode_fn = lambda: "plan"
    s.scouts_fn = lambda: [SCOUTS[2]]


def scene_fresh(s: ui.ChatScreen) -> None:
    s.folder_fn = lambda: "~/code/vision"
    s.add(ui.header_renderable(model="Opus 5.5 · high", directory="~/code/vision", voice="off", version="2.4.0", scouts=3, needing=1))
    s.placeholder = "What are we doing?"
    s.scouts_fn = lambda: SCOUTS[:1]


def scene_picker(s: ui.ChatScreen) -> None:
    s.title_fn = lambda: "Fix the flaky upload test"
    s.folder_fn = lambda: "~/code/vision"
    s.add(ui.reply_grid("Done. Two ways to land it, your call."))
    tabs = [("Claude", [("opus", "Opus 5.5", "the strongest, for hard work"), ("sonnet", "Sonnet 5.5", "fast and sharp, the daily driver"),
                        ("haiku", "Haiku 5.5", "quick answers"), ("fable", "Fable 5.1", "long-form writing")], "via Claude Code"),
            ("Codex", [("gpt", "GPT-6 Astra", "OpenAI's strongest")], "via Codex"), ("Grok", [("grok", "Grok 4.6", "")], "via Grok")]
    s.open_picker("Model", tabs, "opus", lambda v: None)
    s._picker["idx"] = 1


def scene_question(s: ui.ChatScreen) -> None:
    s.title_fn = lambda: "Fix the flaky upload test"
    s.folder_fn = lambda: "~/code/vision"
    s.add(ui.reply_grid("Done. Two ways to land it, your call."))
    s.open_questions([
        {"question": "How should I land the fix?", "header": "Landing", "options": [
            {"label": "Commit on a branch and open a PR", "description": "the repo's usual way: one squashed commit per PR"},
            {"label": "Commit straight to master", "description": "skips review"},
            {"label": "Leave it uncommitted", "description": "you'll look first"}]},
        {"question": "Run the tests first?", "header": "Tests", "options": [{"label": "Yes"}, {"label": "No"}]},
    ], lambda a: None)


def scene_shell(s: ui.ChatScreen) -> None:
    s.title_fn = lambda: "Disk space"
    s.folder_fn = lambda: "~/code/vision"
    s.add(ui.shell_grid("du -sh ~/.cache"), gap_before=True)
    s.add(ui.shell_output_grid("4.1G\t/home/me/.cache", 0))
    s.add(ui.shell_grid("ls /nope"), gap_before=True)
    s.add(ui.shell_output_grid("ls: cannot access '/nope': No such file or directory", 2))
    s.shell_mode = True
    s.area.text = "git status --short"


def scene_unfolded(s: ui.ChatScreen) -> None:
    scene_chat(s)
    s.toggle_tools()  # ctrl-o: a row per call and the edit's diff


def scene_narrow(s: ui.ChatScreen) -> None:
    scene_chat(s)


def scene_scouts(s: ui.ChatScreen) -> None:
    scene_fresh(s)
    s.scouts_fn = lambda: SCOUTS


SCENES = {  # name: (fill, columns, rows)
    "chat": (scene_chat, 104, 44),
    "unfolded": (scene_unfolded, 104, 52),
    "working": (scene_working, 104, 28),
    "fresh": (scene_fresh, 104, 26),
    "picker": (scene_picker, 104, 30),
    "question": (scene_question, 104, 30),
    "shell": (scene_shell, 104, 22),
    "narrow": (scene_narrow, 60, 44),
    "scouts": (scene_scouts, 104, 26),
}


# ---------------------------------------------------------------- capture and HTML
def capture(fill, width: int, height: int) -> tuple[list[dict], object]:
    """Run the screen with `fill`'s scene for SETTLE_S and copy what the renderer last drew."""
    grab: dict = {}
    done = threading.Event()
    ui._private_output = lambda: None  # the session's output below, not the real terminal
    with create_pipe_input() as pipe:
        output = Vt100_Output(_Nowhere(), lambda: Size(rows=height, columns=width), term="xterm-256color")
        with create_app_session(input=pipe, output=output):
            screen = ui.ChatScreen(os.path.join(tempfile.mkdtemp(), "history"), lambda: "", buddy=Buddy(),
                                   mode=lambda: "auto", status_right=lambda: "wake off", corner=lambda: "Opus 5.5 · high")
            fill(screen)

            def copy():
                time.sleep(SETTLE_S)
                last = screen.app.renderer._last_screen
                grab["rows"] = [dict(last.data_buffer[y]) for y in range(height)] if last else []
                grab["style"] = screen.app._merged_style
                done.set()

            threading.Thread(target=copy, daemon=True).start()
            threading.Thread(target=screen.run, daemon=True).start()
            done.wait(SETTLE_S + 10)
    return grab.get("rows") or [], grab.get("style")


def _colour(value: str | None) -> str | None:
    if not value or value in ("default", "ansidefault"):
        return None
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value
    return ANSI.get(value)


def page(rows: list[dict], style, width: int, title: str) -> str:
    """The captured cells as HTML: one inline box per cell, so the grid holds whatever the font."""
    out = []
    for y, row in enumerate(rows):
        cells, x = [], 0
        while x < width:
            ch = row.get(x)
            if ch is None:
                cells.append("<i>&nbsp;</i>")
                x += 1
                continue
            attrs = style.get_attrs_for_style_str(ch.style)
            fg, bg = _colour(attrs.color), _colour(attrs.bgcolor)
            if attrs.reverse:
                fg, bg = bg or BG, fg or FG
            w = max(1, ch.width)
            css = f"width:{w}ch;"
            char = ch.char or " "
            if char in "▀▄" and (fg or bg):
                top, bottom = (fg or FG, bg or BG) if char == "▀" else (bg or BG, fg or FG)
                css += f"background:linear-gradient({top} 50%,{bottom} 50%);"
                char = " "
            else:
                if fg:
                    css += f"color:{fg};"
                if bg:
                    css += f"background:{bg};"
                if attrs.bold:
                    css += "font-weight:700;"
                if attrs.italic:
                    css += "font-style:italic;"
                if attrs.underline:
                    css += "text-decoration:underline;"
                if attrs.strike:
                    css += "text-decoration:line-through;"
            if char in "│╭╮╰╯┃▏▕":
                css += "transform:scaleY(1.4);"
            cells.append(f'<i style="{css}">{html.escape(char) if char != " " else "&nbsp;"}</i>')
            x += w
        out.append("<div>" + "".join(cells) + "</div>")
    return f"""<!doctype html><meta charset=utf-8><style>
body{{margin:0;background:#0b0a0f;padding:22px;font-family:'Adwaita Mono','Hack','Noto Sans Mono',monospace;font-size:14px}}
.win{{background:{BG};color:{FG};border-radius:10px;display:inline-block;overflow:hidden;border:1px solid #2a2635}}
.bar{{height:30px;line-height:30px;text-align:center;color:#8f89a6;font:13px sans-serif;background:#1c1a24;border-bottom:1px solid #2a2635}}
.t{{padding:8px 10px}}
.t div{{height:19px;white-space:pre;display:flex}}
.t i{{font-style:normal;display:inline-block;height:19px;line-height:19px;text-align:center;overflow:visible}}
</style><div class=win><div class=bar>{html.escape(title)}</div><div class=t>{"".join(out)}</div></div>"""


def shoot(name: str) -> Path:
    fill, width, height = SCENES[name]
    rows, style = capture(fill, width, height)
    OUT.mkdir(parents=True, exist_ok=True)
    doc = OUT / f"{name}.html"
    doc.write_text(page(rows, style, width, f"vision · {name}"), encoding="utf-8")
    png = OUT / f"{name}.png"
    size = f"{width * 9 + 80},{height * 19 + 100}"
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(["firefox", "--headless", "--no-remote", "--profile", profile, "--screenshot", str(png),
                        f"--window-size={size}", doc.as_uri()], capture_output=True, timeout=90)
    return png


def main(names: list[str]) -> None:
    if len(names) == 2 and names[0] == "--one":
        print(shoot(names[1]), flush=True)
        os._exit(0)  # the screen's threads don't wind down on their own
    for name in names or list(SCENES):
        if name not in SCENES:
            print(f"no scene {name!r}: {', '.join(SCENES)}")
            continue
        # One process a scene: a prompt_toolkit app never quite leaves, and the next would share its session.
        subprocess.run([sys.executable, __file__, "--one", name])


if __name__ == "__main__":
    main(sys.argv[1:])
