"""Vision's own long-term memory, shared by every brain.

A folder of markdown files under Vision's data dir (not Claude Code's per-project memory folder,
which only the Claude brain would ever see), one line per fact. MEMORY.md is the core: what should
shape every conversation, handed whole to whichever brain is active. Everything else is filed by
topic, one `<topic>.md` each whose first line says what it covers; the persona lists the topics and
the brain reads a file when the work touches it, so memory can grow without crowding the prompt.
/remember and /forget are the manual path, `vision memory <words>` searches the lot.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from vision.config import DATA_DIR

MEMORY_DIR = DATA_DIR / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
HEADER = "# Vision's memory\n\nOne fact per line. Vision reads this at the start of every conversation, whichever brain is active.\n\n"
MAX_PROMPT_CHARS = 12_000  # past this the core is truncated in the prompt and the brain is asked to tidy it
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def slug(topic: str) -> str:
    """A topic's file stem from whatever was typed ("iPhone app", "iphone-app.md"). Raises ValueError."""
    s = re.sub(r"[^a-z0-9]+", "-", topic.strip().lower().removesuffix(".md")).strip("-")
    if not _SLUG.fullmatch(s) or s == "memory":
        raise ValueError(f"not a topic name: {topic!r}")
    return s


def _path(topic: str = "") -> Path:
    return MEMORY_DIR / f"{slug(topic)}.md" if topic else MEMORY_FILE


def read(topic: str = "") -> str:
    """One file raw, the core by default ("" when it does not exist yet)."""
    try:
        return _path(topic).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def facts(topic: str = "") -> list[str]:
    """The bullet lines of one file (the core by default), without the header."""
    return [ln.rstrip() for ln in read(topic).splitlines() if ln.startswith("- ")]


def topics() -> list[tuple[str, str, int]]:
    """Every topic file as (name, what it covers, number of facts), by name."""
    out = []
    try:
        paths = sorted(p for p in MEMORY_DIR.glob("*.md") if p != MEMORY_FILE and _SLUG.fullmatch(p.stem))
    except OSError:
        return out
    for p in paths:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        about = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else ""
        out.append((p.stem, about, sum(ln.startswith("- ") for ln in lines)))
    return out


def search(words: str) -> list[tuple[str, str]]:
    """Fact lines holding every word (case-insensitive), across the core and all topics, as (file name, line)."""
    need = words.lower().split()
    if not need:
        raise ValueError("say what to look for")
    hits = []
    for name in ["", *(t[0] for t in topics())]:
        hits += [(_path(name).name, ln) for ln in facts(name) if all(w in ln.lower() for w in need)]
    return hits


def remember(fact: str, topic: str = "", about: str = "") -> str:
    """Append one fact (dated) to the core or a topic file; returns the line written. `about` is the
    first line of a topic file this call creates (what it covers)."""
    fact = " ".join(fact.split())
    if not fact:
        raise ValueError("nothing to remember")
    path = _path(topic)
    line = f"- {date.today().isoformat()}: {fact}"
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    text = read(topic)
    if not text:
        text = f"# {' '.join(about.split()) or slug(topic).replace('-', ' ')}\n\n" if topic else HEADER
    elif not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line + "\n", encoding="utf-8")
    return line


def forget(needle: str) -> list[str]:
    """Drop every fact line containing `needle` (case-insensitive), in the core and every topic;
    returns the lines removed."""
    needle = needle.strip().lower()
    if not needle:
        raise ValueError("say what to forget")
    gone = []
    for name in ["", *(t[0] for t in topics())]:
        kept, dropped = [], []
        for ln in read(name).splitlines():
            (dropped if ln.startswith("- ") and needle in ln.lower() else kept).append(ln)
        if dropped:
            _path(name).write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
            gone += dropped
    return gone


def backup() -> Path:
    """A copy of the whole folder beside it (memory-backups/<timestamp>), made before a tidy rewrites it."""
    import shutil
    from datetime import datetime

    dest = MEMORY_DIR.parent / "memory-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if MEMORY_DIR.is_dir():
        shutil.copytree(MEMORY_DIR, dest)
    else:
        dest.mkdir()
    return dest


def tidy_prompt(saved: Path) -> str:
    """The turn that has a brain clean the memory up (`vision memory tidy`, /memory tidy)."""
    return (
        f"Tidy your memory folder {MEMORY_DIR}. A copy was just saved to {saved}, so rewrite freely. Read "
        f"{MEMORY_FILE.name} and every topic file, then rewrite them so that:\n"
        "- only what a later conversation could not work out for itself is kept: a preference, a decision or "
        "rejection, a fact about the user's machines, accounts or projects;\n"
        "- lines that describe what was built, how code works or how far a task got are dropped;\n"
        "- lines saying the same thing become one, and of lines that contradict each other only the one still "
        "true survives (check the code or config when unsure);\n"
        f"- {MEMORY_FILE.name} holds only facts about the user, and rules for how you talk move to "
        "~/.config/vision/personality.md unless it already says them;\n"
        "- each topic file is one project or area, headed `# Name: what it covers`; move stray lines to the file "
        "they belong in, split a file that has become a catch-all, and delete files left empty.\n"
        "Keep each line's date. Finish by telling the user in a few lines what you merged, moved and dropped."
    )


def drop(line: str, topic: str = "") -> bool:
    """Remove one fact, named by its whole line, from the core or a topic (the phone's swipe to delete)."""
    line = line.rstrip()
    lines = read(topic).splitlines()
    if not line.startswith("- ") or line not in (ln.rstrip() for ln in lines):
        return False
    kept = [ln for ln in lines if ln.rstrip() != line]
    _path(topic).write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    return True


def entry(line: str, file: str = "") -> dict:
    """A fact line taken apart for a screen: {line, date, text} (and the file it is in, for search hits)."""
    m = re.match(r"- (\d{4}-\d{2}-\d{2}): (.*)", line)
    out = {"line": line, "date": m[1] if m else "", "text": m[2] if m else line[2:]}
    if file:
        out["topic"] = "" if file == MEMORY_FILE.name else file.removesuffix(".md")
    return out


def prompt_section(text: str | None = None) -> str:
    """The MEMORY block of the persona: the core's contents, the topic list and the rules for adding."""
    text = read() if text is None else text
    body = "\n".join(ln for ln in text.splitlines() if ln.startswith("- ")).strip()
    tidy = ""
    if len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS].rsplit("\n", 1)[0]
        tidy = (" It has grown past what fits here and is truncated: when you next have a moment, move the lines "
                "that only matter for one area into topic files and consolidate the rest into fewer lines.")
    contents = f"Its current contents:\n{body}" if body else "It is empty so far."
    filed = topics()
    if filed:
        index = (
            "The rest is filed by topic, one file each in that folder. Read a topic's file before you work on or "
            "answer about what it covers; `vision memory <words>` searches every file at once:\n"
            + "\n".join(f"- {name}.md ({n}): {about}" for name, about, n in filed)
        )
    else:
        index = "Nothing is filed by topic yet."
    return (
        f"MEMORY: your long-term memory is the folder {MEMORY_DIR} (Vision's own; it is shared by every brain "
        f"and is not any Claude Code, Codex or Grok memory folder). {MEMORY_FILE} is the core, the user's profile, "
        f"given to you in every conversation. {contents}{tidy}\n{index}\n"
        "Add to it when the user asks you to remember something, corrects how you work, makes a decision you should "
        "not undo later (\"don't bring that back\"), or shares a durable fact about themselves, their machines, "
        "accounts or projects. Save only what a later conversation could not work out for itself: never what you "
        "built, how the code works or how far a task got, which the code and its history already say. One line per "
        "fact, `- YYYY-MM-DD: fact`, written with your file or shell tools. Before adding a line, look for one that "
        "already covers it and rewrite that instead; delete lines that turn out wrong or stale. "
        "A fact about one project or area goes in that project's topic file; when none fits, start `<project>.md` in "
        "the folder with a first line `# Name: what it covers`. The core is the user's profile: only facts about them "
        "go there, never notes about the memory itself or things that are simply true of you, such as your name. "
        "How you should talk or behave is not a memory: it belongs in ~/.config/vision/personality.md. "
        "Do not store secrets, and do not save things this conversation alone needs. Mention a save in a few words, "
        "no more. If you cannot write the files (read-only sandbox, plan mode), tell the user to run /remember <fact>."
    )
