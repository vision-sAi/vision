"""One look for /usage across brains.

Claude Code's own report sets the shape: a table titled "<Provider> subscription usage", one row per
window ("Current session", "Current week", "Current week (Fable)"), a bar plus a
percentage, and the reset time as "Sep 20 3:50am". Codex and Grok render through the same helpers so
the three tables read alike.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from rich.console import Group
from rich.table import Table
from rich.text import Text

from vision.providers import get as _provider
from vision.providers import names as _provider_names

from vision.theme import ACCENT, DANGER, LINE, WARN, mix, truecolor

BAR_BLUE = "#5b8cff"  # where the bar's violet runs to, the apps' gradient


def bar(pct: float) -> str:
    """A 20-cell line on a quiet track: violet into blue while there's room, amber from 60%, red from 85%.
    The gradient needs 24-bit colour; without it the fill is flat violet."""
    n = min(20, max(0, int(round(pct / 5))))
    if pct >= 60:
        fill = f"[{DANGER if pct >= 85 else WARN}]{'━' * n}[/]"
    elif truecolor():
        fill = "".join(f"[{mix(ACCENT, BAR_BLUE, i / 20)}]━[/]" for i in range(n))
    else:
        fill = f"[{ACCENT}]{'━' * n}[/]"
    return f"{fill}[{LINE}]{'━' * (20 - n)}[/]"


def used_cell(pct: float | None) -> str:
    return f"{bar(pct)} [bold]{pct:4.0f}%[/bold]" if pct is not None else "?"


def resets_cell(ts: float | None) -> str:
    """Wording for a reset time: 'Sep 20 3:50am' (or 'Sep 25 11pm' on the hour)."""
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(ts)
    clock = dt.strftime("%I:%M%p").lstrip("0").lower()
    if clock.startswith(":"):
        clock = "12" + clock
    if ":00" in clock:
        clock = clock.replace(":00", "")
    return f"{dt.strftime('%b')} {dt.day} {clock}"


def usage_table(provider: str, plan: str | None = None) -> Table:
    title = f"{provider} subscription usage" + (f" ({plan})" if plan else "")
    t = Table(title=title, title_style="heading", title_justify="left", header_style="table.header", box=None, padding=(0, 3, 0, 0))
    t.add_column("window")
    t.add_column("used", justify="right")
    t.add_column("resets", style="dim")
    return t


def add_window(t: Table, label: str, pct: float | None, resets: float | str | None) -> None:
    when = resets if isinstance(resets, str) else resets_cell(resets)
    t.add_row(label, used_cell(pct), when)


def _expiry(ts: float | None) -> str | None:
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts)
    return f"{dt.strftime('%b')} {dt.day}"


def banked_groups(banked: dict | None) -> list[dict]:
    """The banked resets split by kind and expiry, soonest first: [{"count", "expires_at", "kind", "label"}].
    Older payloads without "resets" become one group on the soonest expiry."""
    if not banked or not banked.get("count"):
        return []
    groups = [g for g in banked.get("resets") or [] if isinstance(g, dict) and g.get("count")]
    return groups or [{"count": banked["count"], "expires_at": banked.get("expires_at"), "kind": None, "label": banked.get("label")}]


def _resets(n: int, kind: str | None) -> str:
    """'1 Full Reset', '2 Session Resets', or plain '2 resets' when the kind is unknown."""
    kind = kind or "reset"
    return f"{n} {kind}{'s' if n != 1 else ''}"


def _expires(n: int, day: str | None) -> str:
    return f"expire{'s' if n == 1 else ''} {day}" if day else "no expiry date"


def banked_text(banked: dict | None) -> str | None:
    """'1 Full Reset banked, expires Oct 22', or with several kinds or dates '2 resets banked:
    1 Full Reset expires Oct 4, 1 Full Reset expires Oct 22'; None when there are none."""
    groups = banked_groups(banked)
    if not groups:
        return None
    if len(groups) == 1:
        g = groups[0]
        day = _expiry(g.get("expires_at"))
        return f"{_resets(g['count'], g.get('kind'))} banked" + (f", {_expires(g['count'], day)}" if day else "")
    n = banked["count"]
    return f"{n} reset{'s' if n != 1 else ''} banked: " + ", ".join(
        f"{_resets(g['count'], g.get('kind'))} {_expires(g['count'], _expiry(g.get('expires_at')))}" for g in groups)


def add_banked(t: Table, banked: dict | None) -> None:
    """Rows under the windows for saved resets (Claude's /limit-reset grants, Codex's banked resets),
    one per kind and expiry date so each reset's own type and deadline show."""
    for i, g in enumerate(banked_groups(banked)):
        n, day = g["count"], _expiry(g.get("expires_at"))
        t.add_row("Banked resets" if i == 0 else "", f"[accent]{_resets(n, g.get('kind'))}[/accent]",
                  _expires(n, day) if day else "")


def _title(words: str) -> str:
    """'Full reset' -> 'Full Reset', leaving the rest of each word alone ('5 hr' stays '5 hr')."""
    return " ".join(w[:1].upper() + w[1:] for w in words.split())


def claude_reset_kind(clears: list | None) -> str | None:
    """What a Claude grant resets, from the limits it clears: session and week together are a
    Full Reset, one of them alone a Session or Weekly Reset, a model's own week (seven_day_fable)
    that model's reset. The extra-usage variant of a window adds nothing to the name."""
    wins = {c for c in clears or [] if isinstance(c, str) and "overage" not in c}
    if {"five_hour", "seven_day"} <= wins:
        return "Full Reset"
    if wins == {"five_hour"}:
        return "Session Reset"
    if wins == {"seven_day"}:
        return "Weekly Reset"
    models = sorted(w.removeprefix("seven_day_") for w in wins if w.startswith("seven_day_"))
    if len(models) == 1 and wins == {f"seven_day_{models[0]}"}:
        return f"{models[0].capitalize()} Reset"
    return None


def codex_reset_kind(title: str | None) -> str | None:
    """'Full reset (Weekly + 5 hr)' -> 'Full Reset': the credit's own title, minus the detail."""
    if not isinstance(title, str) or not title.strip():
        return None
    return _title(title.split(" (")[0].strip())


def group_by_expiry(items: list[tuple[int, float | None, str | None, str | None]]) -> list[dict]:
    """(count, expires_at, kind, label[, id]) tuples merged by kind and expiry, soonest first, undated last."""
    merged: dict[tuple, dict] = {}
    for n, ends, kind, label, *_ in items:
        g = merged.setdefault((ends, kind), {"count": 0, "expires_at": ends, "kind": kind, "label": label})
        g["count"] += n
        g["label"] = g["label"] or label
    return sorted(merged.values(), key=lambda g: (g["expires_at"] is None, g["expires_at"] or 0, g["kind"] or ""))


def footer(source: str, at: float | None = None) -> Text:
    """A dim note under the table, e.g. 'from the last reply's snapshot at 14:03:12'."""
    if at:
        return Text(f"{source} at {time.strftime('%H:%M:%S', time.localtime(at))}", style="dim")
    return Text(source, style="dim")


def usage_group(*parts) -> Group:
    return Group(*[p for p in parts if p is not None])


# ---------------------------------------------------------------- structured data (the phone's /usage page)
USAGE_LINE = re.compile(r"^(?P<label>[^:]+):\s+(?P<pct>\d+)% used(?:\s+·\s+resets\s+(?P<reset>.+))?$")
# Providers with a subscription /usage can read, in registry order; a throwaway brain for one runs its
# default model (models.provider_default).
PROVIDERS = _provider_names(has_usage=True)


def _claude_reset_ts(text: str) -> float | None:
    """'Sep 22, 3:50am (America/New_York)' → epoch, assuming this year (next year if that is already past)."""
    raw = text.replace(" (America/New_York)", "").strip()
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            dt = datetime.strptime(raw.upper(), fmt)
        except ValueError:
            continue
        now = datetime.now()
        dt = dt.replace(year=now.year)
        if dt < now:
            dt = dt.replace(year=now.year + 1)
        return dt.timestamp()
    return None


def _window(label: str, pct: float | None, resets_at: float | None, resets: str | None = None) -> dict:
    return {
        "label": label,
        "used_percent": None if pct is None else round(float(pct), 1),
        "resets_at": resets_at,
        "resets": resets or (resets_cell(resets_at) if resets_at else None),
    }


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
# The reset grants only come back to a caller that looks like a recent Claude Code CLI
# (2.1.278 was refused with ineligible_reason "cli_version", 2.1.280 accepted, Sep 2026).
CLAUDE_MIN_CLI = "2.1.280"


def _claude_cli_version() -> str:
    """The newest installed Claude Code version, so the server's minimum moves with updates."""
    import os

    root = os.path.expanduser("~/.local/share/claude/versions")
    try:
        found = [v for v in os.listdir(root) if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    except OSError:
        found = []
    key = lambda v: tuple(int(p) for p in v.split("."))
    return max(found + [CLAUDE_MIN_CLI], key=key)


def _iso_ts(s) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude_banked(timeout: float = 10) -> dict | None:
    """Claude's redeemable usage-limit resets (the `/limit-reset` grants), read with Claude Code's
    own login: {"count", "expires_at", "label"}, or None when there are none or it can't be read."""
    return claude_banked_from({"cedar_ember": _claude_grants(timeout)})


def _claude_login() -> tuple[str, str | None] | None:
    """Claude Code's OAuth token and organisation, or None when it's missing or expired. An expired
    token is skipped rather than refreshed (refreshing rotates the login Claude Code itself uses)."""
    import json
    import os

    from vision.config import cli_logins_allowed

    if not cli_logins_allowed():
        return None
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(home, ".credentials.json"), encoding="utf-8") as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return None
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token or (expires and expires / 1000 < time.time()):
        return None
    org = None
    state = os.path.join(home, ".claude.json") if os.environ.get("CLAUDE_CONFIG_DIR") else os.path.expanduser("~/.claude.json")
    try:
        with open(state, encoding="utf-8") as f:
            org = (json.load(f).get("oauthAccount") or {}).get("organizationUuid")
    except (OSError, ValueError):
        pass
    return token, org


def _claude_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-cli/{_claude_cli_version()} (external, cli)",
        "x-app": "cli",
        "Accept": "application/json",
    }


def _claude_grants(timeout: float = 10) -> dict | None:
    """The raw `cedar_ember` block (grants with ids, usable_now, next_grant_id…). Undocumented
    endpoint. It rate-limits hard, so after a 429 it is left alone for CLAUDE_BANKED_COOLDOWN, and a
    failed or skipped read falls back to the last good answer if it is under CLAUDE_BANKED_MAX_AGE old."""
    import json
    import urllib.error
    import urllib.request

    login = _claude_login()
    if not login:
        return _claude_banked_cached()
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers=_claude_headers(login[0]))
    global _claude_throttled_until
    if time.time() < _claude_throttled_until:
        return _claude_banked_cached()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _claude_throttled_until = time.time() + CLAUDE_BANKED_COOLDOWN
        return _claude_banked_cached()
    except Exception:
        return _claude_banked_cached()
    _claude_banked_save(body)
    return (body or {}).get("cedar_ember")


CLAUDE_BANKED_MAX_AGE = 600  # seconds a saved answer may stand in for a failed read
CLAUDE_BANKED_COOLDOWN = 60  # seconds to leave the endpoint alone after a 429
_claude_throttled_until = 0.0


def _claude_banked_file():
    from vision.config import STATE_DIR
    return STATE_DIR / "claude_banked.json"


def _claude_banked_save(body: dict) -> None:
    """Keep only the grants block and when it was read; re-parsed on use so expiries still apply."""
    import json
    try:
        f = _claude_banked_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"at": time.time(), "cedar_ember": (body or {}).get("cedar_ember")}), encoding="utf-8")
    except OSError:
        pass


def _claude_banked_cached() -> dict | None:
    import json
    try:
        saved = json.loads(_claude_banked_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or time.time() - (saved.get("at") or 0) > CLAUDE_BANKED_MAX_AGE:
        return None
    return saved.get("cedar_ember")


def claude_banked_from(body: dict) -> dict | None:
    block = (body or {}).get("cedar_ember")
    if not isinstance(block, dict) or block.get("eligible") is not True:
        return None
    now = time.time()
    items = []
    for g in block.get("grants") or []:
        if not isinstance(g, dict) or g.get("paused"):
            continue
        left = g.get("resets_left")
        ends = _iso_ts(g.get("ends_at"))
        if not isinstance(left, int) or left < 1 or (ends and ends <= now):
            continue
        items.append((left, ends, claude_reset_kind(g.get("clears")), g.get("label"), g.get("id")))
    return _banked(items)


def _banked(items: list[tuple]) -> dict | None:
    """{"count", "expires_at" (soonest), "label", "resets" (per kind and expiry), "items" (one per
    reset, with the id that spends it; a grant holding two resets gives two rows)} or None when empty."""
    groups = group_by_expiry(items)
    if not groups:
        return None
    first = groups[0]
    each = [{"id": rid, "kind": kind, "expires_at": ends, "label": label}
            for n, ends, kind, label, *rest in items for rid in [rest[0] if rest else None] for _ in range(n)]
    each.sort(key=lambda r: (r["expires_at"] is None, r["expires_at"] or 0, r["kind"] or ""))
    return {"count": sum(g["count"] for g in groups), "expires_at": first["expires_at"], "label": first["label"],
            "resets": groups, "items": each}


def codex_banked(rl: dict) -> dict | None:
    """Codex's banked resets from the app-server's rateLimitResetCredits (snake-cased onto the
    rate-limit dict as reset_credits by codex.fetch_rate_limits). availableCount is the count;
    each listed credit brings its own expiry, and any the list leaves out count as undated."""
    block = (rl or {}).get("reset_credits")
    if not isinstance(block, dict):
        return None
    count = block.get("available_count")
    if not isinstance(count, int) or count < 1:
        return None
    live = [c for c in block.get("credits") or [] if isinstance(c, dict) and c.get("status") == "available"]
    items = [(1, c["expires_at"] if isinstance(c.get("expires_at"), (int, float)) else None, codex_reset_kind(c.get("title")), c.get("title"), c.get("id"))
             for c in live[:count]]
    if count > len(items):  # the list came up short: the rest have no known kind or expiry
        items.append((count - len(items), None, None, None))
    return _banked(items)


# ---------------------------------------------------------------- spending a banked reset
CLAUDE_RESET_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"


def use_banked(provider: str, reset_id: str | None = None) -> dict:
    """Spend one banked reset, the one `reset_id` names or else the provider's pick:
    {"ok", "outcome", "message"}, the message worded for the person."""
    p = _provider(provider)
    spend = p.hook("use_banked") if p and p.banked_resets else None
    if spend is None:
        banked = " and ".join(q.label for q in map(_provider, _provider_names(banked_resets=True)))
        return {"ok": False, "outcome": "unsupported", "message": f"Only {banked} bank resets."}
    return spend(reset_id)


def _pick_claude_grant(block: dict, grant_id: str | None = None) -> tuple[dict | None, str | None]:
    """The grant to spend: `grant_id` when given, else the one Claude Code would (next_grant_id when
    it is usable, else the first usable one). None and why not when it can't be used now."""
    grants = [g for g in block.get("grants") or [] if isinstance(g, dict) and (g.get("resets_left") or 0) > 0]
    if grant_id:
        grants = [g for g in grants if g.get("id") == grant_id]
        if not grants:
            return None, "That reset's already gone."
    usable = [g for g in grants if g.get("usable_now") and not g.get("paused") and isinstance(g.get("id"), str)]
    for g in usable:
        if g["id"] == block.get("next_grant_id"):
            return g, None
    if usable:
        return usable[0], None
    if any(g.get("use_requires_limit") for g in grants):
        return None, "That reset only works once you've hit a limit."
    if grants and all(g.get("paused") for g in grants):
        return None, "Claude's resets are paused right now."
    return None, "No Claude resets to use." if not grants else "Claude won't let that reset be used right now."


def use_claude_reset(grant_id: str | None = None, timeout: float = 35) -> dict:
    """Claude Code's `/limit-reset` claim, with Claude Code's own login. Undocumented endpoint."""
    import json
    import urllib.error
    import urllib.request
    import uuid

    from vision.config import cli_logins_allowed

    if not cli_logins_allowed():
        return {"ok": False, "outcome": "unsupported", "message": "Using a Claude reset needs read_cli_logins = true under [brain]."}
    login = _claude_login()
    if not login:
        return {"ok": False, "outcome": "auth_error", "message": "Claude's login has lapsed. Send Claude a message so it refreshes, then try again."}
    token, org = login
    if not org:
        return {"ok": False, "outcome": "auth_error", "message": "Couldn't find your Claude organisation; run /login in Claude Code."}
    block = _claude_grants()
    if not isinstance(block, dict) or block.get("eligible") is not True:
        return {"ok": False, "outcome": "unavailable", "message": "Couldn't read your Claude resets right now; try again in a minute."}
    grant, why = _pick_claude_grant(block, grant_id)
    if not grant:
        return {"ok": False, "outcome": "not_offered", "message": why}
    body = json.dumps({"program": "cedar_ember", "grant_id": grant["id"], "request_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(CLAUDE_RESET_URL.format(org=org), data=body, method="POST",
                                 headers={**_claude_headers(token), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return {"ok": False, "outcome": "rate_limited", "message": "Claude says slow down; try again in a minute."}
        if e.code in (401, 403):
            return {"ok": False, "outcome": "auth_error", "message": "Claude refused the login; run /login in Claude Code, then try again."}
        return {"ok": False, "outcome": "error", "message": f"Claude's reset failed (HTTP {e.code})."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "outcome": "error", "message": f"Couldn't reach Claude: {e}"}
    finally:
        _claude_forget()  # the grant count has (or may have) moved; the next read goes to the server
    result = (reply or {}).get("result")
    messages = {
        "reset": "Claude's limits are reset.",
        "not_limited": "Nothing to reset, you're not at a limit.",
        "already_used": "That reset was already used.",
        "cooldown": "Claude wants a breather between resets; try again shortly.",
        "ineligible": "Claude says this account can't use it right now.",
    }
    return {"ok": result == "reset", "outcome": result or "unavailable",
            "message": messages.get(result, "Claude couldn't do the reset right now; try again later.")}


def _claude_forget() -> None:
    try:
        _claude_banked_file().unlink()
    except OSError:
        pass


def use_codex_reset(credit_id: str | None = None) -> dict:
    """Codex's own reset spend, through `codex app-server`; without a credit id the backend picks."""
    import uuid

    from vision.codex import CodexError, consume_reset_credit, find_codex

    try:
        outcome = consume_reset_credit(find_codex(), str(uuid.uuid4()), credit_id)
    except CodexError as e:
        return {"ok": False, "outcome": "error", "message": str(e)}
    messages = {
        "reset": "Codex's limits are reset.",
        "nothingToReset": "Nothing to reset, you're not at a limit.",
        "noCredit": "No Codex resets left to use.",
        "alreadyRedeemed": "That reset was already used.",
    }
    return {"ok": outcome == "reset", "outcome": outcome, "message": messages.get(outcome, f"Codex said {outcome}.")}


def _claude_data(brain) -> dict:
    data = _claude_windows(brain)
    data["banked"] = claude_banked()
    return data


def _claude_windows(brain) -> dict:
    text = brain.usage_report()
    if text:
        windows = []
        for line in text.splitlines():
            m = USAGE_LINE.match(line.strip())
            if not m:
                continue
            label = m.group("label")
            label = {"Current week (all models)": "Current week", "Current week (Fable)": "Fable"}.get(label, label)
            reset = (m.group("reset") or "").replace(" (America/New_York)", "")
            windows.append(_window(label, float(m.group("pct")), _claude_reset_ts(reset) if reset else None, reset or None))
        return {"windows": windows, "source": "live"}
    usage = brain.last_usage or brain.cached_usage()
    if not usage:
        return {"windows": [], "error": "No usage data yet. Ask Vision something first."}
    labels = {"five_hour": "Current session", "seven_day": "Current week", "seven_day_overage_included": "Current week (incl. extra usage)"}
    wins = (usage.get("info") or {}).get("unifiedWindows") or {}
    return {
        "windows": [_window(labels.get(k, k), float(w.get("utilization") or 0) * 100, w.get("resetsAt")) for k, w in wins.items()],
        "source": "snapshot",
        "at": usage.get("at") or None,
        "notes": ["from the last reply's rate-limit event"],
    }


def _codex_data(brain) -> dict:
    limits = brain.rate_limits()
    if not limits:
        return {"windows": [], "error": "No Codex usage yet: ask something first, or run `codex login`."}
    rl = limits["rate_limits"]
    windows = []
    for key, default in (("primary", "Current session"), ("secondary", "Current week")):
        w = rl.get(key) or {}
        if not w:
            continue
        mins = w.get("window_minutes") or 0
        label = default if not mins else ("Current session" if mins < 1440 else ("Current week" if mins == 10080 else "Current window"))
        windows.append(_window(label, w.get("used_percent"), w.get("resets_at")))
    notes = []
    credits = rl.get("credits") or {}
    if credits.get("unlimited"):
        notes.append("credits: unlimited")
    elif credits.get("has_credits"):
        notes.append(f"credits: {credits.get('balance')}")
    if rl.get("rate_limit_reached_type"):
        notes.append(f"limit reached: {rl['rate_limit_reached_type']}")
    if limits.get("source") != "live":
        notes.insert(0, "from the last reply's rate-limit snapshot")
    return {
        "plan": (rl.get("plan_type") or "").capitalize() or None,
        "windows": windows,
        "banked": codex_banked(rl),
        "source": limits.get("source"),
        "at": limits.get("at"),
        "notes": notes,
    }


def grok_usage(brain) -> dict:
    """The registry's usage hook for Grok: its allowance doesn't need the brain."""
    return _grok_data()


def _grok_data() -> dict:
    from vision.grok import fetch_subscription

    from vision.config import cli_logins_allowed

    if not cli_logins_allowed():
        return {"windows": [], "error": "Grok usage is off: it needs read_cli_logins = true under [brain]."}
    sub = fetch_subscription()
    if not sub:
        return {"windows": [], "error": "Grok usage unavailable: needs a live grok.com login (run `grok login`)."}
    period = sub.get("period") or "weekly"
    window = {"weekly": "Current week", "monthly": "Current month", "daily": "Current day"}.get(period, f"Current {period}")
    windows = [_window(window, sub.get("used_percent"), sub.get("resets_at"))]
    for item in sub.get("products") or []:
        windows.append(_window(f"{window} ({item['label']})", item["percent"], sub.get("resets_at")))
    notes = []
    prepaid = sub.get("prepaid_cents") or 0
    cap = sub.get("on_demand_cap_cents") or 0
    if prepaid:
        notes.append(f"extra credits ${prepaid / 100:.2f}")
    if cap:
        notes.append(f"on-demand ${(sub.get('on_demand_used_cents') or 0) / 100:.2f} / ${cap / 100:.2f}")
    return {"plan": sub.get("plan") or None, "windows": windows, "source": "live", "notes": notes}


def usage_data(cfg, brain, provider: str) -> dict:
    """One provider's subscription usage as plain data, the phone's shape of `/usage`:
    {provider, plan, windows:[{label, used_percent, resets_at, resets}], banked:{count, expires_at, label}|None,
    notes:[…], source, at, error}.
    A brain for another provider is created on the fly (its last thread resumed, so Codex can read
    its rollout) without switching what answers the chat."""
    from vision.models import provider_default, provider_label

    out: dict = {"provider": provider, "label": provider_label(provider), "plan": None, "windows": [], "banked": None, "notes": [], "source": None, "at": None, "error": None}
    try:
        if brain is None or brain.provider != provider:
            from dataclasses import replace

            from vision.brain import create_brain

            brain = create_brain(replace(cfg.brain, model=provider_default(provider)), continue_session=True)
        p = _provider(provider)
        read = p.hook("usage") if p and p.has_usage else None
        if read is None:
            out["error"] = f"no usage for {provider}"
        else:
            out.update(read(brain))
    except Exception as e:  # one unavailable CLI should not hide the other providers
        out["error"] = f"{provider_label(provider)} usage unavailable: {e}"
    return out


def usage_all(cfg, brain) -> list[dict]:
    """Every subscription provider in a stable order, fetched in parallel (each is a CLI call)."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        return list(pool.map(lambda p: usage_data(cfg, brain, p), PROVIDERS))


# ---------------------------------------------------------------- the conversation model's `usage` field
# Words that make a request about subscription usage. Narrow on purpose: "memory usage" or "the
# speed limit" are not about the plan, "how much Claude usage do I have left" is.
_USAGE_WORDS = re.compile(
    r"\b(?:(?:claude|codex|grok|subscription|plan|my|weekly|session|rate)[- ](?:usage|limits?|quota|allowance)|"
    r"usage (?:left|remaining|limits?|page)|how much (?:(?:claude|codex|grok) )?(?:usage|quota|allowance)|"
    r"(?:usage|quota|allowance) (?:do i have|have i got|is left)|hit (?:my|the) (?:usage )?(?:limit|cap)|"
    r"(?:limits?|usage|session|week) reset)\b",
    re.IGNORECASE,
)


def is_usage_request(text: str) -> bool:
    return bool(_USAGE_WORDS.search(text or ""))


def requested_providers(text: str) -> tuple[str, ...]:
    """The providers a usage question names, Claude when it names none ("how much usage have I got")."""
    named = tuple(p for p in PROVIDERS if re.search(rf"\b{p}\b", text or "", re.IGNORECASE))
    if re.search(r"\b(?:all|every|each)\b", text or "", re.IGNORECASE) and not named:
        return PROVIDERS
    return named or PROVIDERS[:1]


def usage_text(rows: list[dict]) -> str:
    """The phone's Usage page as a few plain lines for the conversation model: each window's percent
    used and reset, banked resets and notes, or the provider's error. Figures only, nothing secret."""
    lines = []
    for row in rows:
        head = f"{row.get('label') or row.get('provider')} subscription usage" + (f" ({row['plan']})" if row.get("plan") else "")
        if row.get("error"):
            lines.append(f"{head}: {row['error']}")
            continue
        lines.append(head + ":")
        for w in row.get("windows") or []:
            pct = w.get("used_percent")
            used = f"{pct:g}% used, {max(0.0, 100 - pct):g}% left" if pct is not None else "not reported"
            lines.append(f"- {w['label']}: {used}" + (f", resets {w['resets']}" if w.get("resets") else ""))
        banked = banked_text(row.get("banked"))
        if banked:
            lines.append(f"- {banked}")
        lines += [f"- {note}" for note in row.get("notes") or []]
        if row.get("source") != "live" and row.get("at"):
            lines.append(f"- as of {time.strftime('%H:%M', time.localtime(row['at']))}")
    return "\n".join(lines)


def prefetch(cfg, brain, prompt: str, timeout: float = 15):
    """Start fetching usage for a usage question, as the phone's /usage does (usage_data, so the same
    figures). Returns a thread whose .result holds usage_text (or .error a short reason); None when
    the request is not about usage. The caller joins it just before it needs the packet."""
    import threading

    if not is_usage_request(prompt):
        return None
    providers = requested_providers(prompt)

    def run():
        try:
            rows = usage_all(cfg, brain) if providers == PROVIDERS else [usage_data(cfg, brain, p) for p in providers]
            t.result = usage_text(rows)
        except Exception as e:  # never let a usage lookup break the conversation
            t.error = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=run, daemon=True, name="usage")
    t.result = None
    t.error = None
    t.timeout = timeout
    t.start()
    return t
