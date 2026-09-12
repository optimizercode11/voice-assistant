#!/usr/bin/env python3
"""A stand-in for the `codex` binary that speaks `codex exec --json`.

The server under test runs one child per turn: `codex exec --json ... PROMPT`
for the first instruction and `codex exec resume --json ... THREAD PROMPT` for
every later one, with stdin closed.  This fixture writes the same JSONL event
shapes the real CLI does (observed 2026-09-12, codex-cli 0.154.0):
`thread.started`, `turn.started`, `item.started`/`item.completed` with
`command_execution`, `file_change` and `agent_message` items, and
`turn.completed` or `turn.failed`.

It also refuses the two mistakes the real CLI punishes: `--profile` on argv
(rejected by `exec resume`, so the server must flatten the profile into `-c`
overrides for both forms) and an open stdin (the real CLI blocks forever
reading it).

Behaviour is scripted by the instruction text itself:
  "slow N"     run a command for N seconds, then edit a file, then answer
  "code"       answer with a Markdown report full of code, plus a SPOKEN line
  "nospoken"   answer in Markdown WITHOUT a SPOKEN line (fallback path)
  "quote"      mention the SPOKEN mark mid-text, then a real one at the end
  "die"        exit mid-turn with code 3
  "error"      a turn.failed event, then exit 1
  "thread"     say whether this child was fresh or resumed, and with which id
  anything else: a short echo answer with a SPOKEN line
"""
import json
import os
import sys
import time


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def item(kind: str, identity: str, **fields) -> None:
    emit({"type": "item.started", "item": {"id": identity, "type": kind, "status": "in_progress", **fields}})
    emit({"type": "item.completed", "item": {"id": identity, "type": kind, "status": "completed", **fields}})


def answer(text: str) -> None:
    emit({"type": "item.completed", "item": {"id": "item_msg", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})


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
    argv = sys.argv[1:]
    if "--version" in argv:
        print("codex-cli 0.0.0 (fake codex)")
        return 0
    assert argv and argv[0] == "exec", f"fixture expects `exec`, got {argv[:2]}"
    assert "--json" in argv, "the server must ask for JSONL events"
    assert "--profile" not in argv and "-p" not in argv, "exec resume rejects --profile: flatten it into -c overrides"
    assert "--skip-git-repo-check" in argv, "a voice thread may start outside a repository"
    overrides = [argv[index + 1] for index, flag in enumerate(argv) if flag == "-c"]
    assert any(pair.startswith("model_provider=") for pair in overrides), f"the profile must reach argv: {overrides}"
    assert "CLAUDECODE" not in os.environ, "a nested launch must not inherit CLAUDECODE"
    assert sys.stdin.read() == "", "stdin must be closed: the real CLI blocks forever reading it"
    resume = argv[1] == "resume"
    if resume:
        assert "-C" not in argv and "--add-dir" not in argv, "exec resume accepts neither -C nor --add-dir"
        thread, prompt = argv[-2], argv[-1]
    else:
        assert "-C" in argv, "the first turn names its working directory"
        thread, prompt = f"fake-thread-{os.getpid()}", argv[-1]
    assert prompt.startswith("Spoken instruction"), "the prompt begins with the speech frame"
    assert "SPOKEN:" in prompt, "the spoken-line appendix must be in the prompt"
    if os.environ.get("FAKE_CODEX_STDOUT_NOISE"):
        sys.stdout.write("Loading...\n")
        sys.stdout.flush()
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    body = prompt.split("\n\n", 1)[1] if "\n\n" in prompt else prompt
    instruction = body.split("\n\n---", 1)[0].strip()
    words = instruction.split()
    head = words[0].lower() if words else ""
    if head == "slow":
        seconds = float(words[1]) if len(words) > 1 else 2.0
        emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "sleep",
                                                "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
        time.sleep(seconds)
        emit({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution", "command": "sleep",
                                                  "aggregated_output": "", "exit_code": 0, "status": "completed"}})
        item("file_change", "item_1", changes=[{"path": "x.py", "kind": "update"}])
        answer(f"Slept {seconds:g}s.\n\nSPOKEN: I waited as asked and edited one file.")
    elif head == "code":
        item("command_execution", "item_0", command="make test-chat", aggregated_output="14 passed", exit_code=0)
        item("file_change", "item_1", changes=[{"path": "tools/voice_chat.py", "kind": "update"}])
        answer(CODE_REPORT)
    elif head == "nospoken":
        answer(NOSPOKEN_REPORT)
    elif head == "quote":
        answer(QUOTE_REPORT)
    elif head == "die":
        emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "exit 3",
                                                "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
        sys.stdout.flush()
        os._exit(3)
    elif head == "error":
        emit({"type": "turn.failed", "error": {"message": "the model refused"}})
        return 1
    elif head == "thread":
        answer(f"{'Resumed' if resume else 'Fresh'} {thread}\n\nSPOKEN: {'Resumed' if resume else 'Fresh'} {thread}")
    else:
        answer(f"You said {instruction}\n\nSPOKEN: You said {instruction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
