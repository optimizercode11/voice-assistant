"""Persistent, explicitly addressed Codex sessions for the voice interface.

The database owns identity and selection; the model never owns either. Codex's
app-server owns execution and its thread history. Transport failures are not
retried as new turns: an uncertain result stays uncertain until reconciled.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import mcp_claude as speech
from codex_app_server import AppServer, AppServerError, AppServerRPCError

ACTIVE = {"starting", "working", "waiting_input", "interrupting", "unknown"}
CONTEXT = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SESSION_INSTRUCTIONS = speech.APPENDIX.replace("Claude Code", "Codex") + """
You are one named coding session, supervised by the voice session manager.
Do not spawn other AI agents or Codex sessions. Keep changes in this session's
working directory. Do not start background builds or detached shell jobs.
Use a separate build output directory for this session when a shared build
cache would serialize or conflict with another session. Report results from
actual commands. A request to stop speaking is not cancellation of your task.
"""


class SessionError(ValueError):
    pass


class Manager:
    def __init__(self, state_dir, cwd, argv, *, model, provider, profile,
                 access="full", max_active=3, notify=None, backend_factory=AppServer):
        if not 1 <= max_active <= 3:
            raise SessionError("At most three managed workers; reserve one coordinator slot.")
        self.root = Path(state_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lockfile = (self.root / "manager.lock").open("a+")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise SessionError("Another session manager owns this state directory.")
        self.cwd = str(Path(cwd).expanduser().resolve())
        if not Path(self.cwd).is_dir():
            self.lockfile.close()
            raise SessionError("Default workspace must be an existing directory.")
        self.db = sqlite3.connect(self.root / "sessions.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, name TEXT UNIQUE, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS focus (context TEXT PRIMARY KEY, session TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS cursors (context TEXT, session TEXT, seq INTEGER, PRIMARY KEY(context,session));
        """)
        self.lock = threading.RLock()
        self.argv, self.model, self.provider, self.profile = list(argv), model, provider, profile
        self.access, self.max_active, self.notify = access, max_active, notify
        self.factory, self.backend, self.loaded = backend_factory, None, set()
        self.closed = False
        self.records = {sid: json.loads(data) for sid, data in self.db.execute("SELECT id,data FROM sessions")}
        self.pending = {}  # live app-server request IDs; never persisted as reusable IDs
        for row in self.records.values():
            if row["state"] in ACTIVE:
                row.update(state="interrupted", error="Session manager restarted. The previous task was interrupted; send a follow-up to resume its saved thread.", questions=[])
                self._save(row)

    @staticmethod
    def context(value="voice"):
        if not isinstance(value, str) or not CONTEXT.fullmatch(value):
            raise SessionError("Invalid conversation context.")
        return value

    @staticmethod
    def text(value, field, maximum):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
            raise SessionError(f"{field} must be nonempty text, at most {maximum} characters.")
        return value.strip()

    def _save(self, row):
        row["updated_at"] = time.time()
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (row["session_id"], row["name"].casefold(), json.dumps(row)))
        self.db.commit()

    def _view(self, row):
        keys = ("session_id", "name", "cwd", "state", "thread_id", "turn_id", "working_on", "model", "model_provider", "profile", "created_at", "updated_at", "error", "last_update", "questions")
        result = {k: row.get(k) for k in keys}
        for key, bound in (("working_on", 240), ("error", 500), ("last_update", 400)):
            if isinstance(result.get(key), str):
                result[key] = result[key][:bound]
        result["requires_attention"] = row["state"] in {"waiting_input", "failed", "unknown"}
        return result

    def _selected(self, context):
        row = self.db.execute("SELECT session FROM focus WHERE context=?", (context,)).fetchone()
        return row[0] if row else None

    def _resolve(self, session, context):
        if session is None:
            session = self._selected(context)
            if not session:
                raise SessionError("No Codex session selected in this conversation. List sessions, then select or name one.")
        name = self.text(session, "session", 120)
        matches = [r for r in self.records.values() if name == r["session_id"] or name.casefold() == r["name"].casefold()]
        if len(matches) != 1:
            raise SessionError(f"No unique managed Codex session named {name!r}. List sessions for exact names or IDs.")
        return matches[0]

    def _emit(self, method, row, **extra):
        if not self.notify:
            return
        payload = {"session_id": row["session_id"], "session_name": row["name"],
                   "context_id": row.get("context_id", "voice"), "turn_id": row.get("turn_id", ""),
                   "state": row["state"], "event_id": uuid.uuid4().hex, **extra}
        try:
            self.notify(method, payload)
        except (OSError, BrokenPipeError):
            pass

    def _changed(self, row, **extra):
        self._save(row)
        self._emit("notifications/voice/session", row, session=self._view(row), **extra)

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionError("Codex session operation exceeded its deadline; it was not retried.")
        return remaining

    def _ensure_backend(self, deadline=None):
        if self.backend is not None and self.backend.alive:
            return self.backend
        if self.backend is not None:
            raise SessionError("Codex connection was lost. Restart the session manager to recover saved threads; do not resend an uncertain task.")
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env["CUDA_VISIBLE_DEVICES"] = ""  # CLI is an HTTP client, never a CUDA worker
        self.backend = self.factory(self.argv, self.cwd, env=env,
                                    on_notification=self._notification,
                                    on_request=self._request,
                                    on_disconnect=self._disconnected)
        self.backend.start(timeout=self._remaining(deadline) if deadline is not None else 8)
        return self.backend

    def _thread_params(self, row):
        return {"cwd": row["cwd"], "model": self.model, "modelProvider": self.provider,
                "approvalPolicy": "never", "sandbox": "danger-full-access" if self.access == "full" else "workspace-write",
                "developerInstructions": SESSION_INSTRUCTIONS,
                "config": {"features.multi_agent": False, "agents.max_threads": 1}}

    def create_session(self, name, cwd=None, context_id="voice"):
        context = self.context(context_id)
        name = self.text(name, "name", 60)
        if any(ord(c) < 32 for c in name):
            raise SessionError("Session names cannot contain control characters.")
        with self.lock:
            if any(r["name"].casefold() == name.casefold() for r in self.records.values()):
                raise SessionError("That session name already exists. Select it or use another name.")
            if len(self.records) >= 32:
                raise SessionError("The manager holds 32 sessions; archive a finished session before creating another.")
            sid = "s_" + uuid.uuid4().hex[:12]
            if cwd is None:
                directory = Path(self.cwd) / "sessions" / sid
                directory.mkdir(parents=True)
            else:
                path = Path(self.text(cwd, "cwd", 2000)).expanduser()
                directory = (path if path.is_absolute() else Path(self.cwd) / path).resolve()
                if not directory.is_dir():
                    raise SessionError("The project directory does not exist. Use workspace tools to locate it first.")
            row = dict(session_id=sid, name=name, cwd=str(directory), state="idle", thread_id="", turn_id="",
                       working_on="", model=self.model, model_provider=self.provider, profile=self.profile,
                       context_id=context, created_at=time.time(), error="", last_update="", questions=[], activity={})
            # Creation is cheap and durable. A Codex thread is allocated on first send.
            self.records[sid] = row
            self.db.execute("INSERT OR REPLACE INTO focus VALUES(?,?)", (context, sid))
            self._changed(row, selected_session_id=sid)
            return {"status": "created", "session": self._view(row), "selected_session_id": sid, "context_id": context}

    def list_sessions(self, context_id="voice", offset=0, limit=10):
        context = self.context(context_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32:
            raise SessionError("offset must be nonnegative and limit between 1 and 32.")
        with self.lock:
            rows = sorted(self.records.values(), key=lambda r: r["created_at"])
            # Keep complete JSON below the generic tool-result clipping budget.
            page = []
            for r in rows[offset:offset+limit]:
                snapshot = self._view(r)
                snapshot.pop("questions", None)
                if page and len(json.dumps(page + [snapshot])) > 4400:
                    break
                page.append(snapshot)
            return {"sessions": page,
                    "selected_session_id": self._selected(context), "context_id": context,
                    "total": len(rows), "next_offset": offset+len(page) if offset+len(page) < len(rows) else None,
                    "active": sum(r["state"] in ACTIVE for r in rows), "max_active": self.max_active}

    def select_session(self, session, context_id="voice"):
        context = self.context(context_id)
        with self.lock:
            row = self._resolve(session, context)
            self.db.execute("INSERT OR REPLACE INTO focus VALUES(?,?)", (context, row["session_id"]))
            self.db.commit()
            self._emit("notifications/voice/session", row, session=self._view(row), context_id=context, selected_session_id=row["session_id"])
            return {"session": self._view(row), "selected_session_id": row["session_id"], "context_id": context}

    def status(self, session=None, context_id="voice"):
        with self.lock:
            return {"session": self._view(self._resolve(session, self.context(context_id)))}

    def _admit(self, row):
        other = [r for r in self.records.values() if r["session_id"] != row["session_id"] and r["state"] in ACTIVE]
        if len(other) >= self.max_active:
            raise SessionError("All three worker slots are occupied. Wait or interrupt a named session; one slot is reserved for the coordinator.")
        cwd = Path(row["cwd"])
        for r in other:
            other_cwd = Path(r["cwd"])
            if cwd == other_cwd or cwd in other_cwd.parents or other_cwd in cwd.parents:
                raise SessionError(f"{r['name']} is already working in an overlapping directory. Use a separate worktree or wait.")

    def _load(self, row, deadline):
        if (row["model"], row["model_provider"], row["profile"]) != (self.model, self.provider, self.profile):
            raise SessionError("This saved session uses a different model profile. Restore its original configuration or create a new session; it will not silently switch providers.")
        backend = self._ensure_backend(deadline)
        if row["session_id"] in self.loaded:
            return backend
        params = self._thread_params(row)
        method = "thread/start"
        if row["thread_id"]:
            method = "thread/resume"
            params["threadId"] = row["thread_id"]
        result = backend.request(method, params, timeout=self._remaining(deadline))
        thread = result.get("thread", {})
        tid = thread.get("id")
        if not isinstance(tid, str) or not tid:
            raise SessionError("Codex returned no thread identity.")
        if row["thread_id"] and tid != row["thread_id"]:
            raise SessionError("Codex resumed a different thread; refusing to send.")
        # An explicit profile must never silently become the paid/default provider.
        if result.get("model", self.model) != self.model or result.get("modelProvider", self.provider) != self.provider:
            raise SessionError("Codex returned a different model/provider than the configured profile.")
        row["thread_id"] = tid
        self.loaded.add(row["session_id"])
        self._save(row)
        return backend

    def send(self, instruction, session=None, context_id="voice"):
        deadline = time.monotonic() + 8
        context = self.context(context_id)
        instruction = self.text(instruction, "instruction", speech.MAX_INSTRUCTION)
        with self.lock:
            row = self._resolve(session, context)
            if row["state"] in ACTIVE:
                raise SessionError("That session is busy. Use steer to change its current task, or wait before sending a new task.")
            self._admit(row)
            backend = self._load(row, deadline)
            row.update(state="starting", working_on=instruction, context_id=context, turn_id="", error="", questions=[],
                       started_at=time.time(), last_text="", seen=[], activity={"commands": 0, "files_edited": 0, "other_tools": 0})
            self._changed(row)
            try:
                result = backend.request("turn/start", {"threadId": row["thread_id"],
                    "input": [{"type": "text", "text": speech.FRAME + instruction, "text_elements": []}]},
                    timeout=self._remaining(deadline))
                turn = result.get("turn", {})
                if not isinstance(turn.get("id"), str) or not turn["id"]:
                    raise SessionError("Codex accepted a request without a turn identity.")
                row.update(turn_id=turn["id"], state="working")
                self._changed(row)
                self._emit(speech.WORKING_METHOD, row, instruction=instruction[:200])
                return {"status": "working", "session": self._view(row), "note": "Codex accepted the task. This is not a completion report."}
            except (AppServerError, SessionError) as error:
                row.update(state="failed" if isinstance(error, AppServerRPCError) else "unknown",
                           error=f"Turn start could not be confirmed: {error}. Do not automatically resend.")
                self._changed(row)
                raise SessionError(row["error"])

    def steer(self, instruction, session=None, context_id="voice"):
        instruction = self.text(instruction, "instruction", speech.MAX_INSTRUCTION)
        with self.lock:
            row = self._resolve(session, self.context(context_id))
            if row["state"] != "working" or not row["turn_id"]:
                raise SessionError("This session has no running turn to steer. Use send for a new task or answer a pending question.")
            self._ensure_backend().request("turn/steer", {"threadId": row["thread_id"], "expectedTurnId": row["turn_id"],
                "input": [{"type": "text", "text": instruction, "text_elements": []}]})
            return {"status": "steered", "session": self._view(row)}

    def interrupt(self, session=None, context_id="voice"):
        with self.lock:
            row = self._resolve(session, self.context(context_id))
            if row["state"] not in ACTIVE:
                return {"status": "idle", "session": self._view(row), "note": "There is no running task to interrupt."}
            if not row["turn_id"]:
                raise SessionError("The active turn identity is unknown. Check status; do not cancel another session.")
            self._ensure_backend().request("turn/interrupt", {"threadId": row["thread_id"], "turnId": row["turn_id"]})
            row["state"] = "interrupting"
            self._changed(row)
            return {"status": "interrupting", "session": self._view(row), "note": "Cancellation requested; wait for the interrupted event. This does not claim to terminate detached background terminals."}

    def answer(self, request_id, answers, session=None, context_id="voice"):
        with self.lock:
            row = self._resolve(session, self.context(context_id))
            request = self.pending.get(request_id)
            if not request or request["session_id"] != row["session_id"] or request["turn_id"] != row["turn_id"]:
                raise SessionError("That question is stale or belongs to another session.")
            if not isinstance(answers, dict) or set(answers) != set(request["question_ids"]):
                raise SessionError("Answer exactly the question IDs returned in status.")
            values = {key: {"answers": [self.text(value, "answer", 2000)]} for key, value in answers.items()}
            self._ensure_backend().respond(request["rpc_id"], result={"answers": values})
            del self.pending[request_id]
            row["questions"] = [q for q in row["questions"] if q["request_id"] != request_id]
            row["state"] = "waiting_input" if row["questions"] else "working"
            self._changed(row)
            return {"status": "answered", "session": self._view(row)}

    def archive_session(self, session, context_id="voice"):
        with self.lock:
            row = self._resolve(session, self.context(context_id))
            if row["state"] in ACTIVE:
                raise SessionError("Interrupt this session and wait for it to stop before archiving.")
            if row["thread_id"]:
                self._ensure_backend().request("thread/archive", {"threadId": row["thread_id"]})
            sid = row["session_id"]
            self.db.execute("DELETE FROM focus WHERE session=?", (sid,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.db.commit()
            del self.records[sid]
            self.loaded.discard(sid)
            self._emit("notifications/voice/session", row, session={**self._view(row), "state": "archived"})
            return {"status": "archived", "session_id": sid, "note": "Codex history is archived; project files were kept."}

    def updates(self, session=None, detail=False, context_id="voice"):
        context = self.context(context_id)
        if type(detail) is not bool:
            raise SessionError("detail must be boolean.")
        with self.lock:
            row = self._resolve(session, context)
            sid = row["session_id"]
            saved = self.db.execute("SELECT seq FROM cursors WHERE context=? AND session=?", (context, sid)).fetchone()
            seq = saved[0] if saved else 0
            events = list(self.db.execute("SELECT seq,data FROM events WHERE session=? AND seq>? ORDER BY seq LIMIT 5", (sid, seq)))
            result = []
            snapshot = self._view(row)
            for sequence, data in events:
                item = json.loads(data)
                if not detail:
                    item.pop("detail", None)
                else:
                    # A technical report is returned intact up to this explicit
                    # clip, never consumed then destroyed by registry clipping.
                    item["detail"] = item.get("detail", "")[:3000]
                size = lambda values: len(json.dumps({"session": snapshot, "new": True, "updates": values}))
                if size(result + [item]) > 5400:
                    if result:
                        break
                    if "detail" in item:
                        item["detail"] = item["detail"][:max(0, 3000 - (size([item]) - 5400))]
                    if size([item]) > 5400:
                        # Pending questions belong in status; a report can use a
                        # compact identity without losing a consumed event.
                        snapshot = {k: v for k, v in snapshot.items() if k in ("session_id", "name", "state", "turn_id")}
                result.append(item)
                seq = sequence
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?)", (context, sid, seq))
            self.db.commit()
            return {"session": snapshot, "new": bool(result), "updates": result}

    def _find_thread(self, params):
        tid = params.get("threadId")
        return next((r for r in self.records.values() if r["thread_id"] and r["thread_id"] == tid), None)

    def _notification(self, method, params):
        if not isinstance(params, dict):
            return
        with self.lock:
            if self.closed:
                return
            row = self._find_thread(params)
            if row is None:
                return
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = params.get("turnId") or turn.get("id")
            if not turn_id or turn_id != row["turn_id"]:
                return  # late events from a prior turn cannot complete this one
            if method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                kind = item.get("type")
                if kind == "agentMessage" and method == "item/completed":
                    row["last_text"] = str(item.get("text") or "")[:16000]
                elif kind in {"commandExecution", "fileChange", "mcpToolCall"}:
                    key = str(item.get("id", ""))
                    if key not in row["seen"]:
                        row["seen"] = (row["seen"] + [key])[-1000:]
                        counter = {"commandExecution": "commands", "fileChange": "files_edited", "mcpToolCall": "other_tools"}[kind]
                        row["activity"][counter] += 1
                    line = str(item.get("command") or item.get("tool") or kind)
                    self._emit(speech.TRACE_METHOD, row, kind="activity", line=line[:400])
                self._save(row)
            elif method == "turn/completed" and row["state"] in ACTIVE:
                state = turn.get("status")
                if state == "completed":
                    row["state"] = "idle"
                    text = row.get("last_text") or "Codex finished without a text report."
                    spoken, detail = speech.split_reply(text)
                elif state == "interrupted":
                    row["state"] = "interrupted"
                    spoken, detail = "The task was interrupted.", row.get("last_text", "")
                else:
                    row["state"] = "failed"
                    error = turn.get("error") or {}
                    row["error"] = str(error.get("message", "Codex failed before finishing."))[:600] if isinstance(error, dict) else str(error)[:600]
                    spoken, detail = "The task failed: " + row["error"], row.get("last_text", "")
                row["questions"] = []
                self.pending = {key: value for key, value in self.pending.items() if value["session_id"] != row["session_id"]}
                row["last_update"] = spoken[:600]
                event = {"session_id": row["session_id"], "session_name": row["name"], "turn_id": row["turn_id"],
                         "spoken": spoken[:600], "detail": detail[:4500], "is_error": row["state"] == "failed",
                         "state": row["state"], "seconds": round(time.time()-row.get("started_at", time.time()), 1),
                         "activity": row["activity"]}
                self.db.execute("INSERT INTO events(session,data) VALUES(?,?)", (row["session_id"], json.dumps(event)))
                self.db.execute("DELETE FROM events WHERE seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT 500)")
                self._changed(row)
                self._emit(speech.UPDATE_METHOD, row, **event)

    def _request(self, identifier, method, params):
        with self.lock:
            row = self._find_thread(params) if isinstance(params, dict) else None
            if self.closed or not row or params.get("turnId") != row["turn_id"]:
                self.backend.respond(identifier, error={"code": -32602, "message": "Unknown or stale session request"})
                return
            if method != "item/tool/requestUserInput":
                # Current access policies don't ask for approval. Unexpected new
                # requests must not be silently authorized by a voice summary.
                self.backend.respond(identifier, error={"code": -32601, "message": "This voice client does not support that request"})
                return
            questions = params.get("questions")
            if not isinstance(questions, list) or not questions or len(questions) + len(row["questions"]) > 3:
                self.backend.respond(identifier, error={"code": -32602, "message": "Unsupported question shape"})
                return
            request_id = "q_" + uuid.uuid4().hex[:16]
            clean = [{"id": q.get("id"), "question": str(q.get("question", ""))[:300],
                      "options": [str(o.get("label", ""))[:80] for o in (q.get("options") or [])[:4] if isinstance(o, dict)]}
                     for q in questions if isinstance(q, dict)]
            if (len(clean) != len(questions) or
                    any(not isinstance(q["id"], str) or not 1 <= len(q["id"]) <= 80 or not q["question"] for q in clean) or
                    len({q["id"] for q in clean}) != len(clean)):
                self.backend.respond(identifier, error={"code": -32602, "message": "Invalid question IDs or text"})
                return
            self.pending[request_id] = {"session_id": row["session_id"], "turn_id": row["turn_id"], "rpc_id": identifier,
                                        "question_ids": [q["id"] for q in clean]}
            row.update(state="waiting_input", questions=row["questions"] + [{"request_id": request_id, **q} for q in clean])
            self._changed(row)
            self._emit(speech.UPDATE_METHOD, row, spoken="I need your answer. " + " ".join(q["question"] for q in clean),
                       detail="", is_error=False, activity=row["activity"])

    def _disconnected(self, reason):
        with self.lock:
            if self.closed:
                return
            for row in self.records.values():
                if row["state"] in ACTIVE:
                    row.update(state="unknown", error="Codex connection lost; task outcome is unconfirmed.", questions=[])
                    self._changed(row)
            self.pending.clear()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            backend = self.backend
        if backend is not None:
            backend.close()
        with self.lock:
            for row in self.records.values():
                if row["state"] in ACTIVE:
                    row.update(state="interrupted", error="Session manager stopped; send a follow-up to resume this saved thread.", questions=[])
                    self._save(row)
            self.db.close()
            self.lockfile.close()
