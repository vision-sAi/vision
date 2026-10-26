"""Deterministic routing for the front end: who answers a request, and at what effort.

The conversation model (the chat's own model: a Claude, Codex or local one) is the front end. It talks to the user, answers
genuinely basic questions, and gets the weather and search-only web results as data. Everything
substantive goes to an agent that Vision's supervisor launches (vision/supervisor.py). The decision
is made here, in code, before the model sees the request, in this order:

1. an explicit choice by the user (`/agent codex --effort high`, "use Opus high for this",
   "answer this locally", "only search the web for this");
2. a weather request → Apple WeatherKit;
3. a request for current information → a search-only web lookup;
4. a genuinely basic question → the front end answers itself;
5. everything else → the default agent (Opus 5) at the default effort (medium), or at the high effort
   for architecture, hard debugging, repository-wide changes, security-sensitive work, multi-stage
   research, work spanning several systems, and a task that already failed at the default effort.

Nothing here runs a model or touches the network: `route()` is a pure function of the text and the
config, so it is cheap, testable, and the same in audit mode and live mode.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from vision.config import Config, RouterConfig
from vision.models import EFFORT_WORDS, THINKING_OFF

# -- the answer -------------------------------------------------------------------
LOCAL, WEATHER, SEARCH, DELEGATE, ANSWER, CANCEL = "local", "weather", "search", "delegate", "answer", "cancel"
KINDS = (LOCAL, WEATHER, SEARCH, DELEGATE, ANSWER, CANCEL)


@dataclass(frozen=True)
class Override:
    """What the user asked for explicitly. `text` is the request with the instruction taken out
    ("" when the instruction was the whole message, so it applies to the previous request)."""

    kind: str  # LOCAL | WEATHER | SEARCH | DELEGATE | CANCEL
    agent: str = ""  # DELEGATE: the agent's allowlist name ("opus", "codex")
    effort: str = ""  # DELEGATE: "" = the router's default for that route
    text: str = ""


@dataclass(frozen=True)
class Route:
    kind: str
    agent: str = ""  # DELEGATE: allowlist name
    effort: str = ""  # DELEGATE: the effort to run at
    explicit: bool = False  # the user chose it
    reason: str = ""  # one short phrase for the audit log and the chat
    text: str = ""  # the request the route applies to
    signals: tuple[str, ...] = field(default_factory=tuple)  # what the classifier matched

    @property
    def label(self) -> str:
        if self.kind == DELEGATE:
            return f"{self.agent} {self.effort}".strip()
        return self.kind

    def describe(self, cfg: RouterConfig) -> str:
        """`delegate → Opus 5 medium (code change)`, for the audit line."""
        from vision.models import model_label

        if self.kind == DELEGATE:
            model = agent_model(cfg, self.agent) or self.agent
            what = f"{model_label(model) or model} {self.effort}".strip()
            return f"delegate → {what}" + (f" ({self.reason})" if self.reason else "")
        return self.kind + (f" ({self.reason})" if self.reason else "")


# -- agents ----------------------------------------------------------------------
_AGENT_WORDS = {  # how people say it → allowlist name
    "opus": "opus", "claude": "opus", "codex": "codex", "gpt": "codex", "chatgpt": "codex", "openai": "codex",
}
_LOCAL_WORDS = ("local", "locally", "the local model", "yourself")


def agent_model(cfg: RouterConfig, agent: str) -> str:
    """The catalogue model an allowlist name maps to ("" when the name is not on the list). An empty
    mapping for codex means the first Codex model in the catalogue."""
    name = (agent or "").strip().lower()
    if name not in cfg.agents:
        return ""
    model = (cfg.agents.get(name) or "").strip()
    if model:
        return model
    if name == "codex":
        from vision.models import CODEX_MODELS

        return CODEX_MODELS[0].alias if CODEX_MODELS else ""
    if name == "opus":
        return "opus"
    return ""


def agent_names(cfg: RouterConfig) -> list[str]:
    return [n for n in cfg.agents if agent_model(cfg, n)]


def agent_for_model(cfg: RouterConfig, model: str) -> str:
    """The allowlist name whose model this is ("" when no agent runs on it)."""
    for name in cfg.agents:
        if agent_model(cfg, name) == (model or "").strip().lower():
            return name
    return ""


# -- explicit overrides ---------------------------------------------------------------
# Spoken phrasing never means the local thinking switch ("handle this off the record"), so "off" is
# only accepted spelled out: /agent <name> --effort off.
_EFFORTS = "|".join(w for w in EFFORT_WORDS if w != THINKING_OFF)


def _known_agent_words() -> list[str]:
    """Every word that reads as "a model to hand this to": the allowlist nicknames, the catalogue
    aliases and the usual vendor names. A name outside the allowlist is then refused, not ignored."""
    from vision.models import MODELS

    words = list(_AGENT_WORDS) + [m.alias for m in MODELS] + ["gemini", "llama", "mistral", "deepseek", "sonnet", "haiku", "fable", "grok", "o3", "o4"]
    return sorted(dict.fromkeys(w.lower() for w in words), key=len, reverse=True)  # longest first: "gpt-5.5" before "gpt"


_AGENT_NAMES = "|".join(re.escape(w) for w in _known_agent_words())
_EFFORT_PHRASE = (
    rf"(?:(?:with|at|on|using)\s+)?(?:(?:an?\s+)?(?P<effort>{_EFFORTS})(?:\s+effort|\s+reasoning)?)"
)
_TAIL = r"(?:\s+(?:for|on|with|to|at)\s+(?:this|that|it|this one|the task|the job|this task)(?:\s+one)?)?"
# "Use Opus high for this." / "Launch Codex with high effort." / "Have Codex handle this." / "Get Opus to do it."
_NL_AGENT = re.compile(
    rf"\b(?:(?:please\s+)?(?:use|launch|run|start|spin up|try|ask|get|have|let|make|send (?:this|that|it) to|hand (?:this|that|it) to|give (?:this|that|it) to|delegate (?:this|that|it) to|route (?:this|that|it) to)"
    rf"\s+)(?P<agent>{_AGENT_NAMES})(?![\w\-.])(?:\s+5(?:\.\d)?)?"
    rf"(?:\s+{_EFFORT_PHRASE})?"
    rf"(?:\s+(?:to\s+)?(?:handle|do|take|tackle|look at|look into|sort|fix|deal with|work on|run|have a go at)\s+(?:this|that|it|this one))?"
    rf"(?:\s+{_EFFORT_PHRASE.replace('effort>', 'effort2>')})?"
    rf"{_TAIL}[.!]?",
    re.IGNORECASE,
)
# "Answer this locally." / "answer locally" / "keep it local" / "don't delegate" / "no agents"
_NL_LOCAL = re.compile(
    r"\b(?:(?:please\s+)?(?:answer|do|handle|reply(?: to)?|keep|try|take)\s+(?:this|that|it|this one)?\s*(?:locally|yourself|local|on the local model|with the local model|using the local model)"
    r"(?:\s+(?:with|using|on)\s+the local model)?"
    r"|(?:just|only)?\s*(?:answer|reply)\s+locally|locally,?\s+please|no (?:agents?|delegation)|(?:don't|do not|dont) delegate(?: this| that| it)?"
    r"|keep (?:this|that|it) local|local only)[.!]?",
    re.IGNORECASE,
)
# "Only search the web for this." / "just search" / "web search only" / "search only"
_NL_SEARCH = re.compile(
    r"\b(?:(?:please\s+)?(?:only|just)\s+(?:do a\s+)?(?:web\s+)?search(?:\s+the\s+web)?(?:\s+(?:for|on)\s+(?:this|that|it))?"
    r"|(?:web\s+)?search(?:\s+the\s+web)?\s+only(?:\s+(?:for|on)\s+(?:this|that|it))?"
    r"|search[- ]only(?:\s+(?:for|on)\s+(?:this|that|it))?)[.!]?",
    re.IGNORECASE,
)
_NL_CANCEL = re.compile(
    r"^\s*(?:please\s+)?(?:cancel|stop|abort|kill|halt)(?:\s+(?:the|that|this))?(?:\s+(?:agent|run|task|job|worker|work|it|that|opus|codex))?\s*[.!]?\s*$",
    re.IGNORECASE,
)
_LEAD = re.compile(r"^[\s:,;.\-–—]*(?:and|then|please|to|so)?[\s:,;.\-–—]*", re.IGNORECASE)
_COMMANDS = ("agent", "local", "search", "weather", "cancel", "opus", "codex")


class OverrideError(ValueError):
    """The user's instruction could not be honoured as written (never silently changed)."""


def _clean(text: str) -> str:
    text = _LEAD.sub("", text.strip(), count=1)
    text = re.sub(r"[\s,;:\-–—]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_command(text: str, cfg: RouterConfig) -> Override | None:
    """A slash command at the start of the message, or None. `/agent opus --effort high fix the bug`,
    `/agent codex`, `/opus`, `/codex high`, `/local <question>`, `/search <query>`, `/weather [place]`,
    `/cancel`. Raises OverrideError for a malformed one so it is never mistaken for a request."""
    m = re.match(r"^/(\w+)(?:\s+(.*))?$", text.strip(), re.DOTALL)
    if not m or m.group(1).lower() not in _COMMANDS:
        return None
    cmd, rest = m.group(1).lower(), (m.group(2) or "").strip()
    if cmd == "cancel":
        return Override(CANCEL, text=rest)
    if cmd == WEATHER:
        return Override(WEATHER, text=f"what's the weather in {rest}" if rest else "what's the weather")
    if cmd in (LOCAL, SEARCH):
        return Override(cmd, text=rest)
    words = rest.split()
    agent = cmd if cmd in ("opus", "codex") else ""
    effort = ""
    remainder: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        lw = w.lower()
        if lw in ("--effort", "-e", "--e"):
            if i + 1 >= len(words):
                raise OverrideError("--effort needs a level: " + ", ".join(EFFORT_WORDS))
            effort, i = words[i + 1].lower(), i + 2
            continue
        if lw.startswith("--effort="):
            effort, i = lw.split("=", 1)[1], i + 1
            continue
        if not agent and not remainder:
            agent, i = lw, i + 1
            continue
        if agent and not effort and not remainder and lw in EFFORT_WORDS:
            effort, i = lw, i + 1
            continue
        remainder.append(w)
        i += 1
    if not agent:
        raise OverrideError("/agent needs a name: " + ", ".join(agent_names(cfg) or cfg.agents))
    agent = _AGENT_WORDS.get(agent, agent)
    if agent not in cfg.agents:
        raise OverrideError(f"unknown agent {agent!r}; the allowlist is " + ", ".join(agent_names(cfg) or ["(empty)"]))
    if effort and effort not in EFFORT_WORDS:
        raise OverrideError(f"unknown effort {effort!r}; levels are " + ", ".join(EFFORT_WORDS))
    return Override(DELEGATE, agent=agent, effort=effort, text=" ".join(remainder).strip())


def parse_override(text: str, cfg: RouterConfig) -> Override | None:
    """An explicit choice in the message: a slash command, or a natural-language instruction such as
    "Use Opus high for this." The instruction is removed from `text`; what is left is the request."""
    cmd = parse_command(text, cfg)
    if cmd is not None:
        return cmd
    if _NL_CANCEL.match(text):
        return Override(CANCEL)
    m = _NL_LOCAL.search(text)
    if m:
        return Override(LOCAL, text=_clean(text[: m.start()] + " " + text[m.end():]))
    m = _NL_SEARCH.search(text)
    if m:
        return Override(SEARCH, text=_clean(text[: m.start()] + " " + text[m.end():]))
    m = _NL_AGENT.search(text)
    if m:
        agent = _AGENT_WORDS.get(m.group("agent").lower(), m.group("agent").lower())
        effort = (m.group("effort") or m.group("effort2") or "").lower()
        if agent not in cfg.agents:
            raise OverrideError(f"{m.group('agent')} is not an agent Vision may launch; the allowlist is " + ", ".join(agent_names(cfg) or ["(empty)"]))
        return Override(DELEGATE, agent=agent, effort=effort, text=_clean(text[: m.start()] + " " + text[m.end():]))
    return None


# -- classification ----------------------------------------------------------------
# What makes a request substantive. Each entry is (signal name, pattern); one hit is enough.
_SUBSTANTIVE = [
    ("code", re.compile(r"```|\b(?:code|function|class|method|variable|module|package|library|script|regex|sql|query|api|endpoint|"
                        r"json|yaml|toml|html|css|python|javascript|typescript|rust|golang|java\b|c\+\+|bash|shell|docker|kubernetes|"
                        r"git|commit|branch|merge|rebase|pull request|\bpr\b|repo|repository|codebase|compile|build|lint|typecheck)\b", re.I)),
    ("files", re.compile(r"(?:^|[\s(\"'`])(?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)*|\b\w+\.(?:py|js|ts|tsx|jsx|rs|go|java|rb|c|h|cpp|toml|yaml|yml|json|md|txt|sh|cfg|ini|env|lock|sql|html|css)\b|\b(?:file|folder|directory|path|the project|the repo)\b", re.I)),
    ("work", re.compile(r"\b(?:implement|refactor|fix|debug|build|create|write|generate|add|remove|delete|rename|move|update|upgrade|migrate|deploy|"
                        r"install|configure|set up|setup|run the tests?|run tests?|test it|benchmark|profile|optimi[sz]e|automate|integrate|"
                        r"scaffold|wire up|hook up|convert|port|rewrite the|clean up|tidy)\b", re.I)),
    ("debugging", re.compile(r"\b(?:bug|error|exception|traceback|stack ?trace|crash(?:es|ing)?|fails?|failing|broken|doesn'?t work|not working|"
                             r"won'?t (?:start|run|build|compile)|segfault|timeout|hangs?|freezes?|leak)\b", re.I)),
    ("long-form", re.compile(r"\b(?:essay|report|article|blog post|proposal|spec|specification|documentation|readme|plan(?:ning)?|roadmap|"
                             r"strategy|outline|draft|cover letter|white ?paper|presentation|slides?|detailed|in depth|in-depth|thorough|comprehensive|"
                             r"step[- ]by[- ]step)\b", re.I)),
    ("analysis", re.compile(r"\b(?:analy[sz]e|analysis|evaluate|assess|review|audit|compare|comparison|pros and cons|trade[- ]?offs?|investigate|"
                            r"research|deep dive|diagnose|root cause)\b", re.I)),
    ("recommendation", re.compile(r"\b(?:should (?:i|we)|recommend|recommendation|best (?:way|approach|option|choice|practice)|which (?:one|should)|"
                                  r"is it (?:worth|better)|would you (?:pick|choose|use))\b", re.I)),
    ("high-stakes", re.compile(r"\b(?:medical|diagnos|symptom|dosage|prescription|legal|lawsuit|contract|liabilit|tax(?:es)?\b|invest(?:ment|ing)?|"
                               r"mortgage|pension|insurance|visa|immigration|surgery|pregnan|overdose|suicid|self[- ]harm)", re.I)),
    ("multiple constraints", re.compile(r"\b(?:but also|and also|as well as|in addition|additionally|constraints?|requirements?|must (?:not|also)|"
                                        r"without (?:breaking|changing)|make sure (?:it|that)|while (?:keeping|preserving))\b", re.I)),
    ("uncertain", re.compile(r"\b(?:conflicting|contradict|not sure (?:if|whether|which)|unclear (?:if|whether)|ambiguous|either way|"
                             r"sources? (?:say|disagree))\b", re.I)),
]
# What pushes a delegated task to the high effort.
_HIGH = [
    ("architecture", re.compile(r"\b(?:architect(?:ure|ural)?|system design|design (?:a|the) (?:system|service|schema|architecture|pipeline|platform)|"
                                r"data model|schema design|redesign|re-architect|scalab(?:le|ility)|distributed|microservices?|event[- ]driven)\b", re.I)),
    ("hard debugging", re.compile(r"\b(?:intermittent|flaky|heisenbug|race condition|deadlock|memory leak|nondeterministic|non-deterministic|"
                                  r"can'?t reproduce|hard to reproduce|only sometimes|randomly|no idea why|not sure why|mysterious|weird(?:ly)?|"
                                  r"corrupt(?:ed|ion)?|silently)\b", re.I)),
    ("repository-wide", re.compile(r"\b(?:whole|entire|across the|all the|every)\s+(?:repo|repository|codebase|project|module|file|files|package|service|"
                                   r"services|test suite)s?\b|\brepo(?:sitory)?-wide\b|\bcodebase-wide\b|\bmonorepo\b|\bmass rename\b", re.I)),
    ("security", re.compile(r"\b(?:security|vulnerab|exploit|injection|xss|csrf|auth(?:n|z|entication|orization)?\b|oauth|jwt|password|"
                            r"credential|secret|token|encrypt|decrypt|tls|ssl|certificate|permission|privilege|sandbox|hardening|cve)", re.I)),
    ("multi-stage research", re.compile(r"\b(?:research|literature|survey|compare (?:several|multiple|all)|multi[- ]?(?:step|stage|part)|"
                                        r"end[- ]to[- ]end|full (?:analysis|audit|review)|deep dive|thorough(?:ly)?|comprehensive|exhaustive)\b", re.I)),
    ("several systems", re.compile(r"\b(?:between the|across the|both the)\s+\w+\s+and\b|\b(?:frontend|front-end|backend|back-end|database|queue|cache|"
                                   r"api|worker|cli|server|client|ios|android|web app)\b.*\b(?:frontend|front-end|backend|back-end|database|queue|"
                                   r"cache|api|worker|cli|server|client|ios|android|web app)\b|\bintegrat(?:e|ion) (?:with|between)\b|\bmigration\b", re.I)),
]
# A request for current information: the answer changes with time, so a fresh search beats memory.
_CURRENT = re.compile(
    r"\b(?:latest|newest|most recent|recent(?:ly)?|current(?:ly)?|right now|as of (?:today|now)|at the moment|today'?s|tonight'?s|"
    r"this (?:week|month|year|morning|evening)|yesterday|breaking|news|headlines?|trending|what happened|who won|score|results? of the|"
    r"release(?:d| date| notes)?|announce[ds]?|launch(?:ed)?|out yet|is (?:\w+ )?down|outage|price of|how much (?:is|does|are)|cost of|stock|"
    r"exchange rate|when is the next|schedule for|opening hours|open (?:today|now|tomorrow)|in 20\d\d|202\d)\b",
    re.IGNORECASE,
)
# A basic question: something the front end can answer from general knowledge in a sentence or two.
_BASIC = re.compile(
    r"^(?:(?:hi|hey|hello|yo|hiya|morning|evening|thanks|thank you|cheers|ta|ok(?:ay)?|cool|nice|great|good (?:morning|evening|afternoon|night)|"
    r"how are you|how'?s it going|you there|are you there|goodbye|bye|see you|tell me a joke|what'?s up)\b.{0,40}|"
    r"(?:what(?:'s| is| are| does| do|s)?|who(?:'s| is| was| were)?|which|explain|define|describe|meaning of|what'?s the meaning of|"
    r"difference between|how do you (?:say|spell|pronounce)|translate|rewrite|rephrase|reword|paraphrase|shorten|proofread|correct|"
    r"synonym|antonym|how many|how long|how far|how old|when (?:was|did|is)|where (?:is|was)|why (?:is|do|does|are)|is it true|"
    r"can you explain|what does .{1,40} (?:mean|stand for)|spell|convert|how much is|what time)\b.{0,160})[?.!]?$",
    re.IGNORECASE | re.DOTALL,
)
_STATE_CHANGE = re.compile(r"\b(?:remember|save|note (?:that|this)|remind me|set (?:a|an|the)|turn (?:on|off)|switch|send|email|post|publish|order|buy|book|schedule|cancel my)\b", re.I)
BASIC_MAX_CHARS = 200  # longer than this and it is not a basic question, whatever it starts with


def classify(text: str) -> tuple[str, str, tuple[str, ...]]:
    """(kind, reason, signals) for a request with no explicit choice: WEATHER, SEARCH, LOCAL or DELEGATE.
    `signals` are the substantive markers found (they also decide the effort, see `effort_for`)."""
    from vision.usage import is_usage_request
    from vision.weather import is_weather_request

    body = text.strip()
    if not body:
        return LOCAL, "empty", ()
    if is_weather_request(body):
        return WEATHER, "weather request", ("weather",)
    if is_usage_request(body):
        return LOCAL, "usage request", ("usage",)  # answered from the `usage` field, like the phone's Usage page
    signals = tuple(name for name, pat in _SUBSTANTIVE if pat.search(body))
    high = tuple(name for name, pat in _HIGH if pat.search(body))
    if _CURRENT.search(body) and set(signals) <= {"code"} and not high and not _STATE_CHANGE.search(body):
        return SEARCH, "current information", ("current",)  # "latest python release": news about code is still news
    basic_shape = len(body) <= BASIC_MAX_CHARS and body.count("\n") == 0 and _BASIC.match(body) is not None
    if basic_shape and not high and set(signals) <= {"code"} and "```" not in body:
        return LOCAL, "basic question", ("basic",)  # "What is JSON?": a definition, not a job
    if signals or high:
        return DELEGATE, ", ".join(dict.fromkeys(signals + high)), signals + high
    if _STATE_CHANGE.search(body):
        return DELEGATE, "changes state", ("state change",)
    if len(body) <= BASIC_MAX_CHARS and _BASIC.match(body) and body.count("\n") == 0:
        return LOCAL, "basic question", ("basic",)
    if len(body) <= 60 and body.count(" ") <= 8 and not body.endswith("?"):
        return LOCAL, "short remark", ("basic",)  # "cheers", "ok go on", "right"
    return DELEGATE, "uncertain: not obviously basic", ("uncertain",)


def effort_for(cfg: RouterConfig, signals: tuple[str, ...], failed_before: bool = False) -> tuple[str, str]:
    """(effort, why): the high effort for the hard categories and for a task that already failed at
    the default effort; otherwise the default."""
    hard = [s for s in signals if s in {name for name, _ in _HIGH}]
    if failed_before:
        return cfg.high_effort, "failed at " + cfg.default_effort
    if hard:
        return cfg.high_effort, ", ".join(hard)
    return cfg.default_effort, ""


def route(text: str, cfg: Config, override: Override | None = None, *, waiting: bool = False,
          failed_before: bool = False) -> Route:
    """The route for a request, in priority order: the explicit choice, the weather, current
    information, a basic question, else the default agent. `waiting`: an agent is waiting for the
    user's answer, so a plain message is that answer (an explicit choice still wins)."""
    rc = cfg.router
    if override is not None:
        body = override.text
        if override.kind == CANCEL:
            return Route(CANCEL, explicit=True, reason="cancelled by the user", text=body)
        if override.kind == DELEGATE:
            agent = override.agent or rc.default_agent
            if not agent_model(rc, agent):
                raise OverrideError(f"{agent} is not an agent Vision may launch; the allowlist is " + ", ".join(agent_names(rc) or ["(empty)"]))
            effort = override.effort or effort_for(rc, classify(body)[2], failed_before)[0]
            return Route(DELEGATE, agent=agent, effort=effort, explicit=True, reason="chosen by the user", text=body)
        return Route(override.kind, explicit=True, reason="chosen by the user", text=body)
    if waiting:
        return Route(ANSWER, reason="answers the agent's question", text=text)
    kind, reason, signals = classify(text)
    if kind != DELEGATE:
        return Route(kind, reason=reason, text=text, signals=signals)
    effort, why = effort_for(rc, signals, failed_before)
    return Route(DELEGATE, agent=rc.default_agent, effort=effort, reason=reason + (f"; high: {why}" if why else ""),
                 text=text, signals=signals)


_SECRET = re.compile(r"(?i)(?:(?:api[_-]?key|token|secret|password|passwd|pwd|authorization|bearer)\s*[:=]\s*\S+|sk-[A-Za-z0-9_\-]{8,}|"
                     r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END|"
                     r"\b[A-Fa-f0-9]{32,}\b|\b(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{40,}={0,2})")


def redact(text: str, limit: int = 120) -> str:
    """A request as it may be logged: secrets blanked, cut to `limit` characters, one line."""
    text = _SECRET.sub("[redacted]", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
