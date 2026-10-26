"""The memory review: what a conversation left that is worth keeping, offered as a tick list.

Memory (vision/memory.py) only grows when the user says "remember" or a brain decides a line is worth
saving mid-task. The review runs once at the end of a chat (/quit, Ctrl-D) or on /review and
`vision memory review`: one tool-free call reads the transcript and the facts already kept, and
proposes the durable ones it finds, each with the file it belongs in. Nothing is written until the
user ticks it, so a wrong guess costs a keystroke rather than a stale fact in every later prompt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_PROPOSALS = 8        # more than fits a form, and more than one chat ever leaves
TRANSCRIPT_CHARS = 40_000  # the end of a long conversation is what the facts came out of
FACT_CHARS = 300
MODELS = {"claude": "sonnet"}  # a step up from the suggestion's Haiku: a wrong fact outlives a wrong guess

PROMPT = """You review a conversation between a user and their assistant, Vision, for facts worth keeping in \
Vision's long-term memory, which every later conversation reads.

Keep only what a later conversation could not work out for itself: a durable fact about the user (role, \
preferences, how they like things done), their machines, accounts or projects; a decision not to undo \
("don't bring that back"); a correction to how Vision works or talks. Never what was built or fixed in \
this conversation, how code works, or how far a task got: the code and its history already say that. \
Never secrets (keys, passwords, tokens). Skip anything already in memory (given below) or that merely \
rephrases it.

Reply with a JSON array only, no prose:
[{"fact": "one line, present tense, specific", "topic": "", "about": ""}, ...]
"topic" is the file the fact belongs in: "" for the core (facts about the user themself), an existing \
topic name from the list, or a new short kebab-case name for a project or area; "about" is a few words \
on what a new topic covers (empty for the core or an existing topic). At most 8 entries, best first; an \
empty array [] when nothing qualifies, which is common. Do not use any tools."""


@dataclass
class Proposal:
    fact: str
    topic: str = ""  # "" = the core (MEMORY.md)
    about: str = ""  # what a topic file this creates covers

    @property
    def file(self) -> str:
        return f"{self.topic}.md" if self.topic else "MEMORY.md"


def _norm(text: str) -> str:
    """A fact without its date, case or punctuation, for telling repeats apart."""
    text = re.sub(r"^- \d{4}-\d{2}-\d{2}: ", "", text.strip())
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def parse(answer: str | None) -> list[Proposal]:
    """The model's reply as proposals: the JSON array in it (fenced or bare), bad entries dropped."""
    if not answer:
        return []
    start, end = answer.find("["), answer.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    from vision.memory import slug

    out: list[Proposal] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        fact = " ".join(str(item.get("fact") or "").split())
        if not fact or len(fact) > FACT_CHARS or _norm(fact) in seen:
            continue
        topic = str(item.get("topic") or "").strip()
        try:
            topic = slug(topic) if topic else ""
        except ValueError:
            topic = ""  # not a usable file name: the core keeps it rather than losing it
        about = " ".join(str(item.get("about") or "").split())
        seen.add(_norm(fact))
        out.append(Proposal(fact, topic, about))
    return out[:MAX_PROPOSALS]


def fresh(props: list[Proposal], existing: list[str]) -> list[Proposal]:
    """The proposals memory does not hold yet (the same line, or one that contains it)."""
    had = [_norm(ln) for ln in existing]
    return [p for p in props if _norm(p.fact) and not any(_norm(p.fact) in h for h in had)]


def message(history: list[dict], existing: list[str], topics: list[tuple[str, str, int]]) -> str:
    """What the reviewer reads: the facts kept so far, the topic files, and the conversation."""
    from vision.sessions import format_transcript

    kept = "\n".join(existing) if existing else "(nothing yet)"
    filed = "\n".join(f"{name}: {about}" for name, about, _ in topics) if topics else "(none yet)"
    convo = format_transcript(history, max_chars=TRANSCRIPT_CHARS)
    return f"<memory>\n{kept}\n</memory>\n\n<topics>\n{filed}\n</topics>\n\n<conversation>\n{convo}\n</conversation>"


def proposals(cfg, history: list[dict], timeout: float | None = None) -> list[Proposal]:
    """What the conversation in `history` (rows of role and text) left that is worth keeping, or []
    when it left nothing, no provider could be asked, or the call failed or timed out. The chat's own
    provider is asked first."""
    from vision import memory
    from vision.models import provider_for
    from vision.suggest import one_shot

    if not any(m.get("role") == "assistant" for m in history):
        return []
    existing = memory.facts() + [ln for name, _, _ in memory.topics() for ln in memory.facts(name)]
    topics = memory.topics()
    try:
        prefer = provider_for(cfg.brain.model)
    except Exception:  # noqa: BLE001
        prefer = None
    answer = one_shot(message(history, existing, topics), providers=getattr(cfg.providers, "enabled", None),
                      timeout=timeout if timeout is not None else cfg.memory.review_timeout_s,
                      prompt=PROMPT, models=MODELS, prefer=prefer)
    return fresh(parse(answer), existing)


def question(props: list[Proposal]) -> dict:
    """The tick list: every proposal ticked to start with, untick what should not be kept."""
    from vision.memory import topics

    have = {name for name, _, _ in topics()}
    options = []
    for p in props:
        new = p.topic and p.topic not in have
        options.append({"label": p.fact, "description": f"→ {p.file}" + (f" (new topic: {p.about or p.topic})" if new else "")})
    return {
        "question": "Worth remembering from this conversation? Untick anything that should not be kept.",
        "header": "Memory",
        "multiSelect": True,
        "options": options,
        "preselected": list(range(len(options))),
        "other": False,
    }


def chosen(props: list[Proposal], answers: dict | None) -> list[Proposal]:
    """The proposals ticked on the form (its answer is the labels joined by ", ", so each label is
    looked for whole; a fact that contains another is matched before it is removed from the text)."""
    if not answers:
        return []
    text = next(iter(answers.values()), "") or ""
    picked = []
    for p in sorted(props, key=lambda p: -len(p.fact)):
        if p.fact in text:
            picked.append(p)
            text = text.replace(p.fact, "", 1)
    return [p for p in props if p in picked]


def save(props: list[Proposal]) -> list[tuple[str, str]]:
    """Write the chosen proposals; returns (file name, line written) for each."""
    from vision.memory import remember

    saved = []
    for p in props:
        try:
            saved.append((p.file, remember(p.fact, p.topic, p.about)))
        except ValueError:
            continue
    return saved


def summary(saved: list[tuple[str, str]]) -> str:
    """'remembered 3 (2 in MEMORY.md, 1 in myproject.md)'."""
    if not saved:
        return "nothing added to memory"
    counts: dict[str, int] = {}
    for name, _ in saved:
        counts[name] = counts.get(name, 0) + 1
    where = ", ".join(f"{n} in {name}" for name, n in counts.items())
    return f"remembered {len(saved)} ({where})"


def due(cfg, turns: int) -> bool:
    """Whether leaving this chat should review it: the setting is on and it had enough turns to leave anything."""
    return str(cfg.memory.review).lower() == "ask" and turns >= cfg.memory.review_after_turns
