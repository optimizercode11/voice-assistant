#!/usr/bin/env python3
"""An MCP server that fronts Codex CLI running on the local model, for voice.

THE SAME SHAPE AS tools/mcp_claude.py, ON PURPOSE
    `send` hands over a spoken instruction and returns at once; `updates`
    says what has finished since; with --push a finished turn is a JSON-RPC
    notification the bridge forwards to the page.  The spoken line is asked
    for at the source (a final `SPOKEN:` line), the Markdown report travels
    only as `detail`.  Everything the voice path learned about Claude Code
    applies unchanged, so those helpers are imported rather than copied.

WHAT IS DIFFERENT ABOUT CODEX
    `codex exec` is one process per turn, not one long-lived child: the first
    instruction starts a thread, every later one is `codex exec resume
    <thread> <prompt>`, so the conversation continues across turns and a turn
    in flight is exactly one child.  Its events are JSONL on stdout
    (`thread.started`, `item.started`/`item.completed` with
    `command_execution`, `file_change`, `agent_message`, ...,
    `turn.completed`/`turn.failed`) -- observed 2026-09-12, codex-cli 0.154.0.

    The model is chosen by a Codex *profile*: `$CODEX_HOME/<name>.config.toml`
    (the legacy `[profiles.<name>]` table is rejected by this version).  `exec`
    takes `--profile`, but `exec resume` does not, so the profile file is read
    here and passed to BOTH forms as `-c key=value` overrides, flattened; a
    resumed turn therefore runs on the same model as the first one instead of
    silently falling back to the user's default (which would be a paid remote
    model).  `[projects]` trust tables are dropped from the flattening; the
    server passes `--skip-git-repo-check` instead.

    Codex reads its prompt from stdin when stdin is open and blocks forever on
    a pipe ("Reading additional input from stdin"), so the child's stdin is
    /dev/null and the prompt is an argument.  The prompt begins with the
    speech frame, so it can never be taken for a flag.

SANDBOX
    Codex's own sandbox (bubblewrap) cannot start on the machine this was
    built for: "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
    every command fails before it runs, and the model reports the environment
    as broken.  The default here is therefore --access full
    (`--dangerously-bypass-approvals-and-sandbox`), the same decision the
    operator took for Claude Code on 2026-09-11: the coding agent's judgement
    is trusted and nothing on the bridge second-guesses it.  `--access
    workspace` keeps Codex's sandbox and approval policy for hosts where it
    works.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib

import mcp_claude as shared

SERVER_INFO = {"name": "voice-codex", "version": "1.0"}
AGENT = "Codex"
DEFAULT_PROFILE = "q38f"
COMMAND_ITEMS = {"command_execution"}
EDIT_ITEMS = {"file_change"}
QUIET_ITEMS = {"agent_message", "reasoning"}

APPENDIX = f"""---
You are being driven by voice.  The user speaks; a local speech recogniser
transcribes; the text above reached you inside a frame that says so.  Your
reply is read aloud by a speech engine after a small local model relays it.

Therefore end EVERY reply with one final line that begins exactly with
`{shared.SPOKEN_MARK}` followed by one to three plain spoken sentences, under
sixty words: what you did or found, and any question you need answered.  No
Markdown, no code, no file paths, no identifiers spelled out, no lists.  Say
"the test file" rather than its path.  Everything before that line may be as
detailed as you like; it is shown, not spoken, and only when asked for.

If a key word in the instruction looks like a mishearing, say what you think
was meant and ask, in the spoken line, before doing anything hard to undo."""


def _codex_tools() -> list[dict]:
    tools = json.loads(json.dumps(shared.TOOLS))
    for tool in tools:
        tool["description"] = tool["description"].replace(
            "Claude Code, the coding agent on the user's machine",
            f"{AGENT}, the coding agent on the user's machine that runs on the local model").replace(
            "Claude Code", AGENT)
    return tools


TOOLS = _codex_tools()


# -- the profile, flattened ----------------------------------------------------
def _flatten(prefix: str, value, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), inner, out)
    elif isinstance(value, bool):
        out.append(f"{prefix}={'true' if value else 'false'}")
    elif isinstance(value, (int, float)):
        out.append(f"{prefix}={value}")
    else:
        out.append(f"{prefix}={json.dumps(value, ensure_ascii=False)}")


def profile_path(codex_home: str, profile: str) -> str:
    return os.path.join(codex_home, f"{profile}.config.toml")


def profile_overrides(codex_home: str, profile: str) -> list[str]:
    """`-c key=value` pairs equivalent to `--profile <name>`, usable on resume too.

    Read on every spawn, so an edited profile applies to the next turn.
    """
    with open(profile_path(codex_home, profile), "rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("the profile is empty")
    raw.pop("projects", None)
    out: list[str] = []
    _flatten("", raw, out)
    return out


def access_args(access: str) -> list[str]:
    if access == "full":
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']


# -- the session -----------------------------------------------------------------
class Session:
    """One Codex thread, continued by `exec resume`; one child per turn."""

    def __init__(self, codex: str, cwd: str, profile: str, codex_home: str, access: str,
                 add_dirs: list[str], max_detail: int, log, notify=None):
        self.codex, self.cwd, self.profile, self.codex_home = codex, cwd, profile, codex_home
        self.access, self.add_dirs, self.max_detail, self.log = access, list(add_dirs), max_detail, log
        self.notify = notify
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.thread_id = ""                    # set by the first turn; resumed by every later one
        self.queue: list[str] = []
        self.in_flight: str | None = None
        self.started_at: float | None = None
        self.activity = self._fresh_activity()
        self.seen: set[str] = set()            # item ids counted this turn
        self.last_text = ""
        self.error_text = ""
        self.updates: list[dict] = []
        self.unread = 0
        self.seq = 0
        self.exit: dict | None = None

    @staticmethod
    def _fresh_activity() -> dict:
        return {"commands": 0, "files_edited": 0, "files_read": 0, "other_tools": 0, "last_tool": ""}

    # -- argv ------------------------------------------------------------
    def argv(self, prompt: str, thread_id: str = "") -> list[str]:
        resume = bool(thread_id)
        argv = [self.codex, "exec"] + (["resume"] if resume else []) + ["--json", "--skip-git-repo-check"]
        argv += access_args(self.access)
        for pair in profile_overrides(self.codex_home, self.profile):
            argv += ["-c", pair]
        if resume:
            argv.append(thread_id)              # `exec resume` takes neither -C nor --add-dir
        else:
            argv += ["-C", self.cwd]
            for directory in self.add_dirs:
                argv += ["--add-dir", directory]
        argv.append(prompt)
        return argv

    # -- lifecycle -------------------------------------------------------
    def _spawn_locked(self, instruction: str) -> bool:
        prompt = shared.FRAME + instruction + "\n\n" + APPENDIX
        try:
            argv = self.argv(prompt, self.thread_id)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
            self.in_flight = instruction
            self._finish(spoken=f"I could not start {AGENT}: the {self.profile} profile could not be read "
                                f"({error}).", detail="", is_error=True)
            return False
        env = {key: value for key, value in os.environ.items()
               if key not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}}
        env["CODEX_HOME"] = self.codex_home
        try:
            self.process = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                            encoding="utf-8", bufsize=1, start_new_session=True)
        except OSError as error:
            self.in_flight = instruction
            self._finish(spoken=f"I could not start {AGENT}: {error}.", detail="", is_error=True)
            return False
        self.exit = None
        self.seen, self.last_text, self.error_text = set(), "", ""
        threading.Thread(target=self._read_loop, args=(self.process,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.process,), daemon=True).start()
        self.log(f"spawned codex pid={self.process.pid} {'resume ' + self.thread_id if self.thread_id else 'fresh'}")
        return True

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait(timeout=3)

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr:
                line = line.rstrip()
                if line and "Reading additional input from stdin" not in line:
                    self.log("codex stderr: " + line[:300])
        except (OSError, ValueError):
            pass

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
                if isinstance(event, dict):
                    self._on_event(event)
        except (OSError, ValueError):
            pass
        code = process.wait()
        with self.lock:
            if self.process is process:
                self.exit = {"code": code, "at": time.time()}
                if self.in_flight is not None:
                    self._finish_turn_locked(code)
                self._dispatch_locked()
        self.log(f"codex exited code={code}")

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        with self.lock:
            if kind == "thread.started":
                self.thread_id = str(event.get("thread_id") or "")[:64]
            elif kind in ("item.started", "item.completed"):
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                item_type = str(item.get("type") or "")
                if item_type == "agent_message":
                    if kind == "item.completed" and isinstance(item.get("text"), str):
                        self.last_text = item["text"]
                    return
                if item_type in QUIET_ITEMS:
                    return
                identity = str(item.get("id") or f"{item_type}:{len(self.seen)}")
                if identity in self.seen:
                    return
                self.seen.add(identity)
                self.activity["last_tool"] = item_type
                if item_type in COMMAND_ITEMS:
                    self.activity["commands"] += 1
                elif item_type in EDIT_ITEMS:
                    changes = item.get("changes") if isinstance(item.get("changes"), list) else []
                    self.activity["files_edited"] += max(1, len(changes))
                else:
                    self.activity["other_tools"] += 1
            elif kind == "turn.failed":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                self.error_text = str(error.get("message") or "the turn failed")
            elif kind == "error":
                self.error_text = str(event.get("message") or "error")

    def _finish_turn_locked(self, code: int) -> None:
        text = self.last_text.strip()
        is_error = bool(self.error_text) or code != 0
        if text:
            spoken, detail = shared.split_reply(text)
        else:
            spoken, detail = "", ""
        if is_error:
            reason = self.error_text or f"exit code {code}"
            if not spoken:
                spoken = (f"{AGENT} stopped unexpectedly ({reason}) before it finished. "
                          "Say the instruction again to start a fresh session.")
            detail = (f"{reason}\n\n{detail}").strip()
            self.thread_id = ""                 # the next send starts afresh, as with Claude Code
        elif not spoken:
            spoken = f"{AGENT} finished without saying anything."
        self._finish(spoken=spoken, detail=detail, is_error=is_error)

    def _finish(self, *, spoken: str, detail: str, is_error: bool) -> None:
        self.seq += 1
        elapsed = round(time.time() - self.started_at, 1) if self.started_at else None
        self.updates.append({
            "seq": self.seq,
            "instruction": shared._clip(self.in_flight or "", 200),
            "spoken": spoken,
            "detail": detail,
            "activity": dict(self.activity),
            "is_error": is_error,
            "seconds": elapsed,
            "finished_at": time.time(),
        })
        del self.updates[:-shared.MAX_KEPT_UPDATES]
        if self.notify is not None:
            try:
                self.notify(self.updates[-1], self.max_detail)
                self.updates[-1]["pushed"] = True
            except Exception as error:
                self.log(f"push failed: {error}")
                self.unread = min(self.unread + 1, len(self.updates))
        else:
            self.unread = min(self.unread + 1, len(self.updates))
        self.in_flight, self.started_at = None, None
        self.activity = self._fresh_activity()

    def _dispatch_locked(self) -> None:
        if self.in_flight is not None or not self.queue:
            return
        instruction = self.queue.pop(0)
        self.in_flight, self.started_at = instruction, time.time()
        if not self._spawn_locked(instruction):
            self._dispatch_locked()
            return
        if self.notify is not None:
            try:
                shared.notify_working(instruction)
            except Exception as error:
                self.log(f"working notice failed: {error}")

    # -- the two tools ---------------------------------------------------
    def send(self, instruction: str) -> dict:
        instruction = (instruction or "").strip()
        if not instruction:
            return {"error": "instruction is empty"}
        with self.lock:
            fresh = not self.thread_id
            self.queue.append(instruction[:shared.MAX_INSTRUCTION])
            self._dispatch_locked()
            waiting = len(self.queue)
            working = self.in_flight is not None
        return {
            "status": "queued" if waiting else ("working" if working else "failed"),
            "queued_behind": waiting,
            "new_session": fresh,
            "note": (f"{AGENT} is on it. It usually takes from a few seconds to a few minutes; "
                     "ask for updates when the user wants to know."),
        }

    def state_locked(self) -> dict:
        state = {"state": "working" if self.in_flight is not None else ("idle" if self.thread_id else "not started")}
        if self.in_flight is not None:
            state["working_on"] = shared._clip(self.in_flight, 200)
            state["working_for_seconds"] = round(time.time() - self.started_at, 1) if self.started_at else 0
            state["so_far"] = dict(self.activity)
        if self.queue:
            state["queued"] = len(self.queue)
        if self.exit is not None and self.in_flight is None and self.exit["code"] != 0:
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
                    out["detail"] = shared._clip(latest["detail"], self.max_detail)
                    out["detail_of_seq"] = latest["seq"]
            elif not fresh and out["state"] == "idle":
                out["note"] = "Nothing new since last asked."
            if out["state"] == "working" and not fresh:
                out["note"] = "Still working; nothing finished yet."
            return out


# -- process ---------------------------------------------------------------------
def resolve_codex(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("codex")
    if found:
        return found
    for candidate in ("/usr/local/bin/codex", os.path.join(os.path.expanduser("~"), ".local", "bin", "codex")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def doctor(arguments, codex: str | None) -> int:
    print(f"codex: {codex or 'NOT FOUND (pass --codex /path/to/codex)'}")
    if codex:
        try:
            version = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            version = f"could not run: {error}"
        print(f"version: {version}")
    cwd = os.path.abspath(arguments.cwd)
    print(f"cwd: {cwd} ({'exists' if os.path.isdir(cwd) else 'MISSING'})")
    path = profile_path(arguments.codex_home, arguments.profile)
    ok = True
    try:
        overrides = profile_overrides(arguments.codex_home, arguments.profile)
        print(f"profile: {path} -> {len(overrides)} overrides, passed to exec AND exec resume:")
        for pair in overrides:
            print(f"  -c {pair}")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        ok = False
        print(f"profile: {path} UNREADABLE ({error})")
    print("access: " + ("full -- Codex's sandbox and approvals bypassed (the operator's call, and bubblewrap "
                        "does not start on the built-for host)" if arguments.access == "full"
                        else "workspace -- Codex's workspace-write sandbox, approvals never asked"))
    sketch = Session(codex or "codex", cwd, arguments.profile, arguments.codex_home, arguments.access,
                     arguments.add_dir, arguments.max_detail_chars, lambda _: None)
    try:
        print("argv (first turn): " + " ".join(sketch.argv("<frame + instruction + appendix>")))
        print("argv (later turns): " + " ".join(sketch.argv("<frame + instruction + appendix>", "<thread>")))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        pass
    print(f"detail clip: {arguments.max_detail_chars} chars; spoken line: `{shared.SPOKEN_MARK}` asked for at the source")
    print("updates: " + ("PUSHED to the bridge as " + shared.UPDATE_METHOD + " the moment a turn finishes"
                         if arguments.push else "returned only when `updates` is called (no --push)"))
    return 0 if codex and os.path.isdir(cwd) and ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codex", help="path to the codex binary (default: search PATH and /usr/local/bin)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"Codex profile: $CODEX_HOME/<name>.config.toml (default {DEFAULT_PROFILE})")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"),
                        help="where the profile and Codex's own state live (default $CODEX_HOME or ~/.codex)")
    parser.add_argument("--cwd", default=".", help="working directory the thread starts in")
    parser.add_argument("--add-dir", action="append", default=[], help="extra directory Codex may write in")
    parser.add_argument("--access", choices=("full", "workspace"), default="full",
                        help="full: bypass Codex's sandbox and approvals (default); workspace: its workspace-write sandbox")
    parser.add_argument("--max-detail-chars", type=int, default=shared.MAX_DETAIL_CHARS_DEFAULT)
    parser.add_argument("--push", action="store_true",
                        help="push each finished turn to the bridge as a notification instead of waiting to be asked")
    parser.add_argument("--doctor", action="store_true", help="print the policy this argv encodes and exit")
    arguments = parser.parse_args()
    arguments.codex_home = os.path.abspath(os.path.expanduser(arguments.codex_home))

    codex = resolve_codex(arguments.codex)
    if arguments.doctor:
        return doctor(arguments, codex)

    def log(text: str) -> None:
        sys.stderr.write(f"mcp_codex: {text}\n")
        sys.stderr.flush()

    session = Session(codex or "codex", os.path.abspath(arguments.cwd), arguments.profile, arguments.codex_home,
                      arguments.access, arguments.add_dir, max(200, arguments.max_detail_chars), log,
                      notify=shared.notify_update if arguments.push else None)
    tools = json.loads(json.dumps(TOOLS))
    if arguments.push:
        tools[0]["description"] = tools[0]["description"].replace(
            "Use `updates` later to hear how it went.",
            f"{AGENT} will speak up on its own the moment it finishes, so tell the user they will hear "
            "from it; `updates` is only for asking how it is going meanwhile.")

    def shutdown(*_):
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
                shared._reply(identifier, {"protocolVersion": shared.PROTOCOL, "capabilities": {"tools": {}},
                                           "serverInfo": SERVER_INFO})
            elif method == "tools/list":
                shared._reply(identifier, {"tools": tools})
            elif method == "tools/call":
                params = message.get("params") or {}
                name, args = params.get("name"), params.get("arguments") or {}
                if name == "send":
                    if codex is None:
                        shared._reply(identifier, shared._content({"error": "codex is not installed where this server runs"}, True))
                        continue
                    result = session.send(str(args.get("instruction", "")))
                    shared._reply(identifier, shared._content(result, "error" in result))
                elif name == "updates":
                    shared._reply(identifier, shared._content(session.get_updates(bool(args.get("detail", False)))))
                else:
                    shared._reply(identifier, shared._content({"error": f"unknown tool {name}"}, True))
            else:
                shared._write({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": f"unknown {method}"}})
    finally:
        session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
