#!/usr/bin/env python3
"""Operator control for the assistant's capabilities.

Everything here answers a question an operator actually has before starting a
voice stack: what will the model be able to do, is the index current, and did
the MCP servers come up.  Nothing here restarts anything -- a 30 GiB stack is
started by systemd, per RUNBOOK.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_config
import agent_tools
import retrieval


def _config(path: Path | None) -> agent_config.AgentConfig:
    try:
        return agent_config.load(path)
    except agent_config.ConfigError as error:
        print(f"config: {error}", file=sys.stderr)
        raise SystemExit(2)


def _registry(config, connect: bool = False):
    return agent_tools.Registry.build(config) if connect else agent_tools.Registry(config)


def cmd_doctor(arguments) -> int:
    config = _config(arguments.config)
    registry = _registry(config, connect=bool(arguments.probe))
    problems, warnings = [], []
    try:
        status = registry.status()
        if not config.mcp and not config.retrieval.enabled and not config.fetch.enabled \
                and not any(config.builtins.values()):
            warnings.append("no capabilities are enabled; the assistant will only talk")
        if config.retrieval.enabled:
            notes = status["retrieval"]
            if not notes.get("exists"):
                problems.append(f"retrieval is enabled but {config.retrieval.index} does not exist "
                                "-- run: voicectl index build")
            elif notes.get("stale"):
                warnings.append(f"the index is stale: {notes.get('reason')}")
            elif notes.get("old"):
                warnings.append(f"the index is {notes.get('age_seconds', 0) // 3600}h old")
        for server in status["mcp"]:
            if not server["running"]:
                problems.append(f"mcp server {server['name']} is not running: {server['error'] or 'exited'}")
            elif not server["tools"]:
                warnings.append(f"mcp server {server['name']} connected but offered no tools")
        for note in registry.notes:
            warnings.append(note)
        if config.fetch.enabled:
            warnings.append(f"fetch_url is enabled for {', '.join(config.fetch.allow_hosts)}")
        if shutil.which("ffmpeg") is None:
            warnings.append("ffmpeg is not on PATH; the bridge cannot decode uploads")
        try:
            sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.OperationalError:
            problems.append("this SQLite build has no FTS5; retrieval cannot run here")
        print(f"tools ({len(registry.names())}): {', '.join(registry.names()) or 'none'}")
        for row in status["tools"]:
            print(f"  {row['name']:<34} {row['source']}")
        if status["mcp"]:
            print("mcp:")
            for server in status["mcp"]:
                print(f"  {server['name']:<16} {'up' if server['running'] else 'DOWN':<5} "
                      f"{server['tools']} tools  restarts={server['restarts']}")
        print(f"limits: rounds={config.limits.rounds} per_call={config.limits.per_call_seconds}s "
              f"turn={config.limits.turn_seconds}s result_chars={config.limits.result_chars}")
        for note in warnings:
            print(f"warn: {note}")
        for note in problems:
            print(f"fail: {note}")
        print("doctor: " + ("FAIL" if problems else "ok"))
        return 1 if problems else 0
    finally:
        registry.close()


def cmd_index(arguments) -> int:
    config = _config(arguments.config)
    if not config.retrieval.enabled:
        print("retrieval.enabled is false in the config; refusing to guess a corpus", file=sys.stderr)
        return 2
    if arguments.index_action == "build":
        report = retrieval.build(config.retrieval.sources, config.retrieval.index)
        print(f"indexed {report['documents']} documents into {report['chunks']} chunks "
              f"in {report['seconds']}s at {report['index']}")
        return 0
    report = retrieval.status(config.retrieval.sources, config.retrieval.index,
                              config.retrieval.stale_after_seconds)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("exists") else 1


def cmd_tools(arguments) -> int:
    config = _config(arguments.config)
    registry = _registry(config, connect=bool(arguments.probe))
    try:
        for spec in registry.specs():
            function = spec["function"]
            print(f"{function['name']}\t{registry.tools[function['name']].source}\t{function['description'][:70]}")
            if arguments.verbose:
                print(json.dumps(function["parameters"], indent=2)[:1200])
        return 0
    finally:
        registry.close()


def cmd_mcp(arguments) -> int:
    config = _config(arguments.config)
    if not config.mcp:
        print("no [[mcp.server]] entries")
        return 0
    session = __import__("mcp_client").MCPSession.from_config(config.mcp)
    tools, failures = session.connect()
    for tool in tools:
        print(f"{tool.tool_name}\t{tool.server}\t{tool.description[:60]}")
    for failure in failures:
        print(f"DOWN\t{failure['server']}\t{failure['error']}")
    session.close()
    return 1 if failures and not tools else 0


def main() -> int:
    # --config is accepted either side of the subcommand, because both
    # `voicectl --config x doctor` and `voicectl doctor --config x` are typed
    # from memory and only one of them is what argparse would do by default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="assistant.toml (or set VOICE_TOOLS_CONFIG)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=None,
                        help="assistant.toml (or set VOICE_TOOLS_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", parents=[common], help="what the model will be able to do, and what is broken")
    doctor.add_argument("--probe", action="store_true", help="actually start the MCP servers")
    doctor.set_defaults(handler=cmd_doctor)
    index = commands.add_parser("index", parents=[common], help="the RAG corpus")
    index.add_argument("index_action", choices=["build", "status"])
    index.set_defaults(handler=cmd_index)
    tools = commands.add_parser("tools", parents=[common], help="list the tools the model will be offered")
    tools.add_argument("--probe", action="store_true", help="connect to MCP servers first")
    tools.add_argument("--verbose", action="store_true")
    tools.set_defaults(handler=cmd_tools)
    mcp = commands.add_parser("mcp", parents=[common], help="connect to every configured MCP server and list its tools")
    mcp.set_defaults(handler=cmd_mcp)
    arguments = parser.parse_args()
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
