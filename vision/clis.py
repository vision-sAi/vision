"""The provider CLIs Vision drives (Claude Code, Codex, Grok): where each one is, its version, and
how to update it. `vision --version`, `/version`, `vision doctor` and `vision update` all go through here."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

from vision import models
from vision.models import provider_label

from vision import providers as _registry

PROVIDERS = _registry.with_cli()  # the built-in providers Vision drives through a CLI (/update, /version)

# Every CLI ships its own updater (Provider.update_args); `--version` output differs per tool, hence the parsing below.
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?")


@dataclass
class CliInfo:
    provider: str
    path: str | None = None
    version: str | None = None  # "2.1.278"
    raw: str = ""  # the whole `--version` line, e.g. "grok 1.0.34 (3736acbc8658) [stable]"
    error: str | None = None  # why there is no version: not installed, or `--version` failed

    @property
    def label(self) -> str:
        return provider_label(self.provider, cli=True)

    @property
    def ok(self) -> bool:
        return self.version is not None

    def line(self) -> str:
        """One plain-text line: 'Claude Code 2.1.278 (/home/me/.local/bin/claude)'."""
        if self.ok:
            return f"{self.label} {self.version} ({self.path})"
        return f"{self.label}: {self.error}"


def find_cli(provider: str) -> str:
    """The executable for a provider; raises that provider's error when it is not installed."""
    p = _registry.get(provider)
    if p is not None and p.acp is not None:
        import shutil

        exe = shutil.which(p.acp.command[0])
        if not exe:
            raise FileNotFoundError(f"{p.acp.command[0]} (the {p.label} agent) is not on PATH")
        return exe
    find = p.hook("find_cli") if p else None
    if find is None:
        raise ValueError(f"unknown provider {provider!r}" if p is None else f"{p.label} has no CLI")
    return find()


def install_hint(provider: str) -> str:
    return _registry.cap(provider, "install_hint", "")


def parse_version(text: str) -> str | None:
    """'codex-cli 0.155.1' → '0.155.1'; '2.1.278 (Claude Code)' → '2.1.278'."""
    m = _VERSION.search(text or "")
    return m.group(0) if m else None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # `claude` refuses to nest inside a Claude Code session
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


def cli_version(provider: str, timeout: float = 20) -> CliInfo:
    """Ask one CLI for its version. Never raises: a missing tool or a hung `--version` is reported in `error`."""
    info = CliInfo(provider)
    try:
        info.path = find_cli(provider)
    except Exception as e:  # noqa: BLE001 - each provider raises its own error class
        info.error = f"not installed ({e})"
        return info
    try:
        r = subprocess.run([info.path, "--version"], capture_output=True, text=True, timeout=timeout, env=_env(), encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as e:
        info.error = f"`{os.path.basename(info.path)} --version` failed: {e}"
        return info
    info.raw = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
    info.version = parse_version(info.raw)
    if info.version is None:
        info.error = f"could not read the version from {info.raw!r}" if info.raw else f"`{os.path.basename(info.path)} --version` printed nothing"
    return info


def cli_versions(providers=PROVIDERS) -> list[CliInfo]:
    return [cli_version(p) for p in providers]


# ---------------------------------------------------------------- is it logged in?
# Each CLI is asked in its own way (Provider.hooks["login_status"]); `vision doctor` and the guided setup
# both read the answer through login_status: (ok, detail), and it never raises.
def _short(text: str, limit: int = 300) -> str:
    return (text or "").strip()[:limit]


def claude_login_status(thorough: bool = False, timeout: float = 120) -> tuple[bool, str]:
    """`claude auth status` says at once whether there is a login. `thorough` (vision doctor) sends one
    small headless request instead, the only check that proves the login can take a turn; a Claude Code
    too old to have `auth status` gets the request as well."""
    from vision.config import WORKSPACE_DIR

    exe = find_cli("claude")
    if not thorough:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, env=_env(), timeout=30, encoding="utf-8", stdin=subprocess.DEVNULL)
        try:
            status = json.loads(r.stdout)
        except ValueError:
            status = None
        if isinstance(status, dict) and "loggedIn" in status:
            return (True, "logged in") if status["loggedIn"] else (False, "not logged in")
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [exe, "-p", "Reply with the single word OK", "--output-format", "json", "--no-session-persistence", "--model", "haiku"],
        capture_output=True, text=True, env=_env(), cwd=str(WORKSPACE_DIR), timeout=timeout, encoding="utf-8", stdin=subprocess.DEVNULL,
    )
    if r.returncode == 0 and '"is_error":false' in r.stdout:
        return True, "headless round-trip succeeded"
    return False, _short(r.stderr or r.stdout)


def codex_login_status(thorough: bool = False, timeout: float = 30) -> tuple[bool, str]:
    r = subprocess.run([find_cli("codex"), "login", "status"], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    return r.returncode == 0, _short(r.stdout or r.stderr) or ("authenticated" if r.returncode == 0 else "")


def grok_login_status(thorough: bool = False, timeout: float = 30) -> tuple[bool, str]:
    r = subprocess.run([find_cli("grok"), "models"], capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    if r.returncode == 0:
        return True, (r.stdout.strip().splitlines()[0] if r.stdout.strip() else "authenticated")
    return False, _short(r.stderr or r.stdout)


def login_status(provider: str, *, thorough: bool = False) -> tuple[bool, str]:
    """Whether a provider's CLI is logged in, with a line of detail (what it said, or why not). A
    provider with no login to check counts as fine. `thorough` asks for the slower, surer check."""
    p = _registry.get(provider)
    check = p.hook("login_status") if p else None
    if check is None:
        return True, ""
    try:
        return check(thorough)
    except subprocess.TimeoutExpired:
        return False, "the check timed out"
    except Exception as e:  # noqa: BLE001 - not installed, or the CLI could not be started
        return False, str(e) or type(e).__name__


def works(provider: str) -> tuple[bool, str]:
    """Whether a provider can really take a turn, beyond providers.ready's quick look: a CLI is logged
    in, a server lists its models (a port that merely answers is not one). (ok, detail); never raises."""
    p = _registry.get(provider)
    if p is None:
        return False, f"no provider called {provider!r}"
    if p.cli or p.acp is not None:
        try:
            find_cli(provider)
        except Exception as e:  # noqa: BLE001
            return False, f"not installed ({e})"
        return login_status(provider)
    try:
        ids = local_model_ids(provider=provider)
    except Exception as e:  # noqa: BLE001 - nothing there, or something that is not a model server
        return False, f"its server did not list any models ({e})"
    return (True, f"{len(ids)} model{'s' if len(ids) != 1 else ''}") if ids else (False, "its server lists no models")


@dataclass
class UpdateResult:
    provider: str
    before: str | None
    after: str | None
    returncode: int
    output: str = ""  # captured stdout+stderr (empty when it streamed to the terminal)
    error: str | None = None  # the CLI is missing, or its updater could not be started

    @property
    def label(self) -> str:
        return provider_label(self.provider, cli=True)

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0

    @property
    def changed(self) -> bool:
        return self.ok and self.before != self.after

    def summary(self) -> str:
        """'Claude Code 2.1.278 → 2.1.280', 'Codex 0.155.1 (already up to date)', or what went wrong."""
        if self.error:
            return f"{self.label}: {self.error}"
        if not self.ok:
            tail = self.output.strip().splitlines()[-1] if self.output.strip() else f"exit status {self.returncode}"
            return f"{self.label} update failed: {tail}"
        if self.changed:
            return f"{self.label} {self.before or '?'} → {self.after or '?'}"
        return f"{self.label} {self.after or self.before or '?'} (already up to date)"


# ---------------------------------------------------------------- is an update available?
# Claude Code's installer resolves "latest" from this bucket (npm is the fallback); Codex is a standalone
# build whose numbers track the npm package; Grok has a check-only mode that honours its release channel.
_CLAUDE_LATEST_URL = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/latest"
_NPM_LATEST = "https://registry.npmjs.org/{package}/latest"
CHECK_TTL = 6 * 3600  # a check per provider is good for this long (the status repaint asks every time)


@dataclass
class UpdateCheck:
    provider: str
    current: str | None = None
    latest: str | None = None
    error: str | None = None
    at: float = 0.0

    @property
    def label(self) -> str:
        return provider_label(self.provider, cli=True)

    @property
    def available(self) -> bool | None:
        """True when a newer version is out, False when current, None when the check could not tell."""
        if self.error or not self.current or not self.latest:
            return None
        return _newer(self.latest, self.current)

    def status(self) -> str:
        """Short notice copy: 'Claude Code 2.1.280 available · /update'; nothing when current or unknown."""
        return f"{self.label} {self.latest} available · /update" if self.available else ""


def _key(v: str) -> tuple:
    head, _, pre = v.partition("-")
    nums = tuple(int(x) for x in head.split(".") if x.isdigit())
    return nums, pre == "", pre  # a pre-release sorts below its release


def _newer(latest: str, current: str) -> bool:
    try:
        return _key(latest) > _key(current)
    except ValueError:
        return latest != current


def _http_text(url: str, timeout: float) -> str:
    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "vision"}), timeout=timeout) as r:
        return r.read().decode()


def latest_version(provider: str, timeout: float = 15) -> str:
    """The newest released version of a CLI: its own latest_version hook, else npm. Raises (OSError,
    ValueError, …) when it cannot be found."""
    p = _registry.get(provider)
    own = p.hook("latest_version") if p else None
    if own is not None:
        try:
            v = own(timeout)
            if v:
                return v
        except OSError:
            if not (p and p.npm_package):
                raise
    if not (p and p.npm_package):
        raise ValueError(f"no release feed for {provider}")
    v = json.loads(_http_text(_NPM_LATEST.format(package=p.npm_package), timeout)).get("version")
    if not v:
        raise ValueError(f"no version from npm for {provider}")
    return v


def grok_latest_version(timeout: float = 15) -> str:
    """The Grok CLI asks its own update channel."""
    r = subprocess.run([find_cli("grok"), "update", "--check", "--json"], capture_output=True, text=True, timeout=timeout, env=_env(), encoding="utf-8")
    data = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    if data.get("error"):
        raise ValueError(data["error"])
    if not data.get("latestVersion"):
        raise ValueError((r.stderr or r.stdout).strip()[:200] or "no version in `grok update --check`")
    return data["latestVersion"]


def claude_latest_version(timeout: float = 15) -> str | None:
    """Claude Code's release bucket; None (or OSError) sends latest_version on to npm."""
    return parse_version(_http_text(_CLAUDE_LATEST_URL, timeout))


_checks: dict[str, UpdateCheck] = {}
_checks_lock = threading.Lock()


def check_update(provider: str, *, max_age: float = CHECK_TTL) -> UpdateCheck:
    """Compare the installed CLI with the newest release. Cached per provider for `max_age` seconds;
    never raises (network or CLI trouble lands in `error`, and the status bar then says nothing)."""
    with _checks_lock:
        cached = _checks.get(provider)
    if cached and time.time() - cached.at < max_age:
        return cached
    info = cli_version(provider)
    check = UpdateCheck(provider, current=info.version, at=time.time())
    if not info.ok:
        check.error = info.error
    else:
        try:
            check.latest = latest_version(provider)
        except Exception as e:  # noqa: BLE001 - offline, endpoint changed, CLI hiccup: all mean "unknown"
            check.error = str(e) or type(e).__name__
    with _checks_lock:
        _checks[provider] = check
    return check


def peek_check(provider: str, *, max_age: float = CHECK_TTL) -> UpdateCheck | None:
    """The cached check when it is fresh, else None — never touches the network (for the status bar)."""
    with _checks_lock:
        cached = _checks.get(provider)
    return cached if cached and time.time() - cached.at < max_age else None


def forget_checks(providers=PROVIDERS) -> None:
    """Drop cached checks (after an update, so the status bar re-reads the installed version)."""
    with _checks_lock:
        for p in providers:
            _checks.pop(p, None)


# ---------------------------------------------------------------- each provider's model list
# Asked of the provider itself (see models.CATALOGUES_ON), all in about a second and none of it billed:
# Claude Code's `initialize` handshake, `codex debug models` and `grok models` (each rewrites its CLI's
# cache, which Vision then re-reads), and the local server's /v1/models.
def model_providers() -> tuple[str, ...]:
    """Every provider with a model list to fetch, the config-defined servers included."""
    return tuple(p.name for p in _registry.REGISTRY.values() if p.hooks.get("refresh_models"))


MODELS_TTL = 6 * 3600  # a long-running Vision re-asks this often
CLAUDE_MODELS_RECHECK = 600  # sooner when a Claude turn ran on a model the list lacks (at most this often)
SERVER_MODELS_RECHECK = 60  # a chat server's list, re-asked as its turns start: a swapped model shows by name next turn
_models_at: dict[str, float] = {}
_models_busy: dict[str, threading.Lock] = {}
_models_busy_lock = threading.Lock()
# When each provider's list was last fetched, kept across launches: without it every `vision` started
# the Claude initialize handshake (a Node process), `codex debug models` and `grok models` in the
# background, competing with the first paint and the voice warm-up. Only a fetch that worked is
# recorded, so a CLI that was missing or offline is asked again next time.
_stamps_loaded = False


def _stamp_file():
    from vision.config import STATE_DIR

    return STATE_DIR / "models-refreshed.json"


def _refreshed_at(provider: str) -> float:
    """When `provider`'s list was last fetched, in this process or (the saved stamp) an earlier one."""
    global _stamps_loaded
    if not _stamps_loaded:
        _stamps_loaded = True
        try:
            saved = json.loads(_stamp_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for name, at in (saved.items() if isinstance(saved, dict) else ()):
            if isinstance(at, (int, float)):
                _models_at.setdefault(name, float(at))
    return _models_at.get(provider, 0.0)


def _save_stamp(provider: str, at: float) -> None:
    path = _stamp_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**(saved if isinstance(saved, dict) else {}), provider: at}), encoding="utf-8")
    except OSError:
        pass


def claude_catalogue(timeout: float = 30) -> list[dict]:
    """Claude Code's /model list (value, resolvedModel, supportedEffortLevels, …) from the `initialize`
    control request. No prompt is sent, so no model runs and nothing is billed. Raises when it cannot be read."""
    cmd = [find_cli("claude"), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    req = json.dumps({"type": "control_request", "request_id": "vision-models", "request": {"subtype": "initialize"}})
    r = subprocess.run(cmd, input=req + "\n", capture_output=True, text=True, timeout=timeout, env=_env(),
                       cwd=os.path.expanduser("~"), encoding="utf-8")
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        resp = ev.get("response") or {} if ev.get("type") == "control_response" else {}
        if resp.get("request_id") == "vision-models":
            models_list = (resp.get("response") or {}).get("models")
            if isinstance(models_list, list) and models_list:
                return models_list
            raise ValueError(resp.get("error") or "no models in Claude Code's initialize response")
    raise ValueError((r.stderr or r.stdout).strip()[-200:] or f"claude exited {r.returncode} without an initialize response")


def local_model_ids(timeout: float = 5, provider: str = "local") -> list[str]:
    """The model ids a chat server serves (its /v1/models), or the fixed list its table gives. Raises
    when it cannot be reached."""
    return [m["id"] for m in local_models(timeout, provider)]


def local_models(timeout: float = 5, provider: str = "local") -> list[dict]:
    """{"id", "n_params"} for each model a chat server serves; n_params is llama-server's own count from
    the GGUF (its `meta`), None from servers that don't say. Raises when it cannot be reached."""
    from vision.config import load_config

    ep = _registry.endpoint_for(provider, load_config())
    if ep is None:
        raise ValueError(f"{provider} is not a chat server")
    if ep.models:
        return [{"id": str(m), "n_params": None} for m in ep.models]
    req = urllib.request.Request(ep.base_url.rstrip("/") + "/models", headers={"User-Agent": "vision", **ep.headers()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return [{"id": str(m["id"]), "n_params": (m.get("meta") or {}).get("n_params")}
            for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]


def refresh_models(provider: str, *, max_age: float = MODELS_TTL) -> bool:
    """Ask one provider for its model list and swap it in (models.set_claude_catalogue and friends). At
    most one at a time per provider, and not again within `max_age` seconds. Never raises; True if replaced."""
    with _models_busy_lock:
        lock = _models_busy.setdefault(provider, threading.Lock())
    if not lock.acquire(blocking=False):
        return False
    try:
        if time.time() - _refreshed_at(provider) < max_age:
            return False
        _models_at[provider] = now = time.time()
        try:
            p = _registry.get(provider)
            ok = bool(p and p.hook("refresh_models")(provider))
        except Exception:  # noqa: BLE001 - not installed, offline, Mac asleep, format changed: keep the list we have
            return False
        if ok:
            _save_stamp(provider, now)
        return ok
    finally:
        lock.release()


def refresh_claude_models(provider: str = "claude") -> bool:
    return models.set_claude_catalogue(claude_catalogue())


def refresh_local_models(provider: str = "local") -> bool:
    return models.set_local_models(local_models(provider=provider), provider)


def refresh_acp_models(provider: str) -> bool:
    """An ACP agent's models are whatever its table lists (else just its default): nothing to ask."""
    p = _registry.get(provider)
    return bool(p and p.acp and models.set_agent_models(provider, p.acp.models or ("default",)))


def refresh_cli_models(provider: str) -> bool:
    """A CLI that rewrites its own models cache when asked (Provider.models_args), then re-read it."""
    subprocess.run([find_cli(provider), *_registry.get(provider).models_args], capture_output=True, timeout=60,
                   env=_env(), stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"))
    return models.reload_cli_cache(provider)


def refresh_models_soon(providers=None, *, max_age: float = MODELS_TTL) -> None:
    """refresh_models for each stale provider, on daemon threads (callers are on a turn or the UI). Off under tests."""
    if not models.CATALOGUES_ON:
        return
    for p in model_providers() if providers is None else providers:
        if time.time() - _refreshed_at(p) >= max_age:
            threading.Thread(target=refresh_models, args=(p,), kwargs={"max_age": max_age}, daemon=True).start()


def update_cli(provider: str, *, stream: bool = False, timeout: float = 600) -> UpdateResult:
    """Run the CLI's own updater (`claude update`, `codex update`, `grok update`). With `stream` the
    updater's output goes straight to the terminal; otherwise it is captured into the result."""
    before = cli_version(provider)
    if not before.ok:
        return UpdateResult(provider, None, None, 1, error=before.error)
    cmd = [before.path, *_registry.get(provider).update_args]
    try:
        r = subprocess.run(cmd, capture_output=not stream, text=True, timeout=timeout, env=_env(), stdin=subprocess.DEVNULL, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return UpdateResult(provider, before.version, before.version, 1, error=f"`{os.path.basename(cmd[0])} update` did not finish in {timeout:.0f}s")
    except OSError as e:
        return UpdateResult(provider, before.version, before.version, 1, error=f"could not run `{os.path.basename(cmd[0])} update`: {e}")
    output = "" if stream else ((r.stdout or "") + (r.stderr or ""))
    forget_checks((provider,))
    refresh_models_soon((provider,), max_age=0)  # a new CLI may list new models
    after = cli_version(provider)
    return UpdateResult(provider, before.version, after.version or before.version, r.returncode, output=output)


def update_clis(providers=PROVIDERS, *, stream: bool = False, on_start=None) -> list[UpdateResult]:
    """Update several CLIs in order; `on_start(provider)` is called before each one."""
    results = []
    for p in providers:
        if on_start:
            on_start(p)
        results.append(update_cli(p, stream=stream))
    return results


def _parse_providers(arg: str, command: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """'' or 'all' → every provider; 'claude codex' → those two (in PROVIDERS order). Unknown words raise."""
    words = [w.lower() for w in (arg or "").split()]
    if not words or "all" in words:
        return PROVIDERS
    bad = [w for w in words if w not in PROVIDERS and w not in extra]
    if bad:
        raise ValueError(f"{command} takes all, {', '.join(PROVIDERS[:-1])} or {PROVIDERS[-1]}, not {bad[0]!r}")
    return tuple(p for p in PROVIDERS + extra if p in words)


def parse_update_arg(arg: str) -> tuple[str, ...]:
    return _parse_providers(arg, "update")


def parse_version_arg(arg: str) -> tuple[bool, tuple[str, ...]]:
    """'' or 'all' → (True, every provider); 'codex' → (False, ('codex',)); 'vision grok' → (True, ('grok',))."""
    picked = _parse_providers(arg, "version", extra=("vision",))
    if picked == PROVIDERS:
        return True, PROVIDERS
    return "vision" in picked, tuple(p for p in picked if p != "vision")
