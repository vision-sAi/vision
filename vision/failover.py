"""A subscription running out mid-chat.

Two things, both without any model call of their own:

- Failover: spotting a usage-limit failure in a turn that just ended, and which other provider could
  carry the conversation on (the chat does the switch and sends the message again).
- Quota: how much of the current provider's tightest window is left, from what a turn's rate-limit
  event or the last /usage already recorded, for the input box's corner and a one-time warning.
"""
from __future__ import annotations

import re
import time

# What the three CLIs say when a plan window is used up (Claude Code's result text "You've hit your
# limit", Codex's usage_limit_reached / rate_limit_reached, Grok's 429s). "limit" alone is too broad
# (a context limit, "time limit"), so each phrase is spelt out.
_LIMIT_WORDS = re.compile(
    r"usage[ _]limit|hit your limit|limit reached|limit_reached|rate[ _-]?limit|out of (?:extra )?usage|"
    r"quota|too many requests|\b429\b|usage cap|exceeded your",
    re.IGNORECASE,
)

STAY = "Stay"  # the option that leaves the chat where it is

WINDOW_NAMES = {
    "five_hour": "5-hour window",
    "seven_day": "weekly window",
    "seven_day_overage_included": "weekly window (incl. extra usage)",
    "primary": "5-hour window",
    "secondary": "weekly window",
}


def limit_hit(error: str, usage: dict | None = None) -> bool:
    """Whether a failed turn's error (and, for Claude, the rate-limit info recorded with it) means the
    plan's window is used up rather than anything else going wrong. A cancelled turn never counts."""
    if not error or error == "cancelled":
        return False
    if _LIMIT_WORDS.search(error):
        return True
    info = (usage or {}).get("info") if isinstance(usage, dict) else None
    return isinstance(info, dict) and str(info.get("status") or "").lower() == "rejected"


def order(setting) -> str | list[str]:
    """[brain].failover as the chat reads it: "ask", "off", or a list of provider names to try in turn."""
    if isinstance(setting, (list, tuple)):
        return [str(x).strip().lower() for x in setting if str(x).strip()]
    word = str(setting or "ask").strip().lower()
    return word if word in ("ask", "off") else "ask"


def candidates(cfg, current: str, tried: set[str] | None = None) -> list[tuple[str, str]]:
    """(model alias, label) for every other provider that could take the conversation now: switched on
    in /providers and set up here, in [brain].failover's order when it is a list, else /model's."""
    from vision import providers
    from vision.models import model_label, provider_default, provider_label

    chosen = order(getattr(cfg.brain, "failover", "ask"))
    names = [p.name for p in providers.enabled(cfg)]
    if isinstance(chosen, list):
        names = [n for n in chosen if n in names]
    out = []
    for name in names:
        if name == current or name in (tried or set()):
            continue
        if not providers.ready(name, cfg):
            continue
        model = provider_default(name)
        if not model:
            continue
        out.append((model, f"{provider_label(name)} · {model_label(model)}"))
    return out


def question(provider_label: str, options: list[tuple[str, str]]) -> dict:
    """The form the chat shows when a window is used up: the other providers, or stay."""
    return {
        "question": f"{provider_label}'s usage limit is hit. Carry on with another provider?",
        "header": "Limit",
        "multiSelect": False,
        "options": [{"label": label, "description": f"continue on {label.split(' · ')[0]} from the transcript so far"}
                    for _, label in options]
        + [{"label": STAY, "description": "leave it; try again once the window resets"}],
    }


def chosen(answers: dict | None, q: dict, options: list[tuple[str, str]]) -> str | None:
    """The model picked in the form, or None for Stay / a dismissed form."""
    pick = (answers or {}).get(q["question"], "")
    for model, label in options:
        if pick == label:
            return model
    return None


# ---------------------------------------------------------------- quota


def _pct(value) -> float | None:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return pct


def quota(provider: str, data: dict | None) -> dict | None:
    """The tightest window of a provider as {"left": percent, "window": name, "resets_at": ts | None},
    from the data each one records on its own: Claude's rate-limit event (brain.last_usage),
    Codex's rate_limits dict, Grok's subscription dict. None when nothing is known yet."""
    if not isinstance(data, dict):
        return None
    windows: list[tuple[float, str, float | None]] = []
    if provider == "claude":
        for key, w in ((data.get("info") or {}).get("unifiedWindows") or {}).items():
            used = _pct((w or {}).get("utilization"))
            if used is not None:
                windows.append((used * 100, WINDOW_NAMES.get(key, key.replace("_", " ")), (w or {}).get("resetsAt")))
    elif provider == "codex":
        for key in ("primary", "secondary"):
            w = data.get(key) or {}
            used = _pct(w.get("used_percent"))
            if used is not None:
                mins = int(w.get("window_minutes") or 0)
                name = WINDOW_NAMES[key] if not mins else ("5-hour window" if mins < 1440 else ("weekly window" if mins == 10080 else "window"))
                windows.append((used, name, w.get("resets_at")))
    elif provider == "grok":
        used = _pct(data.get("used_percent"))
        if used is not None:
            period = data.get("period") or "weekly"
            windows.append((used, f"{period} allowance", data.get("resets_at")))
    if not windows:
        return None
    used, name, resets = max(windows, key=lambda w: w[0])
    return {"left": max(0.0, min(100.0, 100.0 - used)), "window": name, "resets_at": resets}


def source(brain) -> dict | None:
    """What `quota` reads for this brain, from memory or a local file only (never a network or model call)."""
    provider = getattr(brain, "provider", "")
    if provider == "claude":
        return getattr(brain, "last_usage", None) or brain.cached_usage()
    if provider == "codex":
        read = getattr(brain, "_rollout_rate_limits", None)
        found = read() if read else None
        return (found or {}).get("rate_limits")
    if provider == "grok":
        from vision.grok import last_subscription

        return last_subscription()
    return None


def in_words(ts: float | None, now: float | None = None) -> str:
    """'40m', '3h 10m', '2d': how long until a window resets ('' when unknown or past)."""
    if not ts:
        return ""
    left = float(ts) - (time.time() if now is None else now)
    if left <= 0:
        return ""
    mins = int(left // 60)
    if mins < 60:
        return f"{max(1, mins)}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def corner_text(q: dict | None) -> str:
    """'38% left' for the corner label; '' when the quota is unknown."""
    if not q:
        return ""
    return f"{round(q['left'])}% left"


def low(q: dict | None, warn_pct: float) -> bool:
    return bool(q) and q["left"] <= float(warn_pct or 0)


def warning(provider_label: str, q: dict | None, warn_pct: float) -> str:
    """"Claude's 5-hour window is nearly used up (12% left, resets in 40m)" once a window crosses the
    threshold; '' above it."""
    if not low(q, warn_pct):
        return ""
    when = in_words(q.get("resets_at"))
    tail = f", resets in {when}" if when else ""
    state = "used up" if q["left"] <= 0 else "nearly used up"
    return f"{provider_label}'s {q['window']} is {state} ({round(q['left'])}% left{tail})"


def key(q: dict | None) -> tuple | None:
    """What makes a warning new again: another window, or the same one after it reset."""
    return (q["window"], q.get("resets_at")) if q else None
