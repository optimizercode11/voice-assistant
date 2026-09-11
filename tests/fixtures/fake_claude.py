#!/usr/bin/env python3
"""A stand-in for the `claude` binary that speaks its stream-json protocol.

The server under test drives a child through `-p --input-format stream-json
--output-format stream-json`, so the honest fixture is a process that reads
user messages on stdin and writes the same event shapes the real CLI does
(observed 2026-09-11, Claude Code 2.1.268): a `system`/`init`, `assistant`
messages with text and tool_use blocks, and a `result` per turn.

Behaviour is scripted by the instruction text itself, so one fixture serves
every test:
  "slow N"     work for N seconds before answering (a turn in progress)
  "code"       answer with a Markdown report full of code, plus a SPOKEN line
  "nospoken"   answer in Markdown WITHOUT a SPOKEN line (fallback path)
  "quote"      mention the SPOKEN mark mid-text, then a real one at the end
  "die"        exit mid-turn with code 3
  "error"      a result with is_error
  anything else: a short echo answer with a SPOKEN line
"""
import json
import os
import sys
import time

SESSION = "fake-session-0001"


def emit(payload: dict) -> None:
    payload.setdefault("session_id", SESSION)
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def tool_use(name: str, **kwargs) -> None:
    emit({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": kwargs}]}})
    emit({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_{name}", "content": "ok"}]}})


def result(text: str, is_error: bool = False, ms: int = 1200) -> None:
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "error_during_execution" if is_error else "success",
          "is_error": is_error, "duration_ms": ms, "duration_api_ms": ms, "num_turns": 1,
          "result": text, "stop_reason": "end_turn", "permission_denials": []})


CODE_REPORT = """I looked at the bridge and changed two files.

```python
def turn(url, body):
    return payload(body)['messages']  # the fix
```

- `tools/voice_chat.py:312` now passes the manifest
- ran `make test-chat` -> 14 passed

SPOKEN: I fixed the manifest bug in the chat module and the tests pass. Nothing else changed."""

NOSPOKEN_REPORT = """## Findings

1. **The bridge** clips at `6000` chars (see `tools/agent_config.py`).
2. Run `make test` to confirm.

```sh
make test
```
"""

QUOTE_REPORT = """Your instructions say to end with a SPOKEN: line, so here is what I did.

Nothing was changed.

SPOKEN: I read the instructions back to you and changed nothing."""


def main() -> int:
    if "--version" in sys.argv:
        print("0.0.0 (fake claude)")
        return 0
    assert "--input-format" in sys.argv and "stream-json" in sys.argv, "fixture expects the stream-json argv"
    assert "--append-system-prompt" in sys.argv, "the spoken-line appendix must be passed"
    assert "CLAUDECODE" not in os.environ, "a nested launch must not inherit CLAUDECODE"
    if os.environ.get("FAKE_CLAUDE_STDOUT_NOISE"):
        # Sabotage aid: a non-JSON line on stdout, which the server must ignore.
        sys.stdout.write("Loading...\n")
        sys.stdout.flush()
    emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "tools": ["Bash", "Edit"], "model": "fake"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        message = json.loads(raw)
        if message.get("type") != "user":
            continue
        content = message["message"]["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        # The frame comes first; the instruction is what follows the blank line.
        instruction = text.split("\n\n", 1)[1] if "\n\n" in text else text
        words = instruction.split()
        head = words[0].lower() if words else ""
        if head == "slow":
            seconds = float(words[1]) if len(words) > 1 else 2.0
            tool_use("Bash", command="sleep")
            time.sleep(seconds)
            tool_use("Edit", file_path="x.py")
            result(f"Slept {seconds:g}s.\n\nSPOKEN: I waited as asked and edited one file.")
        elif head == "code":
            tool_use("Read", file_path="tools/voice_chat.py")
            tool_use("Edit", file_path="tools/voice_chat.py")
            tool_use("Bash", command="make test-chat")
            result(CODE_REPORT)
        elif head == "nospoken":
            result(NOSPOKEN_REPORT)
        elif head == "quote":
            result(QUOTE_REPORT)
        elif head == "die":
            tool_use("Bash", command="exit 3")
            sys.stdout.flush()
            os._exit(3)
        elif head == "error":
            result("", is_error=True)
        else:
            result(f"Echo: {instruction}\n\nSPOKEN: You said {instruction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
