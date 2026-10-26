"""The providers Vision can think with, in one place: what each is called, whether it is set up on
this machine, how to set it up, which brain class drives it, what it can do, and where its own code
lives (its sessions, usage, model list, persona lines).

The rest of Vision asks the registry instead of testing provider names: a capability flag ("does it
have a CLI", "can it hold a voice conversation") or a hook, a "module:function" that is imported
only when called. A new provider is one more entry here plus the functions its hooks name.

Providers come in two kinds: the built-in four below, and endpoints the user adds in config.toml as
`[providers.<name>]` tables (type = "openai": any OpenAI-compatible chat server, Ollama, LM Studio,
vLLM, OpenRouter…), which run on the same driver as [local] (vision/local.py) with their models shown
as `<name>/<model>` so two servers' names can't clash. `register_endpoints` builds those entries when
the config loads.

`[providers] enabled` in config.toml says which ones /model offers (/providers picks them). One that
is enabled but not ready (its CLI is missing, its server does not answer) still shows in /model, as a
single row that says how to set it up, so a new install never fails on its first message.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

# Imports nothing else from Vision at load: config and models build on it.


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible chat server Vision talks to directly (no CLI)."""

    base_url: str = "http://localhost:8080/v1"
    api_key: str = ""  # sent as a bearer token; api_key_env names an environment variable holding it instead
    api_key_env: str = ""
    timeout_s: float = 180
    context: int = 32768  # tokens; the conversation is trimmed to stay inside it
    models: tuple[str, ...] = ()  # fixed list; empty = ask the server's /models

    def key(self) -> str:
        import os

        return self.api_key or (os.environ.get(self.api_key_env, "") if self.api_key_env else "")

    def headers(self) -> dict[str, str]:
        k = self.key()
        return {"Authorization": f"Bearer {k}"} if k else {}


@dataclass(frozen=True)
class AcpAgent:
    """A command that speaks ACP over stdio (vision/acp.py drives it)."""

    command: tuple[str, ...]  # ("gemini", "--experimental-acp")
    models: tuple[str, ...] = ()  # ids the agent takes; () = its own default only ("<name>/default")
    model_flag: str = ""  # the flag that picks one ("--model"); "" = the agent's default always
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Provider:
    name: str  # "claude", as in [providers] enabled and on the wire
    label: str  # "Claude", the /model tab and every short mention
    product: str  # "Claude Code", the thing that runs it (versions, usage headings)
    note: str  # the /model tab's second line
    setup: str  # how to get it ready, shown when it is not
    brain: str  # "module:Class" of the driver
    powered_by: str  # "You are powered by …" in the persona
    # -- its command-line tool (None: Vision talks to it directly, no CLI)
    cli: str | None = None  # the executable; blocked inside every turn so no run spends it behind the user's back
    home_env: str = ""  # env var for the CLI's config dir: pointed at an empty one for every other provider's turns
    turn_env: tuple[tuple[str, str], ...] = ()  # set for its own turns
    install_hint: str = ""  # `vision doctor`'s "Install:" line
    install_cmd: str = ""  # a shell line the guided setup can run to install it ("" = only a page to visit)
    install_url: str = ""  # where to get it when there is no command
    login_cmd: tuple[str, ...] = ()  # what to run, interactively, to log in
    update_args: tuple[str, ...] = ("update",)
    models_args: tuple[str, ...] = ()  # CLI args that make it rewrite its models cache (refresh_cli_models)
    npm_package: str = ""  # where its latest version is published, when no hook says otherwise
    # -- what it can do
    has_usage: bool = False  # a subscription with windows /usage can read
    banked_resets: bool = False  # /usage can spend a saved reset
    usage_from_text: bool = False  # its brain's usage_report is CLI text Vision lays out itself (else usage_renderable)
    conversation: str = ""  # "module:Class" of its voice conversation model; "" = it can't hold one
    conversation_thinks: bool = True  # False: its voice replies never think (effort always off)
    vision_runs_tools: bool = False  # Vision executes its tool calls (and tells it so), rather than its CLI
    keeps_partial_turns: bool = True  # False: its context is saved only after a complete turn
    session_in_directory: bool = False  # its conversations live in the directory they started in: resuming one elsewhere fails
    plan_tool: bool = False  # plan mode ends with an approval tool (ExitPlanMode), not a plan in prose
    fast_model: str = ""  # its fast mode runs only on this model
    default_model: str = ""  # what it means with no model named, when its list has it (else the list's first)
    default_effort_note: str = ""  # shown by its default effort level in /effort
    title_scanner: str = ""  # LiveTitle method that reads a running session's title
    endpoint: Endpoint | None = None  # a config-defined server (the built-in "local" reads [local] instead)
    acp: AcpAgent | None = None  # a config-defined ACP agent
    source: str = "builtin"  # "builtin", "config" ([providers.<name>]) or "plugin" (a vision.providers entry point)
    icon: str = "cpu"  # SF Symbol the phone draws it with
    tint: str = "#8E8E93"  # its colour there
    hooks: dict[str, str] = field(default_factory=dict)

    @property
    def builtin(self) -> bool:
        return self.source == "builtin"

    def describe(self, cfg=None) -> dict:
        """What a client needs to draw and offer it (the phone's /providers)."""
        return {"name": self.name, "label": self.label, "product": self.product, "note": self.note, "icon": self.icon,
                "tint": self.tint, "source": self.source, "setup": self.setup, "has_usage": self.has_usage,
                "banked_resets": self.banked_resets, "conversation": bool(self.conversation),
                "enabled": cfg is None or self in enabled(cfg), "ready": ready(self.name, cfg) if cfg is not None else None}

    def brain_class(self):
        return _load(self.brain)

    def conversation_class(self):
        """Its voice conversation model's class (only when `conversation` is set)."""
        return _load(self.conversation)

    def hook(self, name: str):
        """The function a hook names, or None when this provider has no such hook."""
        target = self.hooks.get(name)
        return _load(target) if target else None


def _load(target: str):
    module, attr = target.split(":")
    return getattr(__import__(module, fromlist=[attr]), attr)


REGISTRY: dict[str, Provider] = {p.name: p for p in (
    Provider(
        "claude", "Claude", "Claude Code", "via Claude Code · Claude Subscription",
        "install Claude Code (curl -fsSL https://claude.ai/install.sh | bash), then run `claude` to log in",
        "vision.brain:Brain", "Claude",
        cli="claude", home_env="CLAUDE_CONFIG_DIR", npm_package="@anthropic-ai/claude-code",
        install_hint="curl -fsSL https://claude.ai/install.sh | bash, then run `claude` to log in",
        install_cmd="curl -fsSL https://claude.ai/install.sh | bash", login_cmd=("claude", "auth", "login"),
        has_usage=True, banked_resets=True, usage_from_text=True, conversation="vision.conversation:ClaudeConversation",
        plan_tool=True, fast_model="opus", default_model="opus", title_scanner="_scan_claude",
        session_in_directory=True,  # ~/.claude/projects/<directory>/<id>.jsonl; --resume looks only there (2.1.289)
        # `-p` leaves out the task tools (TaskCreate, TaskUpdate…) on current models unless opted in
        # (2.1.306); with them the model keeps a checklist, drawn under the live line (vision.checklist).
        # Tool search would defer them behind ToolSearch, where the model never sees when to reach for them.
        turn_env=(("CLAUDE_CODE_ENABLE_TODO_TOOLS", "true"), ("ENABLE_TOOL_SEARCH", "false")),
        icon="sparkles", tint="#D9785A",
        hooks={
            "find_cli": "vision.brain:find_claude", "login_status": "vision.clis:claude_login_status",
            "session_paths": "vision.sessions:claude_session_paths", "parse_session": "vision.sessions:_claude_session",
            "history": "vision.sessions:claude_history", "usage": "vision.usage:_claude_data",
            "use_banked": "vision.usage:use_claude_reset", "latest_version": "vision.clis:claude_latest_version",
            "refresh_models": "vision.clis:refresh_claude_models", "tool_notes": "vision.persona:claude_tool_notes",
        },
    ),
    Provider(
        "codex", "Codex", "Codex", "via Codex CLI · ChatGPT Subscription",
        "install the Codex CLI (npm i -g @openai/codex), then run `codex login`",
        "vision.codex:CodexBrain", "OpenAI Codex",
        cli="codex", home_env="CODEX_HOME", npm_package="@openai/codex",
        install_hint="npm i -g @openai/codex, then run `codex login`", models_args=("debug", "models"),
        install_cmd="npm i -g @openai/codex", login_cmd=("codex", "login"),
        has_usage=True, banked_resets=True, conversation="vision.codex_voice:CodexConversation",
        default_effort_note="Codex default", title_scanner="_scan_codex", icon="terminal", tint="#0F997A",
        hooks={
            "find_cli": "vision.codex:find_codex", "login_status": "vision.clis:codex_login_status",
            "session_paths": "vision.sessions:codex_session_paths", "parse_session": "vision.sessions:_codex_session",
            "history": "vision.sessions:codex_history", "usage": "vision.usage:_codex_data",
            "use_banked": "vision.usage:use_codex_reset", "refresh_models": "vision.clis:refresh_cli_models",
            "models_cache": "vision.models:_codex_models_from_cache", "tool_notes": "vision.persona:codex_tool_notes",
        },
    ),
    Provider(
        "grok", "Grok", "Grok", "via Grok CLI · Grok Subscription",
        "install the Grok CLI (https://x.ai/cli), then run `grok login`",
        "vision.grok:GrokBrain", "Grok",
        cli="grok", home_env="GROK_HOME", install_hint="https://x.ai/cli, then run `grok login`", models_args=("models",),
        install_url="https://x.ai/cli", login_cmd=("grok", "login"),
        turn_env=(("GROK_MEMORY", "0"), ("GROK_DISABLE_AUTOUPDATER", "1")),  # Vision's MEMORY.md is the shared store
        has_usage=True, title_scanner="_scan_grok", icon="bolt.fill", tint="#5C6680",
        hooks={
            "find_cli": "vision.grok:find_grok", "login_status": "vision.clis:grok_login_status",
            "session_paths": "vision.sessions:grok_session_paths", "parse_session": "vision.sessions:_grok_session",
            "history": "vision.sessions:grok_history", "usage": "vision.usage:grok_usage",
            "latest_version": "vision.clis:grok_latest_version", "refresh_models": "vision.clis:refresh_cli_models",
            "models_cache": "vision.models:_grok_models_from_cache", "tool_notes": "vision.persona:grok_tool_notes",
        },
    ),
    Provider(
        "local", "Local", "llama-server", "via your llama-server · free",
        "start llama-server and point [local] base_url in config.toml at it",
        "vision.local:LocalBrain", "an open model on the user's own hardware",
        conversation="vision.local:LocalConversation", conversation_thinks=False,
        vision_runs_tools=True, keeps_partial_turns=False, icon="desktopcomputer", tint="#615CED",
        hooks={
            "session_paths": "vision.sessions:local_session_paths", "parse_session": "vision.sessions:_local_session",
            "history": "vision.sessions:local_history", "refresh_models": "vision.clis:refresh_local_models",
            "tool_notes": "vision.persona:local_tool_notes",
        },
    ),
)}
PROVIDER_NAMES = tuple(REGISTRY)  # the built-in four; `tuple(REGISTRY)` also has the config-defined ones
_ENDPOINT_HOOKS = dict(REGISTRY["local"].hooks)


def register_endpoints(specs: dict[str, dict]) -> list[str]:
    """Replace the config-defined providers with these `[providers.<name>]` tables. A bad one is
    skipped with its name and reason in the returned notes (an unknown type, a built-in name, no URL)."""
    notes: list[str] = []
    before = {n: p for n, p in REGISTRY.items() if p.source == "config"}
    for name in before:
        del REGISTRY[name]
    for raw_name, spec in specs.items():
        name = str(raw_name).strip().lower()
        if not isinstance(spec, dict):
            continue
        if name in REGISTRY or not name.replace("-", "").replace("_", "").isalnum():
            notes.append(f"[providers.{raw_name}]: that name is taken or not a plain word; skipped")
            continue
        kind = str(spec.get("type") or ("acp" if spec.get("command") else "openai")).strip().lower()
        label = str(spec.get("label") or name.capitalize())
        if kind == "acp":
            command = spec.get("command") or ()
            command = tuple(str(c) for c in command) if isinstance(command, (list, tuple)) else tuple(str(command).split())
            if not command or not command[0]:
                notes.append(f"[providers.{raw_name}]: an acp provider needs command; skipped")
                continue
            models = spec.get("models") or ()
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            agent = AcpAgent(command, tuple(str(m) for m in models if str(m).strip()) if isinstance(models, (list, tuple)) else (),
                             str(spec.get("model_flag") or ""), tuple((str(k), str(v)) for k, v in env.items()))
            REGISTRY[name] = Provider(
                name, label, label, f"via `{' '.join(command)}` · an ACP agent",
                f"install {command[0]} so that `{command[0]}` is on PATH, and log in to it if it needs that",
                "vision.acp:AcpBrain", f"{label}, an agent Vision drives over ACP",
                cli=command[0], acp=agent, source="config", icon="cpu", tint=_TINTS[len(REGISTRY) % len(_TINTS)],
                hooks={"session_paths": "vision.sessions:local_session_paths", "parse_session": "vision.sessions:_local_session",
                       "history": "vision.sessions:local_history", "refresh_models": "vision.clis:refresh_acp_models",
                       "tool_notes": "vision.persona:acp_tool_notes"})
            continue
        if kind != "openai":
            notes.append(f"[providers.{raw_name}]: type {kind!r} is not supported (\"openai\" or \"acp\"); skipped")
            continue
        url = str(spec.get("base_url") or "").strip().rstrip("/")
        if not url:
            notes.append(f"[providers.{raw_name}]: needs base_url; skipped")
            continue
        models = spec.get("models") or ()
        ep = Endpoint(url, str(spec.get("api_key") or ""), str(spec.get("api_key_env") or ""),
                      float(spec.get("timeout_s") or 180), int(spec.get("context") or 32768),
                      tuple(str(m) for m in models if str(m).strip()) if isinstance(models, (list, tuple)) else ())
        where = url.split("://", 1)[-1].removesuffix("/v1")
        REGISTRY[name] = Provider(
            name, label, label, f"via {where} · " + ("an API, by key" if ep.api_key or ep.api_key_env else "your own server"), f"start the server at {url}, or fix base_url in [providers.{name}]",
            "vision.local:LocalBrain", f"an open model served by {label}",
            conversation="vision.local:LocalConversation", conversation_thinks=False,
            vision_runs_tools=True, keeps_partial_turns=False, endpoint=ep, source="config",
            icon="server.rack", tint=_TINTS[len(REGISTRY) % len(_TINTS)], hooks=_ENDPOINT_HOOKS)
    if {n: p for n, p in REGISTRY.items() if p.source == "config"} != before:
        # The servers to probe changed. An unchanged table (every load_config, so every new server
        # chat and every /models) keeps the readiness cache: clearing it there made each of those
        # probe every configured server again, a couple of seconds each when one was down.
        forget_ready()
    return notes


_TINTS = ("#C2410C", "#0E7490", "#7C3AED", "#B45309", "#15803D", "#BE185D")  # config-defined servers, in turn
PLUGIN_GROUP = "vision.providers"
_plugins_loaded = False


def load_plugins() -> list[str]:
    """Providers from installed packages: each `vision.providers` entry point is a Provider, or a callable
    returning one or a list of them. Loaded once per process; a broken plugin is skipped with a note."""
    global _plugins_loaded
    if _plugins_loaded:
        return []
    _plugins_loaded = True
    notes: list[str] = []
    try:
        from importlib.metadata import entry_points

        eps = list(entry_points(group=PLUGIN_GROUP))
    except Exception as e:  # noqa: BLE001
        return [f"provider plugins could not be listed: {e}"]
    for ep in eps:
        try:
            obj = ep.load()
            found = obj() if callable(obj) and not isinstance(obj, Provider) else obj
            for p in (found if isinstance(found, (list, tuple)) else [found]):
                if not isinstance(p, Provider):
                    raise TypeError(f"{type(p).__name__} is not a Provider")
                if p.name in REGISTRY:
                    raise ValueError(f"the name {p.name!r} is taken")
                REGISTRY[p.name] = p if p.source == "plugin" else Provider(**{**p.__dict__, "source": "plugin"})
        except Exception as e:  # noqa: BLE001
            notes.append(f"provider plugin {ep.name} ({ep.value}) was skipped: {e}")
    forget_ready()
    return notes


def endpoint_for(provider: str, cfg) -> Endpoint | None:
    """The server a provider's turns go to: [local] for the built-in one, its own table for a config-defined one."""
    p = REGISTRY.get(provider)
    if p is None or p.cli:
        return None
    if p.endpoint is not None:
        return p.endpoint
    if p.name != "local":
        return None  # a plugin provider with no CLI and no endpoint drives itself
    local = getattr(cfg, "local", None)
    if local is None:
        return Endpoint()
    return Endpoint(str(local.base_url), timeout_s=float(local.timeout_s), context=int(local.context))


def endpoint_names() -> tuple[str, ...]:
    """Every provider that is a chat server (the built-in local one and the config-defined ones)."""
    return tuple(p.name for p in REGISTRY.values() if not p.cli)


def names(**caps) -> tuple[str, ...]:
    """Provider names in registry order, those whose fields match every given value:
    names(has_usage=True) → the ones with a /usage page; names(cli=None) is not supported (use with_cli())."""
    return tuple(p.name for p in REGISTRY.values() if all(getattr(p, k) == v for k, v in caps.items()))


def with_cli() -> tuple[str, ...]:
    """Providers driven through a command-line tool, in registry order."""
    return tuple(p.name for p in REGISTRY.values() if p.cli)


def conversation_names() -> tuple[str, ...]:
    """Providers whose models can hold a voice conversation."""
    return tuple(p.name for p in REGISTRY.values() if p.conversation)


def cap(provider: str, attr: str, default=None):
    """One capability of a provider by name ("" or unknown → default)."""
    p = REGISTRY.get(provider)
    return getattr(p, attr) if p is not None else default


SETUP_PREFIX = "setup:"  # a /model row value that means "tell me how to set this up", not a model
_READY_TTL = 30.0
_ready_cache: dict[tuple, tuple[float, bool]] = {}
_ready_lock = threading.Lock()


def get(name: str) -> Provider | None:
    return REGISTRY.get(name)


def enabled(cfg) -> list[Provider]:
    """The providers /model offers, in the order the config lists them."""
    names = getattr(getattr(cfg, "providers", None), "enabled", None)
    names = tuple(REGISTRY) if names is None else names
    return [REGISTRY[n] for n in names if n in REGISTRY]


def _server_answers(base_url: str) -> bool:
    """A TCP connect to the server's host and port, briefly: enough to tell "nothing there" from "up"."""
    parts = urllib.parse.urlsplit(base_url or "")
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=0.4):
            return True
    except OSError:
        return False


def _is_model_server(ep) -> bool:
    """Whether what answers at a server's address is a model server: its /models gives a list (an empty
    one counts: it is up, with nothing loaded yet) or wants a key. Anything else on the port does not,
    so a web app on localhost:8080 is not taken for the local model. A table that names its models is
    trusted as it stands (not every server lists them)."""
    import json
    import urllib.error
    import urllib.request

    if ep.models:
        return True
    req = urllib.request.Request(ep.base_url.rstrip("/") + "/models", headers={"User-Agent": "vision", **ep.headers()})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310  (the user's own server)
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:  # noqa: BLE001 - not HTTP, not JSON, too slow: not a model server
        return False
    return isinstance(data, dict) and isinstance(data.get("data"), list)


def ready(name: str, cfg=None) -> bool:
    """Whether this provider can take a turn here: its CLI is installed, or (local) a model server answers.
    Cached for half a minute so /model opens instantly; a login problem still shows on the first turn."""
    p = REGISTRY.get(name)
    ep = endpoint_for(name, cfg) if p is not None and not p.cli else None
    key = (name, ep.base_url if ep else "")
    now = time.monotonic()
    with _ready_lock:
        hit = _ready_cache.get(key)
        if hit and now - hit[0] < _READY_TTL:
            return hit[1]
    if ep is not None:
        ok = _server_answers(ep.base_url) and _is_model_server(ep)
    elif p is not None and p.acp is not None:
        import shutil

        ok = bool(shutil.which(p.acp.command[0]))
    elif name in REGISTRY:
        from vision import clis

        try:
            clis.find_cli(name)
            ok = True
        except Exception:  # noqa: BLE001  not installed
            ok = False
    else:
        ok = False
    with _ready_lock:
        _ready_cache[key] = (now, ok)
    return ok


def forget_ready() -> None:
    """Drop the cached answers (after an install, a config change)."""
    with _ready_lock:
        _ready_cache.clear()


def model_tabs(cfg, *, setup_rows: bool = True) -> list[tuple[str, list, str]]:
    """/model's tabs: every enabled provider, its models when it is ready, else (setup_rows) one row that
    says how to set it up. Without setup_rows (the phone, which cannot install anything) a provider
    that is not ready is left out."""
    from vision.models import MODEL_TABS

    rows_by_label = {tab: (rows, note) for tab, rows, note in MODEL_TABS}
    tabs = []
    for p in enabled(cfg):
        rows, note = rows_by_label.get(p.label, ([], p.note))
        if ready(p.name, cfg) and rows:
            tabs.append((p.label, rows, note))
        elif ready(p.name, cfg) and setup_rows:
            tabs.append((p.label, [(SETUP_PREFIX + p.name, "No models yet", "the server answered but listed no models; try /model again in a moment")], note))
        elif setup_rows:
            tabs.append((p.label, [(SETUP_PREFIX + p.name, "Not set up", p.setup)], note))
    return tabs


def setup_note(value: str, cfg=None) -> str | None:
    """For a /model pick that is a setup row, what to tell the user; None for a real model."""
    if not value.startswith(SETUP_PREFIX):
        return None
    p = REGISTRY.get(value[len(SETUP_PREFIX):])
    if p is None:
        return "That provider isn't set up here yet."
    if cfg is not None and ready(p.name, cfg):
        return f"{p.label} answered but listed no models yet; try again in a moment."
    return f"{p.label} isn't set up here yet: {p.setup}."


def first_ready_model(cfg) -> str | None:
    """The default model of the first enabled provider that is ready, for a start whose saved model can't run."""
    from vision.models import provider_default

    for p in enabled(cfg):
        if ready(p.name, cfg):
            model = provider_default(p.name)
            if model:
                return model
    return None


def unavailable_reason(provider: str, cfg) -> str | None:
    """Why a turn on this provider can't run here (switched off, not set up), or None when it can."""
    p = REGISTRY.get(provider)
    if p is None:
        return f"Vision has no provider called {provider!r}."
    if p not in enabled(cfg):
        return f"{p.label} is switched off in /providers."
    if not ready(provider, cfg):
        return f"{p.label} isn't set up here yet: {p.setup}."
    return None


def settle(cfg) -> str:
    """At the start of a chat: when the saved model's provider can't run here (switched off, not set up),
    move to the first ready provider's default for this run. Returns a note for the user ("" when the
    saved model stands); config.toml is left as it is."""
    from vision.models import coerce_effort, model_label, provider_for

    why = unavailable_reason(provider_for(cfg.brain.model), cfg)
    if not why:
        return ""
    other = first_ready_model(cfg)
    if not other:
        hints = "; ".join(f"{p.label}: {p.setup}" for p in enabled(cfg)) or "turn one on with /providers"
        return (f"No provider is set up yet, so Vision can't answer. {hints}. "
                "Or add a model server of your own (Ollama, LM Studio, an API) with `vision provider add`. "
                "`vision providers` walks you through it.")
    cfg.brain.model = other
    cfg.brain.effort, _ = coerce_effort(other, cfg.brain.effort)
    return f"{why} Using {model_label(other)} for now (config.toml unchanged)."
