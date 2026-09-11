#!/usr/bin/env python3
"""A Model Context Protocol client over stdio, with no third-party package.

The `mcp` pip package is not installed on either machine and `/mnt` has 5 GB
free, so this is the protocol directly: newline-delimited JSON-RPC 2.0 over a
child's stdin/stdout, which is exactly what MCP's stdio transport is.

What is deliberately *not* trusted
    An MCP server is someone else's program that the assistant is about to
    let name its own tools.  So: the tool list is captured once at startup and
    never re-read mid-turn (a server cannot rename a tool between the model
    choosing it and the bridge running it), every tool carries the server it
    came from, allow/deny lists are applied here rather than in the prompt, and
    a wedged server is SIGTERM'd and restarted a bounded number of times.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "voice-assistant", "version": "1.0"}
MAX_LINE = 4 * 1024 * 1024          # one JSON-RPC message
MAX_RESTARTS = 2


class MCPError(Exception):
    """Any protocol, transport or lifecycle failure. Never a silent empty result."""


@dataclass
class MCPTool:
    server: str
    name: str                       # wire name, as the server listed it
    tool_name: str                  # mcp__<server>__<name>, what the model sees
    description: str
    input_schema: dict = field(default_factory=dict)


def _text_of(result) -> tuple[str, bool]:
    """Flatten a tools/call result to text; MCP content is a list of blocks."""
    if not isinstance(result, dict):
        raise MCPError("tools/call returned a non-object result")
    blocks = result.get("content")
    parts, saw_non_text = [], False
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict):
                saw_non_text = True
                parts.append(f"[{block.get('type', 'unknown')} content the voice assistant cannot speak]")
    if result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:8000])
    if not parts:
        parts.append("(empty result)" if not saw_non_text else "")
    return "\n".join(parts).strip(), bool(result.get("isError"))


class MCPServer:
    """One child process, one request-id space, one reader thread."""

    def __init__(self, name: str, command: str, args: list[str] = (), env: dict[str, str] = (),
                 timeout: float = 15.0, allow: list[str] = (), deny: list[str] = ()):
        self.name = name
        self.command = command
        self.args = list(args)
        self.env = dict(env)
        self.timeout = float(timeout)
        self.allow = list(allow)
        self.deny = list(deny)
        self.ids = itertools.count(1)
        self.pending: dict[int, dict] = {}
        self.lock = threading.Lock()          # guards spawn + write
        self.process: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.tools: list[MCPTool] = []
        self.server_info: dict = {}
        self.restarts = 0
        self.state = "stopped"
        self.last_error = ""
        self.stderr_tail = ""
        # A server may speak first: a JSON-RPC message with a `method` and no
        # `id` is a notification.  Whoever subscribes gets (server, method,
        # params) on the reader thread; an unsubscribed notification is dropped.
        self.on_notification = None

    # -- lifecycle ---------------------------------------------------------
    def _spawn(self) -> None:
        child_env = {key: value for key, value in os.environ.items()
                     if key in {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "NODE_PATH", "VIRTUAL_ENV"}}
        child_env.update(self.env)
        try:
            self.process = subprocess.Popen([self.command, *self.args], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            env=child_env, text=True, encoding="utf-8", bufsize=1,
                                            start_new_session=True)
        except OSError as error:
            raise MCPError(f"cannot start mcp server {self.name}: {error}") from error
        self.reader = threading.Thread(target=self._read_loop, daemon=True, name=f"mcp-{self.name}")
        self.reader.start()
        self.state = "starting"

    def start(self) -> list[MCPTool]:
        with self.lock:
            if self.process is not None and self.process.poll() is None and self.tools:
                return self.tools
            self._fail_pending("mcp server restarted")
            previous, self.reader, self.process = self.process, None, None
            if previous is not None:
                if previous.poll() is None:
                    previous.terminate()
                self._reap(previous)
            self._spawn()
        try:
            result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                 "clientInfo": CLIENT_INFO}, timeout=self.timeout)
            self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
            self.notify("notifications/initialized")
            listed = self.request("tools/list", {}, timeout=self.timeout)
        except MCPError as error:
            self.stop()
            raise
        captured: list[MCPTool] = []
        for row in (listed.get("tools") or []) if isinstance(listed, dict) else []:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
                continue
            # The engine's tool grammar is [A-Za-z0-9_-]; a server that lists
            # "window.title" must not be able to smuggle a name the parser rejects.
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", row["name"].replace("__", "_"))
            visible = f"mcp__{self.name}__{safe}"[:64]
            if self.allow and row["name"] not in self.allow:
                continue
            if row["name"] in self.deny:
                continue
            schema = row.get("inputSchema")
            captured.append(MCPTool(self.name, row["name"], visible,
                                    str(row.get("description") or "")[:1000],
                                    schema if isinstance(schema, dict) else {"type": "object"}))
        # Captured once, here.  Mid-turn re-listing is the hole this closes.
        self.tools = captured
        self.state = "ready"
        return captured

    def _reap(self, process: subprocess.Popen) -> None:
        """Close a child's pipes.  A dead-but-unclosed Popen leaks three fds a restart.

        This Python's Popen has no close(), so the streams are closed by hand.
        The reader is joined first: closing a file object out from under a
        blocked readline() is how you get a warning instead of an answer.
        """
        if process is None:
            return
        if self.reader is not None and self.reader.is_alive():
            self.reader.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass
        try:
            process.poll()
        except OSError:
            pass

    def stop(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            self.tools, self.state = [], "stopped"
            self._fail_pending("mcp server stopped")
        if process is None:
            return
        if process.poll() is not None:
            self._reap(process)
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()                     # SIGTERM first, always
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()                      # only after SIGTERM failed
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        except OSError:
            pass
        self._reap(process)

    def _fail_pending(self, reason: str) -> None:
        for entry in list(self.pending.values()):
            entry["error"] = MCPError(reason)
            entry["done"].set()
        self.pending.clear()

    # -- transport ---------------------------------------------------------
    def _read_loop(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                if len(line) > MAX_LINE:
                    self.last_error = "oversized message"
                    return
                try:
                    message = json.loads(line)
                except ValueError:
                    continue                          # foreign chatter on stdout
                if not isinstance(message, dict):
                    continue
                if "id" not in message:               # a notification: nothing to answer
                    callback, method = self.on_notification, message.get("method")
                    if callback is not None and isinstance(method, str):
                        try:
                            callback(self.name, method, message.get("params") or {})
                        except Exception as error:    # a subscriber's bug must not kill the reader
                            self.last_error = f"notification handler failed: {error}"
                    continue
                with self.lock:
                    entry = self.pending.pop(message["id"], None)
                if entry is None:
                    continue
                if "error" in message:
                    detail = message["error"]
                    entry["error"] = MCPError(f"{detail.get('code', '?')}: {detail.get('message', 'rpc error')}"
                                              if isinstance(detail, dict) else "rpc error")
                else:
                    entry["result"] = message.get("result")
                entry["done"].set()
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self._fail_pending(f"mcp server {self.name} closed its output")
                if self.state == "ready":
                    self.state = "exited"

    def _write(self, message: dict) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise MCPError(f"mcp server {self.name} is not running")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as error:
            raise MCPError(f"cannot reach mcp server {self.name}: {error}") from error

    def request(self, method: str, params: dict, timeout: float | None = None):
        if self.process is not None and self.process.poll() is not None:
            raise MCPError(f"mcp server {self.name} exited with code {self.process.returncode}")
        identifier = next(self.ids)
        entry = {"done": threading.Event()}
        self.pending[identifier] = entry
        self._write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        if not entry["done"].wait(timeout or self.timeout):
            self.pending.pop(identifier, None)
            raise MCPError(f"mcp server {self.name} did not answer {method} in {timeout or self.timeout}s")
        if "error" in entry:
            raise entry["error"]
        return entry.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    # -- the two calls the assistant makes ---------------------------------
    def call(self, tool: MCPTool, arguments: dict, timeout: float | None = None) -> tuple[str, bool]:
        if tool.server != self.name:
            raise MCPError(f"{tool.server} is not {self.name}")
        if not any(tool.tool_name == known.tool_name for known in self.tools):
            raise MCPError(f"{tool.tool_name} is not in the tool list captured at startup")
        try:
            result = self.request("tools/call", {"name": tool.name, "arguments": arguments},
                                  timeout=timeout or self.timeout)
        except MCPError:
            if self.process is not None and self.process.poll() is None and self.restarts < MAX_RESTARTS:
                self.restarts += 1
                self.start()
            raise
        return _text_of(result)

    def restart(self) -> list[MCPTool]:
        self.stop()
        return self.start()

    def status(self) -> dict:
        alive = self.process is not None and self.process.poll() is None
        return {"name": self.name, "state": self.state, "running": alive, "tools": len(self.tools),
                "restarts": self.restarts, "command": self.command, "server_info": self.server_info,
                "error": self.last_error,
                "pid": self.process.pid if alive and self.process is not None else None}


class MCPSession:
    """The set of configured servers, with one shutdown path."""

    def __init__(self, servers: list[MCPServer]):
        self.servers = servers
        self.closed = False

    @classmethod
    def from_config(cls, configs, timeout_default: float = 15.0) -> "MCPSession":
        return cls([MCPServer(c.name, c.command, c.args, c.env, c.timeout_seconds or timeout_default,
                              c.allow, c.deny) for c in configs if c.enabled])

    def connect(self) -> tuple[list[MCPTool], list[dict]]:
        """Connect every server; a broken one is reported, not fatal."""
        tools, failures = [], []
        for server in self.servers:
            try:
                started = time.monotonic()
                tools += server.start()
            except MCPError as error:
                failures.append({"server": server.name, "error": str(error),
                                 "seconds": round(time.monotonic() - started, 2)})
        return tools, failures

    def subscribe(self, callback) -> None:
        """Route every server's notifications to `callback(server, method, params)`."""
        for server in self.servers:
            server.on_notification = callback

    def find(self, tool_name: str) -> tuple[MCPServer, MCPTool] | None:
        for server in self.servers:
            for tool in server.tools:
                if tool.tool_name == tool_name:
                    return server, tool
        return None

    def status(self) -> list[dict]:
        return [server.status() for server in self.servers]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for server in self.servers:
            try:
                server.stop()
            except Exception:
                pass


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE for the child")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--call", help="tools/call this tool name")
    parser.add_argument("--arguments", default="{}")
    arguments = parser.parse_args()
    env = dict(pair.split("=", 1) for pair in arguments.env if "=" in pair)
    server = MCPServer("cli", arguments.command, arguments.arg, env, arguments.timeout)
    try:
        tools = server.start()
        print(json.dumps({"server_info": server.server_info,
                          "tools": [{"name": t.name, "as": t.tool_name, "description": t.description}
                                    for t in tools]}, indent=2))
        if arguments.call:
            match = [t for t in tools if t.name == arguments.call or t.tool_name == arguments.call]
            if not match:
                raise SystemExit(f"no such tool: {arguments.call}")
            text, is_error = server.call(match[0], json.loads(arguments.arguments))
            print(json.dumps({"is_error": is_error, "text": text}, indent=2))
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
