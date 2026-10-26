"""The guided provider setup: `vision providers`, and the first step of the first run (vision/onboard.py).

A tick list first: every provider set up here (ticked when on), the servers found running, and the
kinds that can be added; ticking sets one up, unticking switches it off (its settings kept), so the
same screen serves a new install and a later change. Then as few questions as each needs. A CLI provider (Claude Code, Codex, Grok) is
installed on request and signed in to by running its own login in the foreground; each one is asked
whether it is signed in (clis.login_status), so the sign-in is skipped when it is and checked again
after it has run. A provider that can't answer yet (signed out, or a port that lists no models) is
never the one Vision starts on. Model servers on this machine (Ollama, LM Studio, llama.cpp,
vLLM) are looked for on their usual ports, so a running one is a single pick with no questions. A
hosted API starts from a preset (OpenRouter, OpenAI, Groq) and asks only for the key. Each answer
lands in config.toml at once, so a wizard left halfway has still done something. It ends by saving
the model to start on (asked when something is new or the old default went away) and a short card
of what is ready.

Every question goes through an `IO` so tests script the answers (tests/test_welcome.py)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vision import clis, config, providers
from vision.config import load_config, save_provider_table

SERVER, HOSTED, AGENT = "_server", "_hosted", "_agent"
CUSTOM = "_custom"
from vision.theme import ACCENT
MANY_MODELS = 12  # past this many, ask which ones /model should show


def set_up_note() -> str:
    """The first chat's opening line."""
    from vision.install import voice_backend

    voice = " /talk starts a voice conversation." if voice_backend() else ""
    return f"You're set up. /model switches model; `vision providers` adds a provider or switches one off.{voice}"


@dataclass(frozen=True)
class Preset:
    name: str  # its [providers.<name>] table
    label: str
    url: str
    hint: str  # local: how to start it; hosted: what it is
    key_env: str = ""


LOCAL_SERVERS = (
    Preset("ollama", "Ollama", "http://localhost:11434/v1", "ollama serve"),
    Preset("lmstudio", "LM Studio", "http://localhost:1234/v1", "start the server in LM Studio's Developer tab"),
    Preset("llamacpp", "llama.cpp", "http://localhost:8080/v1", "llama-server -m <model>.gguf"),
    Preset("vllm", "vLLM", "http://localhost:8000/v1", "vllm serve <model>"),
)
HOSTED_APIS = (
    Preset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "hundreds of models on one key", "OPENROUTER_API_KEY"),
    Preset("openai", "OpenAI", "https://api.openai.com/v1", "GPT models, paid by the token", "OPENAI_API_KEY"),
    Preset("groq", "Groq", "https://api.groq.com/openai/v1", "open models, very fast", "GROQ_API_KEY"),
)
def list_models(url: str, key: str = "", timeout: float = 4) -> list[str] | None:
    """The model ids a server lists at <url>/models, or None when nothing answers there."""
    if not providers._server_answers(url):
        return None
    headers = {"User-Agent": "vision", **({"Authorization": f"Bearer {key}"} if key else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/models", headers=headers), timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001  something is listening, it just isn't saying
        return []
    return [str(m["id"]) for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]


def _exe(p) -> str:
    """Where its CLI is: the provider's own finder knows the install folders that aren't on PATH yet."""
    find = p.hook("find_cli")
    try:
        found = find() if find else None
    except Exception:  # noqa: BLE001
        found = None
    return found or shutil.which(p.login_cmd[0]) or p.login_cmd[0]


def setup_rows(cfg=None) -> list[dict]:
    """The subscription CLIs as a setup screen needs them: whether each is installed and signed in, and the
    commands that install it and sign in (the desktop app's first run reads this through
    `vision provider status --json`, and runs those commands itself)."""
    cfg = cfg or load_config()
    clis_ = [p for p in providers.REGISTRY.values() if p.builtin and p.login_cmd]
    installed = {p.name: providers.ready(p.name, cfg) for p in clis_}
    with ThreadPoolExecutor(len(clis_) or 1) as pool:
        logins = list(pool.map(lambda p: clis.login_status(p.name) if installed[p.name] else (False, ""), clis_))
    on = {p.name for p in providers.enabled(cfg)}
    return [{"name": p.name, "label": p.label, "product": p.product, "note": p.note, "enabled": p.name in on,
             "installed": installed[p.name], "signed_in": installed[p.name] and ok, "detail": detail,
             "install_cmd": p.install_cmd or "", "install_url": p.install_url or "",
             "login_cmd": [_exe(p), *p.login_cmd[1:]] if installed[p.name] else list(p.login_cmd)}
            for p, (ok, detail) in zip(clis_, logins)]


def _where(url: str) -> str:
    return url.removeprefix("http://").removeprefix("https://").removesuffix("/v1")


def _models_line(ids: list[str]) -> str:
    n = len(ids)
    return f"{n} model{'s' if n != 1 else ''}: {', '.join(ids[:5])}{'…' if n > 5 else ''}"


class Back(Exception):
    """Esc typed at a question: back to the menu."""


class ConsoleIO:
    """The real console: rich for text and prompts, the arrow-key picker, subprocess for the commands."""

    def __init__(self, console):
        self.console = console

    def say(self, text="") -> None:
        self.console.print(text, highlight=False)

    def pick(self, title: str, options: list[tuple[str, str, str]], current: str = "") -> str | None:
        from vision.ui import pick

        return pick(title, options, current=current)

    def pick_many(self, title: str, options: list[tuple[str, str, str]], ticked=()) -> list[str] | None:
        from vision.ui import pick_many

        return pick_many(title, options, ticked)

    def ask(self, question: str, default: str = "", secret: bool = False) -> str:
        from rich.prompt import Prompt

        answer = Prompt.ask(f"  {question}", console=self.console, default=default, show_default=bool(default), password=secret)
        if "\x1b" in answer:  # the prompt is plain line input, so Esc arrives as text
            raise Back
        return answer.strip()

    def confirm(self, question: str, default: bool = True) -> bool:
        from rich.prompt import Confirm

        return Confirm.ask(f"  {question}", console=self.console, default=default)

    def run(self, cmd: list[str] | str, shell: bool = False) -> int:
        """A command in the foreground with the terminal (installers, logins are interactive)."""
        try:
            return subprocess.run(cmd, shell=shell).returncode
        except (OSError, KeyboardInterrupt):
            return 1

    @contextmanager
    def working(self, text: str):
        """A spinner with a line of text for as long as the block runs (a check that takes a moment)."""
        with self.console.status(f"[dim]{text}[/dim]"):
            yield

    def open_url(self, url: str) -> bool:
        """Open a page in the browser, only where there is a desktop to show it (no text browser over ssh)."""
        if sys.platform != "darwin" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        import webbrowser

        return webbrowser.open(url)


class Wizard:
    def __init__(self, io, cfg=None, *, first_run: bool = False, again: bool = False):
        self.io = io
        self.cfg = cfg or load_config()
        self.first_run = first_run
        self.again = again  # `vision onboard` on a machine already set up: what is on is this person's choice
        self.added: list[str] = []
        self.switched_off: list[str] = []
        self.servers: dict[str, list[str] | None] = {}  # LOCAL_SERVERS name -> its models, None = not running
        self._logins: dict[str, tuple[bool, str]] = {}  # what each CLI said of its login: (signed in, detail)
        self._lists: dict[str, bool] = {}  # whether each server set up here lists any models

    # -- helpers
    def _ready(self, name: str) -> bool:
        providers.forget_ready()
        clis.forget_checks()
        return providers.ready(name, self.cfg)

    def _reload(self) -> None:
        self.cfg = load_config()
        providers.forget_ready()

    def _ok(self, text: str) -> None:
        self.io.say(f"  [green]✓[/green] {text}")

    def _warn(self, text: str) -> None:
        self.io.say(f"  [yellow]![/yellow] {text}")

    def _heading(self, text: str) -> None:
        self.io.say(f"\n[bold {ACCENT}]◆[/] [bold]{text}[/bold]")

    def _taken(self) -> set[str]:
        """Base URLs some provider already points at, so a found server isn't offered twice."""
        urls = set()
        for p in providers.enabled(self.cfg):
            ep = providers.endpoint_for(p.name, self.cfg)
            if ep is not None:
                urls.add(ep.base_url.rstrip("/").replace("127.0.0.1", "localhost"))
        return urls

    def _scan(self) -> None:
        """Look at what is here, all at once: the usual server ports, and each installed CLI's login."""
        installed = [p.name for p in providers.REGISTRY.values() if p.builtin and p.login_cmd and self._ready(p.name)]
        with ThreadPoolExecutor(len(LOCAL_SERVERS) + len(installed) or 1) as pool:
            logins = pool.map(clis.login_status, installed)
            found = pool.map(lambda s: list_models(s.url, timeout=2), LOCAL_SERVERS)
            self._logins.update(zip(installed, logins))
        self.servers = {s.name: ids for s, ids in zip(LOCAL_SERVERS, found)}

    def _signed_in(self, name: str) -> bool:
        """Whether a CLI says it is signed in (asked once; a login run here asks again)."""
        if name not in self._logins:
            self._logins[name] = clis.login_status(name)
        return self._logins[name][0]

    def _lists_models(self, name: str) -> bool:
        """Whether a server set up here lists any models. A port that merely answers does not count:
        anything on localhost:8080 would otherwise pass for the local model server."""
        if name not in self._lists:
            try:
                self._lists[name] = bool(clis.local_model_ids(timeout=4, provider=name))
            except Exception:  # noqa: BLE001 - nothing there, or something that is not a model server
                self._lists[name] = False
        return self._lists[name]

    def _works(self, name: str) -> bool:
        """Whether a provider can really take a turn: beyond providers.ready, its CLI is signed in or
        its server lists models."""
        p = providers.REGISTRY[name]
        if not self._ready(name):
            return False
        if p.builtin and p.login_cmd:
            return self._signed_in(name)
        if providers.endpoint_for(name, self.cfg) is not None:
            return self._lists_models(name)
        return True

    def _cli_state(self, p) -> str:
        if not self._ready(p.name):
            return "installs in a step" if p.install_cmd else "needs installing"
        return "✓ ready" if self._signed_in(p.name) else "installed, needs sign-in"

    def _choices(self) -> tuple[list[tuple[str, str, str]], set[str]]:
        """The tick list's rows and which start ticked: every provider set up here, on or off (ticked when
        on), the servers found running that aren't yet, and the three kinds that can be added."""
        rows, on = [], {p.name for p in providers.enabled(self.cfg)}
        ticked = set()
        for p in providers.REGISTRY.values():
            if p.builtin and p.cli:  # the subscriptions
                plan = p.note.split(" · ")[-1].replace("Subscription", "subscription")
                state = self._cli_state(p)
                rows.append((p.name, p.label, f"your {plan} · {state}"))
                # One that is on stays ticked whatever its sign-in check said just now (setup_cli asks it
                # again): unticked, Enter would switch off a provider that only failed a check for a moment.
                if (state == "✓ ready" and (p.name in on or self.first_run)) or (
                        p.name in on and (self.again or not self.first_run) and self._ready(p.name)):
                    ticked.add(p.name)
            elif p.name == "local":  # [local]: when its server answers, or it's on (a later run)
                up = self._ready(p.name) and self._lists_models(p.name)
                if up or (p.name in on and not self.first_run):
                    ep = providers.endpoint_for(p.name, self.cfg)
                    rows.append((p.name, p.label, f"set up · {_where(ep.base_url)}" + ("" if up else " · not answering right now")))
                    ticked |= {p.name} & on
            elif p.source == "config":  # tables from earlier setups
                where = _where(p.endpoint.base_url) if p.endpoint else "`" + " ".join(p.acp.command) + "`" if p.acp else ""
                rows.append((p.name, p.label, "set up" + (f" · {where}" if where else "")))
                ticked |= {p.name} & on
        taken = self._taken()
        for s in LOCAL_SERVERS:
            ids = self.servers.get(s.name)
            if ids is not None and s.name not in providers.REGISTRY and s.url not in taken:
                rows.append((f"{SERVER}:{s.name}", s.label, f"running on this machine · {len(ids)} model{'s' if len(ids) != 1 else ''} · free, private"))
                if self.first_run:
                    ticked.add(f"{SERVER}:{s.name}")
        rows += [
            (SERVER, "A model server", "add one: Ollama, LM Studio, llama.cpp, vLLM, here or on your network"),
            (HOSTED, "An API key", "add one: OpenRouter, OpenAI, Groq or any OpenAI-compatible API"),
            (AGENT, "An ACP agent", "add one: Gemini CLI and friends, a command Vision drives"),
        ]
        return rows, ticked

    # -- steps
    def intro(self) -> None:
        from rich.box import ROUNDED
        from rich.panel import Panel
        from rich.text import Text

        body = Text()
        body.append("◆ Vision\n", style=f"bold {ACCENT}")
        if self.first_run:
            body.append("Welcome. Tick everything you've got and Vision sets each one up.\n")
            body.append("A subscription you already pay for works best; a free local model works too.\n", style="dim")
            body.append("Change it any time with `vision providers`.", style="dim")
        else:
            body.append("Your providers.\n")
            body.append("Tick to add one, untick to switch it off. Everything ticked shows in /model.", style="dim")
        self.io.say(Panel(body, box=ROUNDED, border_style=ACCENT, padding=(0, 2), expand=False))
        self.io.say("")

    def run(self, intro: bool = True) -> list[str]:
        """The whole flow; returns the providers that are ready at the end. `intro` False leaves the
        card out (the first run has drawn its own)."""
        io = self.io
        if intro:
            self.intro()
        with io.working("looking at what's here…"):
            self._scan()
        rows, ticked = self._choices()
        picked = io.pick_many("What do you use? Tick all that apply.", rows, ticked)
        if picked is None:  # Esc: nothing changes
            return self.finish(changed=False)
        if not picked:
            if any(n in ticked for n in self._on()):
                io.say("  [yellow]![/yellow] Nothing ticked, so nothing changed: Vision needs at least one provider.")
            return self.finish(changed=False)
        # Unticked: off. Shown only for what started ticked; the rest (never set up here, or a [local]
        # with no server) just stop offering a "set this up" row in /model.
        listed = {v for v, _, _ in rows if v in providers.REGISTRY} | ({"local"} if self.first_run and not self._ready("local") else set())
        off = [p.name for p in providers.enabled(self.cfg) if p.name in listed and p.name not in picked]
        if off:
            self._switch_off(off, [n for n in off if n in ticked])
        for choice in picked:
            try:
                if choice.startswith(SERVER + ":"):
                    self.add_found_server(choice.split(":", 1)[1])
                elif choice == SERVER:
                    self.add_server()
                elif choice == HOSTED:
                    self.add_hosted()
                elif choice == AGENT:
                    self.add_agent()
                elif choice in providers.REGISTRY and providers.REGISTRY[choice].cli and providers.REGISTRY[choice].builtin:
                    self.setup_cli(choice)
                elif choice not in self._on():  # an earlier one ticked back on
                    self._switch_on(choice)
            except Back:
                io.say("  [dim]Skipped.[/dim]")
        return self.finish(changed=bool(self.switched_off or self.added))

    def _switch_off(self, names: list[str], shown: list[str]) -> None:
        from vision.config import save_enabled_providers

        save_enabled_providers([p.name for p in providers.enabled(self.cfg) if p.name not in names])
        self._reload()
        self.switched_off = shown
        if shown:
            its, it = ("Its", "it") if len(shown) == 1 else ("Their", "them")
            self.io.say(f"  [dim]Switched off: {', '.join(providers.REGISTRY[n].label for n in shown)}. "
                        f"{its} settings are kept; tick {it} again to bring {it} back.[/dim]")

    def _on(self) -> set[str]:
        return {p.name for p in providers.enabled(self.cfg)}

    def _enable(self, name: str) -> None:
        from vision.config import save_enabled_providers

        if name not in self._on():
            save_enabled_providers([p.name for p in providers.enabled(self.cfg)] + [name])
            self._reload()

    def _switch_on(self, name: str) -> None:
        self._enable(name)
        self._heading(providers.REGISTRY[name].label)
        self._ok("Switched back on")
        self.added.append(name)

    def setup_cli(self, name: str) -> bool:
        io, p = self.io, providers.REGISTRY[name]
        if self._ready(name) and name in self._on() and not self._signed_in(name):
            self._logins.pop(name, None)  # on already: a check that failed for a moment gets a second go
        if self._ready(name) and name in self._on() and self._signed_in(name):
            return True  # already set up and on: nothing to do
        self._heading(p.label)
        if self._ready(name):
            self._ok(f"{p.product} is installed" + (" and signed in" if self._signed_in(name) else ""))
        elif not self._install(p):
            return False
        login, tried = " ".join(p.login_cmd), False
        while p.login_cmd and not self._signed_in(name):
            if not io.confirm(f"{'Try signing in again' if tried else 'Sign in now'}? [dim](runs `{login}`)[/dim]", default=True):
                break
            io.run([_exe(p), *p.login_cmd[1:]])
            self._logins.pop(name, None)  # ask it again: only the CLI knows whether that worked
            tried = True
            if not self._signed_in(name):
                self._warn(f"Still not signed in. [dim]{self._logins[name][1]}[/dim]")
        self._enable(name)
        self.added.append(name)
        if p.login_cmd and not self._signed_in(name):
            self._warn(f"{p.label} can't answer until it's signed in. Run `{login}` when you're ready.")
            return True
        self._ok(f"{p.label} is ready")
        return True

    def _install(self, p) -> bool:
        io = self.io
        if p.install_cmd:
            io.say(f"  {p.product} isn't installed yet. Its installer:\n  [dim]$[/dim] [bold]{p.install_cmd}[/bold]")
            if io.confirm("Run it now?", default=True):
                if io.run(p.install_cmd, shell=True) != 0:
                    self._warn("The installer didn't finish cleanly.")
            else:
                io.ask("Run it yourself, then press Enter")
        else:
            where = p.install_url or p.install_hint
            io.say(f"  {p.product} isn't installed yet. It installs from [link={where}]{where}[/link]")
            if hasattr(io, "open_url") and io.open_url(where):
                io.say("  [dim]Opened it in your browser.[/dim]")
            io.ask("Press Enter once it's installed")
        if self._ready(p.name):
            self._ok(f"{p.product} is installed")
            return True
        self._warn(f"Can't see `{p.cli}` yet. If it did install, open a new terminal so it's on PATH, then `vision providers`.")
        return False

    def _name(self, default: str) -> str | None:
        for _ in range(3):
            name = self.io.ask("A short name for it (models show as name/model)", default=default).lower()
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) and not (name in providers.REGISTRY and providers.REGISTRY[name].source != "config"):
                return name
            self._warn("A plain lowercase word that isn't a built-in provider, please.")
        return None

    def _remember_models(self, name: str, ids: list[str]) -> None:
        from vision import models

        self._reload()
        models.set_local_models(ids, name)

    def add_found_server(self, key: str) -> bool:
        """A server the scan found running: saved as it is, no questions."""
        s = next(s for s in LOCAL_SERVERS if s.name == key)
        self._heading(s.label)
        save_provider_table(s.name, {"base_url": s.url, "label": s.label})
        ids = self.servers.get(s.name) or []
        self._remember_models(s.name, ids)
        self._ok(f"{s.label} added · {_models_line(ids)}" if ids else f"{s.label} added; it has no models loaded yet")
        self.added.append(s.name)
        return True

    def add_server(self) -> bool:
        io = self.io
        rows = [(s.name, s.label, f"{s.url.removesuffix('/v1')} · {'running' if self.servers.get(s.name) is not None else 'not running'}")
                for s in LOCAL_SERVERS]
        rows.append((CUSTOM, "Somewhere else", "any OpenAI-compatible URL"))
        key = io.pick("Which model server?", rows)
        if key is None:
            return False
        self._heading(next(label for v, label, _ in rows if v == key))
        if key == CUSTOM:
            url = io.ask("Its URL (ends in /v1)", default="http://localhost:8080/v1").rstrip("/")
            if not url.startswith(("http://", "https://")):
                self._warn("That needs to start with http:// or https://")
                return False
            host = urllib.parse.urlsplit(url).hostname or "server"
            name = self._name(re.sub(r"[^a-z0-9_-]", "", host.split(".")[0].lower()) or "server")
            if not name:
                return False
            s = Preset(name, name.capitalize(), url, "start it")
        else:
            s = next(s for s in LOCAL_SERVERS if s.name == key)
        ids = list_models(s.url)
        if ids is None and key != CUSTOM:
            io.say(f"  Nothing's answering on {s.url.removesuffix('/v1')} yet. Start it with:\n  [dim]$[/dim] [bold]{s.hint}[/bold]")
            io.ask("Then press Enter")
            ids = list_models(s.url)
        save_provider_table(s.name, {"base_url": s.url, "label": s.label})
        if ids is None:
            self._warn(f"Still nothing there. Saved anyway; its models show in /model once {s.label} is running.")
            self._reload()
        else:
            self._remember_models(s.name, ids)
            self._ok(f"{s.label} added · {_models_line(ids)}" if ids else f"{s.label} added; it has no models loaded yet")
        self.added.append(s.name)
        return True

    def add_hosted(self) -> bool:
        io = self.io
        rows = [(p.name, p.label, p.hint + (" · key found" if os.environ.get(p.key_env) else "")) for p in HOSTED_APIS]
        rows.append((CUSTOM, "Another API", "any OpenAI-compatible endpoint"))
        key = io.pick("Which API?", rows)
        if key is None:
            return False
        self._heading(next(label for v, label, _ in rows if v == key))
        if key == CUSTOM:
            url = io.ask("Its URL (ends in /v1)").rstrip("/")
            if not url.startswith(("http://", "https://")):
                self._warn("That needs to start with http:// or https://")
                return False
            parts = [h for h in (urllib.parse.urlsplit(url).hostname or "").split(".") if h not in ("api", "www")]
            name = self._name(re.sub(r"[^a-z0-9_-]", "", (parts[0] if parts else "api").lower()) or "api")
            if not name:
                return False
            p = Preset(name, name.capitalize(), url, "", f"{name.upper().replace('-', '_')}_API_KEY")
        else:
            p = next(p for p in HOSTED_APIS if p.name == key)
        table = {"base_url": p.url, "label": p.label, "api_key_env": p.key_env}
        secret = os.environ.get(p.key_env, "")
        if secret:
            self._ok(f"Using the key in ${p.key_env}")
        else:
            secret = io.ask(f"Paste your {p.label} API key [dim](hidden; Enter to skip)[/dim]", secret=True)
            if secret:
                table = {"base_url": p.url, "label": p.label, "api_key": secret}
            else:
                io.say(f"  [dim]No key yet: put it in ${p.key_env} (your shell profile) and Vision picks it up.[/dim]")
        ids = list_models(p.url, secret)
        if ids and len(ids) > MANY_MODELS:
            picked = io.ask(f"It offers {len(ids)} models. Which should /model show? [dim](ids, comma-separated; Enter for all)[/dim]")
            table["models"] = [m.strip() for m in picked.split(",") if m.strip()]
        save_provider_table(p.name, table)
        if "api_key" in table:
            os.chmod(config.CONFIG_PATH, 0o600)  # the key is in it now
        if ids is None:
            self._warn(f"{p.label} didn't answer. Saved anyway; check the URL with `vision provider list`.")
            self._reload()
        else:
            shown = table.get("models") or ids
            self._remember_models(p.name, shown)
            self._ok(f"{p.label} added" + (f" · {_models_line(shown)}" if shown else ""))
        self.added.append(p.name)
        return True

    def add_agent(self) -> bool:
        io = self.io
        self._heading("An ACP agent")
        command = io.ask("The command that speaks ACP over stdio", default="gemini --experimental-acp").split()
        if not command:
            return False
        base = re.sub(r"[^a-z0-9_-]", "", os.path.basename(command[0]).lower())
        name = base if base and base not in providers.PROVIDER_NAMES else self._name(f"{base}-acp" if base else "agent")
        if not name:
            return False
        save_provider_table(name, {"type": "acp", "command": command, "label": name.capitalize()})
        self._reload()
        if shutil.which(command[0]):
            self._ok(f"{name.capitalize()} added · {name}/default in /model")
        else:
            self._warn(f"Saved, but `{command[0]}` isn't on PATH here; it shows as not set up until it is.")
        self.added.append(name)
        return True

    def finish(self, changed: bool = True) -> list[str]:
        """Pick the model to start on (when there's something new, or the old default went away) and
        say what's ready. Returns the providers that are ready."""
        io = self.io
        self._reload()
        ready = [p.name for p in providers.enabled(self.cfg) if self._works(p.name)]
        if not ready:
            io.say("[yellow]Nothing's ready yet.[/yellow] `vision providers` brings this back; `vision doctor` shows what's installed.")
            return ready
        from vision import models
        from vision.models import coerce_effort, find, model_label, provider_default

        clis.refresh_models_soon(ready, max_age=0)  # their lists, for /model
        for n in ready:  # a server's list is needed now, to offer its models below
            if providers.endpoint_for(n, self.cfg) is not None and not provider_default(n):
                try:
                    models.set_local_models(clis.local_model_ids(timeout=4, provider=n), n)
                except Exception:  # noqa: BLE001
                    pass
        labels = {providers.REGISTRY[n].label for n in ready}
        tabs = [t for t in providers.model_tabs(self.cfg, setup_rows=False) if t[0] in labels]
        rows = [(v, f"{label} · {tab}", desc) for tab, entries, _ in tabs for v, label, desc in entries]
        values = {v for v, _, _ in rows}
        model = self.cfg.brain.model
        still_good = providers_of(model) in ready and (model in values or not rows)
        if not still_good:
            newest = next((n for n in reversed(self.added) if n in ready), ready[0])
            model = provider_default(newest) or (rows[0][0] if rows else model)
            if rows and model not in values:
                model = rows[0][0]
        if (self.first_run or self.added or not still_good) and len(rows) > 1:
            options = tabs if len(tabs) > 1 else rows  # ←/→ between providers, like /model
            chosen = io.pick("Which model should Vision start on?", options, current=model)
        else:
            chosen = None if still_good else model
        if chosen:
            from vision.cli import _save_defaults

            m = find(chosen)
            effort = m.resting_effort() if m else coerce_effort(chosen, self.cfg.brain.effort)[0]  # a local model starts with thinking off
            _save_defaults(self.cfg, chosen, effort)
            model = chosen
        if not self.first_run:  # the first chat opens full screen over it, with set_up_note() instead
            io.say(self._card(ready, model_label(model) or model) if changed or chosen else "[dim]Nothing changed.[/dim]")
        return ready

    def _card(self, ready: list[str], model: str):
        from rich.box import ROUNDED
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        names = Text()
        for i, n in enumerate(ready):
            names.append("   " if i else "")
            names.append("✓ ", style="green")
            names.append(providers.REGISTRY[n].label)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(style="dim")
        for cmd, what in (("/model", "switch model in a chat"), ("vision providers", "add a provider or switch one off")):
            grid.add_row(cmd, what)
        head = Text.assemble(("All set. ", "bold"), "Default model: ", (model, f"bold {ACCENT}"))
        foot = Text(f"Saved in {str(config.CONFIG_PATH).replace(str(Path.home()), '~', 1)}", style="dim")
        body = Table.grid()
        for part in (head, names, Text(""), grid, Text(""), foot):
            body.add_row(part)
        return Panel(body, box=ROUNDED, border_style=ACCENT, padding=(0, 2), expand=False)


def voice_offered() -> bool:
    """Whether voice_step has something to offer: a machine that can run the voice in real time, without it."""
    from vision import install

    have, hw = install.voice_backend(), install.voice_hardware()
    return hw is not None and not (have and have != "cpu")


def voice_step(io, *, asked: bool = False, heading: bool = True) -> bool:
    """Offer the voice where this machine can run it in real time (Apple Silicon, or an NVIDIA card), and
    install it on a yes. `asked`: the user ran `vision voice setup`, so say where things stand even when
    there is nothing to offer. `heading` False: the caller has drawn one (the first run's step header).
    True when the voice was installed."""
    import shutil as sh

    from vision import install

    have, hw = install.voice_backend(), install.voice_hardware()
    if have and (have != "cpu" or hw is None):
        if asked:
            io.say("  [green]✓[/green] Voice is already set up. /talk in a chat starts a voice conversation; /speak reads replies aloud. "
                   "[dim]`vision voice check` tests the speakers and the microphone.[/dim]")
        return False
    if hw is None:
        if asked:
            io.say("  [yellow]![/yellow] Voice needs an Apple Silicon Mac or an NVIDIA graphics card to keep up with speech, "
                   "and this machine has neither, so Vision is text only here.")
        return False
    backend, what = hw
    if heading:
        io.say(f"\n[bold {ACCENT}]◆[/] [bold]Voice[/bold] [dim](optional)[/dim]")
    io.say(f"  Talk to Vision out loud and hear it answer.\n  [dim]Speech runs on this machine: {what}.[/dim]")
    gb = re.search(r"· (\d+) GB", what)
    if gb and int(gb.group(1)) < install.VOICE_MIN_VRAM_GB:
        io.say(f"  [yellow]![/yellow] That card has {gb.group(1)} GB of memory and the voice wants {install.VOICE_MIN_VRAM_GB}, so it may stutter.")
    free = sh.disk_usage(Path.home()).free / 1e9
    if free < 20:
        io.say(f"  [yellow]![/yellow] Only {free:.0f} GB of disk is free; the voice needs most of that.")
    rows = [("no", "Not now", "text only; `vision voice setup` adds it later"),
            ("yes", "Set up voice", f"downloads {install.VOICE_DOWNLOAD[backend]}")]
    if io.pick("Want voice?", rows) != "yes":
        io.say("  [dim]Text only for now. `vision voice setup` adds the voice whenever you want it.[/dim]")
        return False
    cmd = install.voice_install_cmd(backend)
    if cmd is None:  # a bare pip install: no checkout to sync
        io.say(f"  Install it with:  [bold]{install.setup_hint(backend)}[/bold]  then `vision setup` for the speech models.")
        return False
    io.say("  [dim]Downloading. This takes a while; Ctrl-C stops it and `vision voice setup` picks it back up.[/dim]")
    if io.run(cmd) != 0:
        io.say("  [yellow]![/yellow] The voice didn't finish installing. `vision voice setup` tries again.")
        return False
    io.say("  [green]✓[/green] Voice is installed. Now a quick check that you can hear it and it can hear you.")
    # In a process of its own: this one started before the voice's packages were there.
    io.run([sys.executable, "-P", "-m", "vision", "voice", "check"])
    return True


def providers_of(model: str) -> str:
    from vision.models import provider_for

    return provider_for(model)
