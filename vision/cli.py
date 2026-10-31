"""Vision command-line interface."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from vision import __version__
from vision.brain import agent_frame, context_figure, tool_frame
from vision.checklist import TODO_TOOLS, checklist_of
from vision.buddy import Buddy
from vision.warmup import WarmupProgress, warm_voice
from vision import usage as usage_ui
from vision import clis
from vision.config import (
    CONFIG_PATH,
    LLAMA_DIR,
    LLAMA_URLS,
    MLX_TTS_TIERS,
    MODELS_DIR,
    MODES,
    ORPHEUS_MODEL_URL,
    QWEN_TTS_BASE,
    QWEN_TTS_DESIGN,
    SNAC_MODEL_URL,
    VOICE_DESIGNS,
    VOICES_DIR,
    BrainConfig,
    Config,
    load_config,
    save_brain_defaults,
    save_voice_default,
    save_input_device,
    save_wake_enabled,
    saved_voices,
    voice_choices,
    voice_dir,
)
from vision.install import setup_hint, voice_backend
from vision.models import THINKING_OFF, coerce_effort, effort_choices, model_label, provider_default, provider_for, provider_label, replace_retired_models, supports_effort
from vision.providers import cap
from vision.providers import names as provider_names
from vision.planvoice import plan_question
from vision.reply import status_label
from rich.markup import escape
from typer import rich_utils as _rich_help
from vision.theme import ACCENT_HI, DANGER, DIM, FAINT, LINE, RICH_THEME, WARN
from vision.ui import PLACEHOLDER, ChatScreen, ReplyView, SlashCommand, answered_grid, header_renderable, notice_grid, pick, short_path, show_header, user_grid, reply_grid, hearing_grid

app = typer.Typer(
    help="Vision: your local voice + text AI assistant, driven by Claude Code, OpenAI Codex, or Grok.",
    add_completion=True,  # `vision --install-completion` puts the commands and options on Tab in bash, zsh and fish
    no_args_is_help=False,
    rich_markup_mode="rich",
)
console = Console(theme=RICH_THEME)
NAME = "[accent.bold]Vision[/accent.bold]"

# `--help` is drawn by typer's own console, which knows nothing of RICH_THEME: its styles are module
# constants it reads at render time, so they take the palette's hexes here.
_rich_help.STYLE_OPTION = _rich_help.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {ACCENT_HI}"
_rich_help.STYLE_SWITCH = f"bold {ACCENT_HI}"
_rich_help.STYLE_NEGATIVE_OPTION = _rich_help.STYLE_NEGATIVE_SWITCH = f"bold {DIM}"
_rich_help.STYLE_TYPES = _rich_help.STYLE_TYPES_SEPARATOR = FAINT
_rich_help.STYLE_USAGE = ACCENT_HI
_rich_help.STYLE_HELPTEXT = _rich_help.STYLE_OPTION_DEFAULT = _rich_help.STYLE_ERRORS_SUGGESTION = DIM
_rich_help.STYLE_OPTION_ENVVAR = FAINT
_rich_help.STYLE_REQUIRED_SHORT = _rich_help.STYLE_DEPRECATED = _rich_help.STYLE_ABORTED = DANGER
_rich_help.STYLE_REQUIRED_LONG = DANGER
_rich_help.STYLE_DEPRECATED_COMMAND = FAINT
_rich_help.STYLE_OPTIONS_PANEL_BORDER = _rich_help.STYLE_COMMANDS_PANEL_BORDER = LINE
_rich_help.STYLE_ERRORS_PANEL_BORDER = DANGER

_EXIT_PHRASES = {"exit", "quit", "goodbye", "bye", "goodbye vision", "bye vision", "that's all", "that is all", "stop listening"}


# ---------------------------------------------------------------- helpers
RETIRED_NOTES: list[str] = []  # from the last _cfg: saved models their provider no longer lists, or can't run here


def _cfg(model: Optional[str], voice: Optional[str], effort: Optional[str] = None, *, quiet: bool = False) -> Config:
    """Load config.toml with the command-line overrides. A saved model that its provider has retired runs as
    that provider's default (models.replace_retired_models); the notes land in RETIRED_NOTES and, unless
    `quiet` (the chat screen shows them itself), are printed here."""
    cfg = load_config()
    RETIRED_NOTES[:] = replace_retired_models(cfg, brain=not model)
    if not model:
        from vision.providers import settle

        moved = settle(cfg)  # a saved model whose provider isn't set up here, or is switched off
        if moved:
            RETIRED_NOTES.append(moved)
    RETIRED_NOTES.extend(cfg.providers.notes)  # a [providers.<name>] table Vision couldn't use
    if not quiet:
        for n in RETIRED_NOTES:
            console.print(f"[warn]![/warn] {n}")
    if model:
        cfg.brain.model = model
    if effort:
        effort = effort.lower()
        if not supports_effort(cfg.brain.model, effort):
            allowed = ", ".join(v for v, _, _ in effort_choices(cfg.brain.model)) or "none"
            raise typer.BadParameter(f"{model_label(cfg.brain.model)} supports effort: {allowed}")
        cfg.brain.effort = effort
    elif model:
        cfg.brain.effort, _ = coerce_effort(cfg.brain.model, cfg.brain.effort)
    if voice:
        cfg.voice.voice = voice
    return cfg


def _brain(cfg: Config, voice_mode: bool, cont: bool, new: bool):
    from vision.brain import create_brain

    return create_brain(cfg.brain, voice_mode=voice_mode, continue_session=cont and not new)


def _turn_brain(brain, conversation, from_voice: bool, text: str = "", talk: bool = False):
    """Input origin selects the model; enabling playback never changes that routing. While a voice
    conversation is on (`talk`), typed turns go to the conversation model too, so one model answers
    the whole chat. Otherwise a typed turn goes to the front end when the router is on (`[router]
    typed`), when the message is one of its commands (/agent, /local, /search, /weather, /cancel) or
    when one was set for the next message."""
    from vision.routing import Override, parse_command

    conversation.agent = brain
    conversation.next_channel = "voice" if from_voice else "text"
    if hasattr(conversation, "follow"):
        conversation.follow()  # the voice model is the chat's own pick whenever it can be
    if from_voice or talk:
        return conversation
    rc = getattr(getattr(conversation, "cfg", None), "router", None)
    if rc is not None and getattr(rc, "mode", None) == "on" and getattr(rc, "typed", False) is True:
        return conversation
    if isinstance(getattr(conversation, "pending_override", None), Override):
        return conversation
    try:
        if text and rc is not None and parse_command(text, rc) is not None:
            return conversation
    except Exception:  # noqa: BLE001  (a malformed command: the front end reports it)
        return conversation
    # A typed turn after a call: the brain's session never saw the spoken turns, so they ride in with it.
    from vision.brain import voice_catchup

    history = getattr(conversation, "history", None)
    brain.catchup = voice_catchup(brain, history if isinstance(history, list) else [])
    return brain


def _split_model_arg(arg: str) -> tuple[str, bool]:
    """Accept the legacy 'full' suffix; model switches always carry the transcript."""
    words = arg.split()
    full = any(w.lower() == "full" for w in words[1:])
    return (words[0] if words else ""), full


def _handoff_text(brain, history: list[dict] | None) -> str | None:
    """The conversation to hand the next provider: the outgoing session as saved on disk, or the chat's
    own transcript when that has turns the session never saw (replies from the conversation model, or
    a turn that failed before the provider saved anything). None if there is nothing to carry."""
    from vision.sessions import format_transcript, session_history

    def asked(messages: list[dict]) -> int:
        return sum(1 for m in messages if m.get("role") == "user" and (m.get("text") or "").strip())

    saved = session_history(brain.provider, brain.session_id, limit=0, include_context=True) if brain.session_id else []
    chat = history or []
    return format_transcript(saved if asked(saved) >= asked(chat) else chat) or None


def _switch_model(cfg: Config, brain, model: str, voice_mode: bool, effort: str | None = None, full: bool = False,
                  history: list[dict] | None = None):
    """Apply a model (and optionally effort), replacing the driver when providers differ.

    Same-provider switches keep the existing session. Cross-provider switches carry the conversation
    as text without calling the outgoing model: the saved session, or `history` (the chat's own
    transcript, as the user saw it) when that is the fuller one. `full` is a compatibility-only argument.
    """
    from vision.brain import create_brain

    old_provider = brain.provider
    new_provider = provider_for(model)
    changed = model != cfg.brain.model
    transcript = getattr(brain, "handoff", None)
    if new_provider != old_provider:
        transcript = _handoff_text(brain, history) or transcript
    cfg.brain.model = model
    fast_model = cap(new_provider, "fast_model", "")
    if fast_model and model != fast_model and cfg.brain.fast:
        cfg.brain.fast = False  # e.g. Claude fast mode is Opus-only; /model away from Opus turns it off.
        fast_note = f"fast mode off ({provider_label(new_provider)} fast mode uses {model_label(fast_model).split(' ')[0]})"
    else:
        fast_note = ""
    if effort is not None:
        cfg.brain.effort = effort
    cfg.brain.effort, effort_note = coerce_effort(model, cfg.brain.effort)
    session_note = ""
    if new_provider != old_provider:
        getattr(brain, "close", brain.cancel)()  # its kept process goes with it
        brain = create_brain(cfg.brain, voice_mode=voice_mode)
        session_note = f"new {provider_label(new_provider)} conversation"
        session_note += " from the previous transcript" if transcript else ""
    elif changed and brain.session_id:
        session_note = "carrying the whole transcript (the new model reads it once)"
    if changed and getattr(brain, "context", None) and hasattr(brain, "context_window"):
        brain.context = (brain.context[0], brain.context_window())  # the gauge's size is the new model's
    brain.handoff = transcript
    notes = [n for n in (fast_note, effort_note, session_note) if n]
    return brain, "; ".join(notes)


def _session_stays(brain) -> bool:
    """True when the brain's conversation lives in its directory and cannot follow a /cd: Claude Code keeps
    it under ~/.claude/projects/<directory>/ and `--resume` finds it only from there (2.1.289 answers
    "No conversation found with session ID" anywhere else)."""
    return bool(brain.session_id) and bool(cap(brain.provider, "session_in_directory", False))


def _leave_conversation(brain, history: list[dict] | None) -> str:
    """/cd away from a conversation that stays with its directory: the next message starts a new one in the
    new directory, opening with the transcript so far (as a change of provider does). Returns the note."""
    transcript = _handoff_text(brain, history)
    brain.new_session()
    brain.handoff = transcript
    product = provider_label(brain.provider, cli=True)
    opening = " from the transcript so far" if transcript else ""
    return f"a new conversation{opening} ({product} keeps a conversation in its directory)"


def _apply_session(brain, info, brain_cfg=None) -> str:
    """Resume `info` on this brain. Same provider keeps the native thread; another provider
    starts a fresh session here and injects that transcript on the next message. A conversation
    that lives in its directory (session_in_directory) is continued there: the brain, and
    `brain_cfg` when given, move to it; when that directory is gone, its transcript carries
    into a new conversation here instead."""
    from vision.sessions import format_transcript, session_history

    if info.provider == brain.provider:
        if info.id == brain.session_id:
            return "already in that conversation"
        moved = ""
        home = os.path.abspath(os.path.expanduser(info.cwd)) if info.cwd else ""
        if home and cap(brain.provider, "session_in_directory", False) and home != getattr(brain, "workdir", home):
            if not os.path.isdir(home):
                summary = format_transcript(session_history(info.provider, info.id, limit=0, include_context=True)) or None
                brain.new_session()
                brain.handoff = summary
                opening = " from its transcript" if summary else ""
                return f"continuing “{info.title}”{opening} · its directory {short_path(home)} is gone · {info.short_id}"
            brain.workdir = home
            if brain_cfg is not None:
                brain_cfg.workdir = home
            moved = f" · working in {short_path(home)}"
        brain.resume(info.id)
        return f"resumed “{info.title}” · {info.age()} · {info.short_id}{moved}"
    summary = format_transcript(session_history(info.provider, info.id, limit=0, include_context=True)) or None
    brain.new_session()
    brain.handoff = summary
    src = provider_label(info.provider)
    extra = " from that transcript" if summary else ""
    return f"continuing “{info.title}” from {src}{extra} · {info.short_id}"


def _cd_target(arg: str, current: str, previous: str | None) -> str:
    """Resolve /cd's argument like a shell: '~' or '~/x' is under home, '-' is the previous directory,
    anything else is a path (relative to the current one). Must be an existing directory."""
    word = arg.strip()
    if word == "-":
        if not previous:
            raise ValueError("no previous directory yet")
        word = previous
    target = os.path.expanduser(word)
    if not os.path.isabs(target):
        target = os.path.join(current, target)
    target = os.path.normpath(target)
    if not os.path.isdir(target):
        raise ValueError(f"not a directory: {short_path(target)}" if os.path.exists(target) else f"no such directory: {short_path(target)}")
    if not os.access(target, os.R_OK | os.X_OK):
        raise ValueError(f"cannot enter {short_path(target)}")
    return target


def _cd_choices(arg: str, current: str) -> list[tuple[str, str]]:
    """/cd's menu for what has been typed: the folders inside the directory part of `arg` (relative
    to `current`, '~' expanded), each written out in full with a trailing '/' so Tab fills it and
    the menu carries straight on into it. A typed folder ending in '/' leads with itself, so Enter
    goes there rather than into its first subfolder. Hidden folders show once the name typed starts with '.'."""
    head, _, stem = ("~/" if arg == "~" else arg).rpartition("/")  # "/cd ~" Tabs into home
    base = head + "/" if head or arg.startswith("/") else ""
    rows = [("~", "home"), ("-", "previous directory"), ("..", "up one level")] if not base else []
    folder = os.path.expanduser(base or ".")
    if not os.path.isabs(folder):
        folder = os.path.join(current, folder)
    if base and not stem and os.path.isdir(folder):
        rows = [(base, "this folder")]
    try:
        names = sorted(
            (e.name for e in os.scandir(folder) if e.is_dir() and (stem.startswith(".") or not e.name.startswith("."))),
            key=str.lower,
        )
    except OSError:
        names = []
    # Narrow to the typed stem before the cap, or folders past the 200th could never be reached;
    # names starting with it lead, as the menu orders them.
    low = stem.lower()
    names = [n for n in names if n.lower().startswith(low)] + [n for n in names if low in n.lower() and not n.lower().startswith(low)]
    return rows + [(base + n + "/", "") for n in names[:200]]


def _fast_value(arg: str, current: bool) -> bool:
    """Resolve /fast's optional on/off argument; bare /fast toggles."""
    word = arg.strip().lower()
    if not word:
        return not current
    if word in ("on", "true", "1"):
        return True
    if word in ("off", "false", "0"):
        return False
    raise ValueError("/fast, /fast on or /fast off")


def _set_fast(cfg: Config, brain, enabled: bool, voice_mode: bool):
    """Apply native fast mode, including Claude Code's automatic move to Opus."""
    detail = ""
    fast_model = cap(brain.provider, "fast_model", "")
    if enabled and fast_model and cfg.brain.model != fast_model:
        brain, detail = _switch_model(cfg, brain, fast_model, voice_mode=voice_mode, full=True)
    cfg.brain.fast = enabled
    return brain, detail


def _speaker(cfg: Config):
    """The voice: the background service's when one is running here (it has the GPU), else our own."""
    from vision.servicevoice import speaker

    return speaker(cfg.voice)


def _status(msg: str):
    console.print(f"  [dim]{msg}[/dim]")


def _token_stats(usage: dict | None) -> str:
    """`in 12,345 (11,900 cached) · out 456` from one turn's usage; "" when the provider gave none.
    Claude Code counts cache reads/writes apart from `input_tokens`; Codex folds `cached_input_tokens`
    into its `input_tokens`. Both end up as: everything the model read, with the cached part noted."""
    if not usage:
        return ""
    if "cached_input_tokens" in usage:  # Codex
        total, cached = usage.get("input_tokens", 0), usage["cached_input_tokens"]
    else:  # Claude Code
        cached = usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        total = usage.get("input_tokens", 0) + cached
    out = usage.get("output_tokens", 0)
    return f"in {total:,}" + (f" ({cached:,} cached)" if cached else "") + f" · out {out:,}"


def _footer(elapsed: float, turn, cancelled: bool = False) -> str:
    """The dim line under a reply: `4.8s · in 12,345 (11,900 cached) · out 456`, with `cancelled`
    after the time when the turn was cut short (the counts are then what had arrived by that point)."""
    return " · ".join(p for p in (f"{elapsed:.1f}s", "cancelled" if cancelled else "", _token_stats(turn.usage if turn else None)) if p)


def _failed_turn_delta(text: str | None, error: str, markdown: bool = True) -> str:
    """Extra line for a failed turn, or '' if the reply already is that error.

    Claude streams session-limit (and similar) as assistant text and again as result.error;
    wrapping the same string would print it twice.
    """
    err = (error or "").strip()
    if not err:
        return ""
    body = (text or "").strip()
    wrapped = f"Vision could not answer: {err}"
    marked = f"**Vision could not answer:** {err}"
    if body in (err, wrapped, marked):
        return ""
    note = marked if markdown else wrapped
    return (f"\n\n{note}" if body else note)


def _load_voice(speaker, vc) -> None:
    """Load the voice, then make its filler clips ("Hmm.") while it is idle: they are cached on disk,
    so this costs a few seconds once per voice. A clip that fails is simply not available."""
    speaker._load()
    if vc.filler:
        speaker.prepare_fillers(vc.filler_phrases)


def _arm_filler(ss, speaker, vc) -> None:
    """Have this spoken reply say a filler if its first words are late (see StreamingSpeaker.arm_filler).
    Only clips already made are used: a voice whose fillers are not ready yet just stays quiet."""
    if not vc.filler or vc.filler_after_ms <= 0:
        return
    try:
        ss.arm_filler(speaker.fillers(vc.filler_phrases), vc.filler_after_ms / 1000)
    except Exception:  # noqa: BLE001  a filler is a nicety; never let it break a turn
        pass


def _run_turn(brain, text: str, speaker=None, markdown: bool = True, timing=None, voice_cfg=None):
    """One brain turn rendered in the transcript, optionally spoken. Returns Turn or None if cancelled.
    `voice_cfg` (a spoken request): the reply says a filler if its first words are late."""
    ss = None
    if speaker is not None:
        from vision.tts import StreamingSpeaker

        ss = StreamingSpeaker(speaker, **({"timing": timing} if timing else {}))
        if voice_cfg is not None:
            _arm_filler(ss, speaker, voice_cfg)
    started = time.monotonic()
    with ReplyView(console, markdown=markdown, gate=(lambda: ss.spoken) if ss else None) as view:

        def on_text(delta: str):
            if view.status:
                view.status = ""
            if ss:
                ss.feed(delta)  # sentence by sentence, and before the screen: the voice is what they are waiting on
            view.append(delta)

        def on_status(tool: str):
            if ss and tool:
                ss.flush()  # the text block is over: say its last sentence while the tool runs
            view.set_status(status_label(tool))

        turn = None
        try:
            turn = brain.ask(text, on_text=on_text, on_status=on_status, on_question=view.ask, on_agent=view.set_agent,
                             **({"timing": timing} if timing else {}))
        except KeyboardInterrupt:
            if ss:
                ss.stop()
            brain.cancel()
        cancelled = turn is None or turn.error == "cancelled"
        if turn is not None and turn.is_error and not cancelled:
            extra = _failed_turn_delta(turn.text, turn.error, markdown)
            if extra:
                view.append(extra)
        if ss:
            try:
                ss.finish()  # after a cancel (Ctrl-C, a cut-in) this only winds its threads down and closes the stream
            except KeyboardInterrupt:
                ss.stop()
                ss.finish()
    if timing:
        timing.event("turn_finished")
        timing.write()
    _status(_footer(time.monotonic() - started, turn, cancelled))
    return turn


# ---------------------------------------------------------------- versions / updates
def _update_cell(check: clis.UpdateCheck | None):
    """'up to date' / '2.1.280 available' / dim reason, for the versions table."""
    if check is None:
        return Text("")
    if check.available is None:
        return Text(f"? {check.error or 'unknown'}"[:60], style="dim")
    if check.available:
        return Text(f"{check.latest} available", style="warn")
    return Text("up to date", style="ok")


def _versions_renderable(
    infos: list[clis.CliInfo] | None = None, providers=clis.PROVIDERS, vision: bool = True, checks: dict[str, clis.UpdateCheck] | None = None, check: bool = True
):
    """Vision's own version and the provider CLIs it drives (all three by default), as one small table.
    With `check`, an update column says whether a newer release is out (given `checks`, or looked up)."""
    infos = clis.cli_versions(providers) if infos is None else infos
    if check and checks is None:
        checks = {i.provider: clis.check_update(i.provider) for i in infos if i.ok}
    t = Table(header_style="table.header", show_edge=False, border_style=LINE)
    t.add_column("tool")
    t.add_column("version")
    if check:
        t.add_column("update")
    t.add_column("where", style="dim")
    if vision:
        t.add_row("Vision", __version__, *([""] if check else []), short_path(str(Path(__file__).resolve().parent)))
    for i in infos:
        if i.ok:
            t.add_row(i.label, i.version, *([_update_cell((checks or {}).get(i.provider))] if check else []), short_path(i.path or ""))
        else:
            t.add_row(i.label, Text("missing", style="err"), *([""] if check else []), Text(i.error or "", style="err"))
    return t


def _update_summary(results: list[clis.UpdateResult]) -> str:
    """One line per CLI, coloured by outcome, for the chat transcript or the terminal."""
    lines = []
    for r in results:
        if r.error or not r.ok:
            lines.append(f"[err]✗[/err] {escape(r.summary())}")
        elif r.changed:
            lines.append(f"[ok]✓[/ok] {escape(r.summary())}")
        else:
            lines.append(f"[dim]·[/dim] {escape(r.summary())}")
    return "\n".join(lines)


@app.command()
def version(
    which: Optional[list[str]] = typer.Argument(None, help="all (default), or any of vision, claude, codex, grok."),
):
    """Show Vision's version and the Claude Code, Codex and Grok CLI versions (same as --version)."""
    try:
        show_vision, providers = clis.parse_version_arg(" ".join(which or []))
    except ValueError as e:
        raise typer.BadParameter(str(e))
    console.print(_versions_renderable(providers=providers, vision=show_vision))


@app.command()
def update(
    provider: Optional[list[str]] = typer.Argument(None, help="all (default), or any of claude, codex, grok."),
):
    """Update the provider CLIs (Claude Code, Codex, Grok) with their own updaters."""
    try:
        which = clis.parse_update_arg(" ".join(provider or []))
    except ValueError as e:
        raise typer.BadParameter(str(e))
    results = []
    for p in which:
        console.rule(f"[accent]{provider_label(p, cli=True)}[/accent]")
        results.append(clis.update_cli(p, stream=True))
    console.rule()
    console.print(_update_summary(results))
    if any(not r.ok for r in results):
        raise typer.Exit(1)


# ---------------------------------------------------------------- usage
_USAGE_LINE = re.compile(r"^(?P<label>[^:]+):\s+(?P<pct>\d+)% used(?:\s+·\s+resets\s+(?P<reset>.+))?$")


def _bar(pct: float) -> str:
    return usage_ui.bar(pct)


def _usage_renderable(text: str, full: bool = False):
    """Claude Code's /usage report (session, week, per-model such as Fable) as bars."""
    t = usage_ui.usage_table("Claude")
    rest = []
    for line in text.splitlines():
        m = _USAGE_LINE.match(line.strip())
        if m:
            label = m.group("label")
            if label == "Current week (all models)":
                label = "Current week"
            elif label == "Current week (Fable)":
                label = "Fable"
            usage_ui.add_window(t, label, float(m.group("pct")), (m.group("reset") or "").replace(" (America/New_York)", "") or "?")
        else:
            rest.append(line)
    usage_ui.add_banked(t, usage_ui.claude_banked())
    parts = [t]
    if full:
        parts.append(Text("\n".join(l for l in rest if l.strip() and not l.startswith("You are currently")), style="dim"))
    return usage_ui.usage_group(*parts)


def _render_usage_text(text: str, full: bool = False) -> None:
    console.print(_usage_renderable(text, full))


def _usage_fallback_renderable(usage: dict | None):
    """Fallback using the rate-limit event attached to the last reply."""
    if not usage:
        return Text("No usage data yet. Ask Vision something first, or run `vision usage`.", style="warn")
    info, at = usage.get("info", {}), usage.get("at", 0)
    wins = info.get("unifiedWindows") or {}
    t = usage_ui.usage_table("Claude")
    labels = {"five_hour": "Current session", "seven_day": "Current week", "seven_day_overage_included": "Current week (incl. extra usage)"}
    for key, w in wins.items():
        usage_ui.add_window(t, labels.get(key, key), float(w.get("utilization") or 0) * 100, w.get("resetsAt"))
    usage_ui.add_banked(t, usage_ui.claude_banked())
    return usage_ui.usage_group(t, usage_ui.footer("from the last reply's rate-limit event", at or None))


def _render_usage(usage: dict | None, refreshed: bool):
    console.print(_usage_fallback_renderable(usage))


def _usage_for(brain, full: bool = False):
    if not cap(brain.provider, "usage_from_text", False):
        return brain.usage_renderable(full)
    text = brain.usage_report()
    return _usage_renderable(text, full) if text else _usage_fallback_renderable(brain.last_usage or brain.cached_usage())


_USAGE_PROVIDERS = provider_names(has_usage=True)  # each one's throwaway brain runs models.provider_default


def _split_usage_arg(arg: str) -> tuple[str | None, bool]:
    """'/usage all full' → ("all", True); '/usage' → (None, False). Unknown words raise ValueError."""
    provider, full = None, False
    for w in arg.split():
        w = w.lower()
        if w == "full":
            full = True
        elif w == "all" or w in _USAGE_PROVIDERS:
            provider = w
        else:
            raise ValueError(f"/usage takes all, {', '.join(_USAGE_PROVIDERS)} (and 'full'), not '{w}'")
    return provider, full


def _usage_brain(cfg: Config, brain, provider: str | None):
    """The current brain, or a throwaway one for another provider (resuming its last thread, so
    Codex can read its rollout) without switching what answers the chat."""
    if brain is not None and (provider is None or provider == brain.provider):
        return brain
    from dataclasses import replace

    from vision.brain import create_brain

    return create_brain(replace(cfg.brain, model=provider_default(provider)), continue_session=True)


def _usage_selection(cfg: Config, brain, provider: str | None, full: bool = False):
    """Render one provider, or every provider in a stable order for `/usage all`."""
    if provider != "all":
        return _usage_for(_usage_brain(cfg, brain, provider), full)

    parts = []
    for name in _USAGE_PROVIDERS:
        try:
            parts.append(_usage_for(_usage_brain(cfg, brain, name), full))
        except Exception as e:  # one unavailable CLI should not hide the other providers
            parts.append(Text(f"{provider_label(name)} usage unavailable: {e}", style="dim"))
        parts.append(Text(""))
    return usage_ui.usage_group(*parts[:-1])


def _show_usage(cfg: Config, brain, provider: str | None = None, full: bool = False) -> None:
    label = "all provider" if provider == "all" else provider_label((provider or brain.provider), cli=True)
    with console.status(f"[dim]reading {label} usage…[/dim]"):
        r = _usage_selection(cfg, brain, provider, full)
    console.print(r)


@app.command()
def usage(
    provider: str = typer.Argument("", help="all, claude, codex or grok (default: the configured model's provider)."),
    full: bool = typer.Option(False, "--full", help="Also show what has been contributing to usage."),
):
    """Show subscription usage for a provider."""
    cfg = load_config()
    try:
        which, _ = _split_usage_arg(provider)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    brain = _brain(cfg, voice_mode=False, cont=True, new=False)
    _show_usage(cfg, brain, which, full)


# ---------------------------------------------------------------- default: chat
@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    speak: bool = typer.Option(False, "--speak", "-s", help="Read replies aloud."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Claude alias, Codex slug, or Grok id."),
    effort: Optional[str] = typer.Option(None, "--effort", help="Reasoning effort (model-dependent)."),
    voice: Optional[str] = typer.Option(None, "--voice", "-v", help="Voice preset, name, or blend."),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the last Vision conversation."),
    wake: bool = typer.Option(False, "--wake", "-w", help="Listen for Vision's name from the start (config: wake.enabled)."),
    join: Optional[str] = typer.Option(None, "--join", "-j", help="Join a chat open on the phone (a chat id or prefix; `vision serve` must be running)."),
    version: bool = typer.Option(False, "--version", help="Show version and exit."),
):
    """Start an interactive text chat with Vision (default when no command is given)."""
    if version:
        console.print(_versions_renderable())
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    chat(speak=speak, model=model, effort=effort, voice=voice, cont=cont, wake=wake, join=join)


def _model_label(brain) -> str:
    """Name of the model actually in use, e.g. 'Opus 5'."""
    return model_label(brain.resolved_model()) or brain.cfg.model


def _effort_word(effort: str) -> str:
    """How an effort reads in a status line: the level, "thinking off" for a local model's off switch
    (a bare "off" next to a model name reads as the model being off), "effort off" when there is none."""
    if effort == THINKING_OFF:
        return "thinking off"
    return effort or "effort off"


def _brain_summary(cfg: Config, brain, sep: str = " · ") -> str:
    """'Opus 5 · high · fast': the model actually in use, effort and optional speed tier."""
    parts = [_model_label(brain), _effort_word(cfg.brain.effort)]
    if cfg.brain.fast:
        parts.append("fast")
    return sep.join(parts)


def _voice_summary(cfg: Config, sep: str = " · ") -> str:
    """'Sonnet 4.6 · low': the conversation model that answers what you say ([conversation])."""
    model = cfg.conversation.model
    return sep.join([model_label(model) or model, _effort_word(cfg.conversation.effort)])


def _corner_label(cfg: Config, brain, quota: dict | None = None) -> str:
    """'Sonnet 5 (high) · fast · 38% left': the model typed input goes to, in the input box's bottom-right
    border like the Grok CLI's `Sonnet 5 (high)`. Effort off leaves the parenthesis out; the quota
    (failover.quota, from what turns have reported) is left out until one is known."""
    from vision.failover import corner_text

    label = _model_label(brain)
    if cfg.brain.effort:
        label += f" ({_effort_word(cfg.brain.effort)})"
    if cfg.brain.fast:
        label += " · fast"
    left = corner_text(quota)
    if left:
        label += f" · {left}"
    return label


def _corner_fragments(cfg: Config, brain, quota: dict | None) -> str | list[tuple[str, str]]:
    """The corner label for the frame: a plain string, or fragments with the quota figure in amber
    once it is at or under [brain].quota_warn_pct."""
    from vision.failover import corner_text, low
    from vision.theme import WARN

    label = _corner_label(cfg, brain, quota)
    if not low(quota, cfg.brain.quota_warn_pct):
        return label
    head = label[: -len(corner_text(quota))]
    return [("class:frame.corner", head), (f"class:frame.corner fg:{WARN}", corner_text(quota))]


def _active_summary(cfg: Config, brain, talk: bool, sep: str = " · ") -> str:
    """What answers the next turn: the brain, or while talk is on the conversation model, which then
    takes typed turns as well as spoken ones (the brain is its worker)."""
    return _voice_summary(cfg, sep) if talk else _brain_summary(cfg, brain, sep)


FACTORY_MODEL, FACTORY_EFFORT = BrainConfig.model, BrainConfig.effort  # Opus 5 · medium


def _saved_defaults() -> tuple[str, str]:
    saved = load_config().brain
    return saved.model, saved.effort


def _save_defaults(cfg: Config, model: str, effort: str) -> str:
    """Persist model + effort as Vision's defaults and apply them to this session. Returns a note."""
    effort, note = coerce_effort(model, effort)
    save_brain_defaults(model, effort)
    cfg.brain.model, cfg.brain.effort = model, effort
    out = f"default saved → {model_label(model) or model} · {effort or 'effort off'}"
    return f"{out}; {note}" if note else out


ROUTE_COMMANDS = ("agent", "opus", "codex", "local", "search", "weather", "cancel")  # the front end's commands (vision/routing.py)

HELP_TEXT = (
    "[bold]/model[/bold] pick Claude, Codex or Grok for this session (/model sonnet; /model grok-4.6; /model default → saved model + effort; "
    "same provider: keeps the whole transcript in the existing session; "
    "crossing providers: Vision carries the previous transcript from disk) · "
    "[bold]/effort[/bold] pick a model-supported effort · "
    "[bold]/fast[/bold] toggle faster, higher-usage inference (/fast on or /fast off) · "
    "[bold]/default[/bold] choose and save the default model and effort (/default reset → Opus 5 · medium)\n"
    "[bold]/speak[/bold] toggle spoken replies · [bold]/voice <name>[/bold] change voice\n"
    "[bold]/listen[/bold] say one turn with the mic · [bold]/talk[/bold] voice conversation on/off "
    "(hands-free, right here in the chat; /talk ptt for push-to-talk, Enter on an empty box to start and send; "
    "type to answer instead; Esc, /talk or “goodbye” ends it) · "
    "[bold]/wake[/bold] wake word on/off: say “Vision” whenever the chat is idle and it warms up and listens "
    "(“Vision, what's the weather?” runs straight away; it dozes off again after a quiet moment) · "
    "[bold]/mic[/bold] pick the microphone (/mic <name> or /mic default; saved to config, and a running /talk or /wake moves over) · "
    "[bold]/say <text>[/bold] speak text\n"
    "[bold]/mode[/bold] (or Shift-Tab) switch between [bold]auto[/bold] (every tool runs, no approvals; denied_tools still apply, "
    "and ask_tools patterns show a Yes/No selector first) "
    "and [bold]plan[/bold] (read-only: Vision proposes a plan and carries it out once you approve it) · shown in the box's bottom-left corner\n"
    "[bold]/cd <dir>[/bold] change the working directory for this session (/cd ~, /cd .., /cd - for the previous one; "
    "the brains work there from the next reply on; bare /cd says where you are) · "
    "[bold]/instructions[/bold] the folder's own instructions every brain reads: a .vision.md in the working directory or a folder above it "
    "(up to the repository root or home; [brain].folder_instructions names the file) · "
    "[bold]/diff[/bold] what changed in the files since the last reply started (a checkpoint of the working tree is taken before "
    "each reply in a git repository; /diff 3 compares with the third-last, /diff list shows them) · "
    "[bold]/rewind[/bold] put the files back as they were before the last reply (/rewind 3; a rewind can itself be rewound) · "
    "[bold]/new[/bold] fresh conversation · [bold]/session[/bold] pick an earlier conversation "
    "(Claude, Codex and Grok tabs; same provider resumes that thread, another provider continues it here) "
    "(/session <id> resumes one; /session id shows the current id) · "
    "[bold]/providers[/bold] choose which providers /model offers (/providers claude local to set them directly) · "
    "[bold]/usage[/bold] subscription usage (/usage all for every provider; claude, codex or grok for one; add 'full' for detail) · "
    "[bold]/version[/bold] Vision and the Claude Code, Codex and Grok CLI versions (/version codex for one) · "
    "[bold]/update[/bold] update those CLIs with their own updaters (/update all, or claude, codex, grok) · "
    "[bold]/memory[/bold] show Vision's own memory (shared by every brain; the brains add to it themselves), "
    "[bold]/memory <topic>[/bold] one topic file, [bold]/memory <words>[/bold] search them all, "
    "[bold]/memory tidy[/bold] have the brain clean it up (a copy is kept) · "
    "[bold]/remember <fact>[/bold] add a line by hand ([bold]@topic[/bold] first files it there) · [bold]/forget <text>[/bold] drop the lines containing it · "
    "[bold]/export[/bold] save this conversation as markdown (/export <file or folder>; default ~/Documents/vision/<date>-<title>.md; "
    "`vision sessions grep <words>` searches every conversation from the shell) · "
    "[bold]/review[/bold] what this chat left that is worth remembering, as a tick list (it runs by itself on /quit after a few replies; "
    "[memory] review = \"off\" stops that) · "
    "[bold]/project [goal][/bold] make this chat a project (its work runs on in threads after you leave; a fresh chat joins this folder's project) · "
    "[bold]/threads[/bold] a project chat's threads (/threads all adds closed ones) · [bold]/tell <thread> <text>[/bold] message one of them · "
    "[bold]/copy[/bold] copy the last reply's last code block to the clipboard (/copy 2 the one before it; /copy reply the whole reply) · "
    "[bold]/clear[/bold] clear the screen · [bold]/quit[/bold]\n"
    "[bold]/agent opus|codex [--effort low|medium|high|…] [request][/bold] have that agent do the request (or the next message); "
    "in words works too: “use Opus high for this”, “have Codex handle this” · "
    "[bold]/compare opus:high gpt-6-astra:medium <prompt>[/bold] the same prompt to several exact brains at once (any name /model takes, "
    "with an optional :effort), answers side by side; without a prompt the next message is compared, bare /compare ticks the models; "
    "the brains are read-only and the conversation itself never sees the turn · "
    "[bold]/local[/bold] answer with the conversation model alone (no agent) · [bold]/search[/bold] a search-only web lookup · "
    "[bold]/weather [place][/bold] · [bold]/cancel[/bold] stop the running or waiting agent · "
    "[bold]/router off|audit|on [save][/bold] the front end: off = the voice model decides delegation itself; audit = routes are logged "
    "(not shown), nothing changes; on = basic questions stay with the conversation model, the weather and current facts come as data, "
    "everything else goes to the default agent (Opus 5 · medium, high for hard tasks)\n"
    "[dim]Type / for the command menu (↑/↓ choose · Enter runs · Tab fills in · Esc hides) · "
    "Enter sends (mid-reply it queues) · Ctrl-X sends it into the running reply now (empty box: the oldest queued one) · "
    "↑ on an empty box picks a queued message: Enter edits, Del removes, Esc leaves (the queue waits meanwhile) · "
    "Ctrl-J newline · Ctrl-V pastes a picture or text · PgUp/PgDn or Shift-↑/↓ scroll (Alt-End follows again) · "
    "Esc cancels a reply · Ctrl-O unfolds the last reply's tool calls (a click unfolds one folded row) · Ctrl-D quits[/dim]"
)


def _mic_rows() -> list[tuple[str, str, str]]:
    """Input devices for the /mic menu; [] when audio can't be queried (the command itself says why)."""
    from vision.config import input_device_choices

    try:
        return input_device_choices()
    except Exception:  # noqa: BLE001
        return []


def providers_registry() -> tuple[str, ...]:
    from vision.providers import REGISTRY

    return tuple(REGISTRY)


def _usable_tabs(cfg: Config) -> list:
    """/model's tabs with only the providers that can run here (the plain pickers have no setup rows)."""
    from vision.providers import model_tabs

    return model_tabs(cfg, setup_rows=False)


def _menu_commands(cfg: Config, brain_ref: Callable[[], object]) -> list[SlashCommand]:
    """The /commands offered by the chat screen's pop-up menu, with argument choices where useful."""
    from vision.routing import agent_model

    def models():
        from vision.providers import model_tabs

        rows = [("default", "saved model + effort")]
        for tab, entries, _ in model_tabs(cfg, setup_rows=False):
            rows += [(v, f"{label} · {tab} · {desc}") for v, label, desc in entries]
        return rows

    def efforts():
        return [(v, d) for v, _, d in effort_choices(cfg.brain.model) if v]

    def sessions():
        from vision.sessions import PROVIDER_TITLES, list_all_sessions

        return [
            (s.short_id, f"{s.title} · {PROVIDER_TITLES[s.provider]} · {s.age()}")
            for s in list_all_sessions()
        ]

    return [
        SlashCommand("model", "switch model for this session", models),
        SlashCommand("providers", "which providers /model offers", lambda: [(n, "") for n in providers_registry()]),
        SlashCommand("effort", "reasoning effort for this session", efforts),
        SlashCommand("fast", "toggle faster, higher-usage inference", lambda: [("on", "use the fast service tier"), ("off", "use standard inference")]),
        SlashCommand("default", "choose and save the default model + effort", lambda: [("reset", "back to Opus 5 · medium")]),
        SlashCommand("session", "resume an earlier conversation, or join one open on the phone", sessions, aliases=("sessions", "resume")),
        SlashCommand("cd", "change the working directory (~, .., - or a path)", typed_args=lambda arg: _cd_choices(arg, brain_ref().workdir)),
        SlashCommand("instructions", "the folder's .vision.md instructions in effect, and their text"),
        SlashCommand("new", "start a fresh conversation"),
        SlashCommand("diff", "what changed since the last reply started (/diff 3: the third-last; /diff list)",
                     lambda: [("list", "the checkpoints: one per reply, newest first")]),
        SlashCommand("rewind", "put the files back as they were before the last reply (/rewind 3: before the third-last)"),
        SlashCommand("mode", "auto or plan mode (Shift-Tab toggles)", lambda: [("auto", "every tool runs, no approvals"), ("plan", "read-only until you approve the plan")], aliases=("auto", "plan")),
        SlashCommand("usage", "subscription usage", lambda: [("all", "every provider"), ("claude", "Claude subscription windows"), ("codex", "Codex subscription windows"), ("grok", "Grok weekly allowance"), ("full", "also show what has been contributing")]),
        SlashCommand("version", "Vision and the Claude Code, Codex and Grok versions", lambda: [("all", "Vision and every CLI"), ("claude", "Claude Code only"), ("codex", "Codex only"), ("grok", "Grok only"), ("vision", "Vision only")], aliases=("versions",)),
        SlashCommand("update", "update the provider CLIs with their own updaters", lambda: [("all", "Claude Code, Codex and Grok"), ("claude", "Claude Code only"), ("codex", "Codex only"), ("grok", "Grok only")]),
        SlashCommand("memory", "show Vision's own memory (shared by every brain)"),
        SlashCommand("remember", "add a fact to Vision's memory by hand"),
        SlashCommand("forget", "drop the memory lines containing some text"),
        SlashCommand("export", "save this conversation as markdown (/export <file or folder>; default ~/Documents/vision)"),
        SlashCommand("review", "what this chat left that is worth remembering: tick what to keep (runs by itself on /quit)"),
        SlashCommand("project", "make this chat a project: work that runs on in threads after you leave (/project <goal>, or the goal is what you first asked)"),
        SlashCommand("threads", "a project chat's threads and what each is doing (/threads all: closed ones too)"),
        SlashCommand("tell", "message one of a project chat's threads: /tell <thread> <text>"),
        SlashCommand("speak", "toggle spoken replies"),
        SlashCommand("voice", "change the voice, or switch it off to free the GPU (/voice off, /voice on)",
                     lambda: [("on", "load the voice and speech recognition again"), ("off", "unload them: nothing speaks or listens, the GPU is free")]
                     + voice_choices(cfg.voice)),
        SlashCommand("say", "speak some text aloud"),
        SlashCommand("listen", "say one turn with the mic"),
        SlashCommand("talk", "voice conversation on/off (Esc or “goodbye” ends it)",
                     lambda: [("ptt", "push-to-talk: Enter on an empty box starts and sends each turn")]),
        SlashCommand("mic", "choose the microphone (saved to config)", lambda: [(v or "default", d if v else "system default") for v, _, d in _mic_rows()]),
        SlashCommand("wake", "wake word on/off: say “Vision” to start talking", lambda: [("on", "listen for the name whenever idle"), ("off", "stop listening for it")]),
        SlashCommand("agent", "have an agent do the request: /agent opus --effort high <request>", lambda: [(n, f"→ {model_label(agent_model(cfg.router, n)) or '?'}") for n in cfg.router.agents], aliases=("opus", "codex")),
        SlashCommand("compare", "one prompt to several brains, answers side by side: /compare opus:high gpt-6-astra:medium <prompt>",
                     lambda: [("off", "drop an armed comparison")] + [r for r in models() if r[0] != "default"]),
        SlashCommand("local", "answer with the conversation model alone, no agent"),
        SlashCommand("search", "a search-only web lookup (results as data, no page opened)"),
        SlashCommand("weather", "the live weather (/weather <place>)"),
        SlashCommand("cancel", "stop the agent that is running or waiting for an answer"),
        SlashCommand("router", "the front end: off, audit or on (add save to keep it)", lambda: [("off", "the voice model decides delegation itself"), ("audit", "log routes, change nothing"), ("on", "enforce: basic → local, weather, search, else the default agent")]),
        SlashCommand("copy", "copy the last reply's last code block to the clipboard",
                     lambda: [("reply", "the whole last reply"), ("2", "the code block before the last")]),
        SlashCommand("clear", "clear the screen"),
        SlashCommand("help", "show the command list"),
        SlashCommand("quit", "leave Vision", aliases=("exit", "q")),
    ]


SHOWN_HISTORY = 200  # most turns of a resumed conversation replayed into the transcript


def chat(speak: bool, model: Optional[str], effort: Optional[str], voice: Optional[str], cont: bool, wake: bool = False, join: Optional[str] = None,
         talk: bool = False, ptt: bool = False):
    """The chat screen. `talk` opens it in a voice conversation (`vision talk`), push-to-talk with `ptt`."""
    from vision.brain import PLAN_QUESTION, warm_soon
    from vision.config import STATE_DIR
    from vision.conversation import VoiceConversation
    from vision.sessions import LiveTitle, session_history
    from vision.turnjournal import ChatJournal, latest_terminal_recovery, recovery_context
    import uuid

    cfg = _cfg(model, voice, effort, quiet=True)
    set_up = False
    if not join:
        from vision.onboard import first_run

        ran = first_run(cfg)  # a machine's first chat: the guided setup, then a fresh read of what it saved
        if ran is False:  # it stopped short (nothing can answer yet, or it was left halfway): no chat to open
            raise typer.Exit()
        if ran:
            cfg = _cfg(model, voice, effort, quiet=True)
            from vision import providers as _providers

            set_up = any(_providers.ready(p.name, cfg) for p in _providers.enabled(cfg))
    recovery = latest_terminal_recovery() if not (cont or join or model or effort) else None
    turn_journal = ChatJournal(recovery[0] if recovery else f"terminal-{uuid.uuid4().hex}")
    if not turn_journal.claim():  # another window opened the same crashed chat a moment ago
        recovery = None
        turn_journal = ChatJournal(f"terminal-{uuid.uuid4().hex}")
        turn_journal.claim()
    if recovery:
        from vision.brain import create_brain

        recovered = recovery[1]
        for lost_text in recovered["pending"]:
            turn_journal.append("unqueued", text=lost_text)
            turn_journal.append("steer_failed", text=lost_text)
        cfg.brain.model = recovered["model"] or cfg.brain.model
        cfg.brain.effort = recovered["effort"] or cfg.brain.effort
        sid = recovered["session_id"]
        # The local provider saves its model context only after a complete turn. Carry
        # the journal's transcript into a fresh context when it stopped mid-turn.
        if not cap(provider_for(cfg.brain.model), "keeps_partial_turns", True):
            sid = None
        brain = create_brain(cfg.brain, voice_mode=False, session_id=sid)
        if not sid:
            brain.handoff = recovery_context(recovered["history"], recovered["recovery_rows"])
    else:
        brain = _brain(cfg, voice_mode=False, cont=cont, new=False)
    conversation = VoiceConversation(cfg, agent=brain)
    state = {"speak": speak, "speaker": None, "mic": None, "stt": None, "ss": None}
    # Voice modes run on a worker thread inside the screen (see voice_loop): `talk` is on while a
    # conversation is running, `hearing` is the mic status for the status row, `typed` carries a
    # message typed while the mic was open, and `cancel` stops the current recording.
    state.update({"talk": False, "once": False, "hearing": "", "typed": None, "cancel": None, "thread": None})
    # Push-to-talk (`/talk ptt`, `vision talk --ptt`): `ptt_press` is the event Enter on an empty box sets,
    # to start recording and then to send it; None while nothing waits on it.
    state.update({"ptt": False, "ptt_press": None})
    # The wake word (see wake_loop): `wake` is the switch, `wake_thread`/`wake_cancel` the listener
    # waiting for the name; the utterance that woke it is handed to voice_loop as `woken`.
    state.update({"wake": False, "wake_thread": None, "wake_cancel": None, "wake_listener": None, "quitting": False, "update_checks": set(), "update_noted": set()})
    state.update({"compare": (), "compare_cancel": None})  # /compare: the picks armed for the next message; how to stop a running one
    # Cutting into a spoken reply by voice (`barge_in` in [listen]) uses the same small listener as the
    # wake word; `cut_in` turns False after it fails once, so a dead mic is reported once, not every turn.
    # `barge`: the cut-in listening during the reply now being spoken (a plan heard by voice lets go of it).
    state.update({"listener_lock": threading.Lock(), "cut_in": cfg.listen.barge_in != "off", "barge": None})
    state["prev_dir"] = None  # where /cd came from, for /cd -
    # Typed turns use one serial worker.  The input remains live while a reply is running, but the
    # provider CLIs still see exactly one turn at a time and therefore keep session order intact.
    import queue

    from vision.turnqueue import QueuedTurn, TurnQueue

    state.update({"turn_queue": TurnQueue(), "turn_lock": threading.Lock(), "turn_active": False, "turn_thread": None})
    state.update({"quota": None, "quota_warned": set()})  # the corner's figure, and the windows already warned about
    state.update({"brain_turns": 0, "reviewing": False})  # replies this chat has had (the memory review needs a few), and one running
    # The guess at the next message (offer_suggestion): the last turns it reads, as (session, role, text),
    # and a count that moves on with every turn, so a guess that lands after a newer one started is dropped.
    state.update({"recent": [], "suggest_gen": 0})
    # Shell mode (`!`): the command running now, and those run since the last message as (command,
    # output, exit code; None when stopped), told to the brain with the next one (brain.shell_note).
    state.update({"shell_proc": None, "shell_runs": []})
    # The running turn's driver and its `steered(text)` (splits the reply where a Ctrl-X message went
    # in). Messages sent meanwhile wait in the queued strip above the input box (vision.turnqueue).
    state.update({"driver": None, "steered": None})
    # The working tree before each brain turn (vision.checkpoints), for /diff and /rewind, and the
    # (provider, session id) they were taken in: another conversation starts from what was saved for it.
    state.update({"checkpoints": [], "cp_key": None})

    def checkpoint_list() -> list:
        """This conversation's checkpoints, oldest first. A conversation whose id is not known yet
        (its first turn) has none; one being resumed brings back the ones saved for it."""
        from vision import checkpoints

        key = (brain.provider, brain.session_id)
        if state["cp_key"] != key:
            # The first turn's checkpoint is kept once the id arrives (the previous key had no id).
            carried = state["cp_key"] is not None and state["cp_key"][1] is None and state["cp_key"][0] == key[0] and key[1]
            if not carried:
                state["checkpoints"] = checkpoints.load(*key)
            state["cp_key"] = key
        return state["checkpoints"]

    def take_checkpoint(text: str) -> None:
        """Record the working tree before a brain turn in a git repository; nothing outside one."""
        from vision import checkpoints

        cp = checkpoints.snapshot(brain.workdir, text)
        if cp is None:
            return
        cps = checkpoint_list()
        cps.append(cp)
        del cps[:-checkpoints.PER_SESSION]

    def save_checkpoints(session_id: str | None) -> None:
        from vision import checkpoints

        if state["checkpoints"]:
            checkpoints.save(brain.provider, session_id, state["checkpoints"])
            checkpoint_list()  # the key now carries the id
    # The running turn's reply is over and only background jobs (a background Bash, a Monitor) keep it
    # open: the reply is closed on screen and a message goes straight in (as with Claude Code) rather
    # than queueing behind jobs that may run for an hour. See ChatScreen.hold_reply.
    state.update({"held": False, "hold_lock": threading.RLock()})
    # `vision serve` finds this chat through its socket (link.py) and shows it on the phone as a
    # terminal chat: messages from there run here, and every turn here streams there.
    state["link"] = None
    # Joined to a chat `vision serve` runs (remote.py): `home` is the local brain (and its model,
    # effort, directory) to return to when the chat closes or /new leaves it.
    state.update({"server": None, "home": None})
    # The chat a `--join` window was opened for (the phone's "Open on computer"): it has no
    # conversation of its own, so it never lists as a terminal chat and quits when that chat closes.
    state["guest"] = None
    if not recovery:
        turn_journal.append("chat", source="terminal", model=cfg.brain.model, effort=cfg.brain.effort,
                            session_id=brain.session_id)
        turn_journal.append("base", history=session_history(brain.provider, brain.session_id or ""))

    def journal_event(kind: str, **fields):
        if state["server"] is None:  # a joined server chat has its own durable journal
            turn_journal.append(kind, **fields)

    def reset_turn_journal():
        nonlocal turn_journal
        turn_journal.append("close")
        turn_journal.release()
        turn_journal = ChatJournal(f"terminal-{uuid.uuid4().hex}")
        turn_journal.claim()
        turn_journal.append("chat", source="terminal", model=cfg.brain.model, effort=cfg.brain.effort,
                            session_id=brain.session_id)
        turn_journal.append("base", history=session_history(brain.provider, brain.session_id or ""))
    if speak and cfg.voice.enabled:
        state["speaker"] = _speaker(cfg)
        threading.Thread(target=state["speaker"]._load, daemon=True).start()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    def speaker():
        if state["speaker"] is None:
            state["speaker"] = _speaker(cfg)
        return state["speaker"]

    def voice_off() -> bool:
        """The voice switch is off ([voice] enabled, read afresh: the phone or another terminal may have flipped it)."""
        from vision.config import voice_enabled

        return not voice_enabled()

    def refuse_voice() -> bool:
        """True (and a note saying so) when the voice is switched off: nothing that speaks or listens starts."""
        if voice_off():
            note("the voice is off · /voice on turns it back on")
            return True
        return False

    def voice_power(on: bool):
        """/voice on|off: the same switch as the phone's Settings, saved as [voice] enabled and told to the background
        service, which loads or unloads its voice and ears. Off also unloads this terminal's own, ends a voice
        conversation and stops the wake word listening; spoken replies wait until it is back on."""
        from vision.config import save_voice_enabled
        from vision.servicevoice import set_service_voice

        try:
            save_voice_enabled(on)
        except Exception as e:  # noqa: BLE001
            note(f"[err]could not save config.toml: {e}[/err]")
            return
        cfg.voice.enabled = on
        told = set_service_voice(on)
        if on:
            note("voice on" + (" · the background service is loading it" if told else ""))
            start_wake()
            return
        stop_listening()
        stop_wake()

        def unload():
            for key in ("stt", "speaker"):  # Whisper first, then the voice and its GPU claim
                resource, state[key] = state[key], None
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as e:  # noqa: BLE001
                        note(f"[warn]{key} did not unload cleanly: {e}[/warn]")
            state["mic"] = None

        background(unload)
        note("voice off · nothing speaks or listens, and the GPU is free (/voice on brings it back)"
             + ("" if told is not False else " · the background service didn't answer; it frees the GPU when it next restarts"))
        screen.app.invalidate()

    def update_notice() -> None:
        """Once a session per installed CLI, whichever brain is on, a chat note line each when it has a
        newer release ('Codex 0.156.0 available · /update'). Runs on every status repaint and reads the
        cached checks; the first look at a provider starts one in the background, so nothing blocks on
        the network. The notes wait until no reply is streaming, and are posted off the repaint."""
        if screen.busy:
            return
        if not state.get("self_update_noted"):
            # Vision itself, the same way: one note a session when its branch on the remote has moved on.
            from vision import selfupdate

            own = selfupdate.peek_or_ask(on_answer=lambda _: screen.app.invalidate())
            if own is not None and own.available is not None:  # one that couldn't tell tries again once it expires
                state["self_update_noted"] = True
                if own.available:
                    background(note, own.status())
        for provider in clis.PROVIDERS:
            if provider in state["update_noted"]:
                continue
            check = clis.peek_check(provider)
            if check is not None:
                if check.available is not None:  # a failed check (offline, feed down) tries again once it expires
                    state["update_noted"].add(provider)
                if check.available:
                    background(note, check.status())
                continue
            if provider not in state["update_checks"]:
                state["update_checks"].add(provider)

                def go(provider=provider):
                    try:
                        clis.check_update(provider)
                    finally:
                        state["update_checks"].discard(provider)
                        screen.app.invalidate()
                background(go)

    def corner_label():
        if state["talk"] and not state["once"]:
            return _voice_summary(cfg, " ")
        return _corner_fragments(cfg, brain, state["quota"])

    def refresh_quota():
        """After a turn (or /usage): the corner's figure from what the provider has recorded, and once per
        window a note when it is nearly used up. Reads memory or a local file only, never the network."""
        from vision import failover

        try:
            q = failover.quota(brain.provider, failover.source(brain))
        except Exception:  # noqa: BLE001  (a figure in a border must never break a turn)
            q = None
        state["quota"] = q
        k = failover.key(q)
        if k and failover.low(q, cfg.brain.quota_warn_pct) and k not in state["quota_warned"]:
            state["quota_warned"].add(k)
            note(f"[warn]{failover.warning(provider_label(brain.provider), q, cfg.brain.quota_warn_pct)}[/warn]")
        screen.app.invalidate()

    def status_line() -> str:
        # The model that answers sits in the input box's corner (see corner_label): the brain, or the
        # conversation model while talking, which then answers typed turns too. The folder is in the
        # box's other corner and the scouts lead the row (see scout_list).
        parts = []
        if state["talk"]:
            parts.append(f"talk · {state['hearing'] or 'replying'}")
        elif state["speak"]:
            parts.append("speech on")  # off is the default and says nothing
        if cfg.router.mode != "off":
            parts.append(f"router {cfg.router.mode}")
        if state["compare"]:
            from vision.compare import armed_label

            parts.append(f"comparing next: {armed_label(list(state['compare']))}")
        update_notice()
        return "  ·  ".join(parts)

    def wake_status() -> str:  # bottom-right corner: `34% ctx  ·  wake off` (the figures the brain has)
        parts = []
        ctx = getattr(brain, "context", None)
        if ctx and ctx[1]:
            parts.append(f"{min(100, round(100 * ctx[0] / ctx[1]))}% ctx")
        parts.append("wake on" if state["wake"] else "wake off")
        return "  ·  ".join(parts)

    # The scouts (vision serve's projects) for the status row and the open card: asked for in the background
    # every SCOUT_POLL_S and read from this cache meanwhile, so a repaint never waits on the server; none
    # while no server runs.
    scouts = {"list": [], "at": 0.0, "asking": False, "known": False}

    def scout_list() -> list[dict]:
        if not scouts["asking"] and time.monotonic() - scouts["at"] > SCOUT_POLL_S:
            scouts["asking"] = True
            background(fetch_scouts)
        return scouts["list"]

    def fetch_scouts() -> None:
        try:
            got = _serve_call("/projects", why="the scouts live there")
        except (SystemExit, Exception):  # noqa: BLE001  (no server, or one that didn't answer: no scouts shown)
            got = []
        got = [p for p in got if isinstance(p, dict) and p.get("status") != "archived"] if isinstance(got, list) else []
        first = not scouts["known"]
        scouts.update(list=got, at=time.monotonic(), asking=False, known=True)
        entry = state.get("open_card")
        if first and got and entry is not None and entry in screen.entries:
            entry.renderable = open_card()  # the card was drawn before the scouts were known
            entry.invalidate()
        screen.app.invalidate()

    buddy = Buddy(cfg.buddy.name, slide_in=cfg.buddy.slide_in) if cfg.buddy.enabled else None
    screen = ChatScreen(str(STATE_DIR / "history"), status_line, buddy=buddy, mode=lambda: cfg.brain.mode, status_right=wake_status,
                        corner=corner_label)
    screen.folder_fn = lambda: short_path(brain.workdir)  # beside the title and the mode chip
    screen.scouts_fn = scout_list
    if buddy:
        buddy.speaking_fn = lambda: bool(state["ss"] and state["ss"].speaking)
    screen.commands = _menu_commands(cfg, lambda: brain)
    live_title = LiveTitle()
    screen.title_fn = lambda: live_title.get(brain.provider, brain.session_id)  # the terminal tab

    def load_arrow_history(provider: str | None = None, sid: str | None = None):
        """Up/Down follows this conversation's user turns, including a resumed session's."""
        from vision.sessions import user_turns

        p = provider or brain.provider
        s = brain.session_id if sid is None else sid
        screen.set_history(user_turns(p, s) if s else [])

    load_arrow_history()

    def note(msg: str, kind: str = "info"):
        """A system line in the transcript. A message wrapped whole in `[err]…[/err]` is an error,
        `[warn]…[/warn]` a warning: they get the `✗` / `⚠` mark instead of the info `·`. The older
        `[red]`/`[yellow]` wrappers still count, for callers outside this file."""
        for tag, k in (("err", "error"), ("red", "error"), ("warn", "warn"), ("yellow", "warn")):
            if msg.startswith(f"[{tag}]") and msg.endswith(f"[/{tag}]"):
                msg, kind = msg[len(tag) + 2 : -(len(tag) + 3)], k
                break
        screen.add(notice_grid(msg, kind))

    def open_card():
        """The open card: the buddy, Vision's version, the model, the folder, the voice and the scouts."""
        from vision.ui import scout_needs

        found = scouts["list"] if scouts["known"] else []
        return header_renderable(
            model=_active_summary(cfg, brain, state["talk"]),  # "Opus 5 · high", as in the composer's corner
            directory=short_path(brain.workdir),
            voice=cfg.voice.voice if state["speak"] else "off",
            resumed=bool(brain.session_id),
            version=__version__,
            scouts=len(found) or None,
            needing=sum(scout_needs(p) > 0 for p in found),
        )

    def paint_open():
        """The open card, first block on open and after /clear (see open_card)."""
        state["open_card"] = screen.add(open_card())

    def show_history(provider: str, sid: str, limit: int = SHOWN_HISTORY):
        """A resumed conversation's earlier turns, in the transcript where they can be scrolled, not
        only in the model's memory. The transcript is cleared first so the chat reads as one thread,
        then the open card sits above those turns."""
        from vision.sessions import session_history

        paint_turns(session_history(provider, sid, limit=limit + 1), limit)

    def paint_turns(turns: list[dict], limit: int = SHOWN_HISTORY):
        with screen._lock:
            screen.entries.clear()
        paint_open()
        if len(turns) > limit:
            turns = turns[-limit:]
            note("earlier turns not shown…")
        for turn in turns:
            if turn["role"] == "user":
                screen.add(user_grid(turn["text"]), gap_before=True)
            else:
                marks = past_marks(turn)
                if marks:
                    screen.add_past_reply(turn["text"], marks)
                else:
                    screen.add(reply_grid(turn["text"]))

    def past_marks(turn: dict) -> list[tuple[int, object]]:
        """Rebuild the rows a past reply ran, in the order they appeared beside its text."""
        from vision.brain import ToolCall, run_from_frame
        from vision.remote import RemoteAgentRun

        marks = []
        for f in turn.get("agents") or []:
            cut_off = not f.get("done", True)
            run = run_from_frame({**f, "done": True}, RemoteAgentRun(f.get("id", ""), "agent", ""))
            run.failed, run.cut_off = bool(f.get("failed")) or cut_off, bool(f.get("cut_off")) or cut_off
            run.status_text = f.get("status") or ""
            marks.append((int(f.get("at") or 0), run))
        for f in turn.get("tools") or []:
            cut_off = not f.get("done", True)
            call = ToolCall(f.get("id", ""), f.get("name", ""), f.get("detail", ""), done=True,
                            is_error=bool(f.get("is_error")) or cut_off,
                            output=f.get("output") or ("Interrupted while this tool was running" if cut_off else ""),
                            diff=f.get("diff") or [], input=f.get("input") or [])
            marks.append((int(f.get("at") or 0), call))
        return marks

    if recovery:
        paint_turns(recovery[1]["history"])
        screen.set_history([row["text"] for row in recovery[1]["history"]
                            if row.get("role") == "user" and row.get("text")])
        note("The last reply was interrupted. Check any tool changes, then type to continue.", "warn")
    elif brain.session_id:  # --continue: the picked-up conversation is on screen from the start
        show_history(brain.provider, brain.session_id)
    else:
        paint_open()
        if set_up:
            from vision.welcome import set_up_note

            note(set_up_note())

    # ---- one reply, run on a worker thread so the UI keeps drawing
    def run_turn(text: str, from_voice: bool = False, timing=None, speak_remote: bool = False, follow: bool = False,
                 voice_remote: bool = False, talk_remote: bool = False, phone: bool = False):
        """`follow`: the turn is already running on the phone chat this screen is joined to; show it
        as it streams rather than sending the message (RemoteBrain.ask knows it is that turn).
        `voice_remote`: spoken on the phone; `talk_remote`: typed during a call on the phone. Both go
        to the conversation model, like talk mode here. `phone`: sent from the phone. Phone turns
        (and followed ones) are spoken on the phone if at all, never out of this machine's speakers."""
        if from_voice and timing is None and cfg.conversation.timing:
            from vision.timing import VoiceTiming
            timing = VoiceTiming()
        link = state["link"]
        link_post = link.post if link else (lambda ev: None)
        driver = brain if follow else _turn_brain(brain, conversation, from_voice or voice_remote, text,
                                                        talk=(state["talk"] and not state["once"]) or talk_remote)
        state["driver"] = driver
        if state["shell_runs"] and driver is brain and not follow:
            from vision.brain import shell_note

            brain.shell, state["shell_runs"] = shell_note(state["shell_runs"]), []  # ahead of this message (inject_handoff)
        if cfg.brain.checkpoints and driver is brain and not follow and not joined():
            take_checkpoint(text)  # the tree before this reply, for /diff and /rewind
        state["suggest_gen"] += 1
        screen.suggestion = ""
        ss = None
        local = not (phone or follow)
        if local and (state["speak"] or (state["talk"] and not state["once"])) and not voice_off():  # a voice conversation always talks back
            from vision.tts import StreamingSpeaker

            ss = StreamingSpeaker(speaker(), timing=timing)
            if from_voice:
                _arm_filler(ss, speaker(), cfg.voice)  # "Hmm." if the first words are late
        state["ss"] = ss
        entry = screen.start_reply(markdown=not from_voice)
        entry.tokens_fn = lambda: getattr(brain, "output_tokens", 0)  # the `↓ 1.2k` on the live line (Claude Code only)
        entry.checklist_fn = lambda: checklist_of(driver).items()  # the model's task list under the live line
        if ss:
            entry.gate = lambda: ss.spoken_position  # keep the sub-character timing for smooth frames
        started = time.time()
        journal_event("start", text=text)
        # The phone mirrors this chat: one line for a model switched since the last message, the same a
        # phone-side switch gets, however many /model it took (none if it went back).
        now, noted = (cfg.brain.model, cfg.brain.effort), state.get("noted")
        if noted and now != noted:
            effort_word = _effort_word(now[1])
            link_post({"type": "note", "text": f"Switched to {_model_label(brain)} ({effort_word})"
                       if now[0] != noted[0] else f"Effort set to {effort_word}"})
        state["noted"] = now
        link_post({"type": "start", "text": text, "speak": speak_remote, "voice": bool(voice_remote)})
        if link:
            link.post_summary(force=True)

        streamed = []  # the reply as it streamed (where steered messages split it, what agents keep)
        homes = {}  # agent id → the reply entry its row lives in (a steered message splits the reply)
        tool_homes = {}  # a call that finishes after a split still belongs to its starting reply
        agent_frames = {}
        tool_frames = {}
        steers = []

        def on_text(d):
            journal_event("delta", text=d)
            streamed.append(d)
            screen.update_reply(entry, delta=d)
            if ss:
                ss.feed(d)  # sentence by sentence; a voice reply streams in as the model writes it
            link_post({"type": "delta", "text": d})

        def steered(t):
            # A message went into the running turn (Ctrl-X here, or from the phone): it sits in the
            # transcript where it went in, and the rest of the reply follows it (as Claude Code shows it).
            nonlocal entry
            journal_event("steered", text=t)
            steers.append((len("".join(streamed)), t, len(agent_frames)))
            if t in screen.sent:
                screen.sent.remove(t)  # out of the strip over the input box
            with state["hold_lock"]:
                held = state["held"]
                # Into a held turn it reads as a new turn (its own timer); mid-reply the turn goes on.
                entry = screen.split_reply(entry, user_grid(t), same_turn=not held)
                if held:
                    state["held"] = False
                    screen.release_hold()
            if ss:
                ss.flush()
            link_post({"type": "steered", "text": t})

        state["steered"] = steered

        def on_background(label):
            # The model finished with background jobs still running (label), or is at work again ("").
            nonlocal entry
            with state["hold_lock"]:
                if label:
                    if any(not r.done for r in entry.agents.values()):
                        return  # subagents at work keep their rows live in the reply (its live line counts them)
                    if state["held"]:
                        screen.set_background(label)
                        return
                    state["held"] = True
                    screen.hold_reply(entry, label)
                elif state["held"]:
                    state["held"] = False
                    entry = screen.start_reply(markdown=entry.markdown)  # it answers a job's notice: a reply of its own
                    screen.release_hold()
                    return
                else:
                    return
            if not state["turn_queue"].empty():
                steer_now("", every=True)  # what was typed while it worked goes in now, as Claude Code sends it once the reply ends

        brain.on_background = on_background

        def steer_taken(t):
            # Claude took a steered message in: into the transcript where it went in (steered).
            steered(t)

        brain.on_steer_taken = steer_taken

        def on_status(tool):
            if ss and tool:
                ss.flush()  # the text block is over: say its last sentence while the tool runs
            screen.update_reply(entry, status=status_label("" if tool in TODO_TOOLS else tool), force=True)  # the checklist shows a todo call
            link_post({"type": "status", "tool": tool, "label": status_label(tool)})

        def on_question(questions):
            nonlocal entry
            journal_event("question", questions=questions)
            screen.update_reply(entry, status="waiting for your answer…", force=True)
            if buddy:
                buddy.listening()
            plan = plan_question(questions) if ss and state["talk"] and not state["once"] and state["mic"] is not None else None
            closed = threading.Event()

            def opened():
                # Only once the form is up, so the summary says `waiting`; the phone drops its copy of the
                # form when a later summary says it no longer is (answered here).
                link_post({"type": "question", "questions": questions})
                if link:
                    link.post_summary(force=True)
                if plan:
                    background(hear_plan_answer, plan, closed, ss)  # a voice conversation answers it out loud

            try:
                answers = screen.ask_questions(questions, on_open=opened)  # answered here or on the phone, whichever is first
            finally:
                closed.set()
            journal_event("answered", questions=questions, answers=answers)
            link_post({"type": "answered", "questions": questions, "answers": answers})  # the phone shows it in the reply
            if link:
                link.post_summary(force=True)
            # What was chosen goes into the transcript as a block of its own (as in Claude Code), and
            # what the answer leads to (the plan being carried out, the next step) is a fresh reply.
            blocks = [answered_grid(questions, answers)]
            if questions == [PLAN_QUESTION] and (answers or {}).get(PLAN_QUESTION["question"]) == "Yes":
                blocks.append(Text.from_markup("plan approved → [warn]auto mode[/warn] (Shift-Tab or /mode plan to plan again)"))
            entry = screen.split_reply(entry, *blocks)
            return answers

        def on_agent(run):
            frame = agent_frame(run)
            frame["at"] = agent_frames.get(run.id, {}).get("at", len("".join(streamed)))
            from vision.turnjournal import compact_agent_frame

            journal_event("agent", frame=compact_agent_frame(frame, agent_frames.get(run.id)))
            if run.id in entry.agents:  # still at work when a question or steer split the reply: it moved down
                homes[run.id] = entry
            home = homes.setdefault(run.id, entry)
            if home is entry:
                screen.update_reply(entry, agent=run)
            else:  # it finished before a question or steered message split the reply: its row stays up there
                home.invalidate()
                screen.app.invalidate()
            agent_frames[run.id] = frame
            link_post(dict(frame))

        def on_tool(call):
            frame = tool_frame(call)
            frame["at"] = tool_frames.get(call.id, {}).get("at", len("".join(streamed)))
            journal_event("tool", frame=frame)
            if call.id in entry.tools:  # still running when a question or steer split the reply: it moved down
                tool_homes[call.id] = entry
            home = tool_homes.setdefault(call.id, entry)
            if home is entry:
                screen.update_reply(entry, tool=call)
            else:
                home.place(call)
                home.invalidate()
                screen.app.invalidate()
            tool_frames[call.id] = frame
            link_post(dict(frame))  # the phone draws it as a row too

        def on_mode_change(_mode):
            screen.app.invalidate()  # the badge
            if link:
                link.post_summary(force=True)

        brain.on_mode_change = on_mode_change
        turn = None
        cancelled = False
        error = ""
        try:
            extra = {"timing": timing} if from_voice and timing else {}
            turn = driver.ask(text, on_text=on_text, on_status=on_status, on_question=on_question, on_agent=on_agent, on_tool=on_tool, **extra)
            cancelled = turn.error == "cancelled"
            if turn.is_error:
                error = turn.error
            if turn.is_error and not cancelled:
                extra = _failed_turn_delta(turn.text, turn.error)
                if extra:
                    screen.update_reply(entry, delta=extra, force=True)
                if buddy:
                    buddy.fail()
        except Exception as e:  # noqa: BLE001
            error = str(e)
            screen.update_reply(entry, delta=f"**Error:** {e}", force=True)
            if buddy:
                buddy.fail()
        finally:
            state["driver"] = state["steered"] = None
            brain.on_background = brain.on_steer_taken = None
            untaken = brain.take_untaken_steers() if hasattr(brain, "take_untaken_steers") else []
            screen.sent.clear()
            for t in untaken:  # the turn ended before Claude took them in: they run next instead
                journal_event("steer_failed", text=t)
                enqueue_turn(t, shown=queue_or_add(t), recorded=True)
            if turn is not None or error:
                journal_event("done", text=(turn.text if turn is not None else "") or "".join(streamed),
                              error=error, session_id=(turn.session_id if turn is not None else None) or brain.session_id)
            if driver is brain and not follow and not joined():
                save_checkpoints((turn.session_id if turn is not None else None) or brain.session_id)
            if (agent_frames or tool_frames) and not joined():
                # The rows stay with the conversation (the server keeps a joined chat's itself).
                from vision import agentlog

                reply = (turn.text if turn is not None else "") or ""
                try:
                    agentlog.record_turn(brain.provider, (turn.session_id if turn is not None else None) or brain.session_id,
                                         agentlog.turn_entries(text, reply, "", "".join(streamed), list(agent_frames.values()), steers,
                                                               list(tool_frames.values())))
                except Exception:  # noqa: BLE001  (a history nicety must never break a turn)
                    pass
            link_post({
                "type": "done",
                "text": (turn.text if turn is not None else "") or "",
                "error": error,
                "busy": not state["turn_queue"].empty(),
                "session_id": (turn.session_id if turn is not None else None) or brain.session_id,
                "model": turn.model if turn is not None else None,
                "duration_ms": int((time.time() - started) * 1000),
            })
            # `cancelled` sits on the footer beside the time rather than in the reply text (the token
            # counts are not on the footer: the context figure is in the status row, /usage has the rest).
            if ss:
                try:
                    ss.finish()
                except Exception:
                    pass
            if turn is not None and not error and not follow:
                state["brain_turns"] += 1
            screen.end_reply(entry, cancelled=cancelled)
            with state["hold_lock"]:
                if state["held"]:
                    state["held"] = False
                    screen.release_hold()
            state["ss"] = None
            spoken = from_voice or voice_remote or talk_remote or (state["talk"] and not state["once"])
            elapsed = time.time() - started
            if local and not spoken and not cancelled and cfg.chat.notify_after_s and elapsed >= cfg.chat.notify_after_s and screen.maybe_unfocused:
                # A long reply done while you were in another window: a line on the desktop (a spoken
                # one is heard, a phone one is the phone's; a cancel was you).
                from vision.notify import desktop_notify, reply_summary
                from vision.ui import took

                reply = (turn.text if turn is not None else "") or "".join(streamed)
                what = f"error: {error}" if error else reply_summary(reply)
                desktop_notify("Vision", " · ".join(p for p in (f"reply ready · {took(elapsed)}", what) if p))
            offer_suggestion(text, turn, bool(error) or turn is None, spoken, link_post)
            if timing:
                timing.event("turn_finished")
                timing.write()
            audit_line(text, driver is conversation, "voice" if from_voice else "text")
            if driver is brain and not joined():
                refresh_quota()
        return turn

    def forget_suggestion():
        """The conversation changed (/new, another session, joining or leaving a phone chat): the guess was for the
        old one. Off the box now, and one still being made is dropped when it lands."""
        state["suggest_gen"] += 1
        state["recent"] = []
        screen.suggestion = ""
        screen.app.invalidate()

    def offer_suggestion(text: str, turn, failed: bool, spoken: bool, link_post):
        """After a typed reply, guess the user's next message (vision/suggest.py) and show it grey in the
        empty box, on the phone too when this chat is linked. A joined chat gets the server's guess instead."""
        if joined():
            return
        sid = (turn.session_id if turn is not None else None) or brain.session_id
        reply = (turn.text if turn is not None else "") or ""
        state["recent"] = (state["recent"] + [(sid, "user", text), (sid, "assistant", reply)])[-8:]
        if failed or spoken or not cfg.remote.suggestions or not state["turn_queue"].empty():
            return
        rows = [{"role": role, "text": t} for s, role, t in state["recent"] if s == sid]
        gen = state["suggest_gen"]

        def guess():
            from vision.suggest import suggest

            text = suggest(rows, cfg.providers.enabled)
            if text and gen == state["suggest_gen"] and not screen.busy and not state["quitting"]:
                screen.suggestion = text
                screen.app.invalidate()
                link_post({"type": "suggestion", "text": text})

        background(guess)

    def audit_line(text: str, routed: bool, channel: str):
        """In audit mode, what the router would have done with this request, logged (not shown in the
        chat). A turn the front end handled was logged with its route already; one that went straight
        to the brain is classified here so typed requests are reviewed too."""
        from vision import routing

        if cfg.router.mode != "audit":
            return
        if routed and conversation.last_route is not None:
            return
        try:
            route = routing.route(text, cfg)
        except routing.OverrideError:
            return
        conversation.supervisor.log("route", route=route, channel=channel, mode="audit")

    def cancel_reply():
        screen.answer_questions(None)
        background(conversation.cancel)
        background(brain.cancel)
        if state["compare_cancel"]:
            background(state["compare_cancel"])  # a /compare at work: every brain of it
        if state["ss"]:
            state["ss"].stop()
        elif state["speaker"]:
            state["speaker"].stop()

    def background(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def drain_turns():
        """Run typed follow-ups in submission order, never concurrently on the same brain. While a
        queued message is being picked or edited in the strip, the queue is held: nothing goes."""
        pending = state["turn_queue"]
        try:
            while not state["quitting"]:
                if not pending.wait_free(0.25):
                    continue
                with state["turn_lock"]:
                    free, item = pending.pop_if_free()
                    if not free:
                        continue
                    if item is None:
                        # Hand the queue back while holding the same lock used by enqueue_turn.
                        # Otherwise a message arriving after pop() but before the old worker's
                        # finally block sees turn_active=True and is left with no worker.
                        state["turn_active"] = False
                        state["turn_thread"] = None
                        return
                try:
                    if item.shown:
                        screen.add(user_grid(item.text), gap_before=True)
                    screen.app.invalidate()
                    if item.compare:
                        run_compare(item.text, list(item.compare))
                    else:
                        tried: set[str] = set()
                        while True:
                            turn = run_turn(item.text, speak_remote=item.speak, follow=item.follow, voice_remote=item.voice,
                                            talk_remote=item.talk, phone=item.phone)
                            # The provider's window is used up: carry on elsewhere (failover.py) and send the
                            # same message again, as many times as there are providers left to try.
                            if not (turn is not None and turn.is_error and limit_turn(turn) and not item.follow and not item.voice
                                    and not item.talk and fail_over(tried)):
                                break
                except Exception as e:  # noqa: BLE001
                    state["driver"] = state["steered"] = None
                    note(f"turn failed: {e}", "warn")
                    try:
                        journal_event("done", text="", error=str(e), session_id=brain.session_id)
                    except OSError:
                        pass
        finally:
            with state["turn_lock"]:
                if state["turn_thread"] is threading.current_thread():
                    state["turn_active"] = False
                    state["turn_thread"] = None

    def run_compare(text: str, picks: list):
        """A /compare turn (drain_turns runs it in place of run_turn): the prompt to every pick at once,
        each a fresh read-only brain carrying the conversation so far, the answers side by side when
        done. The chat's own brain is not asked and its session never sees the turn."""
        import copy

        from vision import compare
        from vision.brain import create_brain
        from vision.turnjournal import recover

        journal_event("unqueued", text=text)  # not a turn of the conversation: a restart must not run it again
        entry = screen.start_reply(markdown=True)
        screen.update_reply(entry, status="comparing…", force=True)
        link = state["link"]
        link_post = link.post if link else (lambda ev: None)
        records = turn_journal.read()
        history = recover(records)["history"] if records else None  # this window's chat, as shown
        transcript = _handoff_text(brain, history)

        def make_brain(pick):
            bc = copy.deepcopy(cfg.brain)
            bc.model, bc.effort = pick.model, pick.effort
            bc.mode = "plan"  # read-only: two brains at once must not both edit the working tree
            bc.keep_alive_s = 0  # one turn, then gone
            bc.fast = False
            bc.workdir = brain.workdir
            b = create_brain(bc, voice_mode=False)
            compare.silence(b)
            b.handoff = transcript
            return b

        cmp = compare.Comparison(picks, text, make_brain,
                                 on_change=lambda: screen.update_reply(entry, status=cmp.progress(), force=True))
        state["compare_cancel"] = cmp.cancel
        try:
            answers = cmp.run()
        finally:
            state["compare_cancel"] = None
        screen.end_reply(entry, cancelled=cmp.cancelled)
        screen.remove_entry(entry)  # the answers are a block of their own, with each brain's time in it
        screen.add(compare.CompareView(answers, text, untouched=_model_label(brain)))
        link_post({"type": "note", "text": compare.as_markdown(answers, text)})
        if link:
            link.post_summary(force=True)

    def start_compare(text: str, picks: list):
        """Queue a /compare like a typed message: it waits behind a running reply, and replies wait behind it."""
        from vision import providers

        for p in picks:
            why = providers.setup_note(p.model, cfg) or providers.unavailable_reason(provider_for(p.model), cfg)
            if why:
                note(f"[warn]{why}[/warn]")
                return
        state["compare"] = ()
        enqueue_turn(text, shown=queue_or_add(text), recorded=True, compare=tuple(picks))

    def enqueue_turn(text: str, speak_remote: bool = False, follow: bool = False, voice: bool = False, talk: bool = False,
                     shown: bool = False, recorded: bool = False, phone: bool = False, compare: tuple = ()):
        """`speak_remote`: the phone asked for this reply spoken; the server does that from the deltas.
        `follow`, `voice`, `talk`, `phone`: see run_turn. `shown`: it waits in the queued strip (queue_or_add)."""
        pending = state["turn_queue"]
        with state["turn_lock"]:
            if not recorded:
                journal_event("queued", text=text)
            pending.put(QueuedTurn(text, speak_remote, follow, voice, talk, shown=shown, phone=phone, compare=compare))
            if state["turn_active"]:
                return
            state["turn_active"] = True
            thread = threading.Thread(target=drain_turns, daemon=True)
            state["turn_thread"] = thread
            thread.start()

    def warm_brain():
        """A setting that is part of the brain's command line changed (/model, /effort, /mode, /fast, /cd, a
        memory line) or the conversation did (/new, /session): the process for the new settings boots now,
        while the next message is being typed (Brain.warm), rather than once it is sent. A joined chat runs
        on the server's brain, which warms its own."""
        if not joined():
            warm_soon(brain)

    def switch(model: str, effort: str | None = None, full: bool = False, save: bool = False, prefix: str = "model → "):
        """Change model on a worker thread while preserving the conversation."""
        from vision import providers

        why = providers.setup_note(model, cfg) or providers.unavailable_reason(provider_for(model), cfg)
        if why:
            note(f"[warn]{why}[/warn]")
            return
        if screen.busy:
            note("still replying — switch models once it has finished (Esc cancels)")
            return
        if joined():
            # The server owns the session: it switches, then reports back with a note and a fresh summary.
            brain.switch_model(model, effort)
            if save:
                note(_save_defaults(cfg, model, effort if effort is not None else cfg.brain.effort))
            return
        screen.busy = True

        def go():
            try:
                apply_switch(model, effort=effort, full=full, save=save, prefix=prefix)
            finally:
                screen.busy, screen.busy_label = False, ""
                if state["link"]:
                    state["link"].post_summary(force=True)
        background(go)

    def apply_switch(model: str, effort: str | None = None, full: bool = False, save: bool = False,
                     prefix: str = "model → ") -> bool:
        """The switch itself, on the calling (worker) thread: the brain is replaced when the provider
        changes, with the chat's transcript carried over. True when it went through."""
        nonlocal brain
        before = (cfg.brain.model, cfg.brain.effort)
        link = state["link"]
        try:
            from vision.turnjournal import recover

            records = turn_journal.read()
            history = recover(records)["history"] if records else None  # this window's chat, as shown
            brain, detail = _switch_model(cfg, brain, model, voice_mode=False, effort=effort, full=full,
                                          history=history)
            warm_brain()  # the process for the new pick boots while the next message is being typed
            journal_event("model", model=cfg.brain.model, effort=cfg.brain.effort,
                          session_id=brain.session_id)
            conversation.agent = brain
            conversation.follow()  # talk mode speaks through the new pick too
            msg = _save_defaults(cfg, cfg.brain.model, cfg.brain.effort) if save else f"{prefix}{_brain_summary(cfg, brain)}"
            note(f"{msg}; {detail}" if detail else msg)
            state.setdefault("noted", before)  # the phone hears of it above the next message
            state["quota"] = None  # the new provider's figure comes with its first turn
            return True
        except Exception as e:  # noqa: BLE001
            note(f"[err]model switch failed: {e}[/err]")
            if link:
                link.post({"type": "error", "text": f"model switch failed: {e}"})
            return False

    def limit_turn(turn) -> bool:
        """Whether a failed turn ran into the provider's usage limit (rather than anything else)."""
        from vision.failover import limit_hit

        return limit_hit(turn.error, getattr(brain, "last_usage", None))

    def fail_over(tried: set[str]) -> bool:
        """The provider's window is used up: offer the other providers (or take [brain].failover's first
        ready one unasked) and switch, with the transcript so far. True when the message should go again."""
        from vision import failover

        if joined() or failover.order(cfg.brain.failover) == "off":
            return False
        current = brain.provider
        options = failover.candidates(cfg, current, tried)
        if not options:
            note(f"[warn]{provider_label(current)}'s usage limit is hit and no other provider is set up here (/providers).[/warn]")
            return False
        if isinstance(cfg.brain.failover, list):
            model = options[0][0]
        else:
            q = failover.question(provider_label(current), options)
            journal_event("question", questions=[q])
            answers = screen.ask_questions([q])
            journal_event("answered", questions=[q], answers=answers)
            model = failover.chosen(answers, q, options)
            if not model:
                return False
        tried.add(current)
        screen.busy, screen.busy_label = True, "switching…"
        try:
            if not apply_switch(model, prefix=f"{provider_label(current)}'s limit is hit → "):
                return False
        finally:
            screen.busy, screen.busy_label = False, ""
            if state["link"]:
                state["link"].post_summary(force=True)
        note("sending your message again")
        return True

    # ---- a chat open on the phone: this screen joins it and the server keeps running it (remote.py)
    def joined() -> bool:
        from vision.remote import RemoteBrain

        return isinstance(brain, RemoteBrain)

    def phone_chats() -> list[dict]:
        """The chats `vision serve` runs on this machine (none when it is not running)."""
        from vision.remote import list_remote_chats, running_server

        server = running_server()
        chats = []
        if server:
            try:
                chats = list_remote_chats(server)
            except Exception as e:  # noqa: BLE001  (the descriptor outlived the server, or it is starting)
                note(f"[warn]vision serve is not answering: {e}[/warn]")
                server = None
        state["server"] = server
        return chats

    def attach(info: dict):
        """Join a phone chat: what is typed here and on the phone reads as one conversation."""
        nonlocal brain
        from vision.remote import CLOSED_ON_PHONE, RemoteBrain, remote_history

        if screen.busy or state["turn_active"]:
            note("[warn]wait for the current reply to finish (Esc cancels it), then /session again[/warn]")
            return
        forget_suggestion()  # the phone chat's own comes with its summary
        if joined() and brain.chat_id == info["chat"]:
            note("already in that conversation")
            return
        server = state["server"]
        before = (cfg.brain.model, cfg.brain.effort, cfg.brain.workdir)

        def foreign_turn(text: str):
            screen.add(user_grid(text), gap_before=True)
            note("from the phone")
            enqueue_turn(text, follow=True)

        def on_note(text: str, kind: str):
            note(f"[err]{text}[/err]" if kind == "error" else text)

        def suggested(text: str):
            # The server's guess at the next message: grey in the empty box, as after a reply here.
            if not (text and (screen.busy or state["turn_active"])):
                screen.suggestion = text
                screen.app.invalidate()

        def closed(reason: str):
            if state["guest"] != info["chat"] or reason != CLOSED_ON_PHONE:
                detach(reason)
                return
            # The phone opened this window for that chat alone: leave like /quit rather than turn into
            # a terminal chat of its own. This is the socket's thread, not the UI loop, so hop over.
            note("closed from the phone")
            loop = screen.app.loop
            if loop is not None:
                loop.call_soon_threadsafe(screen.exit, ("quit",))
            else:
                screen.exit(("quit",))

        remote = RemoteBrain(
            server, info, cfg.brain,
            on_summary=lambda s: screen.app.invalidate(),  # the corner label, the ctx figure, the title
            on_foreign_turn=foreign_turn, on_note=on_note,
            on_answered=lambda: screen.answer_questions(None),
            on_closed=closed,
            on_steered=lambda t: state["steered"](t) if state["steered"] else None,
            on_suggestion=suggested,
        )
        try:
            remote.connect()
        except Exception as e:  # noqa: BLE001
            cfg.brain.model, cfg.brain.effort, cfg.brain.workdir = before
            note(f"[err]could not join that chat: {e}[/err]")
            return
        if joined():
            brain.close()
        else:
            state["home"] = (brain, *before)
        if state["link"]:
            state["link"].stop()  # not a chat of its own while it mirrors one the server runs
            state["link"] = None
        brain = remote
        conversation.agent = brain
        conversation.new_session()
        try:
            turns = remote_history(server, info["chat"])
        except Exception:  # noqa: BLE001
            turns = []
        paint_turns(turns)
        screen.set_history([t["text"] for t in turns if t.get("role") == "user" and t.get("text")])
        note(f"joined “{remote.title or 'new conversation'}” from the phone · typed here and there alike · /new leaves it")
        if (running := remote.pending_turn()) is not None:
            foreign_turn(running)  # joined mid-reply: follow it from here
        screen.app.invalidate()

    def detach(reason: str = ""):
        """Back to this terminal's own conversation (the phone chat carries on without us)."""
        nonlocal brain
        if not joined():
            return
        brain.close()
        state["guest"] = None
        forget_suggestion()  # the phone chat's guess
        home, state["home"] = state["home"], None
        if home:
            brain, cfg.brain.model, cfg.brain.effort, cfg.brain.workdir = home
        else:
            from vision.brain import create_brain

            brain = create_brain(cfg.brain, voice_mode=False)
        conversation.agent = brain
        conversation.new_session()
        load_arrow_history()
        if brain.session_id:
            show_history(brain.provider, brain.session_id)
        else:
            paint_turns([])
        start_link()
        note(f"left the phone chat{': ' + reason if reason else ''} · back to your own conversation")
        screen.app.invalidate()

    # ---- voice: the mic runs on a worker thread while the screen keeps drawing
    def ears():
        """The reusable mic plus the speech models loaded for this talk session."""
        conversation.warm_up()
        components = ("ears", "voice", "listener") if state["cut_in"] else ("ears", "voice")

        def report(progress):
            if state["talk"]:
                screen.busy_label = f"warming up ears and voice… {len(progress.ready)}/{len(progress.components)} components ready"
                screen.app.invalidate()

        progress = WarmupProgress(components, on_update=report)
        if buddy and state["talk"]:
            buddy.warming(progress)
        if state["mic"] is None:
            from vision.audio import Microphone
            from vision.servicevoice import WHERE, transcriber

            state["mic"], state["stt"] = Microphone(cfg.listen), transcriber(cfg.listen)
            if getattr(state["stt"], "server", None):
                note(f"[dim]listening and speaking through {WHERE} (it has the voice); your mic and speakers are this computer's[/dim]")
        mic, stt = state["mic"], state["stt"]
        listener_error = warm_voice(
            progress, stt.warm_up, lambda: _load_voice(speaker(), cfg.voice),
            small_listener if "listener" in components else None,
        )
        if listener_error is not None:
            state["cut_in"] = False
            note(f"[warn]cutting in by voice is off: {listener_error}[/warn]")
        return mic, stt

    def small_listener():
        """The tiny CPU listener that spots the name: shared by the wake word and cutting into a reply.
        Made (and its model loaded, unless only voice activity is needed) on first use."""
        from vision.wake import WakeListener

        with state["listener_lock"]:
            if state["wake_listener"] is None:
                state["wake_listener"] = WakeListener(cfg.listen, cfg.wake)
            if state["wake"] or cfg.listen.barge_in == "wake":
                state["wake_listener"].warm_up()
            return state["wake_listener"]

    def cut_in():
        """Start listening for a cut-in while the reply about to be spoken plays; None when that is off."""
        if not state["cut_in"]:
            return None
        from vision.wake import BargeIn

        try:
            return BargeIn(small_listener(), cfg.listen.barge_in, on_cut=cancel_reply).start()
        except Exception as e:  # noqa: BLE001  the model or the mic failed: say so once, keep talking
            state["cut_in"] = False
            note(f"[warn]cutting in by voice is off: {e}[/warn]")
            return None

    def cut_in_done(barge) -> tuple | None:
        """The reply is over: let go of the mic; what was said to cut in, if anything, is the next turn."""
        if barge is None:
            return None
        hit = barge.stop()
        if barge.error is not None:
            state["cut_in"] = False
            note(f"[warn]cutting in by voice is off: {barge.error}[/warn]")
        return hit

    def hear_plan_answer(question: dict, closed: threading.Event, ss) -> None:
        """A plan waiting for approval in a voice conversation: say its first sentence, then listen for
        the answer ("go", "no", or what to change) until the form is answered some other way (the keys,
        the phone, Esc), which sets `closed`. The mic is otherwise shut until a reply is over."""
        from vision.planvoice import listen_for_answer
        from vision.tts import StreamingSpeaker

        barge, state["barge"] = state["barge"], None
        if barge is not None:
            barge.stop()  # an answer said to the plan must not cut the reply short, or come back as a turn of its own
        listen_for_answer(question, closed, ss, lambda: StreamingSpeaker(speaker()), state["mic"], state["stt"],
                          screen.answer_questions, set_hearing, note, lambda: state["talk"])

    def set_hearing(what: str):
        state["hearing"] = what
        screen.placeholder = f"{what}  speak, or type a message  (Esc ends listening)" if what else PLACEHOLDER
        screen.app.invalidate()

    def voice_loop(once: bool, woken=None, cut: bool = False):
        """Listen → transcribe → reply, until told to stop (/talk, Esc, “goodbye”) or, for /listen, once.

        `woken` is the (audio, command) the wake word heard: that utterance is the first turn (if it was
        only the name, a chime asks for the request), and a quiet follow-up window ends the conversation.
        `cut` says it was heard while cutting into a spoken reply, so it may open with the tail of that
        reply and the name is looked for anywhere in it.
        """
        from vision.config import resolve_device
        from vision.stt import LiveTranscript
        from vision.tts import chime
        from vision.wake import match_wake

        import queue

        typed: queue.Queue[str] = state["typed"]

        def settled():
            """Wait until nothing is being replied: the mic only opens once a reply is fully shown and spoken."""
            while screen.busy and state["talk"]:
                time.sleep(0.02)

        try:
            settled()
            if not state["talk"]:
                return
            screen.busy, screen.busy_label = True, "warming up ears and voice…"
            try:
                mic, stt = ears()
            finally:
                screen.busy, screen.busy_label = False, ""
                if buddy:
                    buddy.rest()
            if not state["talk"]:  # /talk was switched off while the models were warming up
                return
            if not once:
                if woken is None:
                    note(
                        f"voice conversation on · ears {stt.device} · voice {speaker().voice} — "
                        + ("push-to-talk: Enter on an empty box starts recording, Enter again sends it. " if state["ptt"] else "just speak; pause to send. ")
                        + "Type to answer instead. Esc, /talk or “goodbye” ends it"
                    )
            out_dev = resolve_device(cfg.voice.output_device, "output")
            # The wake utterance goes through the real ears too: the tiny model only had to spot the name.
            # `expect_name` says where to look for it in the transcript ("start" / "anywhere" / "").
            pending = woken[0] if woken else None
            expect_name = ("anywhere" if cut else "start") if woken else ""
            follow_up = cfg.wake.follow_up_s if woken else None

            def next_typed() -> str | None:
                try:
                    return typed.get_nowait()
                except queue.Empty:
                    return None

            preview: list = [None]  # the live `you ›` line of the utterance being heard, until the final text lands

            def push_to_talk(cancel: threading.Event):
                """Enter on an empty box starts recording and Enter again sends it. None when Esc, a typed
                message or /talk ended the wait first."""
                press = state["ptt_press"] = threading.Event()
                try:
                    while not cancel.is_set():
                        if press.wait(0.05):
                            break
                    if cancel.is_set():
                        return None
                    state["ptt_press"] = cancel  # the next Enter (like Esc or a typed message) ends the recording
                    if cfg.listen.chime:
                        chime("listen", out_dev)
                    set_hearing("recording… Enter to send")
                    return mic.record_until_enter(stop=cancel)
                finally:
                    state["ptt_press"] = None

            def drop_preview():
                if preview[0] is not None:
                    screen.remove_entry(preview[0])
                    preview[0] = None

            while state["talk"]:
                drop_preview()
                live = None  # the live preview of what is heard this time round, if the ears can give one
                from vision.timing import VoiceTiming
                timing = VoiceTiming() if cfg.conversation.timing else None
                heard = next_typed()  # a message typed while the mic was open (or between turns) takes its turn
                from_voice = heard is None
                wake_audio = heard is None and pending is not None  # the utterance that woke Vision (or cut in)
                if wake_audio:
                    audio, pending = pending, None
                elif heard is None:
                    cancel = threading.Event()
                    state["cancel"] = cancel
                    set_hearing("Enter to talk" if state["ptt"] else "listening…")
                    if cfg.listen.chime and not state["ptt"]:
                        chime("listen", out_dev)
                    # The words form on screen while you talk: the mic hands out the audio so far and
                    # LiveTranscript keeps re-reading it into a `you ›` line that the final text replaces.
                    # Re-transcribing a growing utterance on the CPU can take longer than the audio
                    # itself and used to make /talk appear frozen between listening and replying.
                    live = (
                        LiveTranscript(stt, lambda t: screen.update_entry(preview[0], hearing_grid(t)))
                        if cfg.listen.live_ms and stt.can_preview_live and not state["ptt"] else None
                    )

                    def speech_started():
                        set_hearing("hearing you…")
                        if buddy:
                            buddy.listening()
                        if live is not None:
                            preview[0] = screen.add(hearing_grid(""), gap_before=True)

                    try:
                        if buddy:
                            buddy.listening()
                        audio = push_to_talk(cancel) if state["ptt"] else mic.record_utterance(
                            on_speech_start=speech_started, cancel=cancel, start_timeout_s=follow_up,
                            on_audio=live.feed if live is not None else None, every_ms=cfg.listen.live_ms,
                            on_speech_end=live.feed_final if live is not None else None,
                            on_level=buddy.hear if buddy else None,
                            **({"timing": timing} if timing else {}),
                        )
                    finally:
                        if timing:
                            timing.event("preview_stop_start")
                        preview_stopped = live is None or live.stop()
                        if timing:
                            timing.event("preview_stop_end")
                        set_hearing("")
                        if buddy:
                            buddy.listening(False)
                    if not preview_stopped:
                        raise RuntimeError("live transcription stopped responding; voice conversation reset")
                    heard = next_typed()
                    from_voice = heard is None
                    if heard is not None or not state["talk"]:
                        drop_preview()  # a typed message, or Esc, took over: the half-heard line goes
                if heard is None:
                    if not state["talk"]:
                        break
                    if audio is None or audio.size == 0:
                        if once:
                            note("didn't catch that")
                            break
                        if woken:
                            break  # nothing more said in the follow-up window: back to sleep
                        continue
                    # Somebody else's voice (only_me): dropped before it is transcribed. A stranger who
                    # said the name sends Vision back to sleep; mid-conversation the mic simply reopens.
                    from vision import voiceid

                    mine, score = voiceid.check(cfg.listen, audio, note)
                    if not mine:
                        drop_preview()
                        note(voiceid.rejected_note(score))
                        if once or (woken and wake_audio):
                            break
                        expect_name = ""
                        continue
                    screen.busy, screen.busy_label = True, "transcribing…"
                    if buddy:
                        buddy.chore("transcribing")
                    try:
                        heard = _final_transcript(live, stt, audio, timing)
                    finally:
                        screen.busy, screen.busy_label = False, ""
                        if buddy:
                            buddy.rest()
                    if expect_name:
                        command = match_wake(heard, cfg.wake.names, anywhere=expect_name == "anywhere") if heard else None
                        expect_name = ""
                        if command is not None:
                            heard = command
                        if not heard:
                            drop_preview()
                            continue  # only the name: the chime says "go ahead" and the mic opens
                    if not heard:
                        drop_preview()
                        note("didn't catch that")
                        if once:
                            break
                        continue
                    if preview[0] is not None:
                        screen.update_entry(preview[0], user_grid(heard))
                        preview[0] = None
                    else:
                        screen.add(user_grid(heard), gap_before=True)
                    screen.remember_user(heard)
                if not once and heard.lower().strip(" .!?,") in _EXIT_PHRASES:
                    from vision.tts import StreamingSpeaker

                    state["ss"] = ss = StreamingSpeaker(speaker())
                    try:
                        ss.feed("Goodbye.")
                        ss.finish()
                    finally:
                        state["ss"] = None
                    break
                barge = state["barge"] = cut_in() if not once else None  # the mic is free now: hear a cut-in while it speaks
                run_turn(heard, from_voice=from_voice, timing=timing if from_voice else None)
                if once:
                    break
                settled()
                state["barge"] = None
                hit = cut_in_done(barge)
                if hit is not None:
                    pending, expect_name = hit[0], "anywhere" if cfg.listen.barge_in == "wake" else ""
        except (Exception, SystemExit) as e:  # noqa: BLE001  SystemExit: no such input device
            note(f"[err]listening failed: {e}[/err]")
            if buddy:
                buddy.fail()
        finally:
            conversation.model.close()
            state["talk"], state["once"], state["cancel"] = False, False, None
            # Text typed during the last spoken reply still belongs to the chat.
            while not typed.empty():
                enqueue_turn(typed.get_nowait())
            set_hearing("")
            if buddy:
                buddy.rest()
            # Drop Whisper first, then the voice and its claim. When another Vision gets the claim it
            # also gets enough free VRAM to load both sides of a conversation. /speak keeps only the
            # voice because typed replies still use it.
            unload_errors = []
            for resource in (state["stt"], None if state["speak"] else state["speaker"]):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as e:  # noqa: BLE001
                        unload_errors.append(str(e))
            state["thread"] = None
            if unload_errors:
                note(f"[warn]speech did not unload cleanly: {'; '.join(unload_errors)}[/warn]")
            if not once and woken is None:
                note("voice conversation off")
            start_wake()  # back to waiting for the name, if the wake word is on

    def start_listening(once: bool, woken=None, cut: bool = False):
        if refuse_voice():
            return
        if state["talk"]:
            note("already listening" if state["once"] else "voice conversation is already on (/talk again or Esc ends it)")
            return
        import queue

        stale = state["thread"]
        if stale is not None and stale.is_alive():
            stale.join(timeout=1)  # the last conversation is still letting go of the mic
            if stale.is_alive():  # stuck inside the audio library: a second stream on top would hang too
                note("[warn]the mic from the last voice conversation hasn't closed yet; if this keeps up, restart Vision[/warn]")
                return
        stop_wake(join=True)  # one mic stream at a time: the wake listener lets go before the ears open
        state["talk"], state["once"], state["typed"] = True, once, queue.Queue()
        state["thread"] = threading.Thread(target=voice_loop, args=(once, woken, cut), daemon=True)
        state["thread"].start()

    # ---- wake word: a tiny CPU model waits for the name whenever nothing else has the mic
    def wake_loop(cancel: threading.Event):
        """Wait for the name, then hand the utterance to a voice conversation (see voice_loop's `woken`)."""

        def hint(text: str):
            screen.placeholder = text
            screen.app.invalidate()

        try:
            if state["wake_listener"] is None:
                # Not `screen.busy`: typing must keep working while the small model loads (a few seconds).
                hint("loading the wake word…  (type a message meanwhile)")
                if buddy:
                    buddy.chore("loading")
                try:
                    small_listener()
                finally:
                    if buddy:
                        buddy.rest()
            if cancel.is_set():
                return
            hint(f"Say “{wake_name()}” to talk, or type a message…")
            # Drop audio while a reply is being written: the wake word is for an idle chat. While one is
            # being spoken, either drop it too (the speakers must not wake it) or, with barge_in on, keep
            # listening over it for the name / any voice, and cut the reply short when it comes.
            cut = {"yes": False}

            def speaking() -> bool:
                return state["ss"] is not None

            def spotted():
                if speaking():
                    cut["yes"] = True
                    cancel_reply()

            if state["cut_in"]:
                hit = state["wake_listener"].wait(
                    cancel,
                    paused=lambda: screen.busy and not speaking(),
                    replying=speaking,
                    on_spotted=spotted,
                    speech_cuts_in=cfg.listen.barge_in == "speech",
                )
            else:
                hit = state["wake_listener"].wait(cancel, paused=lambda: screen.busy or speaking())
            if hit is not None:
                hint(PLACEHOLDER)
                start_listening(once=False, woken=hit, cut=cut["yes"])  # no chime when a request came with the name
        except (Exception, SystemExit) as e:  # SystemExit: no such input device
            state["wake"] = False
            note(f"[err]wake word off: {e}[/err]")
            if buddy:
                buddy.fail()
        finally:
            if state["wake_thread"] is threading.current_thread():
                state["wake_thread"], state["wake_cancel"] = None, None
            if not state["talk"]:
                hint(PLACEHOLDER)

    def wake_name() -> str:
        return cfg.wake.names[0].capitalize()

    def wake_note():
        name = wake_name()
        note(f"wake word on · say “{name}” and I'll listen (“{name}, …” runs straight away) · /wake off ends it")

    def start_wake():
        """Listen for the name on a worker thread, unless a voice conversation has the mic (it resumes after) or the
        voice is switched off (it starts again with /voice on)."""
        if not state["wake"] or state["quitting"] or state["talk"] or voice_off():
            return
        old = state["wake_thread"]
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=1)  # the previous listener is on its way out after a hand-off
            if old.is_alive():
                return  # no: it is still waiting for the name
        state["wake_cancel"] = threading.Event()
        state["wake_thread"] = threading.Thread(target=wake_loop, args=(state["wake_cancel"],), daemon=True)
        state["wake_thread"].start()

    def stop_wake(join: bool = False):
        """Tell the wake listener to let go of the mic; `join` waits until it has (before opening it again)."""
        cancel, thread = state["wake_cancel"], state["wake_thread"]
        if cancel is not None:
            cancel.set()
        if join and thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def run_shell(command: str):
        """Shell mode's Enter: run the line in the chat's folder with the user's shell, show what it printed,
        and keep it for the brain's next turn. Esc or Ctrl-C stops it; so does shellmode.TIMEOUT_S."""
        from vision import shellmode
        from vision.ui import shell_grid, shell_output_grid

        if state["shell_proc"] is not None:
            note("a command is still running (Esc stops it)", "warn")
            return
        screen.add(shell_grid(command), gap_before=True)
        shown = screen.add(shell_output_grid("", None, running=True))
        # What it prints shows as it prints it: a command that prints and then waits (a login's code) says so now.
        live = shellmode.Live(lambda text: screen.update_entry(shown, shell_output_grid(text, None, running=True)))

        def work():
            try:
                output, code = shellmode.run(command, brain.workdir, started=lambda proc: state.update(shell_proc=proc),
                                             on_output=live.add)
            finally:
                live.close()
                state["shell_proc"] = None
            screen.update_entry(shown, shell_output_grid(output, code))
            state["shell_runs"].append((command, output, code))
            screen.app.invalidate()

        background(work)
        screen.notice("running… (Esc stops it)", 3)

    def stop_shell() -> bool:
        """Esc / Ctrl-C while a shell-mode command runs: stop it (and what it started). True if one was running."""
        from vision import shellmode

        return shellmode.stop(state["shell_proc"])

    def stop_listening() -> bool:
        """Ask the voice loop to stop; returns True if it was running. A reply in flight finishes first."""
        if not state["talk"]:
            return False
        state["talk"] = False
        if state["cancel"]:
            state["cancel"].set()
        # Clear the "listening…" prompt and face right away rather than when the loop winds down (it
        # does so again itself): if the mic ever hangs, the screen must not sit on listening.
        set_hearing("")
        if buddy:
            buddy.rest()
        return True

    def folder_note(workdir: str) -> str:
        """' · instructions: .vision.md' when the folder (or one above it) has instructions the brains read."""
        from vision.folderprompt import summary

        found = summary(workdir, cfg.brain)
        return f" · {found} (/instructions shows them)" if found else ""
    # ---- the memory review: what this chat left that is worth keeping, ticked on the way out
    def review_memory(then_quit: bool) -> None:
        """On a worker thread: one tool-free call over the saved conversation, a tick list of what it
        proposes, the ticked facts written. `then_quit`: leave once it is settled (Esc skips it)."""
        from vision import memreview
        from vision.sessions import session_history

        state["reviewing"] = True
        screen.notice("reviewing what's worth remembering…", 120)
        try:
            history = session_history(brain.provider, brain.session_id, limit=0)
            props = memreview.proposals(cfg, history)
        except Exception:  # noqa: BLE001  (a review must never keep the chat from closing)
            props = []
        screen.notice("", 0)
        saved = []
        if props:
            answers = screen.ask_questions([memreview.question(props)])
            saved = memreview.save(memreview.chosen(props, answers))
        state["reviewing"] = False
        if then_quit:
            if props:
                state["review_note"] = memreview.summary(saved)  # printed once the screen has closed
            # Not the UI loop's thread: hop over to end the app, as a phone-side close does.
            loop = screen.app.loop
            if loop is not None:
                loop.call_soon_threadsafe(screen.exit, ("quit",))
            else:
                screen.exit(("quit",))
            return
        note(memreview.summary(saved) if props else "nothing new worth remembering")
        if saved:
            warm_brain()  # memory is part of the persona: the process for it boots now

    def quit_after_review() -> bool:
        """/quit or Ctrl-D: True when the memory review runs first and leaves afterwards. A chat that
        is mid-reply, on a voice call, joined to the phone, too short, or already reviewing just leaves."""
        from vision import memreview

        if (state["reviewing"] or joined() or screen.busy or state["turn_active"] or state["talk"]
                or not brain.session_id or not memreview.due(cfg, state["brain_turns"])):
            return False
        background(review_memory, True)
        return True

    screen.on_quit = quit_after_review

    # ---- slash commands
    def command(cmd: str, arg: str):
        nonlocal brain
        if cmd in ("speak", "voice", "say", "talk", "listen", "wake", "mic") and voice_backend() is None:
            from vision.install import voice_hardware

            note("[warn]Voice isn't set up here. Quit and run `vision voice setup` to add it.[/warn]" if voice_hardware()
                 else "[warn]Voice needs an Apple Silicon Mac or an NVIDIA graphics card, so this machine is text only.[/warn]")
        elif cmd in ("quit", "exit", "q"):
            if not quit_after_review():
                screen.exit(("quit",))
        elif cmd == "review":
            if joined():
                note("a phone chat is reviewed from the phone")
            elif screen.busy or state["turn_active"] or state["reviewing"]:
                note("wait for the reply to finish, then /review")
            elif not brain.session_id:
                note("nothing to review yet")
            else:
                background(review_memory, False)
        elif cmd == "help":
            note(HELP_TEXT)
        elif cmd == "clear":
            with screen._lock:
                screen.entries.clear()
            paint_open()
        elif cmd == "copy":
            from vision.ui import code_blocks, copy_to_clipboard

            reply = screen.last_reply()
            what = arg.strip().lower()
            text = label = ""
            if not reply:
                note("nothing to copy yet")
            elif what in ("reply", "all"):
                text, label = reply.strip(), "the reply"
            elif what and not what.isdigit():
                note("[warn]/copy copies the last reply's last code block; /copy reply all of it, /copy 2 the block before last[/warn]")
            else:
                blocks = code_blocks(reply)
                n = int(what) if what else 1
                if not blocks:
                    note("the last reply has no code block (/copy reply copies all of it)")
                elif not 1 <= n <= len(blocks):
                    note(f"the last reply has {len(blocks)} code block{'s' if len(blocks) != 1 else ''}")
                else:
                    lang, text = blocks[-n]
                    label = f"code block ({lang})" if lang else "code block"
            if text:
                how = copy_to_clipboard(text, screen.app.output)
                lines = text.count("\n") + 1
                note(f"copied {lines} line{'s' if lines != 1 else ''}: {label}" if how
                     else "[warn]could not reach the clipboard (no wl-copy, xclip or xsel, and the terminal took no OSC 52)[/warn]")
        elif cmd == "model":
            choice, full = _split_model_arg(arg)
            if choice.lower() == "default":
                saved_model, saved_effort = _saved_defaults()
                switch(saved_model, effort=saved_effort, full=full, prefix="→ ")
            elif choice:
                switch(choice, full=full)
            else:
                from vision.providers import model_tabs

                screen.open_picker("Choose a model for this session", model_tabs(cfg), cfg.brain.model, switch)
        elif cmd == "effort":
            def set_effort(choice):
                if not supports_effort(cfg.brain.model, choice):
                    allowed = ", ".join(v for v, _, _ in effort_choices(cfg.brain.model)) or "none"
                    note(f"{model_label(cfg.brain.model)} supports effort: {allowed}")
                    return
                if joined():
                    switch(cfg.brain.model, effort=choice)
                    return
                cfg.brain.effort = choice
                note(f"effort → {cfg.brain.effort or 'off'}")
                warm_brain()
            if arg:
                set_effort(arg.lower())
            else:
                screen.open_picker("Choose an effort level for this session", effort_choices(cfg.brain.model), cfg.brain.effort, set_effort)
        elif cmd == "fast":
            if screen.busy:
                note("still replying — change fast mode once it has finished (Esc cancels)")
                return
            try:
                enabled = _fast_value(arg, cfg.brain.fast)
            except ValueError as e:
                note(f"[warn]{e}[/warn]")
                return
            screen.busy = True

            def go_fast():
                nonlocal brain
                try:
                    brain, detail = _set_fast(cfg, brain, enabled, voice_mode=False)
                    warm_brain()
                    msg = f"fast mode {'on' if enabled else 'off'} · {_brain_summary(cfg, brain)}"
                    note(f"{msg}{'; ' + detail if detail else ''}")
                    screen.app.invalidate()
                except Exception as e:  # noqa: BLE001
                    note(f"[err]fast mode failed: {e}[/err]")
                finally:
                    screen.busy = False

            background(go_fast)
        elif cmd == "default" and arg.strip().lower() == "reset":
            switch(FACTORY_MODEL, effort=FACTORY_EFFORT, save=True)
        elif cmd == "default":
            # Two pickers in a row (model, then effort), then persist both to config.toml.
            saved_model, saved_effort = _saved_defaults()

            def pick_effort(model_choice):
                from vision.providers import setup_note

                if setup_note(model_choice, cfg):
                    note(f"[warn]{setup_note(model_choice, cfg)}[/warn]")
                    return
                selected_effort, _ = coerce_effort(model_choice, saved_effort)
                screen.open_picker(
                    "Default effort level", effort_choices(model_choice), selected_effort,
                    lambda effort_choice: switch(model_choice, effort=effort_choice, save=True),
                )

            from vision.providers import model_tabs

            screen.open_picker("Default model", model_tabs(cfg), saved_model, pick_effort)
        elif cmd == "usage":
            try:
                which, full = _split_usage_arg(arg)
            except Exception as e:  # unknown word, or that provider's CLI is not installed
                note(f"[err]{e}[/err]")
                return
            label = "all provider" if which == "all" else provider_label((which or brain.provider), cli=True)
            owns_busy = not screen.busy
            if owns_busy:
                screen.busy, screen.busy_label = True, f"reading {label} usage…"

            def go():
                try:
                    screen.add(_usage_selection(cfg, brain, which, full=full))
                    refresh_quota()
                except Exception as e:  # an unavailable provider selected by name
                    note(f"[err]{e}[/err]")
                finally:
                    if owns_busy and not state["turn_active"]:
                        screen.busy, screen.busy_label = False, ""
            background(go)
        elif cmd in ("version", "versions"):
            try:
                show_vision, providers = clis.parse_version_arg(arg)
            except ValueError as e:
                note(f"[err]{e}[/err]")
                return
            owns_busy = not screen.busy
            if owns_busy:
                screen.busy, screen.busy_label = True, "reading versions…"

            def go():
                try:
                    screen.add(_versions_renderable(providers=providers, vision=show_vision))
                finally:
                    if owns_busy and not state["turn_active"]:
                        screen.busy, screen.busy_label = False, ""
            background(go)
        elif cmd == "update":
            try:
                which = clis.parse_update_arg(arg)
            except ValueError as e:
                note(f"[err]{e}[/err]")
                return
            if screen.busy:
                note("still busy — run /update once the reply has finished (Esc cancels)")
                return
            names = ", ".join(provider_label(p, cli=True) for p in which)
            screen.busy, screen.busy_label = True, f"updating {names}…"

            def go():
                try:
                    results = clis.update_clis(which, on_start=lambda p: setattr(screen, "busy_label", f"updating {provider_label(p, cli=True)}…"))
                    note(_update_summary(results))
                finally:
                    screen.busy = False
            background(go)
        elif cmd == "memory":
            from vision import memory

            names = {t[0] for t in memory.topics()}
            try:
                topic = memory.slug(arg) if arg else ""
            except ValueError:
                topic = ""
            if arg.strip().lower() == "tidy" and "tidy" not in names:
                # The brain cleans its own memory up, as a turn of this chat; a copy is kept first.
                saved = memory.backup()
                note(f"memory copied to {saved}")
                text = memory.tidy_prompt(saved)
                enqueue_turn(text, shown=queue_or_add(text), recorded=True)
            elif arg and topic not in names:  # not a topic: search every file for the words
                hits = memory.search(arg)
                note("\n".join(f"{name}: {ln}" for name, ln in hits) if hits else "nothing in memory matches that")
            else:
                lines = memory.facts(topic)
                filed = "" if topic else "".join(f"\n{name}.md ({n}): {about}" for name, about, n in memory.topics())
                note(f"{memory._path(topic)}\n" + ("\n".join(lines) if lines else "empty so far: the brain saves facts here, or /remember <fact> adds one")
                     + (f"\n\ntopics (/memory <topic> shows one, /memory <words> searches):{filed}" if filed else ""))
        elif cmd == "remember":
            from vision.memory import remember

            try:
                # "/remember @iphone-app the fact" files it under that topic; plain goes to the core.
                topic, _, rest = arg[1:].partition(" ") if arg.startswith("@") else ("", "", arg)
                note(f"remembered: {remember(rest, topic)}")
                warm_brain()  # memory is part of the persona: the process for it boots now
            except ValueError as e:
                note(f"[err]{e}[/err]")
        elif cmd == "forget":
            from vision.memory import forget

            try:
                gone = forget(arg)
                note("forgot:\n" + "\n".join(gone) if gone else "nothing in memory matches that")
                if gone:
                    warm_brain()
            except ValueError as e:
                note(f"[err]{e}[/err]")
        elif cmd == "export":
            from vision.sessions import SessionInfo, export_session

            if not brain.session_id:
                note("nothing to export yet: the conversation starts with the first message")
            else:
                title = live_title.get(brain.provider, brain.session_id) or "conversation"
                info = SessionInfo(brain.session_id, brain.provider, title, time.time(), brain.workdir)
                try:
                    path = export_session(info, arg, model=_model_label(brain))
                except (OSError, ValueError) as e:
                    note(f"[err]couldn't export: {escape(str(e))}[/err]")
                else:
                    note(f"saved to {escape(short_path(path))}")
        elif cmd in ("threads", "tell"):
            # A project's chat joined from `vision serve` (`vision project open`): its threads live there.
            try:
                found = _project_call("", query=f"chat={getattr(brain, 'chat_id', '')}") if joined() else []
                if not found:
                    note("[dim]/threads and /tell work in a project's chat: `vision project open <id>` joins one[/dim]")
                    return
                pid = found[0]["id"]
                if cmd == "tell":
                    ref, _, text = arg.strip().partition(" ")
                    if not ref or not text.strip():
                        note("[dim]/tell <thread id> <what to tell it>[/dim]")
                        return
                    t = _project_call(f"{pid}/threads/{ref}/tell", {"text": text.strip(), "direct": True})
                    note(f"told {escape(t['title'])} · {escape(_thread_state(t))}")
                    return
                rows = [t for t in _project_call(f"{pid}/threads") if arg.strip() == "all" or t["state"] != "resolved"]
                note("\n".join(f"[{_THREAD_COLOURS.get(t['state'], 'dim')}]{_STATE_MARKS.get(t['state'], '')}[/] "
                               f"[dim]{t['id']}[/dim] {escape(t['title'])} [dim]· {escape(_thread_state(t))}[/dim]" for t in rows)
                     or f"[dim]no threads in {escape(found[0]['title'])} yet[/dim]")
            except SystemExit as e:  # the server's refusal, or no server: said here, the chat goes on
                note(f"[err]{escape(str(e))}[/err]")
        elif cmd == "project":
            # This chat becomes a project's, as it is (projects live in `vision serve`): a joined chat is
            # adopted where it runs; this terminal's own conversation is opened there first, then joined,
            # so threads can report to it after this window is gone. A fresh chat with no goal given
            # joins the project this folder already has instead.
            if screen.busy or state["turn_active"]:
                note("[warn]wait for the reply to finish (Esc cancels it), then /project again[/warn]")
                return
            goal = arg.strip()

            def made(p: dict):
                note(f"this chat is now the project “{escape(p['title'])}” [dim]{p['id']}[/dim] · brief it like a lead: it hands "
                     "work to threads that keep going when you leave, and tells you when one needs you · /threads lists them")

            try:
                if joined():
                    made(_project_call("adopt", {"chat": brain.chat_id, "goal": goal})["project"])
                    return
                sid = brain.session_id
                if not sid and not goal:
                    here = [p for p in _project_call("") if (p.get("workdirs") or [""])[0] == brain.workdir]
                    if here:
                        attach(_project_call(f"{here[0]['id']}/open", {}))
                        return
                    note("[dim]/project <what it's for> starts a project around this chat · say something first and the goal is taken from that[/dim]")
                    return
                if state["link"]:
                    state["link"].stop()  # the server must not see this terminal holding the conversation it is about to take up
                    state["link"] = None
                try:
                    if sid:
                        out = _project_call("adopt", {"session_id": sid, "provider": brain.provider, "goal": goal})
                    else:
                        p = _project_call("", {"goal": goal, "workdir": brain.workdir})
                        out = {"project": p, "chat": _project_call(f"{p['id']}/open", {})}
                except SystemExit:
                    start_link()
                    raise
                brain.new_session()  # the conversation is the server's now: /new comes back to a fresh one here
                attach(out["chat"])
                if joined():
                    made(out["project"])
                else:  # the server has it either way: this window just isn't in it
                    start_link()
                    note(f"the project “{escape(out['project']['title'])}” is made · vision project open {out['project']['id']} joins its chat")
            except SystemExit as e:
                note(f"[err]{escape(str(e))}[/err]")
        elif cmd == "cd":
            if not arg.strip():
                note(f"working in {short_path(brain.workdir)}{folder_note(brain.workdir)} · /cd <dir>, /cd ~, /cd .. or /cd - moves")
                return
            if joined():
                note("[warn]this chat runs under vision serve; its directory is set there[/warn]")
                return
            try:
                target = _cd_target(arg, brain.workdir, state["prev_dir"])
            except ValueError as e:
                note(f"[err]{e}[/err]")
                return
            if target == brain.workdir:
                note(f"already in {short_path(target)}")
                return
            stays = _session_stays(brain)
            if stays and screen.busy:
                note("still replying — change directory once it has finished (Esc cancels)")
                return
            state["prev_dir"] = brain.workdir
            # The live brain and the config both move: brains built later (/model, the voice worker)
            # read cfg.workdir, and every provider spawns its CLI in brain.workdir on each turn, so
            # the change takes effect from the next reply on.
            brain.workdir = target
            cfg.brain.workdir = target
            if stays:
                # A Claude conversation lives in its directory (see _session_stays): the one here cannot
                # come along, so the next message starts a new one there, opening with the transcript.
                from vision.turnjournal import recover

                records = turn_journal.read()
                history = recover(records)["history"] if records else None
                detail = _leave_conversation(brain, history)
                journal_event("model", model=cfg.brain.model, effort=cfg.brain.effort, session_id=brain.session_id)
                note(f"directory → {short_path(target)}; {detail}{folder_note(target)}")
            else:
                note(f"directory → {short_path(target)}" + (" (from the next reply on)" if screen.busy else "") + folder_note(target))
            warm_brain()
            screen.app.invalidate()  # the status row shows the directory
        elif cmd == "instructions":
            from vision import folderprompt

            name = folderprompt._name(cfg.brain)
            if not name:
                note("folder instructions are off ([brain].folder_instructions is empty)")
                return
            found = folderprompt.load(brain.workdir, name)
            other = folderprompt.cli_files(brain.workdir, brain.cfg)
            if not found and not other:
                note(f"no {name} in {short_path(brain.workdir)} or the folders above it (up to the repository root or home); "
                     "write one there and every brain reads it from the next reply on")
                return
            parts = []
            for ins in found:  # nearest first, as /cd's note counts them
                head = short_path(str(ins.path)) + (f" (cut at {folderprompt.MAX_CHARS} characters)" if ins.cut else "")
                parts.append(f"[bold]{head}[/bold]\n{escape(ins.text)}")
            for ins in other:  # another CLI's file: this brain's own CLI wouldn't read it
                head = short_path(str(ins.path)) + " (handed over: this brain doesn't read it by itself)" + (
                    f" (cut at {folderprompt.CLI_MAX_CHARS} characters)" if ins.cut else "")
                parts.append(f"[bold]{head}[/bold]\n{escape(ins.text)}")
            note("\n\n".join(parts))
        elif cmd in ("diff", "rewind"):
            from vision import checkpoints
            from vision.ui import diff_grid

            if joined():
                note("[warn]this chat runs under vision serve, which keeps no checkpoints[/warn]")
                return
            if not cfg.brain.checkpoints:
                note("[warn]checkpoints are off (checkpoints under [brain] in the config)[/warn]")
                return
            if checkpoints.repo_root(brain.workdir) is None:
                note(f"[warn]{short_path(brain.workdir)} is not in a git repository, so there are no checkpoints here[/warn]")
                return
            cps = checkpoint_list()
            if cmd == "diff" and arg.strip().lower() == "list":
                listed = checkpoints.rows(cps)
                note("checkpoints, newest first (/diff <n> compares, /rewind <n> restores):\n"
                     + "\n".join(f"{n:>3}  {age:<8} {label}" for n, age, label in listed) if listed else "no checkpoints yet: one is taken before each reply")
                return
            cp = checkpoints.pick(cps, arg)
            if cp is None:
                note("no checkpoints yet: one is taken before each reply" if not cps else f"[warn]no checkpoint {arg.strip()}: /diff list shows them (1 to {len(cps)})[/warn]")
                return
            if cmd == "rewind" and screen.busy:
                note("still replying — rewind once it has finished (Esc cancels)")
                return
            which = f"before “{cp.label()}”"

            def work():
                try:
                    stat, patch = checkpoints.diff(cp)
                except checkpoints.CheckpointError as e:
                    note(f"[err]{e}[/err]")
                    return
                if not stat:
                    note(f"nothing has changed since {which}")
                    return
                if cmd == "diff":
                    screen.add(diff_grid(f"since {which} ({short_path(cp.root)}):", stat, patch))
                    return
                question = {"question": f"Put the files back as they were {which}?\n\n{stat}", "header": "Rewind", "multiSelect": False,
                            "options": [{"label": "Yes", "description": "undo these changes (the rewind itself can be rewound)"},
                                        {"label": "No", "description": "leave everything as it is"}]}
                answers = screen.ask_questions([question]) or {}
                if answers.get(question["question"]) != "Yes":
                    note("left as it is")
                    return
                try:
                    before = checkpoints.restore(cp)
                except checkpoints.CheckpointError as e:
                    note(f"[err]rewind failed: {e}[/err]")
                    return
                cps.append(before)  # so /rewind 1 undoes the rewind
                del cps[:-checkpoints.PER_SESSION]
                save_checkpoints(brain.session_id)
                note(f"rewound to {which} (/rewind undoes this)")

            background(work)
        elif cmd == "new":
            if screen.busy:
                note("still replying — start a new conversation once it has finished (Esc cancels)")
                return
            detach()  # a joined phone chat: leave it (it carries on there), then start afresh here
            forget_suggestion()
            brain.new_session()
            warm_brain()
            conversation.new_session()
            reset_turn_journal()
            screen.set_history([])
            with screen._lock:
                screen.entries.clear()
            paint_open()
            note("new conversation")
        elif cmd in ("mode", "auto", "plan"):
            word = cmd if cmd != "mode" else arg.strip().lower()
            if word and word not in MODES:
                note(f"[warn]unknown mode “{word}”: auto or plan[/warn]")
                return
            cfg.brain.mode = word or ("plan" if cfg.brain.mode == "auto" else "auto")  # bare /mode or Shift-Tab: toggle
            warm_brain()
            screen.app.invalidate()  # the status-row badge is the only feedback; nothing goes into the transcript
        elif cmd in ("session", "sessions", "resume"):
            from vision.remote import is_remote_key, live_sessions, mark_live_rows, remote_from_key, remote_rows
            from vision.sessions import find_any_session, list_all_sessions, session_from_key, session_key, session_tabs

            listed = list_all_sessions()
            phone = phone_chats()
            live = live_sessions(phone)

            def resume(key: str):
                if screen.busy:
                    note("[warn]wait for the current reply to finish (Esc cancels it), then /session again[/warn]")
                    return
                if is_remote_key(key):
                    hit = remote_from_key(key, phone)
                    if hit is None:
                        note("[err]that chat is gone from the phone[/err]")
                    else:
                        attach(hit)
                    return
                info = session_from_key(key, listed) or find_any_session(key, prefer=brain.provider)
                if info is None:
                    note(f"[err]no conversation starts with “{key}”[/err]")
                    return
                if session_key(info) in live:  # open on the phone: join it rather than fork a second copy
                    attach(live[session_key(info)])
                    return
                detach()  # a joined phone chat: leave it before this brain takes another conversation
                forget_suggestion()
                was = brain.workdir
                msg = _apply_session(brain, info, cfg.brain)
                if brain.workdir != was:
                    state["prev_dir"] = was  # the conversation lives in its directory; /cd - comes back
                if msg != "already in that conversation":
                    reset_turn_journal()
                    conversation.new_session()
                    load_arrow_history(info.provider, info.id)
                    show_history(info.provider, info.id)
                    warm_brain()
                note(msg)

            word = arg.strip().lower()
            if word in ("id", "info"):
                where = f" · joined to phone chat {brain.chat_id}" if joined() else ""
                note(f"current conversation: {brain.session_id or 'none yet (starts with the first message)'}{where}")
            elif word == "new":
                command("new", "")
            else:
                if word:
                    hit = find_any_session(word, prefer=brain.provider)
                    remote = remote_from_key(word, phone) if hit is None else None
                    if hit:
                        resume(session_key(hit))
                    elif remote:
                        attach(remote)
                    else:
                        note(f"[err]no conversation starts with “{arg.strip()}”[/err]")
                elif not listed and not phone:
                    note("no earlier conversations found")
                else:
                    tabs = mark_live_rows(session_tabs(), phone)
                    if state["server"]:
                        tabs.insert(0, ("Phone", remote_rows(phone), "" if phone else "no chats open on the phone"))
                    if joined():
                        current, prefer = f"remote:{brain.chat_id}", "Phone"
                    else:
                        current = f"{brain.provider}:{brain.session_id}" if brain.session_id else ""
                        prefer = provider_label(brain.provider)
                    screen.open_picker("Resume a conversation", tabs, current, resume, prefer_tab=prefer)
        elif cmd == "speak":
            if refuse_voice():
                return
            state["speak"] = state["speaker"] is None or not state["speak"]
            note(f"speech {'on' if state['speak'] else 'off'}")
            if not state["speak"] and state["speaker"] is not None and not state["talk"]:
                background(state["speaker"].close)
            if state["speak"] and not speaker()._loaded:
                # Load the voice now, on a worker thread (as --speak does at start-up), rather than
                # in the middle of the first spoken reply.
                def warm():
                    def report(progress):
                        screen.busy_label = f"loading voice… {len(progress.ready)}/{len(progress.components)} components ready"
                        screen.app.invalidate()

                    progress = WarmupProgress(("voice",), on_update=report)
                    try:
                        progress.run("voice", lambda: _load_voice(speaker(), cfg.voice))
                        note(f"voice ready · {speaker().voice} on {speaker().device_note}")
                    except Exception as e:  # noqa: BLE001
                        note(f"[err]voice failed to load: {e}[/err]")

                background(warm)
        elif cmd == "voice" and arg.strip().lower() in ("on", "off"):
            voice_power(arg.strip().lower() == "on")
        elif cmd == "voice":
            def go():
                try:
                    speaker().set_voice(arg or cfg.voice.voice)
                    note(f"voice → {speaker().voice}")
                    if speaker()._loaded and cfg.voice.filler:
                        speaker().prepare_fillers(cfg.voice.filler_phrases)
                except Exception as e:
                    note(f"[err]{e}[/err]")
            background(go)
        elif cmd == "say":
            if not refuse_voice():
                background(lambda: speaker().say(arg))
        elif cmd == "talk":
            if not stop_listening():
                state["ptt"] = arg.strip().lower() == "ptt"
                start_listening(once=False)
        elif cmd == "listen":
            start_listening(once=True)
        elif cmd == "mic":
            def choices():
                from vision.config import input_device_choices

                try:
                    return input_device_choices()
                except Exception as e:  # noqa: BLE001  no PortAudio, no PipeWire
                    note(f"[err]can't list microphones: {e}[/err]")
                    return []

            def use(spec: str):
                """Switch every mic to `spec` ("" = system default) and reopen whatever is listening on it."""
                from vision.config import resolve_device

                spec = "" if spec.strip().lower() in ("", "default", "system") else spec.strip()
                try:
                    resolve_device(spec, "input", fresh=True)
                except SystemExit as e:
                    note(f"[err]{e}[/err]")
                    return
                cfg.listen.input_device = spec
                save_input_device(spec)
                for mic in (state["mic"], state["wake_listener"].mic if state["wake_listener"] else None):
                    if mic is not None:
                        mic.reconnect()
                talking, waking = state["talk"], state["wake"]
                if talking:
                    stop_listening()
                    thread = state["thread"]
                    if thread is not None and thread.is_alive():
                        thread.join(timeout=5)  # a reply in flight finishes first
                elif waking:
                    stop_wake(join=True)
                mic_note = "the system default" if not spec else f"“{spec}”"
                note(f"mic → {mic_note}" + (" · reopening it" if talking or waking else ""))
                if talking:
                    if state["thread"] is not None and state["thread"].is_alive():
                        note("[warn]the old voice conversation is still busy: /talk when it's done[/warn]")
                    else:
                        start_listening(once=False)
                elif waking:
                    start_wake()

            if arg.strip():
                background(use, arg)
            else:
                def opened():
                    rows = choices()
                    if rows:
                        screen.open_picker("Choose a microphone", rows, cfg.listen.input_device, lambda v: background(use, v))
                        screen.app.invalidate()

                background(opened)  # pw-dump can take a moment: not on the UI thread
        elif cmd == "providers":
            from vision import providers as prov
            from vision.config import save_enabled_providers

            def apply(chosen: list[str]) -> None:
                if not chosen:
                    note("[warn]keep at least one provider[/warn]")
                    return
                cfg.providers.enabled = chosen
                save_enabled_providers(chosen)
                labels = ", ".join(prov.REGISTRY[n].label for n in chosen)
                off = provider_for(cfg.brain.model) not in chosen
                note(f"providers → {labels}" + (f"; this chat stays on {model_label(cfg.brain.model)} until you /model" if off else ""))

            words = [w.strip(",").lower() for w in arg.split() if w.strip(",")]
            if words:
                bad = [w for w in words if w not in prov.REGISTRY]
                if bad:
                    note(f"[warn]unknown provider {', '.join(bad)}: pick from {', '.join(prov.REGISTRY)}[/warn]")
                else:
                    apply([n for n in words if n in prov.REGISTRY])
            else:
                every = list(prov.REGISTRY.values())
                on = {p.name for p in prov.enabled(cfg)}
                question = {
                    "question": "Which providers should /model offer?", "header": "Providers", "multiSelect": True,
                    "options": [{"label": p.label, "description": p.note + ("" if prov.ready(p.name, cfg) else " · not set up yet")}
                                for p in every],
                    "preselected": [i for i, p in enumerate(every) if p.name in on], "other": False,
                }

                def picked(answers):
                    if answers:
                        labels = {x.strip() for x in next(iter(answers.values())).split(",")}
                        apply([p.name for p in every if p.label in labels])

                screen.open_questions([question], picked)
        elif cmd == "wake":
            word = arg.strip().lower()
            if word and word not in ("on", "off"):
                note("[warn]/wake on or /wake off[/warn]")
                return
            on = word == "on" if word else not state["wake"]
            state["wake"] = on
            cfg.wake.enabled = on
            save_wake_enabled(on)
            if on:
                wake_note()
                if state["talk"]:
                    note("(it starts listening for the name once this voice conversation ends)")
                start_wake()
            else:
                stop_wake()
                note("wake word off")
            screen.app.invalidate()
        elif cmd == "compare":
            from vision import compare

            if arg.strip().lower() == "off":
                was = state["compare"]
                state["compare"] = ()
                note("comparison dropped; the next message goes to the chat's brain" if was else "no comparison was armed")
                return
            picks, prompt, err = compare.parse(arg, cfg.brain.effort)
            if err:
                note(f"[warn]{err}[/warn]")
            elif picks and prompt:
                start_compare(prompt, picks)
            elif picks:
                state["compare"] = tuple(picks)
                note(f"the next message goes to {compare.armed_label(picks)} side by side (/compare off drops it)")
            else:
                from vision.providers import model_tabs

                rows = [(v, label, tab) for tab, entries, _ in model_tabs(cfg, setup_rows=False) for v, label, _ in entries]
                question = {
                    "question": "Compare which models? (effort: the session's, fitted to each; name them as /compare opus:high … for other levels)",
                    "header": "Compare", "multiSelect": True, "other": False,
                    "options": [{"label": v, "description": f"{label} · {tab}"} for v, label, tab in rows],
                    "preselected": [i for i, (v, _, _) in enumerate(rows) if v == cfg.brain.model],
                }

                def chosen(answers):
                    if not answers:
                        return
                    picks = compare.picks_from_labels(next(iter(answers.values())), cfg.brain.effort)
                    if len(picks) < 2:
                        note("[warn]tick at least two models to compare[/warn]")
                        return
                    state["compare"] = tuple(picks)
                    note(f"the next message goes to {compare.armed_label(picks)} side by side (/compare off drops it)")

                screen.open_questions([question], chosen)
        elif cmd in ROUTE_COMMANDS:
            from vision.routing import OverrideError, parse_command

            text = f"/{cmd} {arg}".strip()
            try:
                override = parse_command(text, cfg.router)
            except OverrideError as e:
                note(f"[err]{e}[/err]")
                return
            if override is None:
                note(f"[err]unknown command /{cmd}[/err]")
                return
            if override.kind == "cancel" or override.text:
                send(text)  # the front end parses it again and acts on it
            else:
                conversation.pending_override = override
                what = f"{override.agent} {override.effort}".strip() if override.kind == "delegate" else override.kind
                note(f"next message → {what} (or put the request on the same line: /{cmd} {arg + ' ' if arg else ''}<request>)")
        elif cmd == "router":
            from vision.config import ROUTER_MODES, save_config_value

            words = arg.lower().split()
            mode = words[0] if words else ""
            if mode and mode not in ROUTER_MODES:
                note(f"[warn]unknown router mode “{mode}”: off, audit or on[/warn]")
                return
            if mode:
                cfg.router.mode = mode
                if "save" in words[1:]:
                    save_config_value("router", "mode", f'"{mode}"')
            from vision.routing import agent_model

            agents = ", ".join(f"{n} → {model_label(agent_model(cfg.router, n)) or '?'}" for n in cfg.router.agents)
            note(f"router {cfg.router.mode}{' (saved)' if mode and 'save' in words[1:] else ''} · default {cfg.router.default_agent} "
                 f"{cfg.router.default_effort}, hard tasks {cfg.router.high_effort} · agents: {agents} · log ~/.local/state/vision/routing.jsonl")
            screen.app.invalidate()
        else:
            note(f"[err]unknown command /{cmd}[/err]")

    def send(text: str):
        if state["talk"]:
            screen.add(user_grid(text), gap_before=True)
            state["typed"].put(text)
            if state["cancel"]:
                state["cancel"].set()
            return
        if state["held"] or (state["turn_active"] and screen.busy and hasattr(state["driver"], "take_untaken_steers")):
            # Claude at work: straight into its own queue, as Claude Code does with what is typed meanwhile.
            # It waits over the input box and goes in at Claude's next step (a tool result, or once the
            # reply ends), not after the whole turn. Other brains keep Vision's queue (Ctrl-X sends one in).
            steer_now(text)
            return
        enqueue_turn(text, shown=queue_or_add(text), recorded=True)

    def queue_or_add(text: str) -> bool:
        """A message behind a running reply waits in the queued strip over the input box (True: it
        joins the transcript when its turn starts); otherwise it is in the transcript straight away."""
        journal_event("queued", text=text)
        if state["turn_active"]:
            screen.app.invalidate()
            return True
        screen.add(user_grid(text), gap_before=True)
        return False

    def take_queued(only: str | None = None, for_steer: bool = False, queued_id: int | None = None, first: bool = False) -> list[str]:
        """Queued typed messages out of the turn queue (all, or the one reading `only`), their rows
        out of the transcript: they are about to go into the running turn instead."""
        with state["turn_lock"]:
            taken = state["turn_queue"].take_where(
                lambda it: not it.follow and (queued_id is None or it.id == queued_id)
                and (only is None or it.text == only), first_only=first or only is not None or queued_id is not None)
            for item in taken:
                if for_steer:
                    journal_event("steer_attempt", text=item.text)
                journal_event("unqueued", text=item.text)
        screen.app.invalidate()
        return [item.text for item in taken]

    def steer_now(text: str, from_phone: bool = False, queued_id: int | None = None, every: bool = False):
        """Ctrl-X, or Send now on the phone: into the running turn rather than after it; Claude takes
        it at its next step (Codex, Grok and a local model likewise). "" = everything queued. A brain
        that can't take one mid-reply (Codex exec, Grok headless) leaves it queued. `from_phone`: the text may already sit in the queue
        (sent earlier, now pushed) or not (sent with `now`); either way it must get through."""
        running = screen.busy and state["turn_active"]
        if not running:
            # A queued message is already due to run. Sending a second copy here races the
            # turn worker when the previous reply has just ended.
            if text and queued_id is None and (not from_phone or not state["turn_queue"].has_text(text)):
                if from_phone:
                    enqueue_turn(text, shown=queue_or_add(text), recorded=True)
                else:
                    send(text)
            return
        if text:
            if queued_id is not None or (from_phone and state["turn_queue"].has_text(text)):
                texts = take_queued(text, for_steer=True, queued_id=queued_id)
            else:
                texts = [text]
                journal_event("steer_attempt", text=text)
        else:
            texts = take_queued(for_steer=True, first=not every)  # Ctrl-X on an empty box: the oldest one
            if not texts:
                screen.notice("nothing queued", 2)
                return
        driver, split = state["driver"], state["steered"]
        for t in texts:
            if joined():
                brain.steer(t)  # the server sends it in or queues it; its `steered` splits the reply here
            elif getattr(driver, "steer", None) and driver.steer(t):
                if hasattr(driver, "take_untaken_steers"):
                    # Claude holds it until its next step and says when it takes it in (on_steer_taken):
                    # over the input box till then, so the reply it is still writing isn't split by it.
                    screen.sent.append(t)
                    screen.app.invalidate()
                    if state["link"]:
                        state["link"].post_summary(force=True)  # the phone keeps its bubble waiting
                elif split:
                    split(t)
            else:
                journal_event("steer_failed", text=t)
                if not hasattr(driver, "take_untaken_steers"):  # Claude's turn just closing: it simply runs next
                    note("this model can't take a message mid-reply, so it's queued")
                enqueue_turn(t, shown=queue_or_add(t), recorded=True)

    def talk_over(text: str) -> bool:
        """Said (or typed) on a phone call while the call's own turn runs, often waiting on agents it
        launched: into that turn, answered now rather than queued behind the agents. False when no
        such turn is running or it has stopped taking messages: the caller queues it as before."""
        driver, split = state["driver"], state["steered"]
        if not (screen.busy and state["turn_active"] and driver is conversation and split):
            return False
        journal_event("steer_attempt", text=text)
        if not driver.steer(text):
            journal_event("steer_failed", text=text)
            return False
        split(text)
        return True

    def submit(text: str):
        text = text.strip()
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            command(cmd.lower(), arg.strip())
            return
        if state["compare"] and not state["talk"]:
            start_compare(text, list(state["compare"]))  # /compare armed it for this message
            return
        send(text)

    screen.on_submit = submit
    screen.on_steer = steer_now
    screen.turns = state["turn_queue"]  # the queued strip reads and edits the same queue
    screen.can_steer_fn = lambda: joined() or bool(getattr(state["driver"], "steer", None))
    screen.on_cancel = cancel_reply
    screen.on_toggle_mode = lambda: command("mode", "")
    screen.on_interrupt = lambda: stop_shell() or stop_listening()  # Esc / Ctrl-C while idle: stop a `!` command, leave the voice conversation
    screen.on_shell = run_shell

    def empty_enter() -> bool:
        """Enter on an empty box: push-to-talk's start (and send) while it waits on one."""
        press = state["ptt_press"]
        if press is None:
            return False
        press.set()
        return True

    screen.on_empty_enter = empty_enter
    if wake or cfg.wake.enabled:
        state["wake"] = True
        if not talk:  # a voice conversation has the mic first; the name is listened for once it ends
            start_wake()

    # ---- the phone: `vision serve` lists this chat and relays what is typed there (link.py)
    def link_summary() -> dict:
        return {
            "pid": os.getpid(),
            "title": live_title.get(brain.provider, brain.session_id),
            "provider": brain.provider,
            "model": _model_label(brain),
            "model_id": cfg.brain.model,
            "effort": cfg.brain.effort or "",
            "mode": cfg.brain.mode,
            "voice_model": model_label(cfg.conversation.model) or cfg.conversation.model,
            "voice_model_id": cfg.conversation.model,
            "voice_effort": cfg.conversation.effort or "",
            "session_id": brain.session_id,
            "context": context_figure(brain),
            "workdir": brain.workdir,
            "busy": screen.busy or state["turn_active"],
            "waiting": screen.form_open,
            "questions": screen.pending_questions,
            "queued": list(screen.sent) + [item.text for item in state["turn_queue"].shown()],  # sent ones wait for Claude to take them
            "suggestion": screen.suggestion,
        }

    def link_frame(frame: dict):
        kind = frame.get("type")
        if kind == "message":
            text = (frame.get("text") or "").strip()
            if not text:
                return
            if state["talk"]:
                screen.add(user_grid(text), gap_before=True)
                note("from the phone")
                state["typed"].put(text)
                if state["cancel"]:
                    state["cancel"].set()
            elif (frame.get("voice") or frame.get("talk")) and talk_over(text):
                note("from the phone")
            else:
                shown = queue_or_add(text)
                note("from the phone")
                enqueue_turn(text, speak_remote=bool(frame.get("speak")), voice=bool(frame.get("voice")),
                             talk=bool(frame.get("talk")), shown=shown, recorded=True, phone=True)
        elif kind == "unqueue":
            # Removed (or taken back to edit) on the phone: out of the queue here too.
            text = (frame.get("text") or "").strip()
            if text:
                take_queued(text)
        elif kind == "steer":
            text = (frame.get("text") or "").strip()
            if text:
                steer_now(text, from_phone=True)
        elif kind == "answer":
            screen.answer_questions(frame.get("answers"))
        elif kind == "cancel":
            cancel_reply()
        elif kind == "model":
            if frame.get("model"):
                switch(frame["model"], effort=frame.get("effort") or None)
        elif kind == "mode":
            if frame.get("mode") in MODES:
                command("mode", frame["mode"])  # as /mode typed here: the badge changes, nothing goes in the transcript
        elif kind == "note":
            # A reminder set from this chat falling due (Hub.remind).
            if frame.get("text"):
                note(frame["text"])
        elif kind == "quit":
            # Closed on the phone: leave like /quit. The link thread is not the UI loop, so hop over.
            note("closed from the phone")
            loop = screen.app.loop
            if loop is not None:
                loop.call_soon_threadsafe(screen.exit, ("quit",))
            else:
                screen.exit(("quit",))

    def start_link():
        try:
            from vision.link import LinkHost

            state["link"] = LinkHost(link_summary, link_frame, log=lambda m: screen.add(notice_grid(m, "warn")))
            state["link"].start()
        except Exception as e:  # noqa: BLE001  (a read-only state dir: the phone just does not see this chat)
            state["link"] = None
            note(f"[warn]not visible to the phone: {e}[/warn]")
    if not join:  # a `--join` window mirrors a phone chat; it only lists itself if the join misses
        start_link()
    for n in RETIRED_NOTES:  # a saved model its provider no longer lists (see _cfg)
        note(f"[warn]{n}[/warn]")

    def join_on_start():
        """`vision --join <chat>` (the phone's "Open on computer"): attach once the screen is up, so
        the joined history paints into a live app. A miss just leaves a note and starts as usual."""
        from vision.remote import remote_from_key

        phone = phone_chats()
        hit = remote_from_key(join, phone)
        if hit is None:
            note(f"[err]no phone chat starts with “{join}”[/err]" if state["server"] else "[err]vision serve is not running, so there is no phone chat to join[/err]")
            start_link()
            return
        attach(hit)
        if joined():
            state["guest"] = hit["chat"]
        else:
            start_link()

    def project_here_note():
        """A project of this folder's, when `vision serve` has one: say so, since its chat is the one
        whose threads carry on after this window closes. Quiet when there is no server or no project."""
        from vision.remote import running_server

        if joined() or running_server() is None:
            return
        try:
            here = [p for p in _project_call("") if (p.get("workdirs") or [""])[0] == brain.workdir]
        except SystemExit:
            return
        if here and not state["quitting"]:
            note(f"[dim]this folder has the project “{escape(here[0]['title'])}” · /project joins its chat[/dim]")
            screen.app.invalidate()

    def on_start():
        """Once the screen is up: join the phone chat asked for, and start the typed brain's process
        now, while the first message is being typed (Brain.warm), on its own thread so the first
        paint is never behind it. A joined chat runs on the server's brain, not on one here."""
        if join:
            join_on_start()
        else:
            background(project_here_note)
        warm_brain()
        if talk:
            command("talk", "ptt" if ptt else "")

    try:
        screen.run(pre_run=on_start)
    except (EOFError, KeyboardInterrupt):
        pass
    state["quitting"] = True
    if state["link"]:
        state["link"].stop()
    if joined() or hasattr(brain, "close"):
        brain.close()  # a joined chat lets the server go; a kept provider process ends
    stop_wake()
    stop_listening()
    cancel_reply()
    for t in (state["thread"], state["wake_thread"], state["turn_thread"]):
        if t:
            t.join(timeout=3)  # let the mic stream close before the interpreter tears PortAudio down
    if state.get("review_note"):
        console.print(f"[dim]{state['review_note']}[/dim]")
    console.print(f"{NAME} [dim]signing off.[/dim]")


# ---------------------------------------------------------------- talk (speech to speech)
def _final_transcript(live, stt, audio, timing=None) -> str:
    """The utterance's words: the live preview's, when its speech-end snapshot was transcribed during
    the end silence (LiveTranscript.final), else a fresh transcription of the whole recording. The
    final used to run again every time, on the audio the preview had just finished with."""
    ready = live.final if live is not None else None
    if timing:
        timing.event("transcription_start")
    text = ready if ready is not None else stt.transcribe(audio)
    if timing:
        timing.event("transcription_end")
    return text.strip()


def _listen_once(cfg: Config, mic, stt, ptt: bool = False, cancel: threading.Event | None = None) -> str:
    """Single spoken turn with a spinner (used by chat's /listen and `vision listen`)."""
    from vision.config import resolve_device
    from vision.tts import chime

    out_dev = resolve_device(cfg.voice.output_device, "output")
    live = None
    if ptt:
        console.print("[dim]recording… press Enter to stop[/dim]")
        audio = mic.record_until_enter()
    else:
        if cfg.listen.chime:
            chime("listen", out_dev)
        from vision.stt import LiveTranscript

        with console.status("[accent]listening…[/accent]", spinner="dots") as st:
            live = LiveTranscript(stt, lambda t: st.update(f"[accent]hearing you…[/accent] [italic]{escape(t)}[/italic]")) if cfg.listen.live_ms else None
            try:
                audio = mic.record_utterance(
                    on_speech_start=lambda: st.update("[accent]hearing you…[/accent]"), cancel=cancel,
                    on_audio=live.feed if live is not None else None, every_ms=cfg.listen.live_ms,
                    on_speech_end=live.feed_final if live is not None else None,
                )
            finally:
                if live is not None:
                    live.stop()
    if audio is None or audio.size == 0:
        return ""
    from vision import voiceid

    mine, score = voiceid.check(cfg.listen, audio, console.print)
    if not mine:
        console.print(f"[dim]{voiceid.rejected_note(score)}[/dim]")
        return ""
    with console.status("[dim]transcribing…[/dim]", spinner="dots"):
        return _final_transcript(live, stt, audio)



@app.command()
def talk(
    ptt: bool = typer.Option(False, "--ptt", help="Push-to-talk: Enter on an empty box starts recording and sends it."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    effort: Optional[str] = typer.Option(None, "--effort"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the last Vision conversation."),
):
    """Speech-to-speech conversation: the chat, opened with /talk on."""
    chat(speak=False, model=model, effort=effort, voice=voice, cont=cont, talk=True, ptt=ptt)


# ---------------------------------------------------------------- one-shots
@app.command()
def ask(
    question: list[str] = typer.Argument(None, help="Question text. Omit to read from stdin."),
    speak: bool = typer.Option(False, "--speak", "-s", help="Also read the answer aloud."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    effort: Optional[str] = typer.Option(None, "--effort"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the last Vision conversation."),
    plain: bool = typer.Option(False, "--plain", help="Plain text output (for piping)."),
):
    """Ask Vision one question and exit."""
    text = " ".join(question) if question else sys.stdin.read()
    text = text.strip()
    if not text:
        raise typer.BadParameter("empty question")
    cfg = _cfg(model, voice, effort)
    brain = _brain(cfg, voice_mode=speak and not plain, cont=cont, new=False)
    try:
        if plain:
            turn = brain.ask(text)
            if turn.is_error and not turn.text:
                console.print(f"[err]✗[/err] {turn.error}", file=sys.stderr)
                raise typer.Exit(1)
            print(turn.text)
            if speak:
                _speaker(cfg).say(turn.text)
            return
        _run_turn(brain, text, _speaker(cfg) if speak else None)
    finally:
        getattr(brain, "close", lambda: None)()  # a process kept for a next turn has none here


@app.command()
def say(
    text: list[str] = typer.Argument(None, help="Text to speak. Omit to read from stdin."),
    voice: Optional[str] = typer.Option(None, "--voice", "-v", help="Voice preset or name."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write a WAV file instead of playing."),
):
    """Text-to-speech: speak text in Vision's voice (or save it to a WAV)."""
    msg = " ".join(text) if text else sys.stdin.read()
    msg = msg.strip()
    if not msg:
        raise typer.BadParameter("nothing to say")
    cfg = _cfg(None, voice)
    sp = _speaker(cfg)
    if out:
        p = sp.save(msg, out)
        console.print(f"[dim]wrote[/dim] {p}")
        return
    try:
        sp.say(msg)
    except KeyboardInterrupt:
        sp.stop()


@app.command()
def weather(
    place: list[str] = typer.Argument(None, help="A place name (\"Austin, TX\"), or several split by ';'. Omit for home."),
    tomorrow: bool = typer.Option(False, "--tomorrow", help="Add tomorrow to the report."),
    week: bool = typer.Option(False, "--week", help="The whole outlook: tomorrow and the days after."),
    raw: bool = typer.Option(False, "--raw", help="Print the forecast JSON (WeatherKit's shape) instead of the report."),
):
    """Live weather (the report the voice hears; also for the typed brain): Apple WeatherKit when its
    key is set up, otherwise the US National Weather Service or Open-Meteo, which need no account.

    Covers now, the next twelve hours and today unless --tomorrow or --week asks for more."""
    from vision.weather import WeatherError, WeatherKit, lookup_many

    cfg = load_config()
    wk = WeatherKit(cfg.weather)
    name = " ".join(place).strip() if place else None
    scope = "week" if week else "tomorrow" if tomorrow else "today"
    try:
        if name and ";" in name and not raw:
            print(lookup_many(cfg.weather, [p.strip() for p in name.split(";") if p.strip()], scope))
        elif raw:
            spot = wk.geocode(name) if name else wk.default_place()
            print(json.dumps(wk.raw(spot), indent=1))
        else:
            print(wk.report(name, scope=scope))
    except WeatherError as e:
        raise SystemExit(f"weather: {e}")


def _memory_import(source: str) -> None:
    """`vision memory import [FILE|-]`: another assistant's notes about the user, offered as a tick list."""
    from vision import memimport, memreview
    from vision.ui import ask_questions

    if source:
        try:
            text = sys.stdin.read() if source == "-" else Path(source).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise SystemExit(f"memory import: {e}")
        sources = [memimport.Source(f"notes from {'stdin' if source == '-' else Path(source).name}", text)] if text.strip() else []
    else:
        sources = memimport.claude_sources()
    if not sources:
        print("nothing to import: no Claude Code notes found" if not source else "that file is empty")
        return
    console.print(f"[dim]reading {len(sources)} note{'s' if len(sources) != 1 else ''}:[/dim]")
    for s in sources[:12]:
        console.print(f"[dim]  {s.label}[/dim]")
    if len(sources) > 12:
        console.print(f"[dim]  …and {len(sources) - 12} more[/dim]")
    cfg = _cfg(None, None, None, quiet=True)
    with console.status("[dim]picking out what's worth keeping…[/dim]") as status:
        props = memimport.proposals(cfg, sources, on_batch=lambda i, n: n > 1 and status.update(f"[dim]picking out what's worth keeping ({i} of {n})…[/dim]"))
    if not props:
        print("nothing new to bring in: Vision's memory already has it, or there was nothing durable")
        return
    saved = memreview.save(memimport.chosen(props, ask_questions(memimport.questions(props))))
    for name, line in saved:
        print(f"{name}: {line}")
    print(memreview.summary(saved))


@app.command()
def memory(words: list[str] = typer.Argument(None, help="Words to look for, a topic name, `tidy` or `review`. Omit for the core and the topic list.")):
    """Vision's long-term memory: the core, one topic, or every line matching some words.

    `vision memory tidy` has the default model clean it up (merge repeats, drop what is stale or only
    describes code, regroup by project); a copy of the folder is kept first. `vision memory review`
    reads the last conversation for facts worth keeping and lets you tick the ones to save.
    `vision memory import` brings in what Claude Code has noted about you (its CLAUDE.md and every
    project's memory), so Codex, Grok and the rest know it too; `vision memory import FILE` (or `-` for
    stdin) reads another assistant's notes instead, such as a claude.ai memory export."""
    from vision import memory as mem

    arg = " ".join(words or []).strip()
    names = {t[0] for t in mem.topics()}
    if arg.split(" ", 1)[0].lower() == "import" and "import" not in names:
        _memory_import(arg[len("import"):].strip())
        return
    if arg.lower() == "review" and "review" not in names:
        from vision import memreview
        from vision.sessions import list_all_sessions, session_history
        from vision.ui import ask_questions

        newest = max(list_all_sessions(limit=1), key=lambda s: s.last_active, default=None)
        if newest is None:
            raise SystemExit("no conversation to review yet")
        console.print(f"[dim]reviewing “{newest.title}” ({newest.age()})…[/dim]")
        cfg = _cfg(None, None, None, quiet=True)
        props = memreview.proposals(cfg, session_history(newest.provider, newest.id, limit=0))
        if not props:
            print("nothing new worth remembering")
            return
        saved = memreview.save(memreview.chosen(props, ask_questions([memreview.question(props)])))
        for name, line in saved:
            print(f"{name}: {line}")
        print(memreview.summary(saved))
        return
    if arg.lower() == "tidy" and "tidy" not in names:
        saved = mem.backup()
        console.print(f"[dim]memory copied to {saved}[/dim]")
        cfg = _cfg(None, None, None)
        brain = _brain(cfg, voice_mode=False, cont=False, new=True)
        try:
            _run_turn(brain, mem.tidy_prompt(saved), None)
        finally:
            getattr(brain, "close", lambda: None)()
        return
    try:
        topic = mem.slug(arg) if arg else ""
    except ValueError:
        topic = ""
    if arg and topic not in names:
        for name, ln in mem.search(arg):
            print(f"{name}: {ln}")
        return
    print("\n".join(mem.facts(topic)))
    if not topic:
        for name, about, n in mem.topics():
            print(f"{name}.md ({n}): {about}")


@app.command()
def listen(
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Transcribe an audio file instead of the mic."),
    ptt: bool = typer.Option(False, "--ptt", help="Record until Enter instead of auto end-pointing."),
    forever: bool = typer.Option(False, "--forever", help="Keep transcribing utterances until Ctrl-C."),
):
    """Speech-to-text: transcribe your microphone (or a file) and print the text."""
    from vision.stt import Transcriber

    cfg = load_config()
    stt = Transcriber(cfg.listen)
    if file:
        with console.status("[dim]transcribing…[/dim]"):
            print(stt.transcribe_file(str(file)))
        return
    from vision.audio import Microphone

    mic = Microphone(cfg.listen)
    with console.status("[dim]loading speech recognition…[/dim]"):
        stt.warm_up()
    try:
        while True:
            heard = _listen_once(cfg, mic, stt, ptt=ptt)
            if heard:
                print(heard, flush=True)
            if not forever:
                break
    except KeyboardInterrupt:
        pass


@app.command()
def voices(
    preview: bool = typer.Option(False, "--preview", "-p", help="Speak a sample line in each voice."),
    text: str = typer.Option("Good afternoon. Vision online, all systems nominal.", "--text", help="Sample line for --preview."),
):
    """List the voices of the configured engine (`[voice] engine` in the config)."""
    cfg = load_config()
    choices = voice_choices(cfg.voice)
    if cfg.voice.engine == "orpheus":
        t = Table(title="Orpheus voices", show_header=True, header_style="table.header", border_style=LINE, title_style="heading", title_justify="left")
        t.add_column("voice"), t.add_column("note")
        for n, note in choices:
            t.add_row(n, note or ("female" if n in ("tara", "leah", "jess", "mia", "zoe") else "male"))
        console.print(t)
        console.print("[dim]Inline sounds the model can act: <laugh> <chuckle> <sigh> <gasp> <groan> <yawn> <cough> <sniffle>[/dim]")
    else:
        t = Table(title=f"Qwen3-TTS voices in {VOICES_DIR}", show_header=True, header_style="table.header", border_style=LINE, title_style="heading", title_justify="left")
        t.add_column("voice"), t.add_column("kind"), t.add_column("description")
        for n, note in choices:
            design = voice_dir(n) / "design.txt"
            desc = design.read_text(encoding="utf-8").strip() if design.is_file() else ""
            t.add_row(f"[bold]{n}[/bold]" if n == cfg.voice.voice else n, note, desc[:90] + ("…" if len(desc) > 90 else ""))
        console.print(t)
        console.print("[dim]Make more: `vision voice design <name> \"<description>\"` or `vision voice add <name> --from clip.wav`[/dim]")
    playable = [n for n, note in choices if not note.startswith("built-in design, not made")]
    if preview and playable:
        sp = _speaker(cfg)
        for n in playable:
            console.print(f"  ▶ {n}")
            try:
                sp.set_voice(n)
                sp.say(text)
            except KeyboardInterrupt:
                sp.stop()
                break


voice_app = typer.Typer(help="Make and manage voices for the Qwen3-TTS engine.", no_args_is_help=True)
app.add_typer(voice_app, name="voice")

provider_app = typer.Typer(help="The model providers /model offers: the built-in CLIs, and any OpenAI-compatible server you add.", no_args_is_help=True)
app.add_typer(provider_app, name="provider")


@provider_app.command("list")
def provider_list():
    """Every provider Vision knows, whether it is on (/providers) and set up here."""
    from vision.providers import REGISTRY, ready

    cfg = load_config()
    t = Table(header_style="table.header", show_edge=False, border_style=LINE)
    for col in ("provider", "on", "ready", "runs through"):
        t.add_column(col)
    for p in REGISTRY.values():
        on = p.name in cfg.providers.enabled
        ok = ready(p.name, cfg)
        where = p.endpoint.base_url if p.endpoint else p.product if p.cli else p.note.removeprefix("via ")
        t.add_row(f"{p.label} [dim]({p.name})[/dim]", "[ok]yes[/ok]" if on else "[dim]no[/dim]",
                  "[ok]yes[/ok]" if ok else f"[warn]no[/warn] [dim]{p.setup}[/dim]", where)
    console.print(t)
    for n in cfg.providers.notes:
        console.print(f"[warn]![/warn] {n}")


@app.command("providers")
def providers_cmd():
    """Choose your providers: tick your Claude, ChatGPT or Grok subscription, a model server, an API key
    or an ACP agent and it's installed, signed in or pointed at; untick one to switch it off. `vision`
    runs this by itself the first time it starts with nothing ready."""
    provider_setup()


@provider_app.command("status")
def provider_status(as_json: bool = typer.Option(False, "--json", help="Print it as JSON (the desktop app's setup reads it).")):
    """Claude Code, Codex and Grok: installed, signed in, and how to install each and sign in."""
    import json

    from vision.welcome import setup_rows

    rows = setup_rows()
    if as_json:
        print(json.dumps(rows))
        return
    t = Table(header_style="table.header", show_edge=False, border_style=LINE)
    for col in ("provider", "installed", "signed in", "sign in with"):
        t.add_column(col)
    yes, no = "[ok]yes[/ok]", "[warn]no[/warn]"
    for r in rows:
        t.add_row(r["product"], yes if r["installed"] else no, yes if r["signed_in"] else no, " ".join(r["login_cmd"]))
    console.print(t)


@provider_app.command("setup")
def provider_setup():
    """The same as `vision providers`."""
    from vision.welcome import ConsoleIO, Wizard

    try:
        Wizard(ConsoleIO(console)).run()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Setup stopped.[/dim]")


@provider_app.command("add")
def provider_add(
    name: Optional[str] = typer.Argument(None, help="A short name: ollama, openrouter, lmstudio… (its models show as <name>/<model>)."),
    url: Optional[str] = typer.Option(None, "--url", help="The server's OpenAI-compatible base URL, ending in /v1."),
    label: Optional[str] = typer.Option(None, "--label", help="How /model names it (default: the name capitalised)."),
    key_env: Optional[str] = typer.Option(None, "--key-env", help="Environment variable holding its API key, if it needs one."),
    context: Optional[int] = typer.Option(None, "--context", help="Context size in tokens (default 32768)."),
    models: Optional[str] = typer.Option(None, "--models", help="Comma-separated model ids, when the server can't list its own."),
    command: Optional[str] = typer.Option(None, "--command", help="An ACP agent instead of a server: the command that speaks ACP over stdio, e.g. \"gemini --experimental-acp\"."),
    model_flag: Optional[str] = typer.Option(None, "--model-flag", help="ACP agent: the flag that picks a model (\"--model\"), when --models names several."),
):
    """Add a provider: an OpenAI-compatible server (Ollama, LM Studio, vLLM, OpenRouter, a hosted API), or
    with --command an agent that speaks ACP. Asks what it needs, checks it answers, writes [providers.<name>]
    to config.toml."""
    import os
    import re
    import shutil

    from vision.config import save_provider_table
    from vision.providers import REGISTRY, Endpoint

    name = (name or typer.prompt("Short name (e.g. ollama)")).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise typer.BadParameter("a plain lowercase word: letters, digits, - or _")
    if name in REGISTRY and REGISTRY[name].source != "config":
        raise typer.BadParameter(f"{name} is a built-in provider")
    if command:
        argv = command.split()
        if not shutil.which(argv[0]):
            console.print(f"[warn]![/warn] {argv[0]} is not on PATH here; the provider will show as not set up until it is.")
        ids = [m.strip() for m in (models or "").split(",") if m.strip()]
        save_provider_table(name, {"type": "acp", "command": argv, "label": label or "", "models": ids, "model_flag": model_flag or ""})
        console.print(f"[ok]✓[/ok] \\[providers.{name}] saved to {CONFIG_PATH}: an ACP agent, in /model as {name}/default"
                      + (f" and {name}/<model>" if ids else "") + ". Restart Vision (`vision restart`) to pick it up.")
        return
    asked = url is None  # given as options, the rest isn't asked for either (scripts, no terminal)
    url = (url or typer.prompt("Base URL", default="http://localhost:11434/v1")).strip().rstrip("/")
    if not re.match(r"https?://", url):
        raise typer.BadParameter("the URL must start with http:// or https://")
    if key_env is None:
        key_env = typer.prompt("Environment variable holding the API key (blank if none)", default="", show_default=False).strip() if asked else ""
    if key_env and not os.environ.get(key_env):
        console.print(f"[warn]![/warn] {key_env} is not set in this shell; the server will be asked without a key for now.")
    ep = Endpoint(url, api_key_env=key_env or "", context=context or 32768)
    ids: list[str] = [m.strip() for m in (models or "").split(",") if m.strip()]
    if not ids:
        with console.status("[dim]asking the server for its models…[/dim]"):
            try:
                import urllib.request

                req = urllib.request.Request(url + "/models", headers={"User-Agent": "vision", **ep.headers()})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode())
                ids = [str(m["id"]) for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]
            except Exception as e:  # noqa: BLE001
                console.print(f"[err]✗[/err] {url}/models did not answer: {e}")
                if not typer.confirm("Save it anyway?", default=False):
                    raise typer.Exit(1)
    if ids:
        console.print(f"[dim]{len(ids)} model{'s' if len(ids) != 1 else ''}: {', '.join(ids[:6])}{'…' if len(ids) > 6 else ''}[/dim]")
    values = {"base_url": url, "label": label or "", "api_key_env": key_env or "", "context": context, "models": [m.strip() for m in (models or "").split(",") if m.strip()]}
    save_provider_table(name, values)
    console.print(f"[ok]✓[/ok] \\[providers.{name}] saved to {CONFIG_PATH}: its models show in /model as {name}/<model>."
                  + " Restart Vision (`vision restart`) to pick it up." )


@provider_app.command("remove")
def provider_remove(name: str = typer.Argument(..., help="The provider's short name.")):
    """Remove a [providers.<name>] table from config.toml (built-in providers are switched off with /providers instead)."""
    from vision.config import remove_provider_table
    from vision.providers import REGISTRY

    load_config()
    p = REGISTRY.get(name)
    if p is not None and p.source != "config":
        raise typer.BadParameter(f"{name} is not a config-defined provider; switch it off with /providers")
    if p is None:
        raise typer.BadParameter(f"no [providers.{name}] in {CONFIG_PATH}")
    remove_provider_table(name)
    console.print(f"[ok]✓[/ok] \\[providers.{name}] removed from {CONFIG_PATH}")


def _save_voice(name: str, audio, transcript: str | None, design: str | None) -> Path:
    import soundfile as sf

    from vision.tts import SAMPLE_RATE

    d = voice_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / "ref.wav"), audio, SAMPLE_RATE)
    if transcript:
        (d / "ref.txt").write_text(transcript.strip() + "\n", encoding="utf-8")
    elif (d / "ref.txt").exists():
        (d / "ref.txt").unlink()
    if design:
        (d / "design.txt").write_text(design.strip() + "\n", encoding="utf-8")
    elif (d / "design.txt").exists():
        (d / "design.txt").unlink()
    return d


def _check_voice_name(name: str) -> str:
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise typer.BadParameter("voice names are lowercase letters, digits, - and _")
    return name


@voice_app.command("setup")
def voice_setup():
    """Set up the voice: checks this machine can run it (an Apple Silicon Mac or an NVIDIA graphics card),
    then downloads the voice build and its speech models."""
    from vision.welcome import ConsoleIO, voice_step

    try:
        voice_step(ConsoleIO(console), asked=True)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Stopped. `vision voice setup` picks it back up.[/dim]")


@voice_app.command("check")
def voice_check():
    """Check the voice end to end: Vision says a line and asks whether you heard it, listens to one and
    shows what it made of it, then waits for its name. A check that fails offers the other speakers or
    microphones. `vision voice setup` runs this once the voice is installed."""
    from vision import voicecheck
    from vision.welcome import ConsoleIO

    if voice_backend() is None:
        console.print("[warn]Voice isn't installed here.[/warn] `vision voice setup` adds it.")
        raise typer.Exit(1)
    try:
        ok = voicecheck.check(ConsoleIO(console))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Stopped. `vision voice check` runs it again.[/dim]")
        raise typer.Exit(1)
    if not ok:
        raise typer.Exit(1)


@voice_app.command("design")
def voice_design(
    name: str = typer.Argument(..., help="Name for the new voice (e.g. jarvis)."),
    description: Optional[str] = typer.Argument(None, help="How the voice should sound. Omit for a built-in design (jarvis, friday)."),
    text: Optional[str] = typer.Option(None, "--text", help="The line the model speaks to create the reference clip (8-12 s of speech is ideal)."),
    takes: int = typer.Option(3, "--takes", "-n", min=1, max=9, help="How many variations to generate and audition."),
    keep: Optional[int] = typer.Option(None, "--keep", "-k", help="Keep this take without listening (1-based)."),
    use: bool = typer.Option(False, "--use", help="Also make it the default voice in the config."),
):
    """Invent a voice from a text description (Qwen3-TTS VoiceDesign), audition takes and save the best one."""
    from vision.tts import SAMPLE_RATE, design_voice, qwen_present

    name = _check_voice_name(name)
    if description is None:
        if name not in VOICE_DESIGNS:
            raise typer.BadParameter(f"no built-in design for {name!r}; give a description (built-ins: {', '.join(VOICE_DESIGNS)})")
        description, default_text = VOICE_DESIGNS[name]
    else:
        default_text = VOICE_DESIGNS["jarvis"][1]
    text = text or default_text
    cfg = load_config()
    if not qwen_present(QWEN_TTS_DESIGN):
        _fetch_hf(QWEN_TTS_DESIGN)
    console.print(f"[bold]{name}[/bold]: {description}")
    console.print(f"[dim]says: {text}[/dim]")
    clips = []
    with console.status(f"[dim]designing the voice ({takes} take{'s' if takes > 1 else ''})…[/dim]") as st:
        for i, audio in enumerate(design_voice(description, text, cfg.voice.language, cfg.voice.device, takes), 1):
            clips.append(audio)
            st.update(f"[dim]designing the voice (take {i}/{takes} done)…[/dim]")
    if keep is None and takes == 1:
        keep = 1
    if keep is None:
        import sounddevice as sd

        from vision import devrate
        from vision.config import resolve_device

        out = resolve_device(cfg.voice.output_device, "output")
        console.print("[dim]Playing each take. Answer with the number to keep, r to replay, q to give up.[/dim]")
        while True:
            for i, audio in enumerate(clips, 1):
                console.print(f"  ▶ take {i}  ({len(audio) / SAMPLE_RATE:.1f} s)")
                try:
                    devrate.play(audio, SAMPLE_RATE, out)
                    sd.wait()
                except KeyboardInterrupt:
                    sd.stop()
                    break
            ans = typer.prompt("keep", default="1").strip().lower()
            if ans == "r":
                continue
            if ans == "q":
                raise typer.Exit(1)
            if ans.isdigit() and 1 <= int(ans) <= len(clips):
                keep = int(ans)
                break
            console.print("[err]✗[/err] number, r or q")
    if not 1 <= keep <= len(clips):
        raise typer.BadParameter(f"--keep must be 1..{len(clips)}")
    d = _save_voice(name, clips[keep - 1], text, description)
    console.print(f"[ok]✓[/ok] voice [bold]{name}[/bold] saved in {d}")
    if use or cfg.voice.voice == name:
        save_voice_default(name)
        console.print(f"[ok]✓[/ok] default voice → {name}")
    else:
        console.print(f"[dim]use it: `vision voice use {name}` or `/voice {name}` in a chat[/dim]")


@voice_app.command("add")
def voice_add(
    name: str = typer.Argument(..., help="Name for the voice."),
    clip: Path = typer.Option(..., "--from", "-f", exists=True, dir_okay=False, help="A clean 5-15 s recording of the voice (WAV/FLAC/MP3)."),
    text: Optional[str] = typer.Option(None, "--text", help="Exact transcript of the clip. Omit to transcribe it with Whisper."),
    use: bool = typer.Option(False, "--use", help="Also make it the default voice in the config."),
):
    """Clone a voice from a recording. Only clone voices you have the right to use."""
    import numpy as np
    import soundfile as sf

    from vision.tts import SAMPLE_RATE

    name = _check_voice_name(name)
    audio, sr = sf.read(str(clip), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        idx = np.arange(0, len(audio), sr / SAMPLE_RATE)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    secs = len(audio) / SAMPLE_RATE
    if secs < 2:
        raise typer.BadParameter("the clip is too short; give the model at least a few seconds of speech")
    if secs > 30:
        console.print("[warn]![/warn] clips over ~15 s do not clone better; using the first 20 s")
        audio = audio[: SAMPLE_RATE * 20]
    if text is None:
        from vision.stt import Transcriber

        cfg = load_config()
        with console.status("[dim]transcribing the clip with Whisper…[/dim]"):
            text = Transcriber(cfg.listen).transcribe(audio, SAMPLE_RATE).strip()
        console.print(f"[dim]heard: {text}[/dim]")
        if not text:
            console.print("[warn]![/warn] nothing transcribed; saving without a transcript (speaker-embedding only, a little less faithful)")
            text = None
    d = _save_voice(name, audio, text, None)
    console.print(f"[ok]✓[/ok] voice [bold]{name}[/bold] saved in {d}")
    if use:
        save_voice_default(name)
        console.print(f"[ok]✓[/ok] default voice → {name}")


ENROL_PHRASES = (
    "Vision, what's the weather looking like this afternoon?",
    "Remind me to call the dentist tomorrow at ten.",
    "Open the project folder and run the tests again.",
)


def _clip_16k(path: Path):
    """A file's audio as float32 mono 16 kHz, the mic's own format."""
    import numpy as np
    import soundfile as sf

    from vision.voiceid import SAMPLE_RATE

    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        idx = np.arange(0, len(audio), sr / SAMPLE_RATE)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


@voice_app.command("enrol")
@voice_app.command("enroll", hidden=True)
def voice_enrol(
    clips: list[Path] = typer.Option(None, "--from", "-f", exists=True, dir_okay=False, help="Recordings of your voice (WAV/FLAC/MP3) instead of the mic; give two or three."),
    check: bool = typer.Option(False, "--check", help="Record one phrase and show how well it matches the enrolment."),
    forget: bool = typer.Option(False, "--forget", help="Delete the enrolment."),
):
    """Teach Vision your voice: say three short phrases (or give recordings) and it keeps a voiceprint;
    with only_me = true in the config's listen section anyone else's voice is ignored. Only your own voice, please."""
    from vision import voiceid

    if forget:
        console.print(f"[ok]✓[/ok] enrolment removed" if voiceid.forget_enrolment() else "nothing enrolled")
        return
    cfg = load_config()
    with console.status("[dim]loading the speaker model (downloads 26 MB on first run)…[/dim]"):
        vid = voiceid.VoiceId()
        vid.load()
    if check:
        if not voiceid.enrolled():
            console.print("[warn]nothing enrolled yet[/warn]  → vision voice enrol")
            raise typer.Exit(1)
        audio = _clip_16k(clips[0]) if clips else _record_phrase(cfg, "say anything, a sentence or two")
        if audio is None or audio.size == 0:
            console.print("[warn]![/warn] didn't catch that")
            raise typer.Exit(1)
        score = vid.score(audio)
        verdict = "that's you" if score >= cfg.listen.only_me_threshold else "not recognised"
        console.print(f"{score:.2f} · {verdict} (threshold {cfg.listen.only_me_threshold:.2f}: only_me_threshold under \\[listen])")
        return
    takes = []
    if clips:
        for clip in clips:
            audio = _clip_16k(clip)
            if len(audio) < voiceid.MIN_SPEECH_S * voiceid.SAMPLE_RATE:
                raise typer.BadParameter(f"{clip} is too short; a few seconds of speech each")
            takes.append(audio)
    else:
        console.print("Three short phrases, one at a time. Speak as you would to Vision.")
        for phrase in ENROL_PHRASES:
            audio = _record_phrase(cfg, f"say: [bold]{phrase}[/bold]")
            if audio is None or audio.size == 0:
                console.print("[warn]![/warn] didn't catch that; once more")
                audio = _record_phrase(cfg, f"say: [bold]{phrase}[/bold]")
            if audio is None or audio.size == 0:
                raise typer.Exit(1)
            takes.append(audio)
    with console.status("[dim]making the voiceprint…[/dim]"):
        embeddings = [vid.embed(a) for a in takes]
        path = voiceid.save_enrolment(embeddings)
        vid._enrolment = None  # re-read: the scores below are against what was just saved
        scores = [vid.score(a) for a in takes]
    console.print(f"[ok]✓[/ok] your voice is enrolled ({path})")
    console.print(f"  each phrase against the voiceprint: {', '.join(f'{s:.2f}' for s in scores)} (threshold {cfg.listen.only_me_threshold:.2f})")
    if min(scores) < cfg.listen.only_me_threshold + 0.1:
        console.print("[warn]![/warn] one phrase scores close to the threshold: enrol again somewhere quieter, or lower only_me_threshold a little")
    if not cfg.listen.only_me:
        console.print("  switch it on with [bold]only_me = true[/bold] under \\[listen] in the config (vision config --edit)")


def _record_phrase(cfg: Config, prompt: str):
    """One utterance from the mic for the enrolment, end-pointed like a /talk turn."""
    from vision.audio import Microphone
    from vision.config import resolve_device
    from vision.tts import chime

    mic = Microphone(cfg.listen)
    if cfg.listen.chime:
        chime("listen", resolve_device(cfg.voice.output_device, "output"))
    with console.status(f"[accent]{prompt}[/accent]", spinner="dots") as st:
        return mic.record_utterance(on_speech_start=lambda: st.update("[accent]hearing you…[/accent]"), start_timeout_s=20)


@voice_app.command("use")
def voice_use(name: str = typer.Argument(..., help="A saved voice (qwen3) or an Orpheus voice/preset.")):
    """Make a voice the default in the config."""
    cfg = load_config()
    names = [n for n, _ in voice_choices(cfg.voice)]
    if name not in names:
        raise typer.BadParameter(f"unknown voice {name!r}; run `vision voices`")
    save_voice_default(name)
    console.print(f"[ok]✓[/ok] default voice → {name}")


@voice_app.command("rm")
def voice_rm(name: str = typer.Argument(..., help="A saved voice to delete.")):
    """Delete a saved voice."""
    import shutil as _shutil

    if name not in saved_voices():
        raise typer.BadParameter(f"no saved voice {name!r}")
    _shutil.rmtree(voice_dir(name))
    console.print(f"[ok]✓[/ok] removed {name}")


# ---------------------------------------------------------------- remote (iOS app)
@app.command("recover")
def recover_turns():
    """Show prompts, partial replies and queued messages saved before a crash."""
    from vision.turnjournal import interrupted_journals

    chats = interrupted_journals()
    if not chats:
        console.print("No interrupted turns or queued messages.")
        return
    for chat_id, state in chats:
        console.print(f"\n[bold]{chat_id}[/bold]  {state['model'] or 'unknown model'}")
        for row in state["recovery_rows"]:
            if row.get("role") == "user":
                console.print(Text(f"You: {row.get('text') or ''}"))
            else:
                if row.get("text"):
                    console.print(Text(f"Vision: {row['text']}"))
                for tool in row.get("tools") or []:
                    status = "interrupted" if tool.get("cut_off") else "failed" if tool.get("is_error") else "done"
                    label = " ".join(part for part in (tool.get("name"), tool.get("detail")) if part)
                    console.print(Text(f"  {label or 'Tool'} ({status})"))
                if row.get("error"):
                    console.print(f"[warn]![/warn] {row['error']}")


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host", help="Bind address (config: remote.host, default 0.0.0.0: the LAN, for the phone)."),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Port (config: remote.port, default 8765)."),
    public_url: Optional[str] = typer.Option(None, "--public-url", help="The https:// address the phone uses (your tunnel)."),
    new_token: bool = typer.Option(False, "--new-token", help="Rotate the pairing token (old phones must re-pair)."),
    no_qr: bool = typer.Option(False, "--no-qr", help="Do not print the pairing QR code."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    effort: Optional[str] = typer.Option(None, "--effort"),
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    cont: bool = typer.Option(False, "--continue", "-c", help="Continue the last Vision conversation."),
    new: bool = typer.Option(False, "--new", help="Open a fresh chat on startup."),
):
    """Serve Vision to the Vision Remote iOS app (chat + voice over HTTP/WebSocket). Pair by scanning the QR code."""
    from vision.remote import running_server
    from vision.server import TOKEN_FILE, pairing_info, serve as run_server

    cfg = _cfg(model, voice, effort)
    from vision import service

    def taken() -> dict | None:
        other = running_server()
        return other if other and other.get("port") == (port or cfg.remote.port) and other["pid"] != os.getpid() else None

    if os.environ.get(service.ENV):  # a unit written before install cleaned its PATH may still carry the shims
        from vision.brain import without_shims

        os.environ["PATH"] = without_shims(os.environ.get("PATH", ""))
    other = taken()  # looked at before the token is touched: --new-token must not strand a server that is up
    if other and os.environ.get(service.ENV):  # as the service: not a failure to retry, it takes over after
        console.print(f"vision serve is already running (pid {other['pid']}); the service takes over when it stops.")
        service.wait_while(lambda: taken() is not None)
    elif other:
        ours = service.running() and not service.waiting(other)  # a waiting service is not the one serving
        what = "as the background service" if ours else f"in another window (pid {other['pid']})"
        console.print(f"[warn]vision serve is already running {what}.[/warn] "
                      "`vision pair` shows its pairing code, `vision restart` restarts it"
                      + (", `vision service remove` stops the service." if ours else "."))
        raise typer.Exit(1)
    info = pairing_info(cfg, host, port, public_url, regenerate_token=new_token)
    brain = _brain(cfg, voice_mode=False, cont=cont, new=new)
    show_header(
        console,
        model=_brain_summary(cfg, brain),
        directory=short_path(brain.workdir),
        voice=cfg.voice.voice,
        resumed=bool(brain.session_id),
        extra=(
            f"listening on {info['local']}" + (f" · phone uses {info['url']}" if info["url"] != info["local"] else ""),
            f"token in {short_path(str(TOKEN_FILE))} · p shows the pairing code · r restarts once replies finish · c clears · Ctrl-C stops",
        ),
    )
    if not no_qr:
        _print_pairing(info)

    def log(msg: str):
        console.print(Text("  " + msg, style="dim"))

    def banner():
        """What `c` leaves on the cleared window: where it listens and the keys."""
        console.print(Text(f"  vision serve · {info['url']} · p shows the pairing code · r restarts once replies finish · c clears · Ctrl-C stops", style="dim"))

    if service.stay_awake() is not None:  # a Mac asleep is a phone that cannot reach Vision
        log("keeping this Mac awake while serving (on AC power)")
    run_server(cfg, brain, info, log=log, follow_defaults=not (model or effort), open_initial=cont or new, on_clear=banner,
               on_pair=lambda: _print_pairing(info))


def _print_pairing(info: dict) -> None:
    """The pairing code: the QR, the same thing to type by hand, and how far the address reaches."""
    from vision.server import qr_lines

    console.print()
    for line in qr_lines(info["payload"]):
        console.print("   " + line, markup=False, highlight=False)
    console.print("   [dim]scan it with the Vision Remote app[/dim]")
    console.print(f"   [dim]or enter by hand:[/dim] {info['url']}  [dim]token[/dim] {info['token']}")
    if info["url"] == info["local"]:
        where = "on this computer only" if info["local"].startswith("http://127.") else "on this Wi-Fi only"
        console.print(f"   [warn]{where}:[/warn] [dim]for anywhere, `tailscale serve --bg {info['port']}` then `vision pair` "
                      "for a new code (docs/guide.md, Remote).[/dim]")
    console.print()


@app.command()
def pair():
    """Show the pairing code for the Vision Remote app: the QR `vision serve` prints when it starts,
    again. For a server running in the background, or one whose window has scrolled past it."""
    from vision.remote import running_server
    from vision.server import pairing_info

    cfg = load_config()
    server = running_server()
    _print_pairing(pairing_info(cfg, None, server.get("port") if server else None, None))
    if server is None:
        console.print("[warn]vision serve isn't running[/warn], so the phone can't connect yet: "
                      "`vision serve` in a terminal, or `vision service install` to keep it running in the background.")
        raise typer.Exit(1)


service_app = typer.Typer(help="`vision serve` as a background service, so the phone finds Vision whenever the computer is on.", no_args_is_help=True)
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install():
    """Start `vision serve` in the background now and at every login (a systemd user unit on Linux, a
    launchd agent on macOS; no root). It runs with the PATH of this shell, so the CLIs it finds here
    are the ones it uses."""
    from vision import service
    from vision.remote import running_server

    server = running_server()
    if server and not service.running():
        console.print(f"[warn]vision serve is running in another window (pid {server['pid']}).[/warn] Stop it there first (Ctrl-C); the service takes the same port.")
        raise typer.Exit(1)
    try:
        path = service.install()
    except service.ServiceError as e:
        console.print(f"[err]✗[/err] {e}")
        raise typer.Exit(1)
    console.print(f"[ok]✓[/ok] vision serve runs in the background now and at every login [dim]({short_path(str(path))})[/dim]")
    if service.lingers() is False:
        console.print("[dim]It stops when you log out. `loginctl enable-linger` keeps it running without a login session.[/dim]")
    console.print(f"[dim]`vision pair` shows the pairing code · `vision restart` after an update · logs: {service.logs_hint()}[/dim]")


@service_app.command("remove")
def service_remove():
    """Stop the background service and take it out of login."""
    from vision import service

    try:
        removed = service.remove()
    except service.ServiceError as e:
        console.print(f"[err]✗[/err] {e}")
        raise typer.Exit(1)
    console.print("[ok]✓[/ok] the background service is gone; `vision serve` in a terminal still works" if removed
                  else "[dim]no background service was installed[/dim]")


@service_app.command("status")
def service_status():
    """Whether the background service is installed and running."""
    from vision import service
    from vision.remote import running_server

    if not service.installed():
        console.print("[dim]not installed[/dim] · `vision service install` sets it up")
        raise typer.Exit(1)
    server = running_server()
    if service.waiting(server):
        console.print(f"[warn]waiting[/warn] behind the vision serve in a terminal [dim](pid {server['pid']})[/dim]; "
                      f"it takes over when that one stops. [dim]Logs: {service.logs_hint()}[/dim]")
        return
    if service.running() and server:
        console.print(f"[ok]✓[/ok] running [dim]({server['url']}, pid {server['pid']}) · logs: {service.logs_hint()}[/dim]")
        return
    console.print(f"[warn]installed but not answering.[/warn] Logs: {service.logs_hint()}")
    raise typer.Exit(1)


@app.command()
def upgrade():
    """Update Vision from where it was installed, then restart `vision serve` on the new code."""
    from vision import selfupdate
    from vision.remote import running_server

    if not selfupdate.installed():
        console.print("[warn]This Vision is a working checkout, not the installed one.[/warn] Update it with `git pull`.")
        raise typer.Exit(1)
    try:
        gone = selfupdate.orphaned()
    except (OSError, subprocess.SubprocessError):
        gone = None  # the remote can't be asked: the installer says so itself
    if gone:
        try:
            now = selfupdate.follow_default()
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            console.print(f"[err]`{gone}`, the branch this Vision was installed from, is gone from its remote, "
                          f"and it couldn't be moved to the main one:[/err] {e}")
            raise typer.Exit(1)
        console.print(f"`{gone}`, the branch this Vision was installed from, is gone from its remote: now following `{now}`.")
    done = subprocess.run(["sh", str(selfupdate.ROOT / "install.sh")])
    if done.returncode != 0:
        console.print("[err]The installer failed[/err]; the running Vision is unchanged.")
        raise typer.Exit(done.returncode)
    selfupdate.forget()
    if running_server() is None:
        console.print("Updated. `vision serve` isn't running, so there is nothing to restart.")
        return
    restart()


@app.command()
def restart():
    """Restart `vision serve` on the new code once every chat has finished its reply; nothing is cut off."""
    import urllib.request

    from vision.remote import running_server

    server = running_server()
    if server is None:
        console.print("[warn]![/warn] vision serve isn't running")
        raise typer.Exit(1)
    req = urllib.request.Request(server["url"] + "/restart", data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {server['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310  (our own loopback server)
            waiting = int(json.loads(resp.read().decode()).get("waiting") or 0)
    except OSError as e:
        console.print(f"[err]couldn't reach vision serve:[/err] {e}")
        raise typer.Exit(1)
    if waiting:
        console.print(f"restarting once {waiting} chat{'s' * (waiting != 1)} finish{'es' * (waiting == 1)} replying")
    else:
        console.print("restarting now")


SCOUT_POLL_S = 20  # how often the chat asks vision serve for its scouts (the status row, the open card)


# ---------------------------------------------------------------- reminders and scheduled turns
def _serve_call(path: str, body: dict | None = None, method: str = "",
                why: str = "scheduled tasks and reminders fire from it") -> object:
    """One request to the `vision serve` on this machine; exits with a plain line when it can't be made."""
    import urllib.error
    import urllib.request

    from vision.remote import running_server

    server = running_server()
    if server is None:
        raise SystemExit(f"vision serve isn't running: {why}")
    req = urllib.request.Request(server["url"] + path, data=None if body is None else json.dumps(body).encode(),
                                 method=method or ("GET" if body is None else "POST"),
                                 headers={"Authorization": f"Bearer {server['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  (our own loopback server)
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise SystemExit(str(detail or e))
    except OSError as e:
        raise SystemExit(f"couldn't reach vision serve: {e}")


def _ancestors() -> list[int]:
    """This process's parents, nearest first: a terminal chat is found by its pid among them."""
    out, pid = [], os.getppid()
    while pid > 1 and len(out) < 32:
        out.append(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            try:
                pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or 0)
            except (OSError, ValueError):
                break
    return out


def _schedule(kind: str, text: str, at: str, delay: str, on: str, repeat: str) -> None:
    from datetime import datetime

    from vision.scheduled import parse_clock, when

    text = text.strip()
    if not text:
        raise SystemExit("say what it is for")
    if bool(at) == bool(delay):
        raise SystemExit("give one of --at 18:00 or --in 20m")
    try:
        if repeat and repeat != "once":
            if delay or on:
                raise ValueError("a repeating one takes --at only")
            body = {"repeat": repeat, "time": parse_clock(at)}
            if repeat == "weekly":
                body["weekday"] = datetime.now().weekday()
        else:
            date, clock = when(delay, at, on)
            body = {"repeat": "once", "date": date, "time": clock}
    except ValueError as e:
        raise SystemExit(str(e))
    body.update(kind=kind, prompt=text, chat=os.environ.get("VISION_CHAT", ""), pids=_ancestors())
    task = _serve_call("/scheduled", body)
    if not task.get("next_run"):
        _serve_call(f"/scheduled/{task['id']}", method="DELETE")
        raise SystemExit("that time has already passed")
    due = datetime.fromtimestamp(task["next_run"]).strftime("%a %d %b %H:%M")
    every = "" if task["repeat"] == "once" else f", then {task['repeat']}"
    print(f"{task['id']}: {kind} set for {due}{every}" + ("" if task.get("chat") else " (no chat to come back to: it opens its own)" if kind == "task" else ""))


_AT = typer.Option("", "--at", help="A clock time: 18:00, 6pm. Today if still ahead, else tomorrow.")
_IN = typer.Option("", "--in", help="A delay from now: 20m, 1h30m, 2d.")
_ON = typer.Option("", "--on", help="The day for --at: YYYY-MM-DD or tomorrow.")
_REPEAT = typer.Option("", "--repeat", help="daily, weekdays or weekly (on today's weekday); with --at.")


@app.command()
def remind(text: list[str] = typer.Argument(..., help="What to be reminded of."),
           at: str = _AT, delay: str = _IN, on: str = _ON, repeat: str = _REPEAT):
    """A reminder: the words come back at that time as a phone notification and a note in the chat
    it was set from. No model runs."""
    _schedule("reminder", " ".join(text), at, delay, on, repeat)


@app.command()
def schedule(prompt: list[str] = typer.Argument(..., help="What Vision should do then, as you would ask it."),
             at: str = _AT, delay: str = _IN, on: str = _ON, repeat: str = _REPEAT):
    """A scheduled turn: Vision runs the prompt at that time, in the chat it was set from (a fresh
    one when that chat has gone), and the phone is notified with the reply."""
    _schedule("task", " ".join(prompt), at, delay, on, repeat)


@app.command()
def scheduled(cancel: str = typer.Option("", "--cancel", help="Remove the task or reminder with this id."),
              test_push: bool = typer.Option(False, "--test-push", help="Send a test notification to the registered phones.")):
    """Every reminder and scheduled task with its id and next time; --cancel removes one."""
    from datetime import datetime

    if test_push:
        r = _serve_call("/push/test", {})
        print(f"sent to {r['devices'] - len(r['failed'])} of {r['devices']} phone{'s' * (r['devices'] != 1)}" + "".join(f"\n{f}" for f in r["failed"]))
        return
    if cancel:
        _serve_call(f"/scheduled/{cancel}", method="DELETE")
        print(f"removed {cancel}")
        return
    for t in _serve_call("/scheduled"):
        due = datetime.fromtimestamp(t["next_run"]).strftime("%a %d %b %H:%M") if t.get("next_run") else "off"
        print(f"{t['id']}  {due}  {t['repeat']:<8} {t.get('kind') or 'task':<8} {t['title']}")


# ---------------------------------------------------------------- projects (projects.py)
sessions_app = typer.Typer(help="Earlier conversations: list them, search their text, or save one as markdown.",
                           invoke_without_command=True)
app.add_typer(sessions_app, name="sessions")


def _sessions_providers(provider: str) -> tuple[str, ...] | None:
    from vision.sessions import PROVIDERS

    word = provider.strip().lower()
    if not word:
        return None
    if word not in PROVIDERS:
        console.print(f"[err]no provider called {escape(provider)}[/err] (one of {', '.join(PROVIDERS)})")
        raise typer.Exit(2)
    return (word,)


def _print_sessions(provider: str, n: int) -> None:
    from vision.sessions import list_sessions, PROVIDER_TITLES

    rows = [s for p in (_sessions_providers(provider) or PROVIDER_TITLES) for s in list_sessions(p, limit=n)]
    if not rows:
        console.print("[dim]no earlier conversations[/dim]")
        return
    t = Table(header_style="table.header", show_edge=False, box=None, padding=(0, 2, 0, 0))
    for col in ("id", "conversation", "provider", "last active", "folder"):
        t.add_column(col, no_wrap=col != "conversation", overflow="ellipsis")
    now = time.time()
    for s in rows:
        t.add_row(f"[dim]{s.short_id}[/dim]", escape(s.title), PROVIDER_TITLES.get(s.provider, s.provider),
                  f"[dim]{s.age(now)}[/dim]", escape(short_path(s.cwd)) if s.cwd else "")
    console.print(t)


@sessions_app.callback()
def sessions_root(ctx: typer.Context):
    """Earlier conversations: list them, search their text, or save one as markdown."""
    if ctx.invoked_subcommand is None:
        _print_sessions("", 30)


@sessions_app.command("list")
def sessions_list(provider: str = typer.Option("", "--provider", "-p", help="claude, codex, grok or local (default: every provider)."),
                  n: int = typer.Option(30, "-n", help="How many per provider.")):
    """The conversations /session offers, newest first within each provider."""
    _print_sessions(provider, n)


@sessions_app.command("grep")
def sessions_grep(words: list[str] = typer.Argument(..., help="Words that must all appear (case-insensitive)."),
                  all_lines: bool = typer.Option(False, "--all-lines", help="Every matching line, not just the best three."),
                  provider: str = typer.Option("", "--provider", "-p", help="claude, codex, grok or local (default: every provider).")):
    """Search what you and Vision said in every conversation, newest first."""
    from vision.sessions import GREP_LINES, PROVIDER_TITLES, grep_sessions, match_window

    needles = [w.lower() for w in words if w.strip()]
    hits = grep_sessions(needles, per_session=0 if all_lines else GREP_LINES, providers=_sessions_providers(provider))
    if not hits:
        console.print("[dim]nothing matches[/dim]")
        raise typer.Exit(1)
    now = time.time()
    for i, m in enumerate(hits):
        if i:
            console.print()
        s = m.info
        head = Text.assemble((s.short_id, "bold"), " · ", s.title, " · ", PROVIDER_TITLES.get(s.provider, s.provider), " · ", (s.age(now), "dim"))
        if s.cwd:
            head.append(" · ").append(short_path(s.cwd), style="dim")
        console.print(head)
        for role, line in m.lines:
            row = Text("  ").append("you › " if role == "user" else "vision › ", style="dim")
            row.append(match_window(line, needles))
            row.highlight_words(needles, f"bold {WARN}", case_sensitive=False)
            console.print(row)
        if m.total > len(m.lines):
            console.print(f"  [dim]+{m.total - len(m.lines)} more lines (--all-lines shows them)[/dim]")


@sessions_app.command("export")
def sessions_export(ref: str = typer.Argument(..., help="The conversation's id, or the start of it."),
                    out: str = typer.Option("", "--out", "-o", help="A file, or a folder for the default name (default: ~/Documents/vision/<date>-<title>.md).")):
    """Save a conversation as markdown: a section per turn, tool calls left out."""
    from vision.sessions import export_session, find_any_session

    info = find_any_session(ref)
    if info is None:
        console.print(f"[err]✗[/err] no conversation starts with “{escape(ref)}”")
        raise typer.Exit(1)
    try:
        path = export_session(info, out)
    except (OSError, ValueError) as e:
        console.print(f"[err]✗[/err] {escape(str(e))}")
        raise typer.Exit(1)
    console.print(f"saved to {escape(short_path(path))}")


project_app = typer.Typer(help="Projects: a goal, a folder and instructions around one chat. They live in `vision serve`.",
                          no_args_is_help=True)
app.add_typer(project_app, name="project")
_PROJECTS_NEED = "projects live in it (start it with `vision serve`)"


def _project_call(path: str, body: dict | None = None, method: str = "", query: str = "") -> object:
    from urllib.parse import quote

    url = "/projects" + "".join("/" + quote(p, safe="") for p in path.split("/") if p) + (f"?{query}" if query else "")
    return _serve_call(url, body, method, why=_PROJECTS_NEED)


def _ago(ts: float) -> str:
    gap = max(0, time.time() - (ts or 0))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if gap >= size:
            return f"{int(gap // size)}{unit} ago"
    return "just now"


def _show_project(p: dict) -> None:
    status = "" if p.get("status") in (None, "active") else f"  {p['status']}"
    console.print(f"[bold]{escape(p['title'])}[/bold] [dim]{p['id']}{status}[/dim]")
    rows = [("goal", p["goal"])]
    rows += [("folder", short_path(d)) for d in p.get("workdirs") or []] or [("folder", "the server's default")]
    if p.get("instructions"):
        rows.append(("instructions", p["instructions"]))
    rows.append(("chat", p.get("chat") or "none yet: `vision project open` starts it"))
    if counts := {k: v for k, v in (p.get("threads") or {}).items() if v}:
        rows.append(("threads", ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())))
    if p.get("tokens_out"):
        rows.append(("usage", f"{p['tokens_out']:,} output tokens across its threads"))
    s = p.get("settings") or {}
    leash = s.get("leash") or {"ask": "on_call", "free": "autonomous"}.get(s.get("autonomy", ""), "checkins")
    rows.append(("settings", f"leash {leash.replace('_', ' ')} · updates {s.get('updates', 'milestones')} · "
                             f"notify {s.get('notify', 'needs_you')} · up to {s.get('max_threads', 3)} threads · {s.get('isolation', 'worktree')}"))
    if rules := p.get("rules"):
        rows.append(("rules", " · ".join(f"{r['action']} {r['rule']}" + ("" if r.get("set") else " (default)") for r in rules)))
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", no_wrap=True)
    t.add_column(overflow="fold")
    for label, value in rows:
        t.add_row(label, escape(value))
    console.print(t)


@project_app.command("new")
def project_new(goal: list[str] = typer.Argument(..., help="What the project is for, as you would brief someone."),
                directory: str = typer.Option("", "--dir", "-d", help="The folder it works in (default: this one)."),
                title: str = typer.Option("", "--title", "-t", help="A short name (default: from the goal)."),
                instructions: str = typer.Option("", "--instructions", "-i", help="Standing instructions for every turn in it.")):
    """Start a project: its chat is told the goal and instructions on every turn."""
    body = {"goal": " ".join(goal), "title": title, "instructions": instructions,
            "workdir": os.path.abspath(os.path.expanduser(directory or os.getcwd()))}
    p = _project_call("", body)
    _show_project(p)
    console.print(f"[dim]open its chat with[/dim] vision project open {p['id']}")


@project_app.command("list")
def project_list(all_: bool = typer.Option(False, "--all", "-a", help="Archived projects too.")):
    """Every project, latest first."""
    projects = _project_call("", query="all=1" if all_ else "")
    if not projects:
        console.print("[dim]no projects yet: start one with[/dim] vision project new \"what it's for\"")
        return
    from vision.buddy import mini

    t = Table(header_style="table.header", show_edge=False, box=None, padding=(0, 2, 1, 0))  # a blank line under each row
    t.add_column("", min_width=6, max_width=6, no_wrap=True)
    for col in ("scout", "project", "doing", "next check-in", "leash", "updated"):
        t.add_column(col, no_wrap=col not in ("project", "doing"), overflow="ellipsis")  # those two wrap on a narrow terminal
    for p in projects:
        scout = p.get("scout") or {}
        mood = "asleep" if p.get("status") == "archived" else p.get("mood") or "asleep"
        face = mini(scout.get("hue", 248), scout.get("trait", "sprout"), mood) if scout.get("name") else []
        # Each row is two lines, beside the face's two: the scout's name over the id you type, the project
        # over its folder.
        who = Text.assemble((scout.get("name") or "—", "heading" if scout.get("name") else "faint"), (f"\n{p['id']}", "faint"))
        title = escape(p["title"]) + (" [dim](archived)[/dim]" if p.get("status") == "archived" else "")
        doing, colour = _scout_doing(p)
        t.add_row(Text("\n").join(face), who, f"{title}\n[faint]{escape(p.get('folder') or '') or 'default'}[/faint]",
                  Text(doing, style=colour), _next_checkin(p.get("checkin") or {}),
                  Text(_LEASH_WORDS.get(p.get("leash") or "", p.get("leash") or ""), style="dim"),
                  Text(_ago(p.get("updated")), style="dim"))
    console.print(t)


_LEASH_WORDS = {"on_call": "on call", "checkins": "check-ins", "autonomous": "autonomous"}
_MOOD_WORDS = {"worried": ("something failed", "err"), "focused": ("working", "accent"), "curious": ("reading up", "accent"),
               "proud": ("landed something", "ok"), "found_it": ("found it", "ok"), "waking": ("waking up", "accent"),
               "out_cold": ("paused while asleep", "faint"), "needs_you": ("needs you", "warn"), "still_waiting": ("needs you", "warn")}


def _scout_doing(p: dict) -> tuple[str, str]:
    """What a project's scout is up to in a few words, and its colour: the desktop's words (scouts.ts stateOf)."""
    if p.get("status") == "archived":
        return "retired", "faint"
    approvals = p.get("approvals") or []
    threads = p.get("threads") or {}
    n = (threads.get("waiting_on_you") or 0) + (threads.get("ready_for_review") or 0) + len(approvals)
    if n == 1 and approvals:
        return f"needs you: {approvals[0].get('command') or approvals[0].get('title') or 'an approval'}", "warn"
    if n:
        return f"{n} need{'s' if n == 1 else ''} you", "warn"
    if word := _MOOD_WORDS.get(p.get("mood") or ""):
        return word
    if p.get("status") == "paused":
        return "paused", "faint"
    return "asleep", "faint"


def _next_checkin(checkin: dict) -> Text:
    """When a scout next checks in: `today 6:00 PM`, `tomorrow 9:00 AM`, `Mon 7:00 AM`; now, off or a dash."""
    import datetime

    if checkin.get("running"):
        return Text("checking in now", style="accent")
    schedule = checkin.get("schedule") or {}
    if not schedule:
        return Text("—", style="faint")
    if not schedule.get("enabled", True) or not schedule.get("next_run"):
        return Text("off", style="faint")
    at = time.localtime(schedule["next_run"])
    days = (datetime.date(*at[:3]) - datetime.date.today()).days
    clock = time.strftime("%I:%M %p", at).lstrip("0")
    day = "today" if days == 0 else "tomorrow" if days == 1 else time.strftime("%a" if days < 7 else "%b %-d", at)
    return Text(f"{day} {clock}", style="dim")


@project_app.command("show")
def project_show(ref: str = typer.Argument(..., help="The project's id, or the start of it.")):
    """A project's goal, folder, instructions and chat."""
    _show_project(_project_call(ref))


@project_app.command("edit")
def project_edit(ref: str = typer.Argument(..., help="The project's id, or the start of it."),
                 goal: str = typer.Option(None, "--goal", "-g", help="A new goal."),
                 title: str = typer.Option(None, "--title", "-t", help="A new name."),
                 instructions: str = typer.Option(None, "--instructions", "-i", help="New standing instructions (\"\" clears them)."),
                 directory: str = typer.Option(None, "--dir", "-d", help="A new folder.")):
    """Change a project; its chat takes the change from its next turn."""
    body = {k: v for k, v in (("goal", goal), ("title", title), ("instructions", instructions)) if v is not None}
    if directory is not None:
        body["workdir"] = os.path.abspath(os.path.expanduser(directory))
    if not body:
        raise SystemExit("nothing to change: give --goal, --title, --instructions or --dir")
    body["id"] = _project_call(ref)["id"]
    _show_project(_project_call("", body))


@project_app.command("open")
def project_open(ref: str = typer.Argument(..., help="The project's id, or the start of it.")):
    """Open the project's chat here (it runs in `vision serve`, so the phone has it too)."""
    chat = _project_call(f"{ref}/open", {})
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, "-m", "vision", "--join", chat["chat"]])


@project_app.command("archive")
def project_archive(ref: str = typer.Argument(..., help="The project's id, or the start of it.")):
    """Put a project away: it leaves the lists and its chat becomes an ordinary one. Nothing is deleted."""
    p = _project_call(ref, method="DELETE")
    console.print(f"archived {escape(p['title'])} [dim]({p['id']})[/dim]")


@project_app.command("pause")
def project_pause(ref: str = typer.Argument("", help="The project's id, or the start of it (default: this chat's).")):
    """Pause a project: its threads stop where they are and nothing new starts until you resume it."""
    p = _project_call(f"{_thread_project(ref)['id']}/pause", {})
    console.print(f"paused {escape(p['title'])}")


@project_app.command("resume")
def project_resume(ref: str = typer.Argument("", help="The project's id, or the start of it (default: this chat's).")):
    """Resume a paused project: its threads carry on where they stopped."""
    p = _project_call(f"{_thread_project(ref)['id']}/resume", {})
    console.print(f"resumed {escape(p['title'])}")


@project_app.command("set")
def project_set(ref: str = typer.Argument(..., help="The project's id, or the start of it."),
                pairs: list[str] = typer.Argument(..., help="setting=value … (leash=on_call|checkins|autonomous, "
                                                            "rule.<land|publish|send|destructive>=does|asks|never|default, "
                                                            "updates=all|milestones|needs_you, "
                                                            "notify=all|needs_you|none, max_threads=1-8, isolation=worktree|shared, "
                                                            "coordinator_model, coordinator_effort, thread_model, thread_effort)")):
    """Change a project's settings."""
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"{pair!r}: write it as setting=value")
        key = key.strip()
        if key.startswith("rule."):
            settings.setdefault("rules", {})[key[len("rule."):]] = value.strip()
        else:
            settings[key] = value.strip()
    p = _project_call(ref)
    _show_project(_project_call("", {"id": p["id"], "settings": settings}))


@project_app.command("threads")
def project_threads(ref: str = typer.Argument("", help="The project's id, or the start of it (default: this chat's)."),
                    all_: bool = typer.Option(False, "--all", "-a", help="Closed threads too.")):
    """A project's threads and what each is doing (as `vision thread list`)."""
    thread_list(project=ref, all_=all_)


@project_app.command("remember")
def project_remember(fact: list[str] = typer.Argument(..., help="What the project should keep in mind from now on."),
                     project: str = typer.Option("", "--project", "-p", help="The project (default: this chat's).")):
    """Keep a fact or decision in the project memory: its coordinator and every thread see it."""
    p = _thread_project(project)
    added = _project_call(f"{p['id']}/memory", {"fact": " ".join(fact)}).get("added") or []
    console.print(f"remembered in {escape(p['title'])}" if added else "[dim]already in the project memory[/dim]")


@project_app.command("memory")
def project_memory(ref: str = typer.Argument("", help="The project's id, or the start of it (default: this chat's)."),
                   forget: int = typer.Option(0, "--forget", help="Take out the fact with this number.")):
    """The project memory: what its threads decided and what you asked it to keep."""
    p = _thread_project(ref)
    if forget:
        gone = _project_call(f"{p['id']}/memory/{forget}", method="DELETE")
        console.print(f"[dim]forgot[/dim] {escape(gone['forgot'])}")
        return
    facts = _project_call(f"{p['id']}/memory").get("facts") or []
    if not facts:
        console.print(f"[dim]nothing in {escape(p['title'])}'s memory yet[/dim]")
        return
    for i, line in enumerate(facts, 1):
        console.print(f"[dim]{i:>3}[/dim] {escape(line[2:])}")


# ---------------------------------------------------------------- project threads (runs.py)
thread_app = typer.Typer(help="A project's threads: work that runs in the background, outside any chat turn.",
                         no_args_is_help=True)
app.add_typer(thread_app, name="thread")
_PROJECT = typer.Option("", "--project", "-p", help="The project's id or the start of it (default: the project of the chat "
                                                    "this runs in, else the only one).")
_STATE_MARKS = {"working": "●", "waiting_on_you": "?", "ready_for_review": "✓", "landing": "↗", "idle": "○", "resolved": "–"}
_THREAD_COLOURS = {"working": "accent", "waiting_on_you": "warn", "ready_for_review": "ok", "landing": "#5b8cff", "idle": "faint", "resolved": "faint"}


def _thread_project(ref: str) -> dict:
    if ref:
        return _project_call(ref)
    if chat := os.environ.get("VISION_CHAT", ""):
        if found := _project_call("", query=f"chat={chat}"):
            return found[0]
    projects = _project_call("")
    if len(projects) == 1:
        return projects[0]
    raise SystemExit("say which project with --project" + (": " + ", ".join(f"{p['id']} ({p['title']})" for p in projects) if projects else
                                                            "; there are none yet (`vision project new`)"))


def _thread_state(t: dict) -> str:
    if t["state"] == "working":
        if t.get("paused"):
            return "paused"
        return "queued" if t.get("queued") else (t.get("activity") or "working")
    return t["state"].replace("_", " ")


def _show_thread(t: dict) -> None:
    console.print(f"{_STATE_MARKS.get(t['state'], '')} [bold]{escape(t['title'])}[/bold] [dim]{t['id']} · {escape(_thread_state(t))}[/dim]")
    r = t.get("last_result") or {}
    rows = [("objective", t["objective"]), ("folder", t.get("folder") or "")]
    if t.get("branch"):
        rows.append(("branch", t["branch"]))
    rows.append(("model", " · ".join(x for x in (t.get("model"), t.get("effort")) if x)))
    if t.get("question"):
        rows.append(("asks", t["question"]))
    if t.get("error"):
        rows.append(("error", t["error"]))
    elif r.get("summary"):
        rows.append(("result", r["summary"]))
    for label, key in (("changed", "changes"), ("checks", "checks"), ("findings", "findings")):
        if r.get(key):
            rows.append((label, "\n".join(r[key])))
    if t.get("note"):
        rows.append(("note", t["note"]))
    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim", no_wrap=True)
    g.add_column(overflow="fold")
    for label, value in rows:
        g.add_row(label, escape(str(value)))
    console.print(g)
    for run in (t.get("runs") or [])[-5:]:
        console.print(f"[dim]{_ago(run['at'])} · {run['kind']} · {run['status'].replace('_', ' ')} · {run.get('duration_s', 0):.0f}s[/dim]"
                      + (f"  {escape(run['summary'])}" if run.get("summary") else ""))


@thread_app.command("new")
def thread_new(objective: list[str] = typer.Argument(..., help="What the thread should do, as you would brief someone."),
               project: str = _PROJECT,
               title: str = typer.Option("", "--title", "-t", help="A short name (default: from the objective)."),
               read_only: bool = typer.Option(False, "--read-only", help="It only looks: no worktree of its own."),
               after: str = typer.Option("", "--after", "-a", help="Threads it builds on (ids, comma-separated): their results go into its brief.")):
    """Start a thread in a project: it works in the background and reports back to the project's chat."""
    p = _thread_project(project)
    t = _project_call(f"{p['id']}/threads", {"objective": " ".join(objective), "title": title, "edits": not read_only,
                                             "after": [a.strip() for a in after.split(",") if a.strip()]})
    console.print(f"started [bold]{escape(t['title'])}[/bold] [dim]({t['id']}) in {escape(p['title'])}"
                  + (", queued behind the others" if t.get("queued") else "")
                  + "[/dim]")
    if t.get("note"):
        console.print(f"[dim]{escape(t['note'])}[/dim]")


@thread_app.command("list")
def thread_list(project: str = _PROJECT, all_: bool = typer.Option(False, "--all", "-a", help="Resolved threads too.")):
    """A project's threads and what each is doing."""
    p = _thread_project(project)
    threads = [t for t in _project_call(f"{p['id']}/threads") if all_ or t["state"] != "resolved"]
    if not threads:
        console.print(f"[dim]no threads in {escape(p['title'])} yet: start one with[/dim] vision thread new \"what to do\"")
        return
    t = Table(header_style="table.header", show_edge=False, box=None, padding=(0, 2, 0, 0))
    for col in ("id", "thread", "state", "updated"):
        t.add_column(col, no_wrap=col != "thread", overflow="ellipsis")
    for th in threads:
        t.add_row(f"[dim]{th['id']}[/dim]", f"[{_THREAD_COLOURS.get(th['state'], 'dim')}]{_STATE_MARKS.get(th['state'], '')}[/] {escape(th['title'])}",
                  escape(_thread_state(th)), f"[dim]{_ago(th.get('updated'))}[/dim]")
    console.print(t)


@thread_app.command("show")
def thread_show(ref: str = typer.Argument(..., help="The thread's id, or the start of it."), project: str = _PROJECT,
                history: bool = typer.Option(False, "--history", "-H", help="Its conversation too, as its provider keeps it.")):
    """One thread: what it's for, where it works, its last result and its recent runs."""
    pid = _thread_project(project)["id"]
    t = _project_call(f"{pid}/threads/{ref}")
    if history:
        for m in _project_call(f"{pid}/threads/{t['id']}/history").get("messages") or []:
            who = "[dim]vision →[/dim]" if m["role"] == "user" else "[accent]thread ←[/accent]"
            console.print(f"{who} {escape(m['text'])}\n")
    _show_thread(t)


@thread_app.command("tell")
def thread_tell(ref: str = typer.Argument(..., help="The thread's id, or the start of it."),
                text: list[str] = typer.Argument(..., help="What to tell it."), project: str = _PROJECT):
    """Message one thread: steers it while it works, answers its question, or gives it more to do."""
    # From a terminal of the user's own (not a Vision turn's shell), the coordinator hears of it too.
    direct = not os.environ.get("VISION_CHAT")
    t = _project_call(f"{_thread_project(project)['id']}/threads/{ref}/tell", {"text": " ".join(text), "direct": direct})
    if not t["id"].startswith(ref.strip().lower()):  # it was closed: a new thread carries the work on
        console.print(f"that thread is closed: started [bold]{escape(t['title'])}[/bold] [dim]({t['id']}) after it, "
                      f"with a handover · {escape(_thread_state(t))}[/dim]")
        return
    console.print(f"told [bold]{escape(t['title'])}[/bold] [dim]· {escape(_thread_state(t))}[/dim]")


@thread_app.command("land")
def thread_land(ref: str = typer.Argument(..., help="The thread's id, or the start of it."), project: str = _PROJECT):
    """Merge a finished thread's branch into the project folder's current branch, and close the thread."""
    t = _project_call(f"{_thread_project(project)['id']}/threads/{ref}/land", {})
    console.print(f"landed [bold]{escape(t['title'])}[/bold]" + (f" [dim]({escape(t['note'])})[/dim]" if t.get("note") else ""))


@thread_app.command("open")
def thread_open(ref: str = typer.Argument(..., help="The thread's id, or the start of it."), project: str = _PROJECT):
    """Open a stopped thread's own session in its provider's CLI, in its folder, to work in it by hand."""
    t = _project_call(f"{_thread_project(project)['id']}/threads/{ref}")
    if t["state"] in ("working", "landing"):
        raise SystemExit(f"it's {_thread_state(t)}: two hands in one session would collide. Follow it with "
                         f"`vision thread show {t['id']} --history`, or stop it first.")
    if not t.get("session_id"):
        raise SystemExit("it has no session to open yet")
    argv = {"claude": ["claude", "--resume", t["session_id"]], "codex": ["codex", "resume", t["session_id"]],
            "grok": ["grok", "--resume", t["session_id"]]}.get(t.get("provider") or "claude")
    if argv is None or not shutil.which(argv[0]):
        raise SystemExit(f"can't open a {t.get('provider')} session from here")
    if os.path.isdir(t.get("workdir") or ""):
        os.chdir(t["workdir"])
    sys.stdout.flush()
    os.execvp(argv[0], argv)


@thread_app.command("stop")
def thread_stop(ref: str = typer.Argument(..., help="The thread's id, or the start of it."), project: str = _PROJECT):
    """Stop a working thread; `vision thread tell` carries it on later."""
    t = _project_call(f"{_thread_project(project)['id']}/threads/{ref}/stop", {})
    console.print(f"stopping [bold]{escape(t['title'])}[/bold]")


@thread_app.command("close")
def thread_close(ref: str = typer.Argument(..., help="The thread's id, or the start of it."), project: str = _PROJECT):
    """Close a thread. Its worktree is removed unless it holds work no other branch has."""
    t = _project_call(f"{_thread_project(project)['id']}/threads/{ref}/resolve", {})
    console.print(f"closed [bold]{escape(t['title'])}[/bold]" + (f" [dim]({escape(t['note'])})[/dim]" if t.get("note") else ""))


# ---------------------------------------------------------------- setup / doctor / config
@app.command()
def onboard():
    """The first-run setup, from the top: your providers (installed, signed in and checked), what Vision
    calls you, auto or plan mode, the voice where this machine can run it, and your phone. `vision` runs
    it by itself on a machine's first chat; here, what is already saved shows as each question's default."""
    from vision.onboard import Flow
    from vision.welcome import ConsoleIO

    if not Flow(ConsoleIO(console)).run(again=True):
        raise typer.Exit(1)


def _download(url: str, dest: Path) -> None:
    import urllib.request

    tmp = dest.with_suffix(dest.suffix + ".part")
    with console.status(f"[dim]downloading {dest.name}…[/dim]"):
        urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def _install_llama() -> None:
    """Unpack the prebuilt llama.cpp CUDA build (binaries + bundled CUDA runtime) flat into LLAMA_DIR."""
    import tarfile
    import tempfile

    LLAMA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for url in LLAMA_URLS:
            archive = Path(tmp) / url.rsplit("/", 1)[1]
            _download(url, archive)
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        member.name = Path(member.name).name  # drop the versioned top-level folder
                        tar.extract(member, LLAMA_DIR)


def _fetch_hf(repo: str) -> None:
    from huggingface_hub import snapshot_download

    from vision.stt import thread_only_tqdm

    thread_only_tqdm()
    with console.status(f"[dim]downloading {repo} (about 4.5 GB)…[/dim]"):
        snapshot_download(repo)


def _setup_mlx(cfg, retime: bool) -> None:
    """Apple Silicon: fetch the MLX voice model, timing the tiers best-first to keep the first that runs
    comfortably faster than real time on this Mac (a slower chip steps down to a lighter model)."""
    from vision.tts import (
        MLX_TARGET_RTF,
        TTSError,
        mac_chip,
        mlx_choice,
        mlx_fetch,
        mlx_present,
        save_mlx_choice,
        time_mlx,
    )

    if cfg.voice.mlx_model:
        if not mlx_present(cfg.voice.mlx_model):
            with console.status(f"[dim]downloading {cfg.voice.mlx_model}…[/dim]"):
                mlx_fetch(cfg.voice.mlx_model)
        console.print(f"[ok]✓[/ok] MLX voice model {cfg.voice.mlx_model} (set as [voice] mlx_model)")
        return
    prev = mlx_choice()
    if not retime and prev.get("chip") == mac_chip() and prev.get("model") and mlx_present(prev["model"]):
        console.print(f"[ok]✓[/ok] MLX voice model {prev['model']} ({prev.get('rtf', '?')} s per second of speech on this Mac)")
        return
    voices = saved_voices()
    voice = cfg.voice.voice if cfg.voice.voice in voices else (voices[0] if voices else None)
    if voice is None:
        console.print(f"[warn]![/warn] no saved voice to time the MLX models with; using {MLX_TTS_TIERS[0]}")
        with console.status(f"[dim]downloading {MLX_TTS_TIERS[0]}…[/dim]"):
            mlx_fetch(MLX_TTS_TIERS[0])
        return
    timed = []
    for repo in MLX_TTS_TIERS:
        if not mlx_present(repo):
            with console.status(f"[dim]downloading {repo}…[/dim]"):
                mlx_fetch(repo)
        try:
            with console.status(f"[dim]timing {repo} on this Mac…[/dim]"):
                rtf = time_mlx(dataclasses.replace(cfg.voice, voice=voice), repo)
        except TTSError as e:
            console.print(f"[warn]![/warn] {repo}: {e}")
            continue
        timed.append((rtf, repo))
        console.print(f"[dim]  {repo}: {rtf:.2f} s per second of speech[/dim]")
        if rtf <= MLX_TARGET_RTF:
            break
    if not timed:
        raise typer.Exit(1)
    rtf, repo = min(timed) if timed[-1][0] > MLX_TARGET_RTF else timed[-1]
    save_mlx_choice(repo, rtf)
    if rtf <= 1.0:
        console.print(f"[ok]✓[/ok] MLX voice model {repo} ({rtf:.2f} s per second of speech on this Mac)")
    else:
        console.print(f"[warn]![/warn] MLX voice model {repo}: {rtf:.2f} s per second of speech, slower than real time on this Mac")


@app.command()
def setup(
    whisper: bool = typer.Option(True, help="Also pre-download the Whisper model."),
    orpheus: bool = typer.Option(False, help="Also fetch the Orpheus engine (needed only if [voice] engine = \"orpheus\")."),
    retime: bool = typer.Option(False, "--retime", help="Apple Silicon: time the MLX voice models again and re-pick."),
):
    """Download voice and speech models (idempotent) and make the default voice if it does not exist yet."""
    from vision.tts import llama_server_bin, model_files, qwen_present, uses_mlx

    cfg = load_config()
    if cfg.voice.engine == "qwen3" or not orpheus:
        mlx = uses_mlx(dataclasses.replace(cfg.voice, engine="qwen3"))
        if not mlx:
            if not qwen_present():
                _fetch_hf(QWEN_TTS_BASE)
            console.print(f"[ok]✓[/ok] Qwen3-TTS voice model ({QWEN_TTS_BASE})")
        if cfg.voice.voice in VOICE_DESIGNS and cfg.voice.voice not in saved_voices():
            console.print(f"[dim]no voice named {cfg.voice.voice!r} yet: designing it (first take, no audition)[/dim]")
            voice_design(cfg.voice.voice, None, None, takes=1, keep=1, use=False)
        if mlx:
            _setup_mlx(cfg, retime)
    if cfg.voice.engine == "orpheus" or orpheus:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        gguf, snac = model_files()
        if not gguf.exists():
            _download(ORPHEUS_MODEL_URL, gguf)  # 2.4 GB
        if not snac.exists():
            _download(SNAC_MODEL_URL, snac)
        console.print(f"[ok]✓[/ok] Orpheus voice model in {MODELS_DIR}")
        if not llama_server_bin().exists():
            _install_llama()
        console.print(f"[ok]✓[/ok] llama.cpp server in {LLAMA_DIR}")
    if whisper:
        from vision.stt import Transcriber

        stt = Transcriber(cfg.listen)
        with console.status("[dim]loading Whisper (downloads on first run)…[/dim]"):
            stt.load()
        console.print(f"[ok]✓[/ok] Whisper ready on {stt.device}")
        if stt.mlx_error:
            console.print(f"  [warn]![/warn] the MLX build did not load ({stt.mlx_error}); faster-whisper instead")

        wake = Transcriber(dataclasses.replace(cfg.listen, whisper_model=cfg.wake.model, device="cpu"))
        with console.status("[dim]loading the wake-word model…[/dim]"):
            wake.load()
        console.print(f"[ok]✓[/ok] wake word (“{cfg.wake.names[0].capitalize()}”) ready on {wake.device}")
    if cfg.listen.only_me:
        from vision import voiceid

        with console.status("[dim]fetching the speaker model (26 MB)…[/dim]"):
            path = voiceid.model_path()
        console.print(f"[ok]✓[/ok] speaker model ({voiceid.MODEL_REPO})" if path else
                      f"[warn]![/warn] speaker model ({voiceid.MODEL_REPO}) could not be fetched; every voice is heard until it is")
    console.print(f"[ok]✓[/ok] config: {CONFIG_PATH}")


@app.command()
def doctor(
    usage: bool = typer.Option(True, "--usage/--no-usage", help="Also show every provider's subscription usage."),
    fix: bool = typer.Option(False, "--fix", help="Put right what can be: install, update or log in to a provider CLI, fetch the speech models, "
                                                   "make the missing voice, restart the background service. Each fix asks first."),
    yes: bool = typer.Option(False, "--yes", "-y", help="With --fix: apply every fix without asking."),
):
    """Check AI provider CLIs (versions, logins, usage), local models, GPU, and audio devices (--fix mends what it can)."""
    ok = "[ok]✓[/ok]"
    bad = "[err]✗[/err]"
    warn = "[warn]![/warn]"
    cfg = load_config()
    fixed: list[str] = []

    def repair(label: str, action) -> bool:
        """With --fix: run `action` (after a y/n unless --yes) and say so. False when it did not happen,
        so the caller prints the hint it would have printed anyway."""
        if not fix:
            return False
        from rich.prompt import Confirm

        if not yes and not Confirm.ask(f"  fix: {label}?", console=console, default=True):
            return False
        console.print(f"[dim]fixing: {label}[/dim]")
        try:
            action()
        except Exception as e:  # noqa: BLE001 - an installer or a service manager failing is a line, not a traceback
            console.print(f"{bad} {label}: {e}")
            return False
        fixed.append(label)
        return True

    # provider CLIs: version, then a login check each
    infos = {i.provider: i for i in clis.cli_versions()}
    for i in list(infos.values()):
        install_cmd = cap(i.provider, "install_cmd", "")
        if not i.ok and install_cmd and repair(f"install {i.label} (`{install_cmd}`)", lambda c=install_cmd: subprocess.run(c, shell=True, check=True)):
            infos[i.provider] = clis.cli_version(i.provider)
    with console.status("[dim]checking for CLI updates…[/dim]"):
        checks = {p: clis.check_update(p) for p, i in infos.items() if i.ok}
    for p, c in list(checks.items()):
        if c.available and repair(f"update {c.label} to {c.latest} (`vision update {p}`)", lambda p=p: clis.update_cli(p, stream=True)):
            infos[p], checks[p] = clis.cli_version(p), clis.check_update(p, max_age=0)
    console.print(_versions_renderable(list(infos.values()), checks=checks))
    if any(c.available for c in checks.values()):
        console.print(f"{warn} newer release available: " + ", ".join(f"{c.label} {c.latest}" for c in checks.values() if c.available) + "  → run `vision update`")
    for i in infos.values():
        if not i.ok:
            console.print(f"{bad} {i.label} CLI: {i.error}\n   Install: {clis.install_hint(i.provider)}")

    for i in infos.values():
        login_cmd = tuple(cap(i.provider, "login_cmd", ()) or ())
        login = " ".join(login_cmd)
        if not (i.ok and login):
            continue
        with console.status(f"[dim]checking the {i.label} login…[/dim]"):
            logged_in, detail = clis.login_status(i.provider, thorough=True)
        if not logged_in and repair(f"log in to {i.label} (`{login}`)", lambda c=login_cmd: subprocess.run(list(c), check=True)):
            logged_in, detail = clis.login_status(i.provider, thorough=True)
        if logged_in:
            console.print(f"{ok} {i.label} login works ({detail})")
        else:
            console.print(f"{bad} {i.label} is not logged in: {detail}\n   Run `{login}` once" + (" and log in." if login == "claude" else "."))

    # usage for every provider that is installed (one missing CLI is reported inline, not fatal)
    if usage and any(i.ok for i in infos.values()):
        with console.status("[dim]reading all provider usage…[/dim]"):
            report = _usage_selection(cfg, None, "all")
        console.print(report)

    # the background service, when there is one: installed and answering, or only installed
    from vision import service

    if service.installed():
        from vision.remote import running_server

        server = running_server()
        if service.waiting(server):
            console.print(f"{warn} background service waiting behind the `vision serve` in a terminal (pid {server['pid']})")
        elif not (service.running() and server) and repair("restart the background service (`vision service install`)", service.install):
            console.print(f"{ok} background service restarted")
        elif service.running() and server:
            console.print(f"{ok} background service running ({server['url']})")
        else:
            console.print(f"{bad} background service installed but not answering  → vision service install (logs: {service.logs_hint()})")

    def done() -> None:
        if fix:
            console.print(f"[ok]fixed:[/ok] " + "; ".join(fixed) if fixed else "[dim]nothing to fix[/dim]")
        console.print(f"[dim]config: {CONFIG_PATH}[/dim]")

    # models, GPU and audio need the voice extra; a text-only install stops here, and that is fine
    import importlib.util

    missing = [m for m in ("numpy", "sounddevice", "soundfile", "faster_whisper") if importlib.util.find_spec(m) is None]
    if missing:
        console.print(f"{warn} voice not installed (optional; {', '.join(missing)} missing). For speech in and out:  {setup_hint()}")
        done()
        return

    # models
    from vision.tts import mac_chip, mlx_choice, mlx_model, models_present, uses_mlx

    have = models_present(cfg.voice)
    mlx = uses_mlx(cfg.voice)
    if cfg.voice.engine == "orpheus":
        what = "Orpheus voice model + llama.cpp"
    elif mlx:
        what = f"Qwen3-TTS voice model on MLX ({mlx_model(cfg.voice)})"
    else:
        what = f"Qwen3-TTS voice model ({QWEN_TTS_BASE})"
    if not have and repair("fetch the speech models (`vision setup`)", lambda: setup(whisper=False, orpheus=False, retime=False)):
        have = models_present(cfg.voice)
    console.print(f"{ok if have else bad} {what}" + ("" if have else "  → run `vision setup`"))
    if mlx and have and not cfg.voice.mlx_model:
        choice, chip = mlx_choice(), mac_chip()
        if (not choice or choice.get("chip") != chip) and repair("time the MLX voice models on this Mac (`vision setup --retime`)", lambda: _setup_mlx(cfg, True)):
            choice = mlx_choice()
        if not choice:
            console.print(f"{warn} MLX voice speed not timed on this Mac yet  → vision setup")
        elif choice.get("chip") != chip:
            console.print(f"{warn} MLX voice model was timed on {choice.get('chip') or 'another Mac'}, not this {chip}  → vision setup --retime")
        else:
            rtf = float(choice.get("rtf", 0))
            console.print(f"{ok if rtf <= 1 else warn} MLX voice speed: {rtf:.2f} s per second of speech on this {chip}"
                          + ("" if rtf <= 1 else " — slower than real time, so speech will break up"))
    if cfg.voice.engine == "qwen3":
        names = saved_voices()
        if cfg.voice.voice not in names and cfg.voice.voice in VOICE_DESIGNS and have \
                and repair(f"design the voice {cfg.voice.voice!r} (`vision voice design {cfg.voice.voice}`)",
                           lambda: voice_design(cfg.voice.voice, None, None, takes=1, keep=1, use=False)):
            names = saved_voices()
        console.print(f"{ok if cfg.voice.voice in names else bad} voice {cfg.voice.voice!r} in {VOICES_DIR} (have: {', '.join(names) or 'none'})" + ("" if cfg.voice.voice in names else f"  → vision voice design {cfg.voice.voice}"))
    from vision import voiceid

    if voiceid.enrolled():
        console.print(f"{ok} your voice is enrolled; only_me is {'on' if cfg.listen.only_me else 'off'} (threshold {cfg.listen.only_me_threshold:.2f})")
    elif cfg.listen.only_me:
        console.print(f"{warn} only_me is on but no voice is enrolled, so every voice is heard  → vision voice enrol")
    else:
        console.print("[dim]  no voice enrolled (optional: vision voice enrol, then only_me = true under \\[listen])[/dim]")
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"], capture_output=True, text=True, encoding="utf-8")
        gpu = r.returncode == 0
        console.print(f"{ok} GPU: {r.stdout.strip()}" if gpu else f"{warn} no NVIDIA GPU; Whisper will use CPU")
    except FileNotFoundError:
        gpu = False
        if sys.platform != "darwin":  # a Mac never has one: not worth a warning
            console.print(f"{warn} nvidia-smi not found; Whisper will use CPU")
    # The voice build should match the machine: voice-cpu can't use a GPU, voice-nvidia's ~6 GB is dead weight without
    # one, and on Apple Silicon only voice-mac speaks in real time.
    import platform

    backend = voice_backend()
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    if backend == "cpu" and apple_silicon:
        console.print(f"{warn} CPU voice build on Apple Silicon: speech runs slower than real time and breaks up  → {setup_hint('mac')}")
    elif backend == "cpu" and gpu:
        console.print(f"{warn} CPU voice build on a machine with an NVIDIA GPU  → {setup_hint('nvidia')}")
    elif backend == "nvidia" and not gpu:
        console.print(f"{warn} NVIDIA voice build (~6 GB of CUDA) on a machine without a GPU  → {setup_hint('cpu')}")
    elif backend == "mac":
        console.print(f"{ok} voice build: mac (MLX, on the Mac's GPU)")
    elif backend:
        console.print(f"{ok} voice build: {backend}")
    try:
        from vision.stt import Transcriber

        stt = Transcriber(cfg.listen)
        with console.status("[dim]loading Whisper…[/dim]"):
            stt.load(quiet=True)
        console.print(f"{ok} Whisper ready: {stt.device}")
        if stt.mlx_error:
            console.print(f"  [warn]![/warn] the MLX build did not load ({stt.mlx_error}); faster-whisper instead")
    except Exception as e:
        console.print(f"{bad} Whisper failed: {e}")

    # audio
    import sounddevice as sd

    devs = sd.query_devices()
    try:
        # PortAudio reports a missing default as -1, which would index the last device (a machine with no mic
        # showed its speakers as the input).
        for label, idx, key in (("input: ", sd.default.device[0], "max_input_channels"), ("output:", sd.default.device[1], "max_output_channels")):
            if 0 <= idx < len(devs) and devs[idx][key] > 0:
                console.print(f"{ok} default {label} {devs[idx]['name']}")
            else:
                console.print(f"{bad} default {label} none  → connect one" + (" (listen and talk need a microphone)" if key == "max_input_channels" else ""))
    except Exception as e:
        console.print(f"{bad} audio device query failed: {e}")
    t = Table(title="Audio devices", header_style="table.header", border_style=LINE, title_style="heading", title_justify="left")
    t.add_column("#"), t.add_column("name"), t.add_column("in"), t.add_column("out")
    for i, d in enumerate(devs):
        if (d["max_input_channels"] or d["max_output_channels"]) and "JACK" not in sd.query_hostapis(d["hostapi"])["name"]:
            t.add_row(str(i), d["name"], str(d["max_input_channels"]), str(d["max_output_channels"]))
    console.print(t)
    from vision.config import pipewire_nodes

    nodes = [(kind, desc) for kind in ("input", "output") for _, desc in pipewire_nodes(kind)]
    if nodes:
        t = Table(title="PipeWire devices (use any part of the name as input_device / output_device)", header_style="table.header", border_style=LINE, title_style="heading", title_justify="left")
        t.add_column("kind"), t.add_column("name")
        for kind, desc in nodes:
            t.add_row(kind, desc)
        console.print(t)
    try:
        from vision.tts import Speaker

        sp = Speaker(cfg.voice)
        with console.status("[dim]loading the voice…[/dim]"):
            sp._load()
        console.print(f"{ok} voice '{cfg.voice.voice}' loads on {sp.device_note} ({sp.voice})" + (" — too slow for conversation on the CPU" if sp.device == "cpu" else ""))
    except Exception as e:
        console.print(f"{bad} voice failed: {e}")
    done()


@app.command()
def default(
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Default model alias or full name (e.g. opus)."),
    effort: Optional[str] = typer.Option(None, "--effort", "-e", help="Default reasoning effort (model-dependent)."),
    reset: bool = typer.Option(False, "--reset", help="Go back to Vision's factory default: Opus 5 · medium."),
):
    """Choose and save Vision's default model and effort level (arrow-key pickers unless given as options)."""
    cfg = load_config()
    if reset:
        console.print(_save_defaults(cfg, FACTORY_MODEL, FACTORY_EFFORT))
        console.print(f"[dim]{CONFIG_PATH}[/dim]")
        return
    m = model or pick("Default model", _usable_tabs(cfg), current=cfg.brain.model)
    if not m:
        raise typer.Exit(1)
    current_effort, _ = coerce_effort(m, cfg.brain.effort)
    e = effort.lower() if effort is not None else pick("Default effort level", effort_choices(m), current=current_effort)
    if e is None:
        raise typer.Exit(1)
    if not supports_effort(m, e):
        allowed = ", ".join(v for v, _, _ in effort_choices(m)) or "none"
        raise typer.BadParameter(f"{model_label(m)} supports effort: {allowed}")
    console.print(_save_defaults(cfg, m, e))
    console.print(f"[dim]{CONFIG_PATH}[/dim]")


config_app = typer.Typer(help="Show (or edit) Vision's configuration: the file, or one setting with `get` and `set`.",
                         invoke_without_command=True, no_args_is_help=False)
app.add_typer(config_app, name="config")


@config_app.callback()
def config(ctx: typer.Context, edit: bool = typer.Option(False, "--edit", "-e", help="Open the config in $EDITOR.")):
    """Show (or edit) Vision's configuration. `vision config set brain.mode plan` changes one setting in place."""
    if ctx.invoked_subcommand is not None:
        return
    load_config()
    if edit:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        subprocess.call([editor, str(CONFIG_PATH)])
        return
    console.print(f"[dim]{CONFIG_PATH}[/dim]")
    console.print(Text(CONFIG_PATH.read_text(encoding="utf-8")))


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="The setting as section.key, e.g. brain.mode or voice.voice.")):
    """Print one setting as config.toml has it (the default, dimmed, when the file leaves it out)."""
    from vision.config import get_config_value

    try:
        value, set_in_file = get_config_value(key)
    except KeyError:
        console.print(f"[err]no setting called {key}[/err] (`vision config` shows them all)")
        raise typer.Exit(1)
    shown = json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)
    console.print(shown if set_in_file else f"[dim]{shown}  (default; not in {CONFIG_PATH.name})[/dim]", highlight=False)


@config_app.command("set")
def config_set(key: str = typer.Argument(..., help="The setting as section.key, e.g. brain.mode."),
               value: str = typer.Argument(..., help='The value: true, 12, a word, or a TOML list such as \'["a", "b"]\'.')):
    """Change one setting in config.toml, keeping its comments and order (a chat that is open picks it up next time it starts)."""
    from vision.config import set_config_value

    try:
        literal = set_config_value(key, value)
    except ValueError as e:
        console.print(f"[err]✗[/err] {e}")
        raise typer.Exit(1)
    section, _, name = key.rpartition(".")
    console.print(f"[ok]✓[/ok] {escape(f'[{section}]')} {name} = {escape(literal)}  [dim]{CONFIG_PATH}[/dim]", highlight=False)


def main():
    from vision.proctitle import hide_cmdline

    hide_cmdline()  # the terminal tab reads `Vision`, not `Vision — /…/.venv/bin/python -m vision`
    code = 0
    try:
        # The program is `vision` however it was started (`python -m vision` would otherwise name the
        # completion variable after __main__.py and the usage line after the interpreter).
        app(prog_name="vision")
    except KeyboardInterrupt:
        console.print()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if not isinstance(e.code, int) and e.code is not None:
            console.print(str(e.code))
    # Skip the interpreter's teardown: with a voice model on the GPU and PortAudio streams open it can
    # take many seconds (or hang in Pa_Terminate on a stalled mic), and everything we keep is already on disk.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
