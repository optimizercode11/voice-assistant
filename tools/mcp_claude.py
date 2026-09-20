#!/usr/bin/env python3
"""An MCP server that fronts one long-lived Claude Code session, for voice.

WHY A SEPARATE PROCESS AND NOT A TOOL IN agent_tools.py
    Claude Code turns take minutes; the bridge gives a tool call ten seconds
    and a whole spoken turn sixty-five.  So nothing here may wait.  `send`
    queues an instruction and returns at once; `updates` returns what has
    accumulated since the last time it was asked.  The session itself lives in
    a child of THIS process, so it survives between spoken turns, and the
    bridge's SIGTERM at shutdown takes it down with us. `stop` cancels its work
    and queued instructions without shutting down this MCP server.

WHY THE SPOKEN LINE IS ASKED FOR AT THE SOURCE
    Claude Code answers in Markdown with code blocks, and a speech engine reads
    Markdown badly.  The weakest summariser in the chain is the local 27B model
    working in 384 tokens from a clipped tool result; the strongest is Claude
    summarising its own work.  So the session is started with a system-prompt
    appendix asking for a final `SPOKEN:` line -- plain sentences, no code, no
    paths -- and that line is what `updates` returns by default.  The Markdown
    is kept as `detail` and handed over only when asked for, so the voice path
    never sees a code block unless the user wanted one read out.

WHAT CROSSES INTO THE SESSION
    The transcript, verbatim, inside a frame that says it came from speech
    recognition.  The local model is told not to paraphrase, and Claude is told
    that an odd word may be a mishearing worth checking before acting on it.
    Permissions are Claude Code's own (bypass by default, per the operator's
    decision); nothing in this file second-guesses them.

HOW A FINISHED TURN REACHES THE PAGE WITHOUT BEING ASKED
    With --push, the moment a turn finishes this process writes a JSON-RPC
    *notification* (no id) on the MCP wire: `notifications/voice/update` with
    the spoken line and the counters.  The bridge forwards it to the page over
    a server-sent events stream and the page speaks it when it is safe to.
    Nothing polls.  A pushed update counts as delivered, so a later `updates`
    call does not repeat it; without --push, `updates` is the only path.

WHAT IT NEVER DOES
    It never writes a Claude Code event to its own stdout: stdout is the MCP
    wire, and the child's stdout is a separate pipe read by a thread.  A stray
    line there would corrupt the protocol the bridge is speaking to us.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVER_INFO = {"name": "voice-claude-code", "version": "1.1"}
PROTOCOL = "2025-03-26"

SPOKEN_MARK = "SPOKEN:"
UPDATE_METHOD = "notifications/voice/update"
WORKING_METHOD = "notifications/voice/working"
TRACE_METHOD = "notifications/voice/trace"     # one line per child event, for the page's console; never spoken
MAX_TRACE_CHARS = 400
MAX_INSTRUCTION = 4000
MAX_SPOKEN_CHARS = 600            # a spoken line longer than this is not a spoken line
MAX_DETAIL_CHARS_DEFAULT = 4500   # under the bridge's 6000-char result clip, with room for the envelope
MAX_KEPT_UPDATES = 20

APPENDIX = f"""
You are being driven by voice.  The user speaks; a local speech recogniser
transcribes; the text reaches you inside a frame that says so.  Your reply is
read aloud by a speech engine after a small local model relays it.

Therefore end EVERY reply with one final line that begins exactly with
`{SPOKEN_MARK}` followed by one to three plain spoken sentences, under sixty
words: what you did or found, and any question you need answered.  No Markdown,
no code, no file paths, no identifiers spelled out, no lists.  Say "the test
file" rather than its path; say "the deploy script" rather than its name.
Everything before that line may be as detailed as you like; it is shown, not
spoken, and only when asked for.

If a key word in the instruction looks like a mishearing, say what you think
was meant and ask, in the spoken line, before doing anything hard to undo.
"""

FRAME = ("Spoken instruction from the user, transcribed by speech recognition. It may contain "
         "mishearings; if a key word looks wrong, ask before acting on it.\n\n")

FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
COMMAND_TOOLS = {"Bash", "PowerShell"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}

TOOLS = [
    {
        "name": "send",
        "description": ("Give Claude Code, the coding agent on the user's machine, a spoken "
                        "instruction or an answer to its question. Pass the user's words as they were "
                        "said, without paraphrasing. Returns immediately; the work happens in the "
                        "background. Use `updates` later to hear how it went."),
        "inputSchema": {"type": "object", "required": ["instruction"], "properties": {
            "instruction": {"type": "string", "minLength": 1, "maxLength": MAX_INSTRUCTION,
                            "description": "The user's words, verbatim."}}},
    },
    {
        "name": "stop",
        "description": ("Stop the Claude Code session managed by this voice assistant and its work, "
                        "discard all queued instructions, and keep it stopped until the user explicitly "
                        "sends a new instruction. Does not stop unrelated Claude Code sessions."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "updates",
        "description": ("What Claude Code has done or said since you last asked: a short spoken "
                        "summary per finished piece of work, plus whether it is still working. Set "
                        "`detail` true only when the user asks to hear the specifics (the diff, the "
                        "command output, the full answer)."),
        "inputSchema": {"type": "object", "properties": {
            "detail": {"type": "boolean",
                       "description": "Include the full written report of the latest finished work."}}},
    },
]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _plain(text: str) -> str:
    """A best-effort spoken form when the reply carried no SPOKEN line."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)       # code blocks gone, not read
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+|^\s*\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|\*|_", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _clip(text, MAX_SPOKEN_CHARS)


def split_reply(text: str) -> tuple[str, str]:
    """(spoken, detail) from Claude's final text.

    The SPOKEN line is the LAST one that starts with the mark, because a reply
    that quotes these instructions back may contain the mark mid-text.
    """
    text = (text or "").strip()
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped.startswith(SPOKEN_MARK):
            spoken = stripped[len(SPOKEN_MARK):].strip().strip("*").strip()
            if spoken and len(spoken) <= MAX_SPOKEN_CHARS:
                detail = "\n".join(lines[:index]).strip()
                return spoken, detail
            break
    plain = _plain(text)
    return (plain or "Claude Code finished without saying anything."), text


class Session:
    """The child, its reader thread, and everything learned from its stdout."""

    def __init__(self, claude: str, cwd: str, extra_args: list[str], max_detail: int, log, notify=None, trace=None):
        self.claude, self.cwd, self.extra_args, self.max_detail, self.log = claude, cwd, list(extra_args), max_detail, log
        # With `trace` set, every child event becomes one clipped line up the
        # wire (2026-09-12: the user asked to SEE the raw output as it happens,
        # not only hear the finished turn).  Shown by the page, never spoken.
        self.trace = trace
        # With `notify` set, a finished turn is PUSHED to the bridge the moment it
        # lands and does not wait to be asked for -- so it is delivered, not
        # unread, and "any news?" afterwards is honestly told there is none.
        # Without it (an older bridge, or --no-push) `updates` is the only path.
        self.notify = notify
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.process_identity: str | None = None
        self.reader: threading.Thread | None = None
        self.queue: list[str] = []             # instructions not yet handed to the child
        self.in_flight: str | None = None      # the instruction Claude is working on
        self.started_at: float | None = None
        self.activity = self._fresh_activity()
        self.updates: list[dict] = []          # finished turns, oldest first
        self.unread = 0
        self.seq = 0
        self.session_id = ""
        self.exit: dict | None = None          # set when the child dies
        self.stopping = False
        self.stopped = False
        self.stop_incomplete: dict[int, str] = {}

    @staticmethod
    def _fresh_activity() -> dict:
        return {"commands": 0, "files_edited": 0, "files_read": 0, "other_tools": 0, "last_tool": ""}

    # -- lifecycle -------------------------------------------------------
    def _spawn(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}}
        argv = [self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--append-system-prompt", APPENDIX] + self.extra_args
        self.process = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1, start_new_session=True)
        row = self._processes().get(self.process.pid)
        self.process_identity = row[3] if row else None
        self.exit = None
        self.stopped = False
        self.session_id = ""
        self.reader = threading.Thread(target=self._read_loop, args=(self.process,), daemon=True)
        self.reader.start()
        threading.Thread(target=self._drain_stderr, args=(self.process,), daemon=True).start()
        self.log(f"spawned claude pid={self.process.pid} cwd={self.cwd}")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def _processes() -> dict[int, tuple[str, int, int, str]]:
        """Linux process identities: state, parent, process group, start ticks."""
        found = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                found[int(path.parent.name)] = (fields[0], int(fields[1]), int(fields[2]), fields[19])
            except (OSError, ValueError, IndexError):
                continue
        return found

    def stop(self) -> dict:
        # Invalidate events BEFORE signaling. A final result racing with stop
        # must not publish success or dispatch the next queued instruction.
        with self.lock:
            if self.stopping:
                return {"status": "stopping", "state": "stopping"}
            self.stopping = True
            self.stopped = True
            process = self.process
            cancelled = self.in_flight is not None
            discarded = len(self.queue)
            self.queue.clear()
            self.in_flight, self.started_at = None, None
            self.activity = self._fresh_activity()
            self.session_id = ""
            owned = dict(self.stop_incomplete)
            snapshot = self._processes()
            if process is not None and (process.pid not in snapshot or
                                        snapshot[process.pid][3] == self.process_identity):
                # start_new_session makes the child the owner of this group.
                # Also capture descendants which created their own groups.
                descendants = {process.pid}
                while True:
                    expanded = descendants | {pid for pid, row in snapshot.items() if row[1] in descendants}
                    if expanded == descendants:
                        break
                    descendants = expanded
                owned.update({pid: row[3] for pid, row in snapshot.items()
                              if pid in descendants or row[2] == process.pid})

        def remaining():
            rows = self._processes()
            known = {pid for pid, identity in owned.items() if pid in rows and rows[pid][3] == identity}
            group_owned = process is not None and any(rows[pid][2] == process.pid for pid in known)
            while True:
                expanded = known | {pid for pid, row in rows.items()
                                    if row[1] in known or (group_owned and row[2] == process.pid)}
                if expanded == known:
                    break
                known = expanded
            # Include commands forked during the graceful shutdown interval.
            owned.update({pid: rows[pid][3] for pid in known})
            return {pid: identity for pid, identity in owned.items()
                    if pid in rows and rows[pid][3] == identity and rows[pid][0] != "Z"}

        def signal_owned(sig):
            live = remaining()
            rows = self._processes()
            signaled_group = False
            if process is not None and any(pid in rows and rows[pid][3] == identity and
                                           rows[pid][2] == process.pid for pid, identity in owned.items()):
                try:
                    os.killpg(process.pid, sig)
                    signaled_group = True
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    self.log(f"could not stop owned group={process.pid}: {error}")
            # Identity checks avoid signaling a reused PID. Never global pkill.
            for pid in live:
                if signaled_group and pid in rows and rows[pid][2] == process.pid:
                    continue
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    self.log(f"could not stop owned pid={pid}: {error}")

        signal_owned(signal.SIGTERM)
        if process is not None and process.stdin:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + 2.0
        while remaining() and time.monotonic() < deadline:
            time.sleep(0.025)
        if remaining():
            signal_owned(signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while remaining() and time.monotonic() < deadline:
                time.sleep(0.025)
        if process is not None:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        left = remaining()
        with self.lock:
            self.stopping = False
            self.stop_incomplete = left
            if not left:
                self.process = None
            self.exit = {"code": process.poll(), "at": time.time()} if process is not None else self.exit
        result = {"status": "stop failed" if left else "stopped", "state": "stop failed" if left else "stopped",
                  "cancelled_work": cancelled, "discarded_queued": discarded,
                  "note": "Send a new instruction explicitly to start a fresh Claude Code session."}
        if left:
            result.update(error="Some owned processes could not be stopped.", remaining_pids=sorted(left))
        return result

    # -- the child's stdout, one event per line ---------------------------
    def _drain_stderr(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr:
                self.log("claude stderr: " + line.rstrip()[:300])
        except (OSError, ValueError):
            pass
        finally:
            process.stderr.close()

    def _read_loop(self, process: subprocess.Popen) -> None:
        try:
            for raw in process.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                self._on_event(event, process)
        except (OSError, ValueError):
            pass
        process.stdout.close()
        code = process.wait()
        if process.stdin:
            process.stdin.close()
        with self.lock:
            if self.process is process and not self.stopped:
                self.queue.clear()
                self.exit = {"code": code, "at": time.time()}
                if self.in_flight is not None:
                    self._finish(spoken=f"Claude Code stopped unexpectedly (exit code {code}) before it "
                                        "finished. Say the instruction again to start a fresh session.",
                                 detail="", is_error=True)
        self.log(f"claude exited code={code}")

    def _on_event(self, event: dict, process: subprocess.Popen | None = None) -> None:
        kind = event.get("type")
        with self.lock:
            if self.stopped or self.stopping or (process is not None and self.process is not process):
                return
            if kind == "system" and event.get("subtype") == "init":
                self.session_id = str(event.get("session_id", ""))[:64]
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text":
                        self._trace("text", block.get("text"))
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    name = str(block.get("name", ""))
                    self._trace("tool", f"{name} {_tool_summary(block.get('input'))}")
                    self.activity["last_tool"] = name
                    if name in COMMAND_TOOLS:
                        self.activity["commands"] += 1
                    elif name in FILE_TOOLS:
                        self.activity["files_edited"] += 1
                    elif name in READ_TOOLS:
                        self.activity["files_read"] += 1
                    else:
                        self.activity["other_tools"] += 1
            elif kind == "user":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self._trace("output", _result_text(block.get("content")))
            elif kind == "result":
                text = event.get("result") if isinstance(event.get("result"), str) else ""
                is_error = bool(event.get("is_error")) or event.get("subtype") not in (None, "success")
                if is_error and not text:
                    text = f"Claude Code reported an error ({event.get('subtype', 'unknown')})."
                spoken, detail = split_reply(text)
                self._finish(spoken=spoken, detail=detail, is_error=is_error,
                             duration_ms=event.get("duration_ms"))
                self._dispatch_locked()

    def _trace(self, kind: str, text) -> None:
        if self.trace is None:
            return
        line = str(text or "").strip()
        if not line:
            return
        try:
            self.trace(kind, line)
        except Exception as error:               # the console is a convenience; the turn goes on
            self.log(f"trace failed: {error}")

    def _finish(self, *, spoken: str, detail: str, is_error: bool, duration_ms=None) -> None:
        self.seq += 1
        elapsed = round(time.time() - self.started_at, 1) if self.started_at else None
        self.updates.append({
            "seq": self.seq,
            "instruction": _clip(self.in_flight or "", 200),
            "spoken": spoken,
            "detail": detail,
            "activity": dict(self.activity),
            "is_error": is_error,
            "seconds": elapsed if duration_ms is None else round(duration_ms / 1000.0, 1),
            "finished_at": time.time(),
        })
        del self.updates[:-MAX_KEPT_UPDATES]
        if self.notify is not None:
            try:
                self.notify(self.updates[-1], self.max_detail)
                self.updates[-1]["pushed"] = True
            except Exception as error:               # the wire failed; fall back to being asked
                self.log(f"push failed: {error}")
                self.unread = min(self.unread + 1, len(self.updates))
        else:
            self.unread = min(self.unread + 1, len(self.updates))
        self.in_flight, self.started_at = None, None
        self.activity = self._fresh_activity()

    # -- the tools ---------------------------------------------------
    def _dispatch_locked(self, *, allow_spawn: bool = False) -> None:
        """Hand the next queued instruction to the child, if it is idle.  Caller holds the lock."""
        if self.in_flight is not None or not self.queue:
            return
        if not self.alive():
            if not allow_spawn:
                return
            self._spawn()
        instruction = self.queue.pop(0)
        message = {"type": "user", "message": {"role": "user", "content": FRAME + instruction}}
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            self.in_flight = instruction
            self._finish(spoken=f"I could not hand that to Claude Code: {error}.", detail="", is_error=True)
            return
        self.in_flight, self.started_at = instruction, time.time()
        if self.notify is not None:
            try:
                notify_working(instruction)
            except Exception as error:
                self.log(f"working notice failed: {error}")

    def send(self, instruction: str) -> dict:
        instruction = (instruction or "").strip()
        if not instruction:
            return {"error": "instruction is empty"}
        with self.lock:
            if self.stopping or self.stop_incomplete:
                return {"error": "Claude Code is still stopping; try stop again before sending new work."}
            fresh = not self.alive()
            self.queue.append(instruction[:MAX_INSTRUCTION])
            self._dispatch_locked(allow_spawn=True)
            waiting = len(self.queue)
            working = self.in_flight is not None
        return {
            "status": "queued" if waiting else "working",
            "queued_behind": waiting,
            "new_session": fresh,
            "note": ("Claude Code is on it. It usually takes from a few seconds to a few minutes; "
                     "ask for updates when the user wants to know."),
        }

    def state_locked(self) -> dict:
        label = ("stopping" if self.stopping else "stop failed" if self.stop_incomplete else
                 "stopped" if self.stopped else "working" if self.in_flight is not None else
                 "idle" if self.alive() else "not started")
        state = {"state": label}
        if self.in_flight is not None:
            state["working_on"] = _clip(self.in_flight, 200)
            state["working_for_seconds"] = round(time.time() - self.started_at, 1) if self.started_at else 0
            state["so_far"] = dict(self.activity)
        if self.queue:
            state["queued"] = len(self.queue)
        if self.exit is not None and self.in_flight is None and not self.alive():
            state["last_exit_code"] = self.exit["code"]
        return state

    def get_updates(self, detail: bool) -> dict:
        with self.lock:
            fresh = self.updates[len(self.updates) - self.unread:] if self.unread else []
            self.unread = 0
            out = self.state_locked()
            out["new"] = bool(fresh)
            out["updates"] = [{key: value for key, value in row.items() if key != "detail"} for row in fresh]
            if detail:
                latest = self.updates[-1] if self.updates else None
                if latest is None:
                    out["detail"] = ""
                else:
                    out["detail"] = _clip(latest["detail"], self.max_detail)
                    out["detail_of_seq"] = latest["seq"]
            elif not fresh and out["state"] == "idle":
                out["note"] = "Nothing new since last asked."
            if out["state"] == "working" and not fresh:
                out["note"] = "Still working; nothing finished yet."
            return out


# -- MCP wire -----------------------------------------------------------------
_WIRE = threading.Lock()   # replies come from the main thread, notifications from the reader thread


def _write(message: dict) -> None:
    with _WIRE:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def notify_update(row: dict, max_detail: int) -> None:
    """A JSON-RPC notification (no id) the bridge may forward to the page.

    The spoken line is what the page says; the report travels too, clipped,
    for the page to SHOW under it -- a person reading wants the raw output,
    the speaker must never get it.  The tool path is unchanged: `updates`
    still hands the report over only when asked.
    """
    params = {key: value for key, value in row.items() if key != "detail"}
    params["detail"] = _clip(row.get("detail") or "", max_detail)
    _write({"jsonrpc": "2.0", "method": UPDATE_METHOD, "params": params})


def _tool_summary(arguments) -> str:
    """The one argument a person reading a console wants: the command, the path, the pattern."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt", "description"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(arguments, ensure_ascii=False)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text", "")) for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
    return ""


def notify_trace(kind: str, line: str) -> None:
    _write({"jsonrpc": "2.0", "method": TRACE_METHOD,
            "params": {"kind": str(kind)[:16], "line": _clip(str(line).strip(), MAX_TRACE_CHARS)}})


def notify_working(instruction: str) -> None:
    _write({"jsonrpc": "2.0", "method": WORKING_METHOD, "params": {"instruction": _clip(instruction, 200)}})


def _reply(identifier, result) -> None:
    _write({"jsonrpc": "2.0", "id": identifier, "result": result})


def _content(payload: dict, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}],
            "isError": bool(is_error)}


def resolve_claude(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, ".local", "bin", "claude"),
                      os.path.join(home, ".claude", "local", "claude"),
                      os.path.join(home, ".npm-global", "bin", "claude"),
                      "/usr/local/bin/claude"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_extra_args(arguments) -> list[str]:
    extra = ["--permission-mode", arguments.permission_mode]
    if arguments.model:
        extra += ["--model", arguments.model]
    if arguments.effort:
        extra += ["--effort", arguments.effort]
    for directory in arguments.add_dir:
        extra += ["--add-dir", directory]
    return extra


def doctor(arguments, claude: str | None) -> int:
    print(f"claude: {claude or 'NOT FOUND (pass --claude /path/to/claude)'}")
    if claude:
        try:
            version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            version = f"could not run: {error}"
        print(f"version: {version}")
    cwd = os.path.abspath(arguments.cwd)
    print(f"cwd: {cwd} ({'exists' if os.path.isdir(cwd) else 'MISSING'})")
    credentials = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    print(f"credentials: {credentials} ({'present' if os.path.exists(credentials) else 'absent -- claude will fail to start unless logged in another way'})")
    print("argv: " + " ".join([claude or "claude", "-p", "--input-format", "stream-json", "--output-format",
                                "stream-json", "--verbose", "--append-system-prompt", "<appendix>"]
                               + build_extra_args(arguments)))
    print(f"detail clip: {arguments.max_detail_chars} chars; spoken line: `{SPOKEN_MARK}` asked for at the source")
    print("updates: " + ("PUSHED to the bridge as " + UPDATE_METHOD + " the moment a turn finishes (and delivered, not unread)"
                         if arguments.push else "returned only when `updates` is called (no --push)"))
    return 0 if claude and os.path.isdir(cwd) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", help="path to the claude binary (default: search PATH and the usual places)")
    parser.add_argument("--cwd", default=".", help="working directory the session runs in")
    parser.add_argument("--model", default="", help="model for the session (default: the user's default)")
    parser.add_argument("--effort", default="", help="effort level for the session")
    parser.add_argument("--permission-mode", default="bypassPermissions",
                        help="Claude Code permission mode (default bypassPermissions, the operator's call)")
    parser.add_argument("--add-dir", action="append", default=[], help="extra directory Claude may work in")
    parser.add_argument("--max-detail-chars", type=int, default=MAX_DETAIL_CHARS_DEFAULT)
    parser.add_argument("--push", action="store_true",
                        help="push each finished turn to the bridge as a notification instead of waiting to be asked")
    parser.add_argument("--doctor", action="store_true", help="print the policy this argv encodes and exit")
    arguments = parser.parse_args()

    claude = resolve_claude(arguments.claude)
    if arguments.doctor:
        return doctor(arguments, claude)

    def log(text: str) -> None:
        sys.stderr.write(f"mcp_claude: {text}\n")
        sys.stderr.flush()

    session = Session(claude or "claude", os.path.abspath(arguments.cwd), build_extra_args(arguments),
                      max(200, arguments.max_detail_chars), log, notify=notify_update if arguments.push else None,
                      trace=notify_trace if arguments.push else None)
    tools = json.loads(json.dumps(TOOLS))
    if arguments.push:
        # The model should not send the person off to ask: the result will be
        # spoken the moment it lands.
        tools[0]["description"] = tools[0]["description"].replace(
            "Use `updates` later to hear how it went.",
            "Claude Code will speak up on its own the moment it finishes, so tell the user they will hear "
            "from it; `updates` is only for asking how it is going meanwhile.")

    shutdown_requested = False

    def shutdown(*_):
        nonlocal shutdown_requested
        shutdown_requested = True
        # Closing stdin and SIGTERM often arrive together. Do not interrupt
        # cleanup already running in the main thread, nor deadlock its lock.
        if session.stopping:
            return
        session.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            method, identifier = message.get("method"), message.get("id")
            if identifier is None:
                continue
            if method == "initialize":
                _reply(identifier, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
            elif method == "tools/list":
                _reply(identifier, {"tools": tools})
            elif method == "tools/call":
                params = message.get("params") or {}
                name, args = params.get("name"), params.get("arguments") or {}
                if name == "send":
                    if claude is None:
                        _reply(identifier, _content({"error": "claude is not installed where this server runs"}, True))
                        continue
                    result = session.send(str(args.get("instruction", "")))
                    _reply(identifier, _content(result, "error" in result))
                elif name == "stop":
                    result = session.stop()
                    _reply(identifier, _content(result, "error" in result))
                elif name == "updates":
                    _reply(identifier, _content(session.get_updates(bool(args.get("detail", False)))))
                else:
                    _reply(identifier, _content({"error": f"unknown tool {name}"}, True))
            else:
                _write({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": f"unknown {method}"}})
            if shutdown_requested:
                break
    finally:
        session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
