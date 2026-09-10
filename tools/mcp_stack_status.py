#!/usr/bin/env python3
"""A real MCP server for the deployment: read-only health of the voice stack.

"MCP is wired up" should mean a second process actually speaking the protocol
answers the model, not that a client exists in principle.  This one is
stdlib-only, read-only, and talks to localhost -- it is the thing the demo
calls, so a broken MCP path shows up here rather than in a fixture.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
from urllib.parse import urlsplit

ENDPOINTS = {
    "tts": ("http://127.0.0.1:8090/health", "Kokoro TTS"),
    "llm": ("http://127.0.0.1:8080/health", "Qwen3.8-27B"),
    "stt": ("http://127.0.0.1:8095/health", "Qwen3-ASR (resident)"),
    "bridge": ("http://127.0.0.1:8092/chat/health", "speech bridge"),
    "tools_bridge": ("http://127.0.0.1:8093/chat/health", "tools bridge"),
}
TOOLS = [{
    "name": "health",
    "description": ("Ask the local voice stack which of its own services are up, and what they "
                    "report about themselves (device, busy flag, limits). Read-only."),
    "inputSchema": {"type": "object", "properties": {
        "service": {"type": "string", "enum": sorted(list(ENDPOINTS) + ["all"])}}},
}]


def probe(name: str) -> dict:
    url, label = ENDPOINTS[name]
    target = urlsplit(url)
    try:
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=4)
        connection.request("GET", target.path)
        response = connection.getresponse()
        body = response.read(65536)
        connection.close()
        try:
            detail = json.loads(body)
        except ValueError:
            detail = {"raw": body.decode("utf-8", "replace")[:200]}
        return {"service": name, "label": label, "status": response.status, "reports": detail}
    except OSError as error:
        return {"service": name, "label": label, "status": "unreachable", "error": str(error)}


def answer(identifier, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        method, identifier = message.get("method"), message.get("id")
        if identifier is None:                       # notifications get no answer
            continue
        if method == "initialize":
            answer(identifier, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                "serverInfo": {"name": "voice-stack-status", "version": "1.0"}})
        elif method == "tools/list":
            answer(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            wanted = ((message.get("params") or {}).get("arguments") or {}).get("service", "all")
            names = list(ENDPOINTS) if wanted in ("all", None) else [wanted]
            rows = [probe(name) for name in names if name in ENDPOINTS]
            down = [row["service"] for row in rows if row.get("status") != 200]
            text = json.dumps(rows, indent=1)
            answer(identifier, {"content": [{"type": "text", "text": text}],
                                "isError": bool(down) and len(down) == len(rows)})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                         "error": {"code": -32601, "message": f"unknown {method}"}}) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
