#!/usr/bin/env python3
"""Exercise the deployed bridge using resident services; no GPU imports or launches.

Run through the host's CPU guard. Creates one uniquely named scratch directory
under /mnt/voice-workspace and preserves it for independent inspection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import shlex
import ssl
import sys
import time
from urllib.parse import urlsplit
import uuid


EXPECTED = {
    "mcp__workspace__" + name
    for name in ("read_file", "write_file", "make_directory", "list_directory", "run_shell")
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://192.168.228.113:8094",
                        help="Deployed HTTPS bridge origin (self-signed TLS accepted).")
    parser.add_argument("--output", type=Path, required=True, help="JSON evidence file.")
    parser.add_argument("--timeout", type=float, default=180,
                        help="Timeout per HTTP operation, at most 180 seconds.")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        parser.error("--url must be an HTTPS origin without credentials, path, or query")
    if not 0 < args.timeout <= 180:
        parser.error("--timeout must be greater than zero and at most 180")

    run = "voice-acceptance-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    directory = "/mnt/voice-workspace/" + run
    filename = directory + "/note.txt"
    content = "Direct workspace acceptance " + run
    shell_marker = "SHELL_OK_" + uuid.uuid4().hex
    evidence = {"url": args.url, "run": run, "directory": directory,
                "file": filename, "expected_content": content, "requests": [], "passed": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "verification_scope": "HTTPS tool metadata, assistant readback, and requested shell byte comparison"}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    def request(path, body=None):
        # These deployments use a private self-signed bridge certificate.
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=args.timeout,
            context=ssl._create_unverified_context())
        record = {"path": path, "request": body}
        evidence["requests"].append(record)
        started = time.monotonic()
        try:
            connection.request("GET" if body is None else "POST", path,
                               None if body is None else json.dumps(body),
                               {"Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            record["status"] = response.status
            raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, "HTTP response exceeded 4 MiB")
            result = json.loads(raw)
            record["response"] = result
            require(response.status == 200, f"{path}: HTTP {response.status}: {result}")
            return result
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            connection.close()
            save()

    def chat(prompt, required_tools):
        print("Checking " + ", ".join(sorted(required_tools)), flush=True)
        result = request("/chat/completions", {"messages": [{"role": "user", "content": prompt}]})
        rows = result.get("tools", [])
        require(rows, "Assistant returned no tool evidence")
        require(all(row.get("source") == "mcp:workspace" for row in rows),
                f"Basic task delegated or used unexpected tool source: {rows}")
        require(all(row.get("ok") is True for row in rows), f"Tool failure: {rows}")
        names = {row.get("name") for row in rows}
        require(required_tools <= names, f"Missing required calls: {required_tools - names}")
        return result

    try:
        health = request("/chat/health")
        require(health.get("available") is True, "Chat is unavailable")
        require(EXPECTED <= set(health.get("tools", [])), "Five workspace tools are not registered")
        require(all(health.get("tool_sources", {}).get(name) == "mcp:workspace" for name in EXPECTED),
                "Workspace tool source mismatch")
        servers = [server for server in health.get("mcp", []) if server.get("name") == "workspace"]
        require(len(servers) == 1 and servers[0].get("running") is True
                and servers[0].get("tools") == 5 and not servers[0].get("error"),
                f"Workspace MCP is not healthy: {servers}")

        chat(f"Create the directory {directory} and write a file named note.txt inside it. "
             f"The file must contain exactly this text, with no trailing newline: {content}",
             {"mcp__workspace__make_directory", "mcp__workspace__write_file"})

        readback = chat(f"List the directory {directory}, then read its note.txt file. "
                        "Reply with the filename and the exact file content you read.",
                        {"mcp__workspace__list_directory", "mcp__workspace__read_file"})
        require(content in readback.get("text", "") and "note.txt" in readback.get("text", ""),
                "Readback did not contain the expected filename and exact text")

        command = ("printf %s " + shlex.quote(content) + " | cmp - " + shlex.quote(filename)
                   + " && printf '%s\\n' " + shlex.quote(shell_marker))
        shell = chat("Run this short shell command exactly as written, then report its output: " + command,
                     {"mcp__workspace__run_shell"})
        require(shell_marker in shell.get("text", ""), "Shell comparison success marker missing")
        evidence["passed"] = True
    except Exception as error:
        evidence["error"] = f"{type(error).__name__}: {error}"
        print(evidence["error"], file=sys.stderr)
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()

    print(("PASS" if evidence["passed"] else "FAIL") + f": evidence {args.output}; scratch {directory}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
