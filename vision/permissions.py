"""What brains may do without asking, as Settings shows and changes it (`GET/POST /permissions`).

Three lists in config.toml's [brain], in toolrules.py's syntax: `denied_tools` is never allowed, `ask_tools`
asks first, and `allow_tools` holds exceptions that run without asking even though an ask rule caught them
(deny still wins). Beside them, `scout_rules` gives every scout a default per action (projects.ACTIONS) over
its leash's, and a scout's own rule still wins over that. A scout's chat and threads never take allow_tools:
what a scout's rules ask stays asked.

They live in Vision's config rather than Claude Code's settings.json so that every brain gets them: Claude
as its own ask and deny rules (brain.py), the brains Vision runs tools for checked by Vision itself (local.py,
localtools.py), and Grok's and ACP agents' deny rules held by their CLI or by Vision. Codex gets every
rule in its prompt only, and Grok and ACP agents their ask rules (persona.py). Claude Code's own rules in its settings.json
are shown, read only: Claude Code applies them to Claude chats itself."""
from __future__ import annotations

import json
import os
from pathlib import Path

KINDS = {"allow": "allow_tools", "ask": "ask_tools", "deny": "denied_tools"}
MAX_RULES = 100
RULE_CHARS = 300


def clean_rule(rule) -> str:
    """One rule as typed, checked. Raises ValueError saying what's wrong."""
    from vision.toolrules import parse

    text = str(rule if rule is not None else "").strip()
    if not text:
        raise ValueError("a rule can't be empty")
    if len(text) > RULE_CHARS:
        raise ValueError(f"a rule can be at most {RULE_CHARS} characters")
    if any(ord(c) < 32 for c in text):
        raise ValueError("a rule is one line")
    if parse(text) is None:
        raise ValueError(f"`{text}` isn't a rule: write a tool, or a tool and what it's used on, e.g. Bash(git push:*)")
    return text


def clean(body, brain) -> dict:
    """The [brain] values `body` sets, checked against what `brain` has now: {allow?, ask?, deny?: [rule]}
    (a list given replaces that list) and {scouts?: {action: does|asks|never|default}}. Returns
    {allow_tools, ask_tools, denied_tools, scout_rules}. Raises ValueError."""
    from vision.projects import clean_rules

    if not isinstance(body, dict):
        raise ValueError("send {allow, ask, deny, scouts}")
    unknown = sorted(set(body) - {*KINDS, "scouts"})
    if unknown:
        raise ValueError(f"unknown {', '.join(unknown)}: send allow, ask, deny or scouts")
    lists = {kind: list(getattr(brain, key, None) or []) for kind, key in KINDS.items()}
    for kind in KINDS:
        if kind not in body:
            continue
        if not isinstance(body[kind], list):
            raise ValueError(f"{kind} must be a list of rules")
        rules = list(dict.fromkeys(clean_rule(r) for r in body[kind]))
        if len(rules) > MAX_RULES:
            raise ValueError(f"{kind} can hold at most {MAX_RULES} rules")
        lists[kind] = rules
    seen: dict[str, str] = {}
    for kind in ("deny", "ask", "allow"):
        for rule in lists[kind]:
            if rule in seen:
                raise ValueError(f"`{rule}` can't both {_WORDS[seen[rule]]} and {_WORDS[kind]}: take it out of one")
            seen[rule] = kind
    scouts = clean_rules(body["scouts"], getattr(brain, "scout_rules", None)) if "scouts" in body else \
        dict(getattr(brain, "scout_rules", None) or {})
    return {**{KINDS[kind]: rules for kind, rules in lists.items()}, "scout_rules": scouts}


_WORDS = {"allow": "be allowed", "ask": "ask first", "deny": "be never allowed"}


def save(values: dict, brain) -> None:
    """Write into config.toml's [brain] what differs from `brain`, each line in place (comments kept)."""
    from vision.config import _toml_str, save_config_value

    for key, value in values.items():
        if value == getattr(brain, key, None):
            continue
        if isinstance(value, dict):
            literal = "{ " + ", ".join(f"{k} = {_toml_str(v)}" for k, v in value.items()) + " }" if value else "{}"
        else:
            literal = "[" + ", ".join(_toml_str(r) for r in value) + "]"
        save_config_value("brain", key, literal)


def claude_rules() -> dict | None:
    """The permission rules in Claude Code's own user settings.json, {path, allow, ask, deny}; None when it
    has none (or can't be read). Vision never writes there."""
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")) / "settings.json"
    try:
        perms = json.loads(path.read_text(encoding="utf-8")).get("permissions") or {}
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(perms, dict):
        return None
    out = {kind: [str(r) for r in perms.get(kind) or [] if isinstance(r, str) and r.strip()] if isinstance(perms.get(kind), list) else []
           for kind in KINDS}
    if not any(out.values()):
        return None
    home = os.path.expanduser("~")
    shown = str(path)
    return {"path": "~" + shown[len(home):] if shown.startswith(home + os.sep) else shown, **out}


def view(brain) -> dict:
    """What Settings shows: the three lists, which deny rules are Vision's own defaults (`builtin`), every
    scout's default per action (`scouts`: rule "" follows the leash, whose defaults are in `leash`), and
    Claude Code's own rules (`claude`, read only, or None)."""
    from vision.config import BrainConfig
    from vision.projects import ACTIONS

    scout_rules = getattr(brain, "scout_rules", None) or {}
    return {
        **{kind: list(getattr(brain, key, None) or []) for kind, key in KINDS.items()},
        "builtin": BrainConfig().denied_tools,
        "scouts": [{"action": a, "label": spec["label"], "rule": scout_rules.get(a, ""), "leash": spec["defaults"]}
                   for a, spec in ACTIONS.items()],
        "claude": claude_rules(),
    }
