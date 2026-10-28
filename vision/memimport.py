"""Bring what another assistant knows about the user into Vision's memory, so every brain starts with it.

Claude Code keeps its own notes: `~/.claude/CLAUDE.md` (the user's standing instructions) and a memory
folder per project under `~/.claude/projects/<folder>/memory/`. Only the Claude brain ever reads them, so
switching a chat to Codex or Grok left all of it behind. `vision memory import` reads them (and, given a
file or `-`, anything else: a claude.ai memory export, ChatGPT's "what do you know about me", notes),
has one tool-free call pick out the durable facts, and offers them as the same tick list the end-of-chat
review uses (vision/memreview.py). Nothing is written until it is ticked; what is, lands in Vision's
memory (vision/memory.py), which every brain reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vision import memreview

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
BATCH_CHARS = 30_000     # one call's worth of notes; more is read in several calls
FILE_CHARS = 12_000      # one note file past this is cut (a log someone saved, not a memory)
MAX_FACTS = 24           # per call: an import moves a lot more than one chat leaves
PAGE = 8                 # options per question on the tick list

PROMPT = """You move what another AI assistant has noted about its user into the user's own assistant, Vision, \
whose long-term memory every later conversation reads, whichever model answers.

Keep only durable facts a later conversation could not work out for itself: who the user is (role, \
preferences, how they like to be spoken to and how they like things done), their machines, accounts and \
people, their projects and what each is, decisions not to undo, and corrections to how an assistant \
should work. Rewrite each as one line about the user or their world, not about the other assistant \
("prefers short answers", not "Claude should keep answers short"). Drop secrets (keys, passwords, tokens), \
progress on a task, how code works or where it lives in a repo, and anything already in Vision's memory \
(given below) or that merely rephrases it.

Reply with a JSON array only, no prose:
[{"fact": "one line, present tense, specific", "topic": "", "about": ""}, ...]
"topic" is the file the fact belongs in: "" for the core (facts about the user themself), an existing \
topic name from the list, or a new short kebab-case name for a project or area; "about" is a few words \
on what a new topic covers (empty for the core or an existing topic). At most 24 entries, best first; \
an empty array [] when nothing qualifies. Do not use any tools."""


@dataclass
class Source:
    label: str   # what the tick list and the notes call it: "Claude Code: ~/code/vision (haru-phone-build.md)"
    text: str


def _project_label(folder: str) -> str:
    """`-home-me-code-vision` (Claude Code's name for /home/me/code/vision) as `~/code/vision` where that
    folder exists; the name as it is otherwise (a dash in a folder's own name can't be told from a slash)."""
    guess = "/" + folder.lstrip("-").replace("-", "/")
    home = os.path.expanduser("~")
    if os.path.isdir(guess):
        return guess.replace(home, "~", 1) if guess.startswith(home) else guess
    return folder


def claude_sources(home: Path | None = None) -> list[Source]:
    """Claude Code's notes about the user: its global CLAUDE.md, then every project's memory files (their
    MEMORY.md is only an index of the others, so it is left out)."""
    root = home or CLAUDE_HOME
    out: list[Source] = []
    glob = root / "CLAUDE.md"
    if glob.is_file():
        out.append(Source("Claude Code: your instructions (~/.claude/CLAUDE.md)", _read(glob)))
    projects = root / "projects"
    if projects.is_dir():
        for folder in sorted(p for p in projects.iterdir() if (p / "memory").is_dir()):
            where = _project_label(folder.name)
            for f in sorted((folder / "memory").glob("*.md")):
                if f.name == "MEMORY.md":
                    continue
                text = _read(f)
                if text.strip():
                    out.append(Source(f"Claude Code: {where} ({f.name})", text))
    return [s for s in out if s.text.strip()]


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= FILE_CHARS else text[:FILE_CHARS] + "\n…(cut)"


def batches(sources: list[Source], limit: int = BATCH_CHARS) -> list[str]:
    """The notes as one or more <notes> blocks of about `limit` characters, each source whole."""
    out, cur = [], ""
    for s in sources:
        block = f'<note from="{s.label}">\n{s.text.strip()}\n</note>\n'
        if cur and len(cur) + len(block) > limit:
            out.append(cur)
            cur = ""
        cur += block
    if cur:
        out.append(cur)
    return out


def message(notes: str, existing: list[str], topics: list[tuple[str, str, int]]) -> str:
    kept = "\n".join(existing) if existing else "(nothing yet)"
    filed = "\n".join(f"{name}: {about}" for name, about, _ in topics) if topics else "(none yet)"
    return f"<memory>\n{kept}\n</memory>\n\n<topics>\n{filed}\n</topics>\n\n<notes>\n{notes}</notes>"


def proposals(cfg, sources: list[Source], timeout: float | None = None, on_batch=None) -> list[memreview.Proposal]:
    """The facts in `sources` worth moving into Vision's memory that it doesn't hold yet ([] when there are
    none or no provider could be asked). Claude is asked first; `on_batch(i, n)` says which call is running."""
    from vision import memory
    from vision.suggest import one_shot

    existing = memory.facts() + [ln for name, _, _ in memory.topics() for ln in memory.facts(name)]
    topics = memory.topics()
    found: list[memreview.Proposal] = []
    parts = batches(sources)
    for i, notes in enumerate(parts):
        if on_batch:
            on_batch(i + 1, len(parts))
        answer = one_shot(message(notes, existing + [p.fact for p in found], topics),
                          providers=getattr(cfg.providers, "enabled", None),
                          timeout=timeout if timeout is not None else max(90.0, cfg.memory.review_timeout_s),
                          prompt=PROMPT, models=memreview.MODELS, prefer="claude")
        found += memreview.fresh(memreview.parse(answer, limit=MAX_FACTS), existing + [p.fact for p in found])
    return found


def questions(props: list[memreview.Proposal]) -> list[dict]:
    """The tick list, a page of a few facts per question so a long import still fits the screen."""
    pages = [props[i : i + PAGE] for i in range(0, len(props), PAGE)]
    out = []
    for n, page in enumerate(pages, 1):
        q = memreview.question(page)
        q["question"] = "Bring these into Vision's memory? Untick anything that should not be kept." + (
            f" ({n} of {len(pages)})" if len(pages) > 1 else "")
        q["header"] = "Import" if len(pages) == 1 else f"Import {n}"
        out.append(q)
    return out


def chosen(props: list[memreview.Proposal], answers: dict | None) -> list[memreview.Proposal]:
    """The ticked facts, across every page of the tick list (answers are keyed by each page's question)."""
    if not answers:
        return []
    pages = [props[i : i + PAGE] for i in range(0, len(props), PAGE)]
    picked: list[memreview.Proposal] = []
    for page, q in zip(pages, questions(props)):
        if q["question"] in answers:
            picked += memreview.chosen(page, {q["question"]: answers[q["question"]]})
    return picked
