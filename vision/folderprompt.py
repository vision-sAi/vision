"""Per-folder instructions: a `.vision.md` in the working directory (or a folder above it) that every
brain reads, the way a project's instructions reach a project chat but without making a project.

Each coding CLI also has a file of its own that it reads by itself: Claude Code a `CLAUDE.md`, Codex an
`AGENTS.md`. A project written for one would leave the other blind, so every brain is handed the ones
its CLI doesn't read (providers.Provider.reads_instructions): a Codex chat in a repo with a CLAUDE.md
gets its rules, and a Claude chat an AGENTS.md's.

The file is looked for from the working directory upwards, stopping at the git root or the home
directory, whichever comes first (so a repo-wide file and a subfolder's both apply). It is read on every
turn, so an edit shows up with the next reply: the prompt is part of a kept-alive process's command line,
which is what makes it restart (brain._fits)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = ".vision.md"
MAX_CHARS = 8_000  # per file; past this the rest is dropped and the prompt says so
CLI_FILES = ("CLAUDE.md", "AGENTS.md")  # the coding CLIs' own instruction files, handed to the brains that don't read them
CLI_MAX_CHARS = 24_000  # they run longer than a .vision.md (a whole repo's notes)


@dataclass
class Instructions:
    path: Path
    text: str
    cut: bool = False  # the file was longer than MAX_CHARS


def _name(cfg) -> str:
    """The configured file name; "" switches the feature off."""
    return (getattr(cfg, "folder_instructions", FILE_NAME) or "").strip()


def find(workdir: str, name: str = FILE_NAME) -> list[Path]:
    """The instruction files that apply to `workdir`, nearest first. The walk ends at the folder that
    holds `.git` or at the home directory (both inclusive); a working directory under neither is
    walked up to the filesystem root."""
    if not name or not workdir:
        return []
    home = Path(os.path.expanduser("~")).resolve()
    here = Path(workdir).expanduser().resolve()
    found: list[Path] = []
    for d in (here, *here.parents):
        f = d / name
        if f.is_file():
            found.append(f)
        if (d / ".git").exists() or d == home:
            break
    return found


def load(workdir: str, name: str = FILE_NAME, cap: int = MAX_CHARS) -> list[Instructions]:
    """The files in effect with their text, nearest first; empty files and unreadable ones are skipped."""
    out = []
    for p in find(workdir, name):
        try:
            text = p.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not text:
            continue
        cut = len(text) > cap
        out.append(Instructions(p, text[:cap].rstrip() if cut else text, cut))
    return out


def section(workdir: str, name: str = FILE_NAME, cap: int = MAX_CHARS, found: list[Instructions] | None = None) -> str:
    """The system-prompt section: every file in effect, the outermost first so the nearest one has the
    last word. "" when there is none."""
    parts = []
    for ins in reversed(found if found is not None else load(workdir, name, cap)):
        head = (f"Instructions from {ins.path} (they hold for every turn while you work in that folder, "
                "after the user's own words in a message):")
        body = ins.text + (f"\n(cut at {cap} characters; the file is longer)" if ins.cut else "")
        parts.append(f"{head}\n{body}")
    return "\n\n".join(parts)


def _provider(cfg) -> str:
    try:
        from vision.models import provider_for

        return provider_for(getattr(cfg, "model", "") or "") or ""
    except Exception:  # noqa: BLE001  - no model yet: treated as reading none of the CLI files
        return ""


def cli_files(workdir: str, cfg) -> list[Instructions]:
    """The CLI instruction files (CLAUDE.md, AGENTS.md) in effect here that this brain's CLI doesn't read by
    itself, nearest first; one file reached twice (AGENTS.md a link to CLAUDE.md) counts once, and not at
    all when the CLI reads it under its own name."""
    if not _name(cfg):
        return []
    from vision.providers import cap

    native = set(cap(_provider(cfg), "reads_instructions", ()) or ())
    # Already read: under the CLI's own name, or as the configured file (folder_instructions = "AGENTS.md").
    seen = {p.resolve() for n in native | {_name(cfg)} for p in find(workdir, n)}
    out = []
    for name in CLI_FILES:
        if name in native:
            continue
        for ins in load(workdir, name, CLI_MAX_CHARS):
            if ins.path.resolve() not in seen:
                seen.add(ins.path.resolve())
                out.append(ins)
    return out


def for_brain(workdir: str, cfg) -> str:
    """`section` with the file name from a BrainConfig (every provider calls this when it builds its prompt),
    then the CLI instruction files this brain's own CLI wouldn't read."""
    own = section(workdir, _name(cfg))
    other = section(workdir, cap=CLI_MAX_CHARS, found=cli_files(workdir, cfg))
    return "\n\n".join(p for p in (own, other) if p)


def summary(workdir: str, cfg) -> str:
    """'instructions: .vision.md' (or '.vision.md ×2', '.vision.md + CLAUDE.md') for a note or the status row;
    "" when none applies."""
    files = find(workdir, _name(cfg))
    names = ([f"{files[0].name}" + (f" ×{len(files)}" if len(files) > 1 else "")] if files else []) + \
        sorted({ins.path.name for ins in cli_files(workdir, cfg)})
    return f"instructions: {' + '.join(names)}" if names else ""
