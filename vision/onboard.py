"""The first run, start to finish: `vision onboard`, and what `vision` runs by itself on a machine that
has never held a chat (see first_run).

One chain of short steps in the plain console, each under a header that says where you are: your
providers (vision/welcome.py's tick list, skipped with a line when one already works), what to call
you and how much to do unasked, the voice where this machine can run it, then the phone (the server
kept running in the background, the pairing code, and a wait for the scan). A finished step is
written to onboarding.json at once, so leaving halfway (Ctrl-C) resumes at the step you left.
`vision onboard` goes through all of it again, with what is saved as each question's default.

The steps are a list (STEPS): a new one is a function that asks its questions through the `IO` and
returns whether it finished. Tests script the answers the same way as the wizard's (tests/test_onboard.py)."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from vision import providers
from vision.config import STATE_DIR, load_config, save_config_value

STATE = STATE_DIR / "onboarding.json"  # {"done": [step names], "finished": bool}
from vision.theme import ACCENT


def load_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state) + "\n", encoding="utf-8")


def has_chatted() -> bool:
    """Whether Vision has held a chat on this machine: every chat leaves a journal behind, and one from
    before the journals left its input history, a last session (any provider's) or the old `welcomed` marker."""
    from vision import turnjournal

    root = turnjournal.JOURNAL_DIR
    if root.exists() and any(root.glob("*.jsonl")):
        return True
    return any(found for name in ("history", "last_session*", "welcomed") for found in STATE_DIR.glob(name))


def _toml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(io, text: str) -> None:
    io.say(f"  [green]✓[/green] {text}")


def _warn(io, text: str) -> None:
    io.say(f"  [yellow]![/yellow] {text}")


def _dim(io, text: str) -> None:
    io.say(f"  [dim]{text}[/dim]")


# ---------------------------------------------------------------- the steps
def providers_step(flow: "Flow") -> bool:
    """Something that can really answer; everything after it needs one, so without one the flow stops
    here. On a first run with a provider already working (Claude Code was signed in before Vision came
    along) there is nothing to ask: it says so and moves on, and `vision providers` adds more later."""
    from vision.welcome import Wizard

    io = flow.io
    wizard = Wizard(io, flow.cfg, first_run=True, again=flow.again)  # first_run: no closing card, the flow goes on
    if not flow.again:
        with io.working("looking at what's here…"):
            working = [p.label for p in providers.enabled(flow.cfg) if wizard._works(p.name)]
        if working:
            _ok(io, f"{' and '.join(working)} {'is' if len(working) == 1 else 'are'} ready. [dim]`vision providers` adds more.[/dim]")
            return True
        _dim(io, "Tick everything you've got and Vision sets each one up. A subscription you already pay for works best; a free local model works too.")
    ready = wizard.run(intro=False)
    flow.cfg = load_config()
    return bool(ready)


def you_step(flow: "Flow") -> bool:
    """What to call you, and whether Vision acts unasked or plans first. Both keep what is saved when skipped."""
    from vision.welcome import Back

    io, brain = flow.io, flow.cfg.brain
    try:
        name = " ".join(io.ask("What should Vision call you? (Enter for nothing)", default=brain.address_user_as).split())
    except Back:
        name = brain.address_user_as
    if name != brain.address_user_as:
        save_config_value("brain", "address_user_as", _toml_string(name))
    if name:
        _ok(io, f"Vision will call you {name}.")
    mode = io.pick("How much should Vision do without asking?", [
        ("auto", "Auto", "runs commands and edits files without asking"),
        ("plan", "Plan", "read-only until you approve its plan"),
    ], current=brain.mode) or brain.mode
    if mode != brain.mode:
        save_config_value("brain", "mode", _toml_string(mode))
    _ok(io, f"Vision starts in {mode} mode. Shift-Tab switches during a chat.")
    flow.cfg = load_config()
    return True


def voice_step(flow: "Flow") -> bool:
    """Optional: the voice, where the machine can run it in real time (see welcome.voice_step)."""
    from vision import welcome

    welcome.voice_step(flow.io, heading=False)
    return True


def voice_offered() -> bool:
    from vision import welcome

    return welcome.voice_offered()


def serve_installed() -> bool:
    """Whether the packages `vision serve` needs are here (the `serve` extra)."""
    import importlib
    import importlib.util

    importlib.invalidate_caches()  # they may have been installed a moment ago, by this process
    return all(importlib.util.find_spec(m) is not None for m in ("fastapi", "uvicorn", "qrcode"))


def wait_for_server(timeout: float = 30.0) -> dict | None:
    """The `vision serve` on this machine once it is up and answering, or None when the time runs out."""
    import time

    from vision.remote import _get, running_server

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server = running_server()
        if server:
            try:
                _get(server, "/health", timeout=3)
                return server
            except (OSError, ValueError):
                pass
        time.sleep(0.5)
    return None


def wait_for_phone(server: dict, timeout: float = 300.0) -> bool:
    """True once the app has connected to this server (its /health counts the phones on it)."""
    import time

    from vision.remote import _get

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _get(server, "/health", timeout=3).get("phones"):
                return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def phone_step(flow: "Flow") -> bool:
    """Optional: Vision on the phone. Whatever it needs is installed after a yes, `vision serve` is kept
    running in the background on another, and the pairing code stays up until the app has scanned it.
    Every way out of it counts as done: nothing later depends on the phone."""
    from vision import service
    from vision.install import sync_command
    from vision.remote import running_server

    io = flow.io
    _dim(io, "The Vision Remote app (built from ios/, see ios/README.md) chats and talks with this computer's Vision.")
    if not io.confirm("Set up your iPhone now?", default=False):
        _dim(io, "Skipped. `vision serve` shows the pairing code whenever you want the phone.")
        return True
    if not serve_installed():
        install = sync_command("serve")
        if not io.confirm(f"That needs a few more packages. Install them now? [dim]({install})[/dim]", default=True):
            _dim(io, "Skipped. Install them and `vision serve` shows the pairing code.")
            return True
        if io.run(install, shell=True) != 0 or not serve_installed():
            _warn(io, f"The install didn't finish. Run it yourself, then `vision serve`:  {install}")
            return True
    server = running_server()
    if server is None and service.kind() and io.confirm(
            "Keep Vision running in the background, so the phone finds it whenever this computer is on?", default=True):
        try:
            with io.working("starting vision serve…"):
                service.install()
                server = wait_for_server()
        except service.ServiceError as e:
            _warn(io, f"The background service didn't start: {e}")
        else:
            if server:
                _ok(io, "Vision runs in the background now and at every login. [dim]`vision service remove` undoes that.[/dim]")
            else:
                _warn(io, f"The background service is installed but not answering yet. [dim]{service.logs_hint()}[/dim]")
    if server is None:
        _dim(io, "`vision serve` in a terminal starts it and shows the pairing code when you want the phone.")
        return True
    from rich.text import Text

    from vision.server import pairing_info, qr_lines

    info = pairing_info(flow.cfg, None, server.get("port"), None)
    io.say(Text("\n" + "\n".join("   " + line for line in qr_lines(info["payload"]))))
    io.say("  Open Vision Remote on your iPhone and scan this.")
    if info["url"] == info["local"]:
        _dim(io, f"This code works on this Wi-Fi. For anywhere: `tailscale serve --bg {info['port']}`, then `vision pair` for a new code.")
    try:
        with io.working("waiting for your phone… Ctrl-C skips"):
            paired = wait_for_phone(server)
    except KeyboardInterrupt:
        paired = False
    if paired:
        _ok(io, "Your phone is connected.")
    else:
        _dim(io, "No phone yet. `vision pair` shows the code again whenever you're ready.")
    return True


@dataclass(frozen=True)
class Step:
    name: str  # what onboarding.json calls it
    title: str  # its header
    run: Callable[["Flow"], bool]  # True: finished. False: it could not be, and the steps after it need it
    applies: Callable[[], bool] = lambda: True  # False: nothing to ask on this machine, so it is left out


STEPS = (
    Step("providers", "Your providers", providers_step),
    Step("you", "You", you_step),
    Step("voice", "Voice", voice_step, voice_offered),
    Step("phone", "Your phone", phone_step),
)


# ---------------------------------------------------------------- the chain
class Flow:
    def __init__(self, io, cfg=None):
        self.io = io
        self.cfg = cfg or load_config()
        self.again = False  # `vision onboard`: every step, whatever is done or already working

    def run(self, again: bool = False) -> bool:
        """Every step that is not done yet, in order (`again`: all of them). True when it reached the end."""
        import typer

        from vision.ui import welcome_renderable
        from vision.welcome import Back

        io, self.again = self.io, again
        steps = [s for s in STEPS if s.applies()]
        done = [] if again else [s.name for s in steps if s.name in load_state().get("done", [])]
        io.say(welcome_renderable(
            "Welcome back. Picking up where you left off." if done else "Welcome. A few quick questions and you're chatting.",
            "Esc skips a question · Ctrl-C leaves, and `vision` picks it up from there"))
        try:
            for n, step in enumerate(steps, 1):
                if step.name in done:
                    continue
                io.say(f"\n[bold {ACCENT}]◆[/] [bold]{step.title}[/bold] [dim]· step {n} of {len(steps)}[/dim]")
                try:
                    finished = step.run(self)
                except Back:  # Esc at a question the step did not catch: on to the next one
                    finished = True
                if not finished:
                    io.say("\n[dim]Setup stops here for now. `vision` picks it up from this step.[/dim]")
                    return False
                done.append(step.name)
                save_state({"done": done, "finished": False})
        except (KeyboardInterrupt, EOFError, typer.Abort):
            io.say("\n[dim]Setup paused. `vision` picks it up from this step.[/dim]")
            return False
        save_state({"done": [s.name for s in STEPS], "finished": True})
        if again:  # a first run goes straight into the chat, which says it is set up (welcome.set_up_note)
            self.summary()
        return True

    def summary(self) -> None:
        """The wizard's own closing card, and a line for what the other steps saved."""
        from vision.models import model_label
        from vision.welcome import Wizard

        io, brain = self.io, self.cfg.brain
        wizard = Wizard(io, self.cfg)
        ready = [p.name for p in providers.enabled(self.cfg) if wizard._works(p.name)]
        io.say("")
        io.say(wizard._card(ready, model_label(brain.model) or brain.model))
        called = f"calls you {brain.address_user_as}" if brain.address_user_as else "has no name for you"
        _dim(io, f"Vision {called} and starts in {brain.mode} mode. `vision onboard` goes through this again; `vision` starts a chat.")


def first_run(cfg, io=None, *, interactive: bool | None = None) -> bool | None:
    """Run the first-run flow when this machine has not been through it and there is a person at the
    terminal. None: nothing ran. True: it ran to the end (the caller reloads its config and opens the
    chat). False: it stopped short (nothing can answer yet, or Ctrl-C), so there is no chat to open.

    A machine that has already held a chat is past its first run: it is marked finished without a
    question, and gets only the provider wizard, as it always has, when nothing is ready."""
    if os.environ.get("VISION_NO_SETUP"):
        return None
    tty = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
    if not tty:
        return None
    state = load_state()
    if not state and has_chatted():
        state = {"done": [s.name for s in STEPS], "finished": True}
        save_state(state)
    if io is None:
        from vision.cli import console
        from vision.welcome import ConsoleIO

        io = ConsoleIO(console)
    if not state.get("finished"):
        return Flow(io, cfg).run()
    if any(providers.ready(p.name, cfg) for p in providers.enabled(cfg)):
        return None
    from vision.welcome import Back, Wizard

    try:
        Wizard(io, cfg, first_run=True).run()
    except (KeyboardInterrupt, EOFError, Back):
        io.say("\n[dim]Setup stopped. `vision providers` picks it back up.[/dim]")
    return True
