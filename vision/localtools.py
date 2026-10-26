"""The Local brain's tools: run here, on this machine, by Vision itself.

A model on a local server has no agent harness of its own, so Vision is the harness: it describes these
tools to the model as OpenAI-style functions, runs each call the model makes, and feeds the result
back. The names match Claude Code's (Bash, Read, Write, Edit, WebSearch, WebFetch) so the persona notes,
the status line and the tool rows in the reply need no special case. Glob and Grep are not offered: Bash
covers them. WebSearch asks DuckDuckGo's HTML endpoint (no key) or Brave's API when `[local].brave_api_key`
is set; WebFetch reads one page as plain text. Nothing else is self-hosted or needs an account.

Enforcement mirrors what the other providers get in auto mode: nothing asks for approval, the
`denied_tools` patterns (`Bash(prefix:*)`) are refused with an error the model sees, and the subprocess
environment blocks `claude`, `codex` and `grok` like every other brain's shell. Plan mode offers Read only.
Tool output is cut to a fixed size: the model's context is small and every result stays in it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

MAX_OUTPUT = 8_000  # characters of one tool result the model gets back (head and tail of anything longer)
HISTORY_OUTPUT = 1_500  # what a result shrinks to once the turn is over and it sits in the transcript
READ_LINES = 400  # default lines per Read
BASH_TIMEOUT = 120.0  # seconds; the model may ask for less
WEB_TIMEOUT = 20.0  # seconds per search or fetch
FETCH_CHARS = 6_000  # default page text per WebFetch (the model may ask for up to MAX_OUTPUT)
SEARCH_RESULTS = 6
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Vision/1.0"
NAMES = ("Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch")
READ_ONLY = ("Read", "WebSearch", "WebFetch")  # what plan mode keeps

SPECS = {
    "Bash": {
        "description": "Run a shell command with bash in the working directory and get its output (stdout and stderr). "
                       "Use it for anything a terminal can do: ls, find, grep, git, python, curl, rm... Keep output short "
                       "(head, grep, wc): it all lands in your context.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run."},
                "description": {"type": "string", "description": "A few words saying what the command does, shown to the user."},
                "timeout_s": {"type": "integer", "description": f"Seconds to wait, at most {int(BASH_TIMEOUT)}."},
            },
            "required": ["command"],
        },
    },
    "Read": {
        "description": "Read a text file, with line numbers. Relative paths are relative to the working directory. "
                       f"Returns up to {READ_LINES} lines; use offset and limit for more of a long file.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line to return (1-based)."},
                "limit": {"type": "integer", "description": "How many lines to return."},
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "description": "Create or overwrite a file with the given content (parent directories are created).",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "description": "Replace an exact string in a file. old_string must appear exactly once unless replace_all is true.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "WebSearch": {
        "description": "Search the web: titles, links and snippets for a query. Follow up with WebFetch on a link "
                       "when the snippets are not enough. Not for the weather (run `vision weather` with Bash).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "count": {"type": "integer", "description": f"Results to return, at most {SEARCH_RESULTS * 2}."}},
            "required": ["query"],
        },
    },
    "WebFetch": {
        "description": "Fetch a web page (http/https) and get its readable text, scripts and markup stripped. "
                       f"Returns up to max_chars characters (default {FETCH_CHARS}).",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["url"],
        },
    },
}


def available(allowed: list[str] | None, mode: str = "auto") -> list[str]:
    """The tool names the Local brain offers: the configured list cut to what Vision can run itself,
    and Read alone in plan mode (there is no sandbox to make a shell read-only)."""
    names = [n for n in NAMES if n in (allowed or [])]
    if mode == "plan":
        names = [n for n in names if n in READ_ONLY]
    return names


def specs(names: list[str]) -> list[dict]:
    """OpenAI-style function specs for `tools` in a chat completion request."""
    return [{"type": "function", "function": {"name": n, **SPECS[n]}} for n in names if n in SPECS]


# -- deny rules ------------------------------------------------------------------
_RULE = re.compile(r"^Bash\((.*)\)$")
_SPLIT = re.compile(r"\n|;|&&|\|\||\||\$\(|`")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def denied(command: str, rules: list[str] | None) -> str | None:
    """The `Bash(prefix:*)` rule the command falls under, or None. Every simple command in a pipeline or
    list is checked (`true; sudo x` is caught), after leading environment assignments and grouping."""
    prefixes: list[tuple[str, str]] = []
    for rule in rules or []:
        m = _RULE.match(rule.strip())
        if not m:
            continue
        inner = m.group(1)
        prefixes.append((rule, inner[:-2] if inner.endswith(":*") else inner))
    if not prefixes:
        return None
    for segment in _SPLIT.split(command):
        seg = segment.strip().lstrip("({ ").strip()
        while (m := _ASSIGNMENT.match(seg)):
            seg = seg[m.end():]
        for rule, prefix in prefixes:
            if seg == prefix or seg.startswith(prefix + " ") or (prefix and not prefix[-1].isalnum() and seg.startswith(prefix)):
                return rule
    return None


# -- running a call ----------------------------------------------------------------
def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    """Head and tail of an over-long result, with a note on how much is missing."""
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.65), int(limit * 0.3)
    return text[:head] + f"\n…[{len(text) - head - tail:,} characters omitted]…\n" + text[-tail:]


def _path(workdir: str, given: str) -> Path:
    p = Path(os.path.expanduser(str(given or "")))
    return p if p.is_absolute() else Path(workdir) / p


def run(name: str, arguments: str | dict, workdir: str, denied_rules: list[str] | None,
        holder: dict | None = None, lock: threading.Lock | None = None, brave_key: str = "") -> tuple[str, bool]:
    """Run one tool call: (result text, is_error). Every failure is text for the model, never an
    exception. A Bash process is kept in `holder["proc"]` so a cancel can kill it. `brave_key` switches
    WebSearch from DuckDuckGo to Brave's API."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "Error: the tool arguments were not a JSON object.", True
    try:
        if name == "Bash":
            return _bash(args, workdir, denied_rules, holder if holder is not None else {}, lock or threading.Lock())
        if name == "Read":
            return _read(args, workdir)
        if name == "Write":
            return _write(args, workdir)
        if name == "Edit":
            return _edit(args, workdir)
        if name == "WebSearch":
            return _search(args, brave_key)
        if name == "WebFetch":
            return _fetch(args)
    except OSError as e:
        return f"Error: {e}", True
    return f"Error: unknown tool {name!r}. Available: {', '.join(NAMES)}.", True


def _bash(args: dict, workdir: str, rules: list[str] | None, holder: dict, lock: threading.Lock) -> tuple[str, bool]:
    from vision.brain import brain_env

    command = str(args.get("command") or "").strip()
    if not command:
        return "Error: no command given.", True
    rule = denied(command, rules)
    if rule:
        return f"Error: this command is forbidden by the user's deny rule {rule}. Tell the user what you would run instead.", True
    try:
        timeout = min(float(args.get("timeout_s") or BASH_TIMEOUT), BASH_TIMEOUT)
    except (TypeError, ValueError):
        timeout = BASH_TIMEOUT
    with lock:
        if holder.get("cancelled"):
            return "Error: cancelled.", True
        proc = subprocess.Popen(["bash", "-c", command], cwd=workdir, env=brain_env("local"), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", encoding="utf-8")
        holder["proc"] = proc
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return clip((out or "") + f"\nError: the command did not finish within {timeout:.0f} s and was killed."), True
    finally:
        with lock:
            holder.pop("proc", None)
    if holder.get("cancelled"):
        return "Error: cancelled.", True
    text = out or ""
    if proc.returncode:
        text += f"\n[exit code {proc.returncode}]"
    return clip(text.strip() or "(no output)"), proc.returncode != 0


def _read(args: dict, workdir: str) -> tuple[str, bool]:
    p = _path(workdir, args.get("file_path"))
    if not p.exists():
        return f"Error: {p} does not exist.", True
    if p.is_dir():
        return f"Error: {p} is a directory; list it with Bash (ls).", True
    lines = p.read_text(errors="replace", encoding="utf-8").splitlines()
    try:
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or READ_LINES))
    except (TypeError, ValueError):
        offset, limit = 1, READ_LINES
    chunk = lines[offset - 1: offset - 1 + limit]
    if not chunk:
        return f"(empty: {p} has {len(lines)} lines)" if lines else f"(empty file: {p})", False
    text = "\n".join(f"{offset + i:6}\t{ln}" for i, ln in enumerate(chunk))
    rest = len(lines) - (offset - 1 + len(chunk))
    if rest > 0:
        text += f"\n… {rest} more lines (offset={offset + len(chunk)})"
    return clip(text), False


def _write(args: dict, workdir: str) -> tuple[str, bool]:
    p = _path(workdir, args.get("file_path"))
    content = str(args.get("content") if args.get("content") is not None else "")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Wrote'} {p} ({len(content):,} characters).", False


def _edit(args: dict, workdir: str) -> tuple[str, bool]:
    p = _path(workdir, args.get("file_path"))
    if not p.is_file():
        return f"Error: {p} does not exist.", True
    old, new = str(args.get("old_string") or ""), str(args.get("new_string") if args.get("new_string") is not None else "")
    if not old:
        return "Error: old_string is empty.", True
    text = p.read_text(errors="replace", encoding="utf-8")
    n = text.count(old)
    if n == 0:
        return f"Error: old_string was not found in {p}. Read the file and copy the text exactly.", True
    if n > 1 and not args.get("replace_all"):
        return f"Error: old_string appears {n} times in {p}; include more surrounding text or set replace_all.", True
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"Edited {p}: replaced {n} occurrence{'s' if n > 1 else ''}.", False


# -- the web ---------------------------------------------------------------------
def _http_get(url: str, headers: dict | None = None, limit: int = 3_000_000) -> tuple[str, bytes]:
    """(content type, body) of a GET, following redirects; the body is cut at `limit` bytes.
    Raises urllib.error.URLError / HTTPError. Patched in tests."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en", **(headers or {})})
    with urllib.request.urlopen(req, timeout=WEB_TIMEOUT) as r:
        return str(r.headers.get("Content-Type") or ""), r.read(limit)


def _web_error(e: Exception, what: str) -> tuple[str, bool]:
    if isinstance(e, urllib.error.HTTPError):
        return f"Error: {what} answered HTTP {e.code}.", True
    reason = getattr(e, "reason", e)
    return f"Error: {what} could not be reached ({reason}).", True


class _DDGResults(HTMLParser):
    """The organic results of html.duckduckgo.com: `a.result__a` (title, redirect href) then `a.result__snippet`."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self.results.append({"title": "", "url": _ddg_target(a.get("href") or ""), "snippet": ""})
            self._field = "title"
        elif tag == "a" and "result__snippet" in cls and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "a":
            self._field = None

    def handle_data(self, data):
        if self._field and self.results:
            self.results[-1][self._field] += data


def _ddg_target(href: str) -> str:
    """The real link behind DuckDuckGo's `//duckduckgo.com/l/?uddg=<url>&rut=...` redirect."""
    target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
    if target:
        return target[0]
    return "https:" + href if href.startswith("//") else href


def _search(args: dict, brave_key: str) -> tuple[str, bool]:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: no query given.", True
    try:
        count = max(1, min(int(args.get("count") or SEARCH_RESULTS), SEARCH_RESULTS * 2))
    except (TypeError, ValueError):
        count = SEARCH_RESULTS
    try:
        if brave_key:
            engine = "Brave Search"
            _, body = _http_get("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": count}),
                                {"Accept": "application/json", "X-Subscription-Token": brave_key})
            results = [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                       for r in (json.loads(body).get("web") or {}).get("results") or []]
        else:
            engine = "DuckDuckGo"
            _, body = _http_get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}))
            page = body.decode("utf-8", errors="replace")
            parser = _DDGResults()
            parser.feed(page)
            results = parser.results
            if not results and "anomaly" in page:
                # Its bot check: the IP has been rate-limited for a while. A key-based engine is the fix.
                return ("Error: DuckDuckGo is refusing automated searches from this address for now (bot check). "
                        "Do not retry this turn: answer from what you know, or WebFetch a page whose URL you know. "
                        "The user can set [local].brave_api_key for a search engine that does not do this."), True
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _web_error(e, engine)
    results = [r for r in results if r["url"]][:count]
    if not results:
        return f"No results from {engine} for {query!r} (or it refused the request; try other words, or WebFetch a likely page).", False
    lines = [f"{engine} results for {query!r}:"]
    for i, r in enumerate(results, 1):
        # Collapsed outside the f-string: an expression with a backslash in it only parses from Python 3.12.
        title = re.sub(r"\s+", " ", r["title"]).strip()
        snippet = re.sub(r"\s+", " ", r["snippet"]).strip()
        lines.append(f"{i}. {title}\n   {r['url']}" + (f"\n   {snippet}" if snippet else ""))
    return clip("\n".join(lines)), False


_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote", "td", "th", "dt", "dd"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe", "nav", "header", "footer", "aside", "form", "button"}
_RAW_REWRITES = (  # a rendered page whose plain source is a URL away: hand the model the source
    (re.compile(r"^https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)$"), r"https://raw.githubusercontent.com/\1/\2/\3"),
)


class _PageText(HTMLParser):
    """A page as reading text: block tags become line breaks, scripts and styles vanish, the title leads."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
        return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()  # one line per block: compact for a small context


def _fetch(args: dict) -> tuple[str, bool]:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: WebFetch needs an http or https URL.", True
    for pattern, replacement in _RAW_REWRITES:
        url = pattern.sub(replacement, url)
    try:
        limit = max(500, min(int(args.get("max_chars") or FETCH_CHARS), MAX_OUTPUT))
    except (TypeError, ValueError):
        limit = FETCH_CHARS
    try:
        ctype, body = _http_get(url)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _web_error(e, url)
    charset = re.search(r"charset=([\w-]+)", ctype)
    text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
        page = _PageText()
        page.feed(text)
        title, text = page.title.strip(), page.text()
    else:
        title = ""
    head = f"{url}" + (f" — {title}" if title else "") + f" ({ctype.split(';')[0] or 'text'}, {len(text):,} characters)"
    return head + "\n\n" + clip(text, limit), False
