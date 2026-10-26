"""Tool rules: the `Tool` / `Tool(specifier)` patterns of [brain].denied_tools and [brain].ask_tools,
matched against a tool call the way Claude Code matches its permission rules, so the brains Vision
runs tools for (local.py) and the forms Vision shows agree with what Claude Code enforces.

Forms (the Claude Code ones, see https://code.claude.com/docs/en/permissions):
  `Bash`, `Bash(*)`          every call of the tool
  `Bash(git push:*)`         a command whose first word(s) are the prefix (`Bash(git push *)` is the same)
  `Bash(npm run build)`      exactly that command
  `Edit(docs/**)`            a file tool on a path matching the glob (`Read`, `Write`, `NotebookEdit` too;
                             Claude Code itself only consults Edit and Read path rules)
  `WebFetch(domain:x.com)`   a fetch of that host
  `Tool(param:value)`        any other top-level input parameter, `value*` for a prefix
Bash commands are checked segment by segment (`cd x && git push` matches `Bash(git push:*)`), as
localtools.denied does for the deny list."""
from __future__ import annotations

import fnmatch
import os
import re

_RULE = re.compile(r"^([A-Za-z0-9_*?]+)(?:\((.*)\))?$")  # the tool position takes a glob (`mcp__*`)
_PATH_TOOLS = ("Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep")
_SPLIT = re.compile(r"\n|;|&&|\|\||\||\$\(|`")  # as localtools splits a pipeline or list


def parse(rule: str) -> tuple[str, str | None] | None:
    """(tool, specifier) of a rule; the specifier is None for a bare tool name (or `*`)."""
    m = _RULE.match(str(rule or "").strip())
    if not m:
        return None
    tool, spec = m.group(1), m.group(2)
    if spec is not None and spec.strip() in ("", "*"):
        spec = None
    return tool, spec


def matches(rule: str, tool: str, tool_input: dict | None, workdir: str = "") -> bool:
    """Whether one rule covers this call. `workdir` resolves relative path globs."""
    parsed = parse(rule)
    if parsed is None:
        return False
    want, spec = parsed
    if not fnmatch.fnmatchcase(tool, want):  # the tool position takes a glob too (`mcp__*`)
        return False
    if spec is None:
        return True
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        from vision.localtools import denied

        command = str(args.get("command") or "")
        # Claude Code's newer `prefix *` reads as the `prefix:*` the deny list has always used.
        spec = spec[:-2] + ":*" if spec.endswith(" *") and not spec.endswith(":*") else spec
        if spec.endswith(":*"):
            return denied(command, [f"Bash({spec})"]) is not None
        return any(seg.strip().lstrip("({ ").strip() == spec for seg in _SPLIT.split(command))  # exact: that command, in any segment
    if tool in _PATH_TOOLS and ":" not in spec:
        path = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")
        return _path_matches(path, spec, workdir)
    if ":" in spec:
        param, _, value = spec.partition(":")
        if tool == "WebFetch" and param == "domain":
            from urllib.parse import urlparse

            host = urlparse(str(args.get("url") or "")).hostname or ""
            return host == value or host.endswith("." + value)
        given = args.get(param)
        if given is None:
            return False
        given = given if isinstance(given, str) else str(given)
        return given.startswith(value[:-1]) if value.endswith("*") else given == value
    first = next((v for v in args.values() if isinstance(v, str)), "")
    return first == spec


def _path_matches(path: str, spec: str, workdir: str) -> bool:
    if not path:
        return False
    full = os.path.abspath(os.path.join(workdir or os.getcwd(), os.path.expanduser(path)))
    candidates = {path, full, os.path.basename(full)}
    rel = os.path.relpath(full, workdir or os.getcwd())
    if not rel.startswith(".."):
        candidates.update({rel, "./" + rel})
    spec = spec[2:] if spec.startswith("./") else spec
    specs = {spec, os.path.expanduser(spec)}
    if "**" in spec:  # `docs/**` also means the directory's direct files, as Claude Code reads it
        specs.add(spec.replace("/**", "/*"))
    return any(fnmatch.fnmatchcase(c, s) for c in candidates for s in specs)


def first_match(rules: list[str] | None, tool: str, tool_input: dict | None, workdir: str = "") -> str | None:
    """The first rule the call falls under, or None."""
    for rule in rules or []:
        if matches(rule, tool, tool_input, workdir):
            return rule
    return None


def summary(tool: str, tool_input: dict | None) -> str:
    """One line for the approval form: the command for Bash, the path for a file tool, and so on."""
    from vision.brain import tool_detail

    args = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        detail = str(args.get("command") or "").strip()  # the command itself, not the model's description of it
        if detail:
            return detail if len(detail) <= 400 else detail[:399] + "…"
    detail = tool_detail(tool, args)
    return f"{tool}: {detail}" if detail else tool


# The form Vision shows before a call an ask rule caught (and the local brain's equivalent).
ASK_QUESTION = {
    "header": "Approve",
    "multiSelect": False,
    "options": [
        {"label": "Yes", "description": "run it"},
        {"label": "Yes, for the rest of this turn", "description": "and anything else this rule matches until the reply ends"},
        {"label": "No", "description": "the model is told you declined and carries on without it"},
    ],
}
YES_FOR_TURN = "Yes, for the rest of this turn"


def ask_question(tool: str, tool_input: dict | None, rule: str | None) -> dict:
    """The approval form for one call: the rule that caught it (if one did) and what it would do."""
    why = f"`{rule}` asks first" if rule else "this needs your say-so"
    return {**ASK_QUESTION, "question": f"Run this? ({why})\n\n{summary(tool, tool_input)}"}


def declined_message(tool: str, tool_input: dict | None) -> str:
    return (f"The user declined to run this: {summary(tool, tool_input)}. Do not retry it or work around it; "
            "carry on without it, or ask what they would prefer.")


def gate(brain, tool: str, tool_input: dict | None, rule: str | None) -> tuple[bool, str] | None:
    """What a scout's rules (projects.py) settle about a call that asks first, before anyone is asked:
    (True, "") to run it, because the user approved this very call (`brain.allowed_once`, used up as it runs)
    or because it's an `rm -r` that stays inside the brain's own folder; (False, message) when the brain
    files an approval request instead of asking now (`brain.on_approval`, set for threads and for check-ins,
    when the user isn't there). None: ask as usual."""
    from vision.projects import RM_RULES, inside_rm

    args = tool_input if isinstance(tool_input, dict) else {}
    allowed = getattr(brain, "allowed_once", None)
    call = summary(tool, args)
    if allowed and call in allowed:
        allowed.remove(call)
        if (used := getattr(brain, "on_allowed_used", None)) is not None:
            used(call)
        return True, ""
    if rule in RM_RULES and inside_rm(str(args.get("command") or ""), getattr(brain, "workdir", "") or ""):
        return True, ""
    ask = getattr(brain, "on_approval", None)
    if ask is None:
        return None
    try:
        approval = ask(tool, args, rule)
    except Exception:  # noqa: BLE001  (no approval could be filed: refuse it, as a worker always was)
        approval = None
    if not approval:
        return False, (f"{tool} needs the user's approval, which couldn't be asked for: carry on without it, and say what "
                       "you would have run.")
    return False, (f"Running this needs the user's approval ({rule or 'it asks first'}), so Vision has asked them "
                   f"(approval {approval}): {call}. Don't retry it or work around it. Carry on with the rest of the work; "
                   "if nothing else can be done without it, finish and say you're waiting on that approval. Once they "
                   "approve, you'll be told to run it.")
