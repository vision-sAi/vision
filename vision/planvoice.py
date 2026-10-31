"""Plan approval by voice, for the phone (server.py) and the terminal (cli.py) alike.

When a plan waits for approval in a voice conversation, its first sentence is said aloud and whatever
the user says next answers the form. Only a reply made of the words below approves or declines; anything
else goes back to the model as changes to make (brain.py treats a free-text choice that way).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

_GO = r"yes|yeah|yep|yup|sure|ok|okay|go|go on|go ahead|go for it|do it|crack on|start|proceed|approved?|sounds good|let'?s go|send it"
_STOP = r"no|nope|nah|stop|cancel|don'?t|hold on|wait|leave it"
_FILL = r"please|then|mate|thanks|it"
GO = re.compile(rf"^\s*(?:{_GO})(?:[\s,.!]+(?:{_GO}|{_FILL}))*[\s,.!]*$", re.IGNORECASE)
STOP = re.compile(rf"^\s*(?:{_STOP})(?:[\s,.!]+(?:{_STOP}|{_FILL}))*[\s,.!]*$", re.IGNORECASE)
PROMPT = "Say go to start, or tell me what to change."


def plan_question(questions: list[dict] | None) -> dict | None:
    """The open form when it is the plan approval (brain.PLAN_QUESTION), else None."""
    return questions[0] if questions and len(questions) == 1 and questions[0].get("header") == "Plan" else None


def plan_answer(question: dict, text: str) -> dict[str, str]:
    """A spoken or typed reply as the plan form's answer: Yes, No, or the words as changes to make."""
    choice = "Yes" if GO.match(text) else "No" if STOP.match(text) else text.strip()
    return {question["question"]: choice}


def plan_summary(question: dict) -> str:
    """What is said aloud when a plan waits: its opening sentence, then how to answer."""
    plan = question.get("question", "").partition("\n\n")[2]
    for line in plan.splitlines():
        if line.lstrip().startswith("#"):
            continue  # a heading ("## Plan") is no summary
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).replace("**", "").replace("`", "").strip()
        if line:
            first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
            if len(first) > 200:
                first = first[:200].rsplit(" ", 1)[0] + "…"
            return f"Here's the plan: {first} {PROMPT}"
    return f"The plan's on screen. {PROMPT}"


def listen_for_answer(question: dict, closed: threading.Event, ss, new_speaker: Callable, mic, stt,
                      answer: Callable[[dict], object], set_hearing: Callable[[str], None],
                      note: Callable[[str], None], talking: Callable[[], bool]) -> None:
    """The terminal's half: once the reply `ss` has finished what it was saying, say the summary on a
    speaker of its own (`new_speaker()`), then listen until a reply is heard and `answer` it, or the
    form is answered some other way (keys, the phone, Esc), which sets `closed`."""
    ss.flush()
    time.sleep(0.3)
    while ss.speaking and not closed.is_set():
        time.sleep(0.05)  # what the reply already said goes first, and the mic never hears it
    if closed.is_set():
        return
    say = new_speaker()
    say.feed(plan_summary(question))
    say.finish()
    while not closed.is_set() and talking():
        set_hearing("listening for the plan answer…")
        try:
            audio = mic.record_utterance(cancel=closed, on_speech_start=lambda: set_hearing("hearing you…"))
        except Exception as e:  # noqa: BLE001  a dead mic: the form still takes keys and the phone
            note(f"[warn]couldn't listen for the plan answer: {e}[/warn]")
            return
        finally:
            set_hearing("")
        heard = stt.transcribe(audio).strip() if audio is not None and audio.size else ""
        if heard and not closed.is_set():
            answer(plan_answer(question, heard))
            return
