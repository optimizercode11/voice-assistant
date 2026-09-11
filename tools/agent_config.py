#!/usr/bin/env python3
"""Configuration for the assistant's capabilities: tools, RAG, MCP.

TOML, because the config is meant to be read and edited by a person, and
`tomllib` is in the standard library on the Python both machines run (3.12).

Everything has a default that keeps the assistant exactly as conservative as it
was before tools existed: no tools offered, no filesystem read outside the
configured corpus, no network.  Enabling a capability is always an explicit
line in the config file, never a side effect of a package being installed.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MAX_TOOLS = 32                 # the engine accepts 64; a voice turn needs fewer
MAX_TOOL_RESULT_CHARS = 6000   # per call, into the model's context
MAX_TOOL_ROUNDS = 4            # generations per turn, so 3 rounds of tool calls: measured live,
                               # "look at that folder" needs roots -> request_directory -> answer


class ConfigError(Exception):
    """A config problem worth refusing to start over. Never a silent default."""


@dataclass
class ToolLimits:
    rounds: int = 2
    per_call_seconds: float = 10.0
    result_chars: int = MAX_TOOL_RESULT_CHARS
    turn_seconds: float = 150.0
    generation_seconds: float = 45.0

    def __post_init__(self):
        self.rounds = max(1, min(MAX_TOOL_ROUNDS, int(self.rounds)))
        self.per_call_seconds = max(0.5, min(120.0, float(self.per_call_seconds)))
        self.result_chars = max(200, min(MAX_TOOL_RESULT_CHARS, int(self.result_chars)))
        self.turn_seconds = max(10.0, min(600.0, float(self.turn_seconds)))
        self.generation_seconds = max(5.0, min(180.0, float(self.generation_seconds)))


@dataclass
class RetrievalConfig:
    enabled: bool = False
    sources: list[Path] = field(default_factory=list)
    index: Path = Path("var/notes.sqlite")
    top_k: int = 4
    max_chars: int = 2400
    stale_after_seconds: int = 86400

    def __post_init__(self):
        self.top_k = max(1, min(8, int(self.top_k)))
        self.max_chars = max(400, min(MAX_TOOL_RESULT_CHARS, int(self.max_chars)))


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: float = 15.0
    allow: list[str] = field(default_factory=list)     # empty = every tool it lists
    deny: list[str] = field(default_factory=list)
    purpose: str = ""                                  # one line, shown to the model

    def __post_init__(self):
        self.name = str(self.name)
        self.timeout_seconds = max(1.0, min(120.0, float(self.timeout_seconds)))


@dataclass
class FetchConfig:
    """Opt-in, allowlisted, bounded.  Empty allow-list means the tool refuses."""
    enabled: bool = False
    allow_hosts: list[str] = field(default_factory=list)
    max_bytes: int = 20000
    timeout_seconds: float = 8.0
    schemes: tuple[str, ...] = ("https",)


@dataclass
class ApprovalsConfig:
    """Directory grants the assistant may ask for *while it runs*.

    Every other capability is fixed at config time; this one grows.  It is
    therefore off unless a person turns it on, and even then it only enables
    the *asking*: no code path from a tool call to a grant exists, and the
    click that grants lives on the page behind an HTTP route the model has no
    tool for.  See tools/approvals.py for why the ordering is that way.
    """
    enabled: bool = False
    file: Path = Path("var/approvals.json")


@dataclass
class AgentConfig:
    limits: ToolLimits = field(default_factory=ToolLimits)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    approvals: ApprovalsConfig = field(default_factory=ApprovalsConfig)
    mcp: list[MCPServerConfig] = field(default_factory=list)
    builtins: dict[str, bool] = field(default_factory=lambda: {"now": True})
    source: Path | None = None

    @property
    def tool_count_hint(self) -> int:
        return (len(self.builtins) + len(self.mcp) + (1 if self.retrieval.enabled else 0)
                + (1 if self.approvals.enabled else 0))


def _typed(raw: dict, key: str, kind, label: str):
    """TOML is typed; a string where a bool belongs is a typo, not a truthy value.

    Coercing "no" to True would silently enable a capability, which is the one
    failure mode a capability config must not have.
    """
    value = raw[key]
    if kind is bool and not isinstance(value, bool):
        raise ConfigError(f"{label}.{key} must be true or false, got {value!r}")
    if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise ConfigError(f"{label}.{key} must be an integer, got {value!r}")
    if kind is float and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ConfigError(f"{label}.{key} must be a number, got {value!r}")
    return value


def _flag(raw: dict, key: str, label: str, default: bool = False) -> bool:
    return _typed(raw, key, bool, label) if key in raw else default


def _number(raw: dict, key: str, label: str, default, kind=int):
    return _typed(raw, key, kind, label) if key in raw else default


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _paths(value, label: str) -> list[Path]:
    if isinstance(value, (str, Path)):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, (str, Path)) for item in value):
        raise ConfigError(f"{label} must be a path or a list of paths")
    return [Path(item) for item in value]


def _retrieval(raw: dict) -> RetrievalConfig:
    if not raw:
        return RetrievalConfig()
    unknown = set(raw) - {"enabled", "sources", "index", "top_k", "max_chars", "stale_after_seconds"}
    if unknown:
        raise ConfigError(f"[retrieval] unknown keys: {sorted(unknown)}")
    return RetrievalConfig(enabled=_flag(raw, "enabled", "retrieval"),
                           sources=_paths(raw.get("sources", []), "retrieval.sources"),
                           index=Path(raw.get("index", str(RetrievalConfig.index))),
                           top_k=_number(raw, "top_k", "retrieval", 4),
                           max_chars=_number(raw, "max_chars", "retrieval", 2400),
                           stale_after_seconds=_number(raw, "stale_after_seconds", "retrieval", 86400))


def _fetch(raw: dict) -> FetchConfig:
    if not raw:
        return FetchConfig()
    unknown = set(raw) - {"enabled", "allow_hosts", "max_bytes", "timeout_seconds", "schemes"}
    if unknown:
        raise ConfigError(f"[fetch] unknown keys: {sorted(unknown)}")
    hosts = raw.get("allow_hosts", [])
    if not isinstance(hosts, list) or any(not isinstance(h, str) for h in hosts):
        raise ConfigError("fetch.allow_hosts must be a list of hostnames")
    schemes = tuple(raw.get("schemes", ("https",)))
    if not schemes or any(s not in {"http", "https"} for s in schemes):
        raise ConfigError("fetch.schemes must be a non-empty subset of http/https")
    fetch = FetchConfig(enabled=_flag(raw, "enabled", "fetch"), allow_hosts=[h.lower() for h in hosts],
                        max_bytes=_number(raw, "max_bytes", "fetch", 20000),
                        timeout_seconds=_number(raw, "timeout_seconds", "fetch", 8.0, float), schemes=schemes)
    if fetch.enabled and not fetch.allow_hosts:
        raise ConfigError("[fetch] enabled but allow_hosts is empty -- refuse to fetch anywhere")
    return fetch


def _approvals(raw: dict) -> ApprovalsConfig:
    if not raw:
        return ApprovalsConfig()
    unknown = set(raw) - {"enabled", "file"}
    if unknown:
        raise ConfigError(f"[approvals] unknown keys: {sorted(unknown)}")
    value = raw.get("file", str(ApprovalsConfig.file))
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("approvals.file must be a path")
    # Relative to the assistant's own directory, always.  An absolute grants
    # file is a second, unreviewed input: the operator would have to remember
    # that the file the *page* writes and the file the *file server* reads are
    # resolved by two different processes with two different working directories.
    if os.path.isabs(value) or value.startswith("~"):
        raise ConfigError("approvals.file must be relative to the assistant's directory, so the "
                          "bridge, the page and the file server all resolve it the same way")
    return ApprovalsConfig(enabled=_flag(raw, "enabled", "approvals"), file=Path(value))


def _mcp(raw: list) -> list[MCPServerConfig]:
    if not isinstance(raw, list):
        raise ConfigError("[[mcp.server]] must be an array of tables")
    servers, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ConfigError("each [[mcp.server]] must be a table")
        unknown = set(row) - {"name", "command", "args", "env", "enabled", "timeout_seconds", "allow",
                              "deny", "purpose"}
        if unknown:
            raise ConfigError(f"[[mcp.server]] unknown keys: {sorted(unknown)}")
        name = str(row.get("name", ""))
        if not name.replace("_", "").replace("-", "").isalnum() or not name:
            raise ConfigError(f"mcp server name must be alphanumeric: {name!r}")
        if name in seen:
            raise ConfigError(f"duplicate mcp server name: {name}")
        seen.add(name)
        if "timeout_seconds" in row and not isinstance(row["timeout_seconds"], (int, float)):
            raise ConfigError(f"mcp server {name} timeout_seconds must be a number")
        command = str(row.get("command", ""))
        if not command:
            raise ConfigError(f"mcp server {name} has no command")
        args = row.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ConfigError(f"mcp server {name} args must be a list of strings")
        env = row.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in env.items()):
            raise ConfigError(f"mcp server {name} env must be a table of strings")
        purpose = row.get("purpose", "")
        if not isinstance(purpose, str):
            raise ConfigError(f"mcp server {name} purpose must be a string")
        servers.append(MCPServerConfig(name=name, command=command, args=list(args), env=dict(env),
                                       purpose=purpose.strip()[:300],
                                       enabled=_flag(row, "enabled", f"mcp.server.{name}", True),
                                       timeout_seconds=float(row.get("timeout_seconds", 15.0)),
                                       allow=list(row.get("allow", [])), deny=list(row.get("deny", []))))
    return servers


def load(path: Path | None) -> AgentConfig:
    """Read a config file.  A missing file is fine; a broken one is not."""
    if path is None:
        path = os.environ.get("VOICE_TOOLS_CONFIG", "")
    if not str(path).strip():
        return AgentConfig()          # no file named: the conservative defaults
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ConfigError(f"{path} is not readable TOML: {error}") from error
    unknown = set(raw) - {"limits", "retrieval", "fetch", "approvals", "mcp", "builtins"}
    if unknown:
        raise ConfigError(f"{path}: unknown top-level keys: {sorted(unknown)}")
    limits_raw = _table(raw, "limits")
    known = {"rounds", "per_call_seconds", "result_chars", "turn_seconds", "generation_seconds"}
    if set(limits_raw) - known:
        raise ConfigError(f"[limits] unknown keys: {sorted(set(limits_raw) - known)}")
    builtins = raw.get("builtins", {"now": True})
    if not isinstance(builtins, dict) or any(not isinstance(k, str) or not isinstance(v, bool)
                                             for k, v in builtins.items()):
        raise ConfigError("[builtins] must be a table of tool = true/false")
    for name in builtins:
        if not name.replace("_", "").isalnum():
            raise ConfigError(f"builtin tool name must be alphanumeric: {name}")
    for key, value in limits_raw.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"[limits] {key} must be a number, got {value!r}")
    config = AgentConfig(limits=ToolLimits(**limits_raw), retrieval=_retrieval(_table(raw, "retrieval")),
                         fetch=_fetch(_table(raw, "fetch")),
                         approvals=_approvals(_table(raw, "approvals")), mcp=_mcp(raw.get("mcp", {}).get("server", [])
                                                                      if isinstance(raw.get("mcp"), dict)
                                                                      else raw.get("mcp", [])),
                         builtins=dict(builtins), source=path)
    if config.tool_count_hint > MAX_TOOLS:
        raise ConfigError(f"{config.tool_count_hint} tools configured; the voice turn caps at {MAX_TOOLS}")
    return config
