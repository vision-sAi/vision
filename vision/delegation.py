"""The data contract between the voice conversation and its silent CLI worker."""
from __future__ import annotations

import json


TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": "string", "minLength": 1},
        "context": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        # Optional: only when the user names a brain or an effort for the work ("get Opus to do it").
        "model": {"type": ["string", "null"]},
        "effort": {"type": ["string", "null"]},
    },
    "required": ["objective", "context", "constraints", "success_criteria"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "failed", "needs_input"]},
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "question": {"type": ["string", "null"]},
        # Optional, for project threads (runs.py): what later work must know (into the project memory),
        # and files made for the user to look at (into the project's Library).
        "decisions": {"type": "array", "items": {"type": "string"}},
        "artifacts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "changes", "checks", "findings", "question"],
    "additionalProperties": False,
}


def _validate(data: object, schema: dict) -> dict:
    if not isinstance(data, dict) or not set(schema["required"]) <= set(data) <= set(schema["properties"]):
        raise ValueError("missing or unexpected fields")
    for key, spec in schema["properties"].items():
        if key not in data:
            continue  # an optional field
        value = data[key]
        kind = spec["type"]
        if kind == "array":
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            valid = isinstance(value, str) or (isinstance(kind, list) and "null" in kind and value is None)
        if not valid or ("enum" in spec and value not in spec["enum"]):
            raise ValueError(f"invalid {key}")
        if spec.get("minLength") and not value.strip():
            raise ValueError(f"empty {key}")
    return data


def validate_task(data: object) -> dict:
    """A task from the voice model; `model`/`effort`, when given, are normalised to catalogue names
    ("Opus" → "opus", "grok" → "grok-4.6") and an unknown one fails the task rather than quietly
    running on the default brain."""
    from vision.models import EFFORT_WORDS, find

    task = _validate(data, TASK_SCHEMA)
    model = task.get("model")
    if model is not None:
        info = find(model.strip().lower())
        if info is None:
            raise ValueError(f"unknown model {model!r}")
        task["model"] = info.alias
    effort = task.get("effort")
    if effort is not None:
        effort = effort.strip().lower()
        if effort not in EFFORT_WORDS:
            raise ValueError(f"unknown effort {effort!r}")
        task["effort"] = effort
    return task


def worker_choice(task: dict) -> tuple[str | None, str | None]:
    """(model, effort) the task asks for, None where the session's worker settings apply."""
    return task.get("model") or None, task.get("effort") or None


def worker_task(task: dict) -> dict:
    """The task as the worker sees it: the brain choice is Vision's business, not the worker's."""
    return {k: v for k, v in task.items() if k not in ("model", "effort")}


def failure(summary: str) -> dict:
    return dict(status="failed", summary=summary, changes=[], checks=[], findings=[], question=None)


def task_result(turn) -> dict:
    if turn.is_error:
        return failure(turn.error or "The worker could not finish the task.")
    try:
        data = turn.data if turn.data is not None else json.loads(turn.text)
        return _validate(data, RESULT_SCHEMA)
    except (ValueError, TypeError):
        # Do not mistake free-form commentary for a successful result or retry a task that may
        # already have changed files. The conversational layer explains the missing confirmation.
        return failure("The worker returned an invalid result. Its actions are unconfirmed; do not repeat the task automatically.")


def _approval_notes(rules: list[str]) -> str:
    return (
        "\nSome commands are denied until the user approves them for this run: "
        + ", ".join(rules)
        + ". If the task needs one, do not work around the denial; return needs_input with a question that "
        "names the exact command and why it is needed. If the user says yes, that command is allowed when "
        "the answer arrives (`granted` lists what was lifted; `still_denied` what was not)."
    )


def _vision_runs_tools(provider: str) -> bool:
    from vision.providers import cap

    return bool(cap(provider, "vision_runs_tools", False))


def worker_prompt(cfg, workdir: str, provider: str, sandbox: str = "", approval: list[str] | None = None) -> str:
    """The worker's system prompt. `approval` names the deny rules the user can lift for this run
    (the supervisor's approval-only commands), so the worker asks instead of giving up."""
    from vision.folderprompt import for_brain
    from vision.memory import prompt_section
    from vision.persona import _tool_notes

    folder = for_brain(workdir, cfg)
    approval = approval if approval is not None else list(getattr(cfg, "approval_rules", None) or [])

    return (
        "You are Vision's silent task worker. A separate conversation model speaks to the user. "
        "Complete only the supplied structured task, respecting its constraints and success criteria. "
        "Do not chat, narrate progress, adopt a voice persona, or address the user. Use tools as needed. "
        "Return ONLY one JSON object matching this schema, with no markdown or commentary:\n"
        + json.dumps(RESULT_SCHEMA)
        + "\nReport concrete findings, actual changes and checks with their outcomes. Never claim an "
        "unrun check passed. Use blocked or needs_input when you cannot finish; put any needed question "
        "in question. Permission and plan approvals still require the user's explicit response.\n"
        + _tool_notes(cfg.allowed_tools, workdir, provider, sandbox, cfg.denied_tools, cfg.mode)
        + "\n" + prompt_section()
        + ("\n" + folder + "\n" if folder else "")
        + "\nFor this worker session, put all explanations, progress, memory-save notices and questions "
        "in the result fields; never speak to the user. For clarifications return needs_input and "
        "question instead of using AskUserQuestion. Plan approval forms are still allowed; open the plan "
        "with one plain sentence saying what you will do, since that sentence is read aloud to the user. "
        "The user's answer to a needs_input question arrives in this same session as a message "
        '{"type": "vision_task_answer", "answer": ...}: carry on with the original task from where you '
        "stopped and return the result object again; never start the task over. "
        'A message {"type": "vision_task_update", "message": ...} is the user speaking while you work: '
        "if it changes what they want (stop, skip, narrow, add), adjust the same task; otherwise carry on. "
        "Other agents may be working alongside you, so an update about separate work (another task, "
        "another agent) is not yours: leave it to Vision and carry on with your own. "
        'A message {"type": "vision_peer_started", "objective": ...} means another agent has just started '
        "work in this same folder: keep off the files that work is likely to change, re-read any shared file "
        "just before you edit it, and don't build, install or run the app at the same time as it; then carry on. "
        "Your result always covers the whole task, never just the update. "
        'A message {"type": "vision_task_continue", "message": ...} after you have returned a result is more '
        "work on the same task from the user: do it with what you already know and return a new result "
        "object covering the follow-up. "
        'A message {"type": "vision_task_resume", ...} means Vision restarted mid-task: check what you had '
        "already done, finish the task, and return the result. "
        "If you start sub-agents, run them in the foreground with run_in_background false; several started "
        "in one message still run side by side, and their reports come back to you before you reply. "
        "Return the result only once every sub-agent has finished, and report only what they actually "
        "returned; a sub-agent you did not hear back from is unconfirmed work, not a finding."
        + (_approval_notes(approval) if approval else "")
        + (f"\n{thread}" if (thread := getattr(cfg, "thread_note", "")) else "")
        + ("\nWork in two steps. First call your tools to actually look and act: the task is about the real "
           "files and system, and a result written from imagination is a failure. Only when the tool "
           "results are in do you reply, and that reply is the JSON object alone. Findings, changes and "
           "checks must each come from a tool result you received in this session."
           if _vision_runs_tools(provider) else "")
    )


def build_task(request: str, *, agent_label: str, effort: str, workdir: str, mode: str, channel: str,
               recent: list[str] | None = None, coding_context: str = "", approval: list[str] | None = None,
               constraints: list[str] | None = None) -> dict:
    """The task the supervisor gives a launched agent for a routed request: the user's own words as
    the objective, the conversation around it as context, and the standing requirements. No plan is
    invented here; the agent inspects the project and makes its own."""
    lines = [f"Channel: the user is {'speaking' if channel == 'voice' else 'typing'} to Vision, a personal assistant; "
             "a separate conversation model relays your result in a sentence or two, so the result fields must carry the facts.",
             f"Selected brain: {agent_label} at {effort or 'default'} effort (chosen by Vision's router; not your concern to change).",
             f"Workspace: {workdir} (Vision's mode is {mode}: {'change files freely, no approvals' if mode == 'auto' else 'read-only until the plan is approved'})."]
    if approval:
        lines.append("Needs the user's explicit approval (return needs_input naming the command): " + ", ".join(approval) + ".")
    if recent:
        lines.append("Recent conversation, oldest first:\n" + "\n".join(f"- {r}" for r in recent[-6:]))
    if coding_context:
        lines.append("Earlier typed work in this session:\n" + coding_context[-4000:])
    return {
        "objective": request.strip(),
        "context": "\n".join(lines),
        "constraints": list(constraints or []) + [
            "Do exactly what the request asks, no more; ask (needs_input) if something essential is missing rather than guessing.",
            "Inspect the project before changing it; make your own plan from what you find.",
            "Never claim a check passed that you did not run.",
        ],
        "success_criteria": [
            "The requested deliverable exists and works, verified by the appropriate check (tests, a run, a read-back).",
            "changes lists every file touched; checks lists every check with its outcome; findings carries anything the user must know.",
        ],
    }
