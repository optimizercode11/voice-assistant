#!/usr/bin/env python3
"""MCP tools for named, persistent Codex App Server sessions."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tomllib

import mcp_claude as wire
import mcp_codex as legacy
from codex_app_server import AppServerError
from codex_sessions import Manager, SessionError


def tool(name, description, properties=None, required=(), read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": {
                **(properties or {}), "context_id": {"type": "string", "maxLength": 80,
                    "description": "Conversation routing key supplied by the voice bridge; omit it."}},
                "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only}}


SESSION = {"type": "string", "maxLength": 120, "description": "Exact session name or ID. Omit to use this conversation's selected session."}
INSTRUCTION = {"type": "string", "minLength": 1, "maxLength": 4000, "description": "The user's instruction, preserving its meaning and details."}
TOOLS = [
    tool("create_session", "Create and select a named Codex coding session. This does not start work; use send afterward. Existing projects require their actual directory; omit cwd for an isolated workspace directory.",
         {"name": {"type": "string", "minLength": 1, "maxLength": 60}, "cwd": {"type": "string", "maxLength": 2000}}, ["name"]),
    tool("list_sessions", "List managed Codex sessions and their actual states, names, IDs and the selected session. Use for 'who is working' or 'who needs me'; never invent session status.",
         {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 32}}, read_only=True),
    tool("select_session", "Select a Codex session for subsequent voice instructions in this conversation. Does not start, stop or alter work.", {"session": SESSION}, ["session"]),
    tool("send", "Start a new task or follow-up in the named or selected Codex session. Returns acceptance, not completion. If busy, use steer for an instruction during the current task. No session selected means ask which one; ordinary file/shell tasks use workspace tools.",
         {"session": SESSION, "instruction": INSTRUCTION}, ["instruction"]),
    tool("status", "Read the actual state and pending questions of a named or selected Codex session.", {"session": SESSION}, read_only=True),
    tool("steer", "Add an instruction to this Codex session's currently running task without starting another task. Use for 'tell Backend to also...' while Backend is working.",
         {"session": SESSION, "instruction": INSTRUCTION}, ["instruction"]),
    tool("interrupt", "Cancel a named or selected Codex task. Returns cancellation requested until Codex confirms interruption. This does not mute speech or pause the microphone; 'stop talking' is not this tool.", {"session": SESSION}),
    tool("answer", "Answer a specific pending Codex question. First read status for its request_id and question IDs. Pass exact session and request_id so another session's question cannot receive this answer.",
         {"session": SESSION, "request_id": {"type": "string", "maxLength": 80},
          "answers": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 2000}}}, ["session", "request_id", "answers"]),
    tool("updates", "Read new completion reports from a named or selected session. Spoken summaries by default; detail=true includes the technical report. Does not start work.",
         {"session": SESSION, "detail": {"type": "boolean"}}),
    tool("archive_session", "Archive a finished Codex session and remove it from the voice session list. Keeps project files and Codex history. Running sessions must first be interrupted.", {"session": SESSION}, ["session"]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex")
    parser.add_argument("--profile", default=legacy.DEFAULT_PROFILE)
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home()/".codex"))
    parser.add_argument("--cwd", default="/mnt/voice-workspace")
    parser.add_argument("--state-dir", default="/mnt/voice-workspace/.codex-sessions")
    parser.add_argument("--max-active", type=int, default=3)
    parser.add_argument("--access", choices=["full", "workspace"], default="full")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args()
    codex = legacy.resolve_codex(args.codex)
    profile_file = Path(legacy.profile_path(os.path.expanduser(args.codex_home), args.profile))
    config = tomllib.loads(profile_file.read_text())
    model, provider = config.get("model"), config.get("model_provider")
    if not codex or not isinstance(model, str) or not isinstance(provider, str):
        raise SessionError("Codex and an explicit model/provider profile are required; no default-model fallback.")
    argv = [codex, "app-server", "--listen", "stdio://"]
    for value in legacy.profile_overrides(str(profile_file.parent), args.profile):
        argv += ["-c", value]
    argv += ["-c", "features.multi_agent=false", "-c", "agents.max_threads=1"]
    if args.doctor:
        print(json.dumps({"codex": codex, "profile": args.profile, "model": model, "model_provider": provider,
                          "cwd": str(Path(args.cwd).resolve()), "state_dir": str(Path(args.state_dir).resolve()),
                          "max_active": args.max_active, "tools": [t["name"] for t in TOOLS]}, indent=2))
        return 0 if Path(args.cwd).is_dir() and 1 <= args.max_active <= 3 else 1
    def notify(method, params):
        wire._write({"jsonrpc": "2.0", "method": method, "params": params})
    manager = Manager(args.state_dir, args.cwd, argv, model=model, provider=provider, profile=args.profile,
                      access=args.access, max_active=args.max_active, notify=notify if args.push else None)
    schemas = {t["name"]: t["inputSchema"] for t in TOOLS}
    # Share the actual registry validator so direct MCP peers get the same
    # argument validation as the model-facing bridge.
    from agent_tools import validate
    def stop(*_):
        manager.close()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for raw in sys.stdin:
            if len(raw) > 64000:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") is None:
                continue
            identifier, method = message["id"], message.get("method")
            if method == "initialize":
                wire._reply(identifier, {"protocolVersion": wire.PROTOCOL, "capabilities": {"tools": {}},
                                         "serverInfo": {"name": "voice-codex-sessions", "version": "1.0"}})
            elif method == "tools/list":
                wire._reply(identifier, {"tools": TOOLS})
            elif method == "tools/call":
                try:
                    params = message.get("params") or {}
                    if not isinstance(params, dict):
                        raise SessionError("Tool call params must be an object.")
                    name, arguments = params.get("name"), params.get("arguments", {})
                    if name not in schemas:
                        raise SessionError("Unknown Codex session tool.")
                    problems = validate(schemas[name], arguments)
                    if problems:
                        raise SessionError("; ".join(problems[:3]))
                    result = getattr(manager, name)(**arguments)
                    wire._reply(identifier, wire._content(result))
                except (SessionError, AppServerError, OSError, TypeError, ValueError) as error:
                    wire._reply(identifier, wire._content({"error": str(error)[:1200]}, True))
            else:
                wire._write({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Unknown method"}})
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"codex sessions: {error}", file=sys.stderr)
        raise SystemExit(1)
