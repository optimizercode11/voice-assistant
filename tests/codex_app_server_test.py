#!/usr/bin/env python3
"""Real subprocess coverage of stdio lifecycle and routing (no inference)."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from codex_app_server import AppServer, AppServerError, AppServerRPCError, AppServerTimeout

FIXTURE = ROOT / "tests/fixtures/fake_codex_app_server.py"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def server(self, **kwargs):
        env = {"CUDA_VISIBLE_DEVICES": "", **kwargs.pop("env", {})}
        server = AppServer([sys.executable, str(FIXTURE), "app-server"],
                           self.temp.name, env=env, **kwargs)
        self.addCleanup(server.close)
        return server.start()

    def test_initializes_and_routes_interleaved_responses(self):
        log = Path(self.temp.name) / "rpc.jsonl"
        server = self.server(env={"FAKE_CODEX_RPC_LOG": str(log)})
        with concurrent.futures.ThreadPoolExecutor(8) as executor:
            work = [executor.submit(server.request, "test/echo", {"value": i, "delay": (8-i)*0.01})
                    for i in range(8)]
            self.assertEqual([future.result()["value"] for future in work], list(range(8)))
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([m["method"] for m in messages[:2]], ["initialize", "initialized"])
        self.assertTrue(server.alive)
        self.assertIs(server.start(), server)

    def test_callback_can_issue_rpc_without_reader_deadlock(self):
        done = threading.Event()
        results = []
        def callback(method, params):
            results.append(server.request("test/echo", {"nested": params["value"]}))
            done.set()
        server = self.server(on_notification=callback)
        server.request("test/notify", {"value": 42})
        self.assertTrue(done.wait(2))
        self.assertEqual(results, [{"nested": 42}])

    def test_server_request_answer_and_notification(self):
        done = threading.Event()
        questions = []
        completed = []
        def on_request(request_id, method, params):
            questions.append((method, params))
            server.request("thread/read", {"threadId": params["threadId"]})
            server.respond(request_id, {"answers": {"choice": {"answers": ["Minimal"]}}})
        def on_notification(method, params):
            if method == "turn/completed":
                completed.append(params)
                done.set()
        server = self.server(on_request=on_request, on_notification=on_notification)
        thread = server.request("thread/start", {"cwd": self.temp.name})["thread"]
        server.request("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "question"}]})
        self.assertTrue(done.wait(2))
        self.assertEqual(questions[0][0], "item/tool/requestUserInput")
        self.assertEqual(completed[0]["turn"]["status"], "completed")

    def test_unhandled_server_request_is_not_approved(self):
        done = threading.Event()
        statuses = []
        def on_notification(method, params):
            if method == "turn/completed":
                statuses.append(params["turn"]["status"])
                done.set()
        server = self.server(on_notification=on_notification)
        thread = server.request("thread/start", {})["thread"]
        server.request("turn/start", {"threadId": thread["id"], "input": [{"type": "text", "text": "question"}]})
        self.assertTrue(done.wait(2))
        self.assertEqual(statuses, ["failed"])

    def test_timeout_is_not_replayed_and_late_response_is_ignored(self):
        log = Path(self.temp.name) / "rpc.jsonl"
        server = self.server(env={"FAKE_CODEX_RPC_LOG": str(log)})
        with self.assertRaises(AppServerTimeout):
            server.request("test/echo", {"value": "late", "delay": 0.15}, timeout=0.02)
        time.sleep(0.2)
        self.assertEqual(server.request("test/echo", {"value": "current"}), {"value": "current"})
        sent = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(m.get("params", {}).get("value") == "late" for m in sent), 1)
        self.assertEqual(server._pending, {})

    def test_eof_fails_every_pending_request(self):
        disconnected = threading.Event()
        server = self.server(on_disconnect=lambda reason: disconnected.set())
        with concurrent.futures.ThreadPoolExecutor(3) as executor:
            pending = [executor.submit(server.request, "test/never", {}, timeout=4) for _ in range(2)]
            with self.assertRaises(AppServerError):
                server.request("test/exit", {})
            for future in pending:
                with self.assertRaises(AppServerError):
                    future.result(timeout=1)
        self.assertTrue(disconnected.wait(1))
        self.assertFalse(server.alive)

    def test_rpc_errors_preserve_code(self):
        server = self.server()
        with self.assertRaises(AppServerRPCError) as raised:
            server.request("no/such/method", {})
        self.assertEqual(raised.exception.code, -32601)
        self.assertTrue(server.alive)

    def test_bad_frames_fail_closed(self):
        for method in ("test/malformed", "test/oversize"):
            with self.subTest(method=method):
                server = self.server()
                with self.assertRaises(AppServerError):
                    server.request(method, {})
                self.assertFalse(server.alive)
                server.close()

    def test_stderr_is_drained_but_not_retained(self):
        server = self.server()
        server.request("test/stderr", {})
        until = time.monotonic() + 1
        while not server.stderr_bytes and time.monotonic() < until:
            time.sleep(0.01)
        self.assertGreater(server.stderr_bytes, 200000)
        self.assertFalse(any("sensitive-token" in str(value) for value in vars(server).values()))

    def test_close_is_idempotent_and_reaps_child_group(self):
        server = self.server()
        child_pid = server.request("test/child", {})["pid"]
        server.close()
        server.close()
        self.assertFalse(server.alive)
        self.assertIsNotNone(server.process.returncode)
        self.assertTrue(all(not thread.is_alive() for thread in server._threads))
        proc_stat = Path(f"/proc/{child_pid}/stat")
        self.assertTrue(not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z")
        with self.assertRaises(AppServerError):
            server.request("test/echo", {})

    def test_initialize_failure_reaps_process(self):
        server = AppServer([sys.executable, str(FIXTURE), "app-server"], self.temp.name,
                           env={"CUDA_VISIBLE_DEVICES": "", "FAKE_CODEX_MODE": "init_fail"})
        self.addCleanup(server.close)
        with self.assertRaises(AppServerRPCError):
            server.start()
        self.assertIsNotNone(server.process.returncode)
        self.assertFalse(server.alive)

    def test_initialize_obeys_callers_deadline_and_reaps(self):
        server = AppServer([sys.executable, str(FIXTURE), "app-server"], self.temp.name,
                           env={"CUDA_VISIBLE_DEVICES": "", "FAKE_CODEX_MODE": "init_slow"})
        self.addCleanup(server.close)
        before = time.monotonic()
        with self.assertRaises(AppServerTimeout):
            server.start(timeout=0.04)
        self.assertLess(time.monotonic() - before, 0.45)
        self.assertIsNotNone(server.process.returncode)


if __name__ == "__main__":
    unittest.main()
