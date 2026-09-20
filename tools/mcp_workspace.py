#!/usr/bin/env python3
"""Direct local file and shell tools over line-delimited stdio MCP.

The explicit working directory is a default, not a security boundary. This
server runs with its OS user's permissions, accepts absolute paths, and can
execute arbitrary shell commands. File/output limits bound individual calls.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

PROTOCOL = "2025-03-26"
SERVER_INFO = {"name": "local-workspace", "version": "1.0"}
MAX_TIMEOUT = 8
MAX_CAP = 16 * 1024 * 1024
ACTIVE_PROCESSES = set()
PROCESS_LOCK = threading.Lock()
OUTPUT_LOCK = threading.Lock()
SHUTTING_DOWN = False


class ToolError(Exception):
    """Invalid arguments or an operation that could not be completed."""


PATH = {"type": "string", "minLength": 1, "maxLength": 4096,
        "description": "Absolute path, ~ path, or path relative to the configured working directory."}


def _tool(name, description, properties, required=(), read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only,
                            "destructiveHint": name in ("write_file", "run_shell"),
                            "openWorldHint": True}}


TOOLS = [
    _tool("read_file", "Read a local UTF-8 text file, with a 1-based line offset. "
          "Reads are byte capped; byte_capped means the file exceeded the read budget.",
          {"path": PATH, "offset": {"type": "integer", "minimum": 1, "default": 1},
           "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200}},
          ("path",), read_only=True),
    _tool("write_file", "Create or overwrite a local UTF-8 file; append=true adds to it. "
          "Parent directories must exist. Runs with the server user's permissions.",
          {"path": PATH, "content": {"type": "string"},
           "append": {"type": "boolean", "default": False}}, ("path", "content")),
    _tool("make_directory", "Create a local directory; parents=true creates missing ancestors. "
          "An existing directory succeeds.",
          {"path": PATH, "parents": {"type": "boolean", "default": True}}, ("path",)),
    _tool("list_directory", "List a local directory, including hidden entries, with bounded output.",
          {"path": dict(PATH, default=".")}, read_only=True),
    _tool("run_shell", "Run a short shell command directly on this host using /bin/bash. "
          "Runs with the server user's full permissions; cwd is not a sandbox. "
          "No interactive input. Output is capped and the process group is killed at timeout. "
          "Use a coding agent for long builds or complex multi-step coding work.",
          {"command": {"type": "string", "minLength": 1, "maxLength": 65536},
           "cwd": PATH,
           "timeout_seconds": {"type": "number", "exclusiveMinimum": 0,
                               "maximum": MAX_TIMEOUT, "default": 5}}, ("command",)),
]
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def validate(name, arguments):
    if not isinstance(name, str) or name not in SCHEMAS:
        raise ToolError("unknown tool")
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    schema = SCHEMAS[name]
    if set(arguments) - set(schema["properties"]):
        raise ToolError("unknown argument(s): " + ", ".join(sorted(set(arguments) - set(schema["properties"]))))
    for key in schema["required"]:
        if key not in arguments:
            raise ToolError(f"missing required argument: {key}")
    for key, value in arguments.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        valid = ((kind == "string" and isinstance(value, str))
                 or (kind == "boolean" and type(value) is bool)
                 or (kind == "integer" and type(value) is int)
                 or (kind == "number" and type(value) in (int, float)))
        if not valid:
            raise ToolError(f"{key} must be {kind}")
        if kind == "string":
            if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", MAX_CAP):
                raise ToolError(f"{key} has an invalid length")
            if key != "content" and "\0" in value:
                raise ToolError(f"{key} must not contain NUL")
        elif kind in ("integer", "number"):
            if kind == "number" and (not math.isfinite(value) or value <= 0):
                raise ToolError(f"{key} must be a finite positive number")
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise ToolError(f"{key} is outside the allowed bounds")


def fit(payload, cap):
    """Trim fields, never serialized JSON; preserve shell status and diagnostics."""
    result = dict(payload)
    dump = lambda: json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    text = dump()
    if len(text) <= cap:
        return text
    result["truncated"] = True
    if "entries" in result:
        result["entries"] = list(result["entries"])
        while result["entries"] and len(dump()) > cap:
            result["entries"].pop()
    while len(dump()) > cap:
        strings = [key for key, value in result.items() if isinstance(value, str) and value]
        if not strings:
            return json.dumps({"error": "result exceeds output cap", "truncated": True})
        key = max(strings, key=lambda field: len(result[field]))
        value = result[key]
        result[key] = value[:len(value) // 2]
    return dump()


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_shells():
    """Cancel active commands and refuse commands racing with transport loss."""
    global SHUTTING_DOWN
    with PROCESS_LOCK:
        SHUTTING_DOWN = True
        for process in ACTIVE_PROCESSES:
            kill_group(process)


def shutdown(signum, _frame):
    stop_shells()
    raise SystemExit(128 + signum)


class Workspace:
    def __init__(self, cwd, max_output_chars=4000, max_read_bytes=65536, max_write_bytes=65536):
        self.cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(self.cwd):
            raise ToolError("--cwd must be an existing directory")
        self.max_output_chars = max_output_chars
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes

    def path(self, value):
        return os.path.abspath(os.path.join(self.cwd, os.path.expanduser(value)))

    @staticmethod
    def regular_fd(path, writing=False, append=False):
        # Never use O_TRUNC before checking the opened descriptor. O_NONBLOCK
        # also prevents a raced-in FIFO from hanging this sequential server.
        try:
            info = os.stat(path)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise ToolError("path is not a regular file")
        flags = os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        flags |= (os.O_WRONLY | os.O_CREAT) if writing else os.O_RDONLY
        if append:
            flags |= os.O_APPEND
        fd = os.open(path, flags, 0o666)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ToolError("path is not a regular file")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read_file(self, arguments):
        path = self.path(arguments["path"])
        offset, limit = arguments.get("offset", 1), arguments.get("limit", 200)
        fd = self.regular_fd(path)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(self.max_read_bytes + 1)
        byte_capped = len(raw) > self.max_read_bytes
        raw = raw[:self.max_read_bytes]
        if b"\0" in raw:
            raise ToolError("binary file: read_file accepts text files")
        lines = raw.decode("utf-8", "replace").splitlines(keepends=True)
        window = lines[offset - 1:offset - 1 + limit]
        return {"path": path, "offset": offset, "lines_returned": len(window),
                "text": "".join(window), "byte_capped": byte_capped,
                "truncated": byte_capped or offset - 1 + len(window) < len(lines)}

    def write_file(self, arguments):
        path, append = self.path(arguments["path"]), arguments.get("append", False)
        content = arguments["content"].encode("utf-8")
        if len(content) > self.max_write_bytes:
            raise ToolError(f"content exceeds max_write_bytes ({self.max_write_bytes})")
        fd = self.regular_fd(path, writing=True, append=append)
        try:
            if not append:
                os.ftruncate(fd, 0)
            written = 0
            while written < len(content):
                count = os.write(fd, content[written:])
                if count == 0:
                    raise ToolError("write made no progress")
                written += count
        finally:
            os.close(fd)
        return {"path": path, "bytes_written": written, "append": append}

    def make_directory(self, arguments):
        path = self.path(arguments["path"])
        created = not os.path.isdir(path)
        if arguments.get("parents", True):
            os.makedirs(path, exist_ok=True)
        else:
            try:
                os.mkdir(path)
            except FileExistsError:
                if not os.path.isdir(path):
                    raise
        return {"path": path, "created": created}

    def list_directory(self, arguments):
        path = self.path(arguments.get("path", "."))
        entries, used, truncated = [], 0, False
        with os.scandir(path) as iterator:
            for entry in iterator:
                if used >= self.max_output_chars:
                    truncated = True
                    break
                kind = ("symlink" if entry.is_symlink() else "directory"
                        if entry.is_dir(follow_symlinks=False) else "file"
                        if entry.is_file(follow_symlinks=False) else "other")
                entries.append({"name": entry.name, "type": kind})
                used += len(entry.name) + 40
        entries.sort(key=lambda entry: entry["name"])
        return {"path": path, "entries": entries, "truncated": truncated}

    def run_shell(self, arguments):
        cwd = self.path(arguments.get("cwd", self.cwd))
        if not os.path.isdir(cwd):
            raise ToolError("cwd must be an existing directory")
        timeout = arguments.get("timeout_seconds", 5)
        retained = {"stdout": bytearray(), "stderr": bytearray()}
        truncated, timed_out = False, False
        with PROCESS_LOCK:
            if SHUTTING_DOWN:
                raise ToolError("server is shutting down")
            process = subprocess.Popen(["/bin/bash", "-c", arguments["command"]], cwd=cwd,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            ACTIVE_PROCESSES.add(process)
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for name in retained:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        kill_group(process)
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = retained[key.data]
                        room = max(0, self.max_output_chars - len(buffer))
                        buffer.extend(chunk[:room])
                        truncated |= len(chunk) > room
                # A background job must not survive a completed tool call either.
                kill_group(process)
                process.wait(timeout=1)
                # Drain only bytes already buffered after exit; never wait for EOF
                # from a descendant that deliberately escaped the process group.
                for key in list(selector.get_map().values()):
                    for _ in range(4):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        buffer = retained[key.data]
                        room = max(0, self.max_output_chars - len(buffer))
                        buffer.extend(chunk[:room])
                        truncated |= len(chunk) > room
        finally:
            kill_group(process)
            process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()
            with PROCESS_LOCK:
                ACTIVE_PROCESSES.discard(process)
        return {"cwd": cwd, "stdout": retained["stdout"].decode("utf-8", "replace"),
                "stderr": retained["stderr"].decode("utf-8", "replace"),
                "exit_code": process.returncode, "timed_out": timed_out,
                "truncated": truncated}

    def call(self, name, arguments):
        validate(name, arguments)
        payload = getattr(self, name)(arguments)
        failed = name == "run_shell" and (payload["timed_out"] or payload["exit_code"] != 0)
        return {"content": [{"type": "text", "text": fit(payload, self.max_output_chars)}],
                "isError": bool(failed)}


def write(message):
    with OUTPUT_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def error_result(workspace, message):
    return {"content": [{"type": "text", "text": fit({"error": message}, workspace.max_output_chars)}],
            "isError": True}


def serve(workspace):
    # Two simultaneous conversations may call tools. Never queue a mutation
    # behind another call: its client might time out before it starts running.
    slots = threading.BoundedSemaphore(2)
    workers = set()
    worker_lock = threading.Lock()

    def invoke(identifier, params):
        try:
            try:
                if not isinstance(params, dict):
                    raise ToolError("params must be an object")
                result = workspace.call(params.get("name"), params.get("arguments", {}))
            except (ToolError, OSError, ValueError, TypeError, OverflowError,
                    subprocess.SubprocessError) as error:
                result = error_result(workspace, str(error))
            write({"jsonrpc": "2.0", "id": identifier, "result": result})
        finally:
            with worker_lock:
                workers.discard(threading.current_thread())
            slots.release()

    for raw in sys.stdin:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") is None:
            continue
        identifier, method = message["id"], message.get("method")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            if not slots.acquire(blocking=False):
                result = error_result(workspace, "workspace is busy: two tool calls are already running; retry later")
            else:
                worker = threading.Thread(target=invoke, args=(identifier, message.get("params", {})))
                with worker_lock:
                    workers.add(worker)
                worker.start()
                continue
        else:
            write({"jsonrpc": "2.0", "id": identifier,
                   "error": {"code": -32601, "message": "unknown method"}})
            continue
        write({"jsonrpc": "2.0", "id": identifier, "result": result})
    # SSH disconnects can deliver stdin EOF without a signal. Cancel shells
    # before waiting, while allowing accepted ordinary file calls to finish.
    stop_shells()
    with worker_lock:
        pending = list(workers)
    for worker in pending:
        worker.join()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True, help="Existing default working directory; not a sandbox.")
    parser.add_argument("--max-output-chars", type=int, default=4000)
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--max-write-bytes", type=int, default=65536)
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args(argv)
    for key, minimum in (("max_output_chars", 256), ("max_read_bytes", 1), ("max_write_bytes", 1)):
        if not minimum <= getattr(args, key) <= MAX_CAP:
            parser.error(f"--{key.replace('_', '-')} must be between {minimum} and {MAX_CAP}")
    try:
        workspace = Workspace(args.cwd, args.max_output_chars, args.max_read_bytes, args.max_write_bytes)
    except (ToolError, OSError, ValueError) as error:
        parser.error(str(error))
    if args.doctor:
        print(json.dumps({"server": SERVER_INFO, "cwd": workspace.cwd,
                          "authority": "Runs as the current OS user. cwd is not a sandbox; "
                                       "absolute paths and arbitrary shell commands are allowed.",
                          "caps": {"max_output_chars": args.max_output_chars,
                                   "max_read_bytes": args.max_read_bytes,
                                   "max_write_bytes": args.max_write_bytes,
                                   "max_timeout_seconds": MAX_TIMEOUT}}, indent=2))
        return 0
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    return serve(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
