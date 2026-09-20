"""Bounded, concurrent stdio transport for Codex App Server (stdlib only).

Notifications and server requests run on a dispatch thread, never the response
reader. No approval is inferred, no timed-out request is retried, and child
stderr is drained without retaining potentially sensitive diagnostics.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import select
import signal
import subprocess
import threading
import time

MAX_FRAME = 4 * 1024 * 1024
MAX_EVENTS = 1024


class AppServerError(Exception):
    pass


class AppServerTimeout(AppServerError):
    pass


class AppServerRPCError(AppServerError):
    def __init__(self, error):
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(str(error.get("message", "App Server request failed")))


class AppServer:
    def __init__(self, argv: list[str], cwd: str, env: dict | None = None,
                 on_notification=None, on_request=None, on_disconnect=None):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_disconnect = on_disconnect
        self.process = None
        self.stderr_bytes = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}
        self._events = queue.Queue(MAX_EVENTS)
        self._threads = []
        self._failure = None
        self._closed = False
        self._started = False

    @property
    def alive(self):
        return (self.process is not None and self.process.poll() is None
                and not self._closed and self._failure is None)

    def start(self, timeout=8):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._lifecycle:
            if self._started:
                if self.alive:
                    return self
                raise AppServerError("App Server instance cannot be restarted")
            if self._closed:
                raise AppServerError("App Server is closed")
            self._started = True
            child_env = os.environ.copy()
            if self.env is not None:
                child_env.update(self.env)
            try:
                self.process = subprocess.Popen(
                    self.argv, cwd=self.cwd, env=child_env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
                os.set_blocking(self.process.stdin.fileno(), False)
                for target, name in [(self._dispatch, "dispatch"),
                                     (self._read, "reader"),
                                     (self._drain_stderr, "stderr")]:
                    thread = threading.Thread(target=target, name=f"codex-{name}", daemon=True)
                    self._threads.append(thread)
                    thread.start()
                self.request("initialize", {
                    "clientInfo": {"name": "voice-assistant", "version": "1.0"},
                    "capabilities": {"experimentalApi": True}}, timeout=max(0.001, deadline-time.monotonic()))
                self._send({"method": "initialized", "params": {}}, deadline)
            except Exception as exc:
                self.close()
                if isinstance(exc, OSError):
                    raise AppServerError("Could not start Codex App Server") from exc
                raise
            return self

    def request(self, method, params, timeout=8):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        waiter = {"event": threading.Event()}
        with self._lock:
            if not self.alive:
                raise AppServerError(self._failure or "App Server is not running")
            request_id = next(self._ids)
            self._pending[request_id] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": params}, deadline)
            if not waiter["event"].wait(max(0, deadline - time.monotonic())):
                raise AppServerTimeout(f"App Server {method} timed out; outcome is unknown")
            if "failure" in waiter:
                raise AppServerError(waiter["failure"])
            message = waiter["message"]
            if "error" in message:
                raise AppServerRPCError(message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError("App Server response result is not an object")
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def respond(self, request_id, result=None, error=None):
        message = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = {} if result is None else result
        self._send(message, time.monotonic() + 8)

    def _send(self, message, deadline):
        wire = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(wire) > MAX_FRAME:
            raise AppServerError("App Server outgoing frame exceeds size limit")
        if not self._write_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise AppServerTimeout("App Server write timed out; outcome is unknown")
        try:
            if not self.alive:
                raise AppServerError(self._failure or "App Server is not running")
            fd = self.process.stdin.fileno()
            sent = 0
            while sent < len(wire):
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([], [fd], [], max(0, left))[1]:
                    # A partially written JSON frame cannot be reused safely.
                    if sent:
                        self._fail("App Server partial write timed out")
                    raise AppServerTimeout("App Server write timed out; outcome is unknown")
                try:
                    sent += os.write(fd, wire[sent:])
                except BlockingIOError:
                    continue
        except (OSError, ValueError) as exc:
            self._fail("App Server write failed")
            raise AppServerError("App Server write failed") from exc
        finally:
            self._write_lock.release()

    def _read(self):
        stream = None
        try:
            # A buffered wrapper avoids byte-at-a-time FileIO.readline reads.
            import io
            stream = io.BufferedReader(self.process.stdout)
            while not self._closed:
                line = stream.readline(MAX_FRAME + 1)
                if not line:
                    self._fail("App Server disconnected")
                    return
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise AppServerError("App Server incoming frame exceeds size limit")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    raise AppServerError("App Server sent invalid JSON") from None
                if not isinstance(message, dict):
                    raise AppServerError("App Server sent a non-object frame")
                if "method" in message:
                    if not isinstance(message["method"], str) or not isinstance(message.get("params", {}), dict):
                        raise AppServerError("App Server sent an invalid method frame")
                    event = ("request", message) if "id" in message else ("notification", message)
                    try:
                        self._events.put_nowait(event)
                    except queue.Full:
                        raise AppServerError("App Server event queue exceeded capacity") from None
                elif "id" in message:
                    request_id = message["id"]
                    if not isinstance(request_id, (str, int)):
                        raise AppServerError("App Server sent an invalid response id")
                    if "error" in message and not isinstance(message["error"], dict):
                        raise AppServerError("App Server sent an invalid error frame")
                    with self._lock:
                        waiter = self._pending.get(request_id)
                        if waiter is not None:
                            waiter["message"] = message
                            waiter["event"].set()
                else:
                    raise AppServerError("App Server frame has no method or id")
        except AppServerError as exc:
            self._fail(str(exc))
        except (OSError, ValueError):
            self._fail("App Server reader stopped")
        finally:
            if stream is not None:
                stream.close()

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.process.stderr.read(8192)
                if not chunk:
                    return
                self.stderr_bytes += len(chunk)
        except (OSError, ValueError):
            pass

    def _dispatch(self):
        while True:
            try:
                kind, message = self._events.get(timeout=0.1)
            except queue.Empty:
                if self._failure is not None or self._closed:
                    break
                continue
            try:
                if kind == "request":
                    if self.on_request:
                        self.on_request(message["id"], message["method"], message.get("params", {}))
                    else:
                        self.respond(message["id"], error={"code": -32601, "message": "Interactive request has no handler"})
                elif self.on_notification:
                    self.on_notification(message["method"], message.get("params", {}))
            except Exception:
                # Application callbacks cannot kill response routing. In
                # particular, an exception must never become an approval.
                pass
        if self.on_disconnect:
            try:
                self.on_disconnect(self._failure or "App Server closed")
            except Exception:
                pass

    def _fail(self, reason):
        with self._lock:
            if self._failure is not None:
                return
            self._failure = reason
            for waiter in self._pending.values():
                waiter["failure"] = reason
                waiter["event"].set()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._fail("App Server closed")
        process = self.process
        if process is not None:
            # Descendants may outlive the parent, so signal the owned group
            # even when poll() already reports the parent's exit.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
