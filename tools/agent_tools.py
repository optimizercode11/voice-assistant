#!/usr/bin/env python3
"""The tool registry: what the model is offered, and what happens when it calls.

Three sources, one shape.  A builtin, a retrieval query and an MCP tool all
arrive at the model as the same OpenAI tool object and all come back through
the same `execute()`, so the loop in voice_chat.py never learns where a tool
came from and no path gets to skip the validator.

Errors are *returned to the model*, not raised as HTTP 500s.  A model that
wrote `{"zone": 3}` instead of `{"zone": "UTC"}` should be told in one readable
sentence and allowed to correct itself; that is a normal turn, not an outage.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import agent_config
import approvals
import retrieval

MAX_ARGUMENT_CHARS = 8000
USER_AGENT = "voice-assistant/1.0 (local; +no-crawl)"


class ToolError(Exception):
    """A refusal worth telling the model about, in its own words."""


# ---------------------------------------------------------------- validation
def _type_ok(value, wanted: str) -> bool:
    if wanted == "object":
        return isinstance(value, dict)
    if wanted == "array":
        return isinstance(value, list)
    if wanted == "string":
        return isinstance(value, str)
    if wanted == "boolean":
        return isinstance(value, bool)
    if wanted == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if wanted == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if wanted == "null":
        return value is None
    return True                                   # unknown keyword: permissive by design


def _describe(path: str, key: str) -> str:
    """A name the model can act on: 'zone', 'filter.from', 'tags[2]'."""
    return key if not path else f"{path}.{key}"


def _where(path: str, fallback: str = "value") -> str:
    return path or fallback


def validate(schema: dict, value, path: str = "") -> list[str]:
    """A JSON-Schema subset: enough to hold a tool's declared shape honest.

    Unknown keywords are ignored rather than rejected -- servers ship schemas
    with `additionalProperties`, `$schema`, `default` -- but every keyword we
    *do* implement is enforced, because that is what stops a model's plausible
    but wrong arguments from reaching someone else's program.
    """
    if not isinstance(schema, dict):
        return []
    problems: list[str] = []
    wanted = schema.get("type")
    if isinstance(wanted, str) and not _type_ok(value, wanted):
        return [f"{path or 'arguments'} must be {wanted}, got {type(value).__name__}"]
    if isinstance(wanted, list) and not any(_type_ok(value, item) for item in wanted):
        return [f"{path or 'arguments'} must be one of {wanted}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path or 'arguments'} must be one of {schema['enum']!r}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            problems.append(f"{_where(path)} needs at least {schema['minLength']} characters")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            problems.append(f"{path or 'value'} is longer than {schema['maxLength']} characters")
        if schema.get("pattern") and not re.search(str(schema["pattern"]), value):
            problems.append(f"{path or 'value'} does not match the required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            problems.append(f"{path or 'value'} must be >= {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            problems.append(f"{path or 'value'} must be <= {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"missing required {_describe(path, key)}")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key in value:
                    problems += validate(sub, value[key], _describe(path, key))
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            problems.append(f"{path or 'value'} needs at least {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            problems.append(f"{path or 'value'} has more than {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                problems += validate(items, element, f"{path}[{index}]")
    return problems


# ------------------------------------------------------------------- results
@dataclass
class ToolResult:
    ok: bool
    content: str
    error: str = ""
    ms: int = 0
    source: str = ""            # filled from the Tool by Registry.execute; a handler may override
    meta: dict = field(default_factory=dict)

    def for_model(self) -> str:
        return self.content if self.ok else f"tool error: {self.error}"

    def public(self) -> dict:
        row = {"name": self.meta.get("name", "?"), "ok": self.ok, "ms": self.ms, "source": self.source}
        if self.error:
            row["error"] = self.error
        if "citations" in self.meta:
            row["citations"] = self.meta["citations"]
        if self.ok and isinstance(self.meta.get("control"), dict) and self.meta["control"]:
            # A control is a request to the PAGE, not to the model: "stop
            # listening after this reply".  It rides on the tool row so the
            # bridge can aggregate it into the answer without the loop knowing
            # which tools exist.  Only a successful call may carry one.
            row["control"] = dict(self.meta["control"])
        return row


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: object
    source: str = "builtin"                  # builtin | retrieval | mcp:<server>
    timeout: float = 10.0
    read_only: bool = True

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description[:1000],
                             "parameters": self.parameters}}


# ------------------------------------------------------------------ builtins
def _now(arguments: dict, context: "Context") -> ToolResult:
    zone = arguments.get("zone", "local")
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    local = datetime.now().astimezone().isoformat(timespec="seconds")
    text = f"UTC {stamp}" if zone == "UTC" else f"local {local} (UTC {stamp})"
    return ToolResult(True, text, meta={"name": "now"})


def _pause_listening(arguments: dict, context: "Context") -> ToolResult:
    """Stop the page taking automatic turns after this reply.

    Measured on the live bridge (2026-09-11): asked "stop listening for a bit",
    the model said "I'll pause listening" and the page kept listening, because
    saying it is all the model could do.  This makes it true.  The tool does
    nothing on the server -- the microphone is in the browser -- it hands the
    page a control on the answer and the page closes the loop.

    Deliberately one-directional.  There is no resume_listening tool: a person
    pauses the microphone precisely so that nothing said in the room reaches the
    model, and a model that could re-open it on its own judgement would undo
    that on the first ambiguous sentence.  Resuming is a click or the space
    bar, on the page, by whoever is looking at it.
    """
    reason = str(arguments.get("reason") or "").strip()[:200]
    return ToolResult(True,
                      "Listening will pause as soon as this reply has been spoken. Tell the user in one "
                      "short sentence that you have stopped listening and that they can press Resume "
                      "listening on the page, or the space bar, when they want to continue. Do not ask a "
                      "question, and do not promise to notice when they are back: you cannot hear "
                      "anything until they resume.",
                      meta={"name": "pause_listening",
                            "control": {"pause_listening": True, "pause_reason": reason}})


def _search_notes(arguments: dict, context: "Context") -> ToolResult:
    config = context.config.retrieval
    status = retrieval.status(config.sources, config.index, config.stale_after_seconds)
    if not status.get("ready"):
        # Stale is a warning, missing is not: an out-of-date index still answers
        # the question, an absent one answers it with nothing, which is worse.
        return ToolResult(False, "", error=f"notes are unavailable: {status.get('reason', 'index not ready')}",
                          meta={"name": "search_notes"})
    hits = retrieval.search(config.index, arguments["query"], arguments.get("k") or config.top_k,
                            config.max_chars)
    if not hits:
        return ToolResult(True, "No passage in the indexed notes matches this. Say that you could not find it "
                                "rather than guessing.", meta={"name": "search_notes", "citations": []})
    body = "\n\n".join(f"[{number + 1}] {hit.label()}\n{hit.text}" for number, hit in enumerate(hits))
    warning = "" if not status.get("stale") else "\n\n(note: the index is older than the files it indexes)"
    return ToolResult(True, body[:config.max_chars] + warning,
                      meta={"name": "search_notes",
                            "citations": [{"path": hit.path, "chunk": hit.ordinal, "heading": hit.heading}
                                          for hit in hits],
                            "stale": bool(status.get("stale"))})


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Re-check the allow-list at every hop, or a 302 is an allow-list bypass."""

    def __init__(self, allow: set[str]):
        super().__init__()
        self.allow = allow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_allowed(newurl, self.allow)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_allowed(url: str, allow: set[str]) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise ToolError("only absolute http(s) URLs are allowed")
    if parts.username or parts.password or parts.fragment:
        raise ToolError("URLs with credentials or fragments are refused")
    if not (host in allow or any(host.endswith("." + entry) for entry in allow)):
        raise ToolError(f"{host} is not in fetch.allow_hosts")
    return parts


def _strip_html(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def _fetch_url(arguments: dict, context: "Context") -> ToolResult:
    config = context.config.fetch
    if not config.enabled:
        return ToolResult(False, "", error="fetch_url is disabled in this deployment", meta={"name": "fetch_url"})
    url = arguments["url"]
    allow = {entry.lower() for entry in config.allow_hosts}
    try:
        _assert_allowed(url, allow)
        if urllib.parse.urlsplit(url).scheme not in set(config.schemes):
            raise ToolError(f"scheme not permitted here ({', '.join(config.schemes)} only)")
    except ToolError as error:
        return ToolResult(False, "", error=str(error), meta={"name": "fetch_url"})
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain,text/html;q=0.9"})
    opener = urllib.request.build_opener(_SafeRedirect(allow))
    try:
        with opener.open(request, timeout=config.timeout_seconds) as response:
            final = _assert_allowed(response.geturl(), allow)
            kind = response.headers.get("Content-Type", "")
            if not any(kind.lower().startswith(allowed) for allowed in
                       ("text/", "application/json", "application/xml", "text/xml")):
                raise ToolError(f"that URL is {kind or 'unknown type'}, not text")
            raw = response.read(config.max_bytes + 1)
    except ToolError as error:
        return ToolResult(False, "", error=str(error), meta={"name": "fetch_url"})
    except (urllib.error.URLError, OSError, ValueError) as error:
        return ToolResult(False, "", error=f"could not fetch it: {getattr(error, 'reason', error)}",
                          meta={"name": "fetch_url"})
    if len(raw) > config.max_bytes:
        raise ToolError("that page is larger than the fetch limit")
    text = _strip_html(raw.decode("utf-8", "replace")) if "html" in kind.lower() else raw.decode("utf-8", "replace")
    return ToolResult(True, f"{final.geturl()} ({kind.split(';')[0]})\n\n{text.strip()[:config.max_bytes]}",
                      meta={"name": "fetch_url", "host": final.hostname})


def urlsplit_scheme(url: str) -> str:
    return urllib.parse.urlsplit(url).scheme


# ------------------------------------------------------------- directory grants
def approvals_store(config: "agent_config.AgentConfig", root=None):
    """The grants file, resolved against the assistant's own directory.

    Three parties need the same file: the builtin that asks, the page that
    grants, and the file server that re-reads it before every call.  Deriving
    the path in one place is what makes that true by construction rather than
    by three people writing the same string.  The Store is stateless -- it
    reads on every call -- which is exactly why a grant needs no restart.
    """
    if not config.approvals.enabled:
        return None
    base = Path(root) if root is not None else Path.cwd()
    return approvals.Store(base / config.approvals.file)


def _request_directory(arguments: dict, context: "Context") -> ToolResult:
    """Ask a human for a directory.  It records a question and grants nothing.

    The wording of the reply is the security control here.  A tool result that
    said "request created" would be read as success, and the model would go on
    to describe a folder it has never opened.  So the first word is NOT GRANTED
    and the instruction is to stop talking about the folder's contents.
    """
    store = approvals_store(context.config, context.root)
    if store is None:
        return ToolResult(False, "", error="directory approvals are not enabled in this deployment",
                          meta={"name": "request_directory"})
    raw = str(arguments.get("path") or "").strip()
    if not raw:
        return ToolResult(False, "", error="request_directory needs a path",
                          meta={"name": "request_directory"})
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        base = Path(context.root) if context.root is not None else Path.cwd()
        candidate = base / candidate
    try:
        record = store.request(candidate, arguments.get("reason", ""))
    except approvals.ApprovalError as error:
        # The refusal text names the deny-list rule, which is what the model
        # needs in order to ask for something narrower instead of retrying.
        return ToolResult(False, "", error=str(error), meta={"name": "request_directory"})
    real = record["realpath"]
    if record.get("already_granted"):
        return ToolResult(True, f"{real} is already approved. Read it with the file tools now.",
                          meta={"name": "request_directory", "granted": True})
    size = f"~{record['files']} files" if not record.get("truncated") \
        else f"{record['files']}+ files"
    warning = ""
    if record.get("hints"):
        warning = (" It also contains credential-looking files: "
                   + ", ".join(str(h) for h in record["hints"][:3]) + ".")
    return ToolResult(True,
                      f"NOT GRANTED. {real} ({size}) is now waiting for the user to approve it on "
                      f"the page.{warning} Say in one short sentence which folder you want to read "
                      f"and why, and that they can approve it on the page. Then stop: you cannot "
                      f"read that folder until they do, so do not describe, summarise or guess at "
                      f"anything inside it, and do not say you have access.",
                      meta={"name": "request_directory", "granted": False, "realpath": real,
                            "files": record.get("files", 0), "hints": record.get("hints", [])})


# ------------------------------------------------------------------ registry
class Context:
    """What a handler may look at.  Deliberately not the HTTP connection."""

    def __init__(self, config: agent_config.AgentConfig, root: Path | None = None, session=None):
        self.config = config
        self.root = root or Path.cwd()
        self.session = session


class Registry:
    def __init__(self, config: agent_config.AgentConfig, context: Context | None = None,
                 session=None):
        self.config = config
        self.context = context or Context(config)
        self.session = session
        self.tools: dict[str, Tool] = {}
        self.notes: list[str] = []            # operator-facing provenance
        self._install_builtins()

    # -- construction ------------------------------------------------------
    def _add(self, tool: Tool) -> None:
        if tool.name in self.tools:
            self.notes.append(f"skipped duplicate tool name {tool.name}")
            return
        self.tools[tool.name] = tool

    def _install_builtins(self) -> None:
        config = self.config
        if config.builtins.get("now", False):
            self._add(Tool("now", "The current date and time. Use this instead of guessing when the user's "
                                  "question depends on today's date or the hour.",
                           {"type": "object", "properties": {"zone": {"type": "string", "enum": ["local", "UTC"]}},
                            "required": []}, _now, "builtin", 2.0))
        if config.builtins.get("pause_listening", False):
            # read_only=True is honest: nothing on the server changes.  The
            # effect is on the page, and only ever in the direction of hearing
            # less.
            self._add(Tool("pause_listening",
                           "Stop listening after this reply: the microphone closes and no further turns "
                           "are taken until the user presses Resume on the page. Call this ONLY when the "
                           "user explicitly asks you to stop or pause LISTENING, to turn the microphone "
                           "off, or says they are stepping away, taking a call or talking to someone "
                           "else for a while. A bare 'stop', 'wait', 'hold on' or 'quiet' means stop "
                           "talking, not stop listening: do not call this for those. Never call it on "
                           "your own initiative, and you cannot undo it: only the user can resume.",
                           {"type": "object", "properties":
                            {"reason": {"type": "string", "maxLength": 200,
                                        "description": "Why, in the user's words, shown on the page"}},
                            "required": []}, _pause_listening, "builtin", 2.0))
        if config.retrieval.enabled:
            self._add(Tool("search_notes", "Search the user's own indexed notes and documents with BM25. "
                                           "Use for anything about their project, machine, decisions or past "
                                           "conversations. Returns quoted passages with their file paths.",
                           {"type": "object", "properties":
                            {"query": {"type": "string", "minLength": 2, "maxLength": 300},
                             "k": {"type": "integer", "minimum": 1, "maximum": 8}},
                            "required": ["query"]}, _search_notes, "retrieval",
                           self.config.limits.per_call_seconds))
        if config.fetch.enabled:
            self._add(Tool("fetch_url", "Fetch one allowlisted web page as plain text. Only hosts named in the "
                                        "deployment config are reachable; anything else is refused.",
                           {"type": "object", "properties": {"url": {"type": "string", "maxLength": 2000}},
                            "required": ["url"]}, _fetch_url, "builtin",
                           self.config.fetch.timeout_seconds + 2, read_only=True))
        if config.approvals.enabled:
            # read_only=False is deliberate: it writes one line to the pending
            # list.  It never touches the folder being asked about, and the
            # status page should not claim this is a pure read.
            self._add(Tool("request_directory",
                           "Ask the user to approve one additional directory for reading. Use it "
                           "whenever the user asks about a folder that is not under one of the roots "
                           "the file tools list (they refuse absolute paths and paths outside a root): "
                           "call this with the absolute path instead of guessing or retrying. It only "
                           "files the request: access is granted by the user approving it on the page, "
                           "never by this tool, so the reply always says NOT GRANTED.",
                           {"type": "object", "properties":
                            {"path": {"type": "string", "minLength": 1, "maxLength": 500,
                                      "description": "Directory to read, absolute or relative to the assistant"},
                             "reason": {"type": "string", "maxLength": 300,
                                        "description": "One sentence on why, shown to the user"}},
                            "required": ["path"]},
                           _request_directory, "builtin", 5.0, read_only=False))

    @classmethod
    def build(cls, config: agent_config.AgentConfig, root: Path | None = None) -> "Registry":
        """Connect MCP servers if configured.  A dead server is a note, not a crash."""
        session = None
        if config.mcp:
            import mcp_client
            session = mcp_client.MCPSession.from_config(config.mcp)
            tools, failures = session.connect()
        else:
            tools, failures = [], []
        registry = cls(config, Context(config, root, session), session)
        for failure in failures:
            registry.notes.append(f"mcp server {failure['server']} did not start: {failure['error']}")
        for tool in tools:
            registry._add(Tool(tool.tool_name, tool.description or f"MCP tool {tool.name} on {tool.server}.",
                               tool.input_schema or {"type": "object", "properties": {}},
                               _mcp_handler(tool), f"mcp:{tool.server}",
                               _server_timeout(config, tool.server)))
        registry.mcp_failures = failures
        return registry

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    # -- the model-facing surface ------------------------------------------
    def specs(self) -> list[dict]:
        return [tool.spec() for tool in sorted(self.tools.values(), key=lambda item: item.name)][:agent_config.MAX_TOOLS]

    def names(self) -> list[str]:
        return sorted(self.tools)

    def manifest(self) -> str:
        """One line per capability, for the system prompt.

        Tool.spec() sends the schemas, and that turned out not to be enough: on
        the live bridge the model answered "look at that folder" by calling a
        tool that does not exist and never found request_directory, because
        nothing told it in planning terms what its tools are FOR.  The schemas
        say how to call; this says when.  MCP servers get one line from the
        operator's `purpose` in the config, so the model hears "web: search and
        read pages on the public internet" rather than five raw tool names.
        """
        lines = []
        by_server: dict[str, list[str]] = {}
        for name in self.names():
            tool = self.tools[name]
            if tool.source.startswith("mcp:"):
                by_server.setdefault(tool.source[4:], []).append(name)
                continue
            lines.append(f"- {name}: {_first_sentence(tool.description)}")
        purposes = {entry.name: entry.purpose for entry in self.config.mcp}
        for server, names in by_server.items():
            purpose = purposes.get(server) or f"tools from the {server} server"
            lines.append(f"- {', '.join(names)}: {purpose}")
        return "\n".join(lines)

    def execute(self, name: str, arguments, deadline: float | None = None) -> ToolResult:
        started = time.monotonic()
        tool = self.tools.get(name)
        if tool is None:
            # Naming a tool that does not exist is the model's mistake to fix, so
            # the reply lists what *is* available instead of a bare 500.
            return ToolResult(False, "", error=f"there is no tool called {name!r}. Available: "
                                               f"{', '.join(self.names()) or 'none'}",
                              ms=_elapsed(started), source="registry")
        if isinstance(arguments, (str, bytes)):
            if isinstance(arguments, bytes):
                arguments = arguments.decode("utf-8", "replace")
            if len(arguments) > MAX_ARGUMENT_CHARS:
                return ToolResult(False, "", error="the arguments were too large", ms=_elapsed(started),
                                  source=tool.source)
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except ValueError as error:
                return ToolResult(False, "", error=f"arguments must be one JSON object: {error}",
                                  ms=_elapsed(started), source=tool.source)
        else:
            parsed = arguments
        if not isinstance(parsed, dict):
            return ToolResult(False, "", error="arguments must be a JSON object", ms=_elapsed(started),
                              source=tool.source)
        problems = validate(tool.parameters or {}, parsed)
        if problems:
            return ToolResult(False, "", error="; ".join(problems[:4]), ms=_elapsed(started), source=tool.source)
        budget = min(tool.timeout, self.config.limits.per_call_seconds)
        if deadline is not None:
            budget = max(0.2, min(budget, deadline - time.monotonic()))
        runner = _Guarded(tool, parsed, self.context)
        runner.start()
        if not runner.done.wait(budget):
            runner.abandoned.set()
            return ToolResult(False, "", error=f"{name} did not finish in {budget:.1f}s and was abandoned",
                              ms=_elapsed(started), source=tool.source)
        result = runner.result or ToolResult(False, "", error=f"{name} produced no result", source=tool.source)
        result.ms = _elapsed(started)
        result.meta.setdefault("name", name)
        if not result.source:
            result.source = tool.source
        if len(result.content) > self.config.limits.result_chars:
            result.content = result.content[:self.config.limits.result_chars] + " […truncated]"
        return result

    def status(self) -> dict:
        return {"tools": [{"name": name, "source": self.tools[name].source,
                           "read_only": self.tools[name].read_only} for name in self.names()],
                "mcp": self.session.status() if self.session is not None else [],
                "notes": self.notes,
                "retrieval": retrieval.status(self.config.retrieval.sources, self.config.retrieval.index,
                                              self.config.retrieval.stale_after_seconds)
                           if self.config.retrieval.enabled else {"enabled": False}}


def _first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text)[:160]


def _server_timeout(config: agent_config.AgentConfig, server: str) -> float:
    for entry in config.mcp:
        if entry.name == server:
            return entry.timeout_seconds
    return config.limits.per_call_seconds


def _mcp_handler(tool):
    def call(arguments: dict, context: Context) -> ToolResult:
        if context.root is None:                     # pragma: no cover - defensive
            raise ToolError("no session for mcp tools")
        server = context.session.find(tool.tool_name) if context.session is not None else None
        if server is None:
            return ToolResult(False, "", error=f"{tool.tool_name} is no longer connected",
                              source=f"mcp:{tool.server}", meta={"name": tool.tool_name})
        mcp_server, captured = server
        try:
            text, is_error = mcp_server.call(captured, arguments)
        except Exception as error:
            return ToolResult(False, "", error=f"{tool.tool_name} failed: {error}",
                              source=f"mcp:{tool.server}", meta={"name": tool.tool_name})
        if is_error:
            return ToolResult(False, "", error=f"{tool.tool_name} reported: {text[:400]}",
                              source=f"mcp:{tool.server}", meta={"name": tool.tool_name})
        return ToolResult(True, text, source=f"mcp:{tool.server}", meta={"name": tool.tool_name})
    return call


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class _Guarded(threading.Thread):
    """A handler that will not outlive its budget.

    The thread is left running when it ignores the deadline -- there is no safe
    way to stop someone else's blocking call -- but it is a daemon and its
    result is discarded, so a hung MCP tool costs the turn its patience and
    nothing else.  The registry, not this thread, owns the process group.
    """

    def __init__(self, tool: Tool, arguments: dict, context: Context):
        super().__init__(daemon=True, name=f"tool-{tool.name}")
        self.tool, self.arguments, self.context = tool, arguments, context
        self.done = threading.Event()
        self.abandoned = threading.Event()
        self.result: ToolResult | None = None

    def run(self) -> None:
        try:
            self.result = self.tool.handler(self.arguments, self.context)
        except ToolError as error:
            self.result = ToolResult(False, "", error=str(error), source=self.tool.source)
        except Exception as error:                       # a tool bug is not a 500
            self.result = ToolResult(False, "", error=f"{self.tool.name} raised {type(error).__name__}: {error}",
                                     source=self.tool.source)
        finally:
            self.done.set()


