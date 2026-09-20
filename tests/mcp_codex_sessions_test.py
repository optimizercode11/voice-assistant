#!/usr/bin/env python3
"""Exercise the advertised named-session tools through their MCP subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from mcp_client import MCPServer

FIXTURE = ROOT / "tests/fixtures/fake_codex_app_server.py"
SERVER = ROOT / "tools/mcp_codex_sessions.py"
EXPECTED = {"create_session", "list_sessions", "select_session", "send", "status", "steer",
            "interrupt", "answer", "updates", "archive_session"}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "test.config.toml"
        self.profile.write_text('model = "test-model"\nmodel_provider = "test-local"\n'
                                '[model_providers.test-local]\nname = "Test local"\n'
                                'base_url = "http://127.0.0.1:1/v1"\nwire_api = "responses"\n')
        self.argv = [str(SERVER), "--codex", str(FIXTURE), "--profile", "test",
                     "--codex-home", str(self.root), "--cwd", str(self.root),
                     "--state-dir", str(self.root / "state"), "--push"]
        self.env = {"CUDA_VISIBLE_DEVICES": "", "FAKE_CODEX_STATE": str(self.root / "fake-state.json"),
                    "FAKE_CODEX_RPC_LOG": str(self.root / "rpc.jsonl")}
        self.notifications = []

    def start(self):
        server = MCPServer("codex_sessions", sys.executable, self.argv, env=self.env, timeout=4)
        server.on_notification = lambda owner, method, params: self.notifications.append((method, params))
        self.addCleanup(server.stop)
        server.start()
        self.server = server
        return server

    def call(self, name, arguments, error=False):
        response = self.server.request("tools/call", {"name": name, "arguments": arguments})
        self.assertEqual(bool(response.get("isError")), error, response)
        return json.loads(response["content"][0]["text"])

    def wait_state(self, session, expected):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.call("status", {"session": session})["session"]
            if state["state"] == expected:
                return state
            time.sleep(0.01)
        self.fail(f"Expected {session} state {expected}; got {state}")

    def test_advertises_tools_and_read_only_hints(self):
        server = self.start()
        listed = server.request("tools/list", {})["tools"]
        self.assertEqual({tool["name"] for tool in listed}, EXPECTED)
        self.assertEqual({tool["name"] for tool in listed if tool["annotations"]["readOnlyHint"]},
                         {"list_sessions", "status"})
        self.assertEqual({tool.name for tool in server.tools if tool.read_only}, {"list_sessions", "status"})
        for tool in listed:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertFalse((self.root / "rpc.jsonl").exists(), "Listing tools must not start Codex")

    def test_invalid_tool_arguments_return_errors_and_server_survives(self):
        self.start()
        cases = [("create_session", {}), ("create_session", {"name": 123}),
                 ("create_session", {"name": "Backend", "unexpected": True}),
                 ("create_session", {"name": "Backend", "context_id": "bad/context"}),
                 ("list_sessions", {"limit": True}), ("list_sessions", {"offset": -1}),
                 ("list_sessions", {"limit": 33}), ("updates", {"detail": "yes"}),
                 ("send", {"instruction": ""}), ("send", {"instruction": "x" * 4001}),
                 ("answer", {"session": "Backend", "request_id": "q", "answers": {"id": 3}}),
                 ("select_session", {"session": None}), ("not_a_tool", {}), ("send", [])]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=str(arguments)[:100]):
                result = self.call(name, arguments, error=True)
                self.assertIn("error", result)
        self.assertEqual(self.call("list_sessions", {})["total"], 0)

    def test_malformed_call_params_cannot_kill_manager(self):
        self.start()
        for params in ["bad", ["bad"], 10]:
            with self.subTest(params=params):
                result = self.server.request("tools/call", params)
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(self.call("list_sessions", {})["total"], 0)

    def test_voice_management_workflow_uses_named_tools(self):
        self.start()
        backend = self.call("create_session", {"name": "Backend", "context_id": "browser_a"})["session"]
        frontend = self.call("create_session", {"name": "Frontend", "context_id": "browser_b"})["session"]
        self.call("send", {"instruction": "slow API task", "context_id": "browser_a"})
        self.call("send", {"instruction": "slow frontend task", "context_id": "browser_b"})
        self.call("select_session", {"session": "Backend", "context_id": "browser_b"})
        steered = self.call("steer", {"instruction": "Preserve the API", "context_id": "browser_b"})
        self.assertEqual(steered["session"]["session_id"], backend["session_id"])
        self.call("interrupt", {"session": "Backend", "context_id": "browser_b"})
        self.wait_state("Backend", "interrupted")
        self.assertEqual(self.call("status", {"session": "Frontend"})["session"]["state"], "working")
        self.call("send", {"session": "Backend", "instruction": "Follow-up task"})
        self.wait_state("Backend", "idle")
        update = self.call("updates", {"session": "Backend"})
        self.assertTrue(update["new"])
        self.assertTrue(all(item["session_id"] == backend["session_id"] for item in update["updates"]))
        self.assertNotEqual(backend["cwd"], frontend["cwd"])
        completed = [params for _, params in self.notifications if params.get("spoken") and params.get("state") == "idle"]
        self.assertTrue(completed)
        self.assertEqual(completed[-1]["session_id"], backend["session_id"])
        self.call("archive_session", {"session": "Backend"})
        self.assertEqual(self.call("list_sessions", {})["total"], 1)

    def test_question_round_trip_through_tools(self):
        self.start()
        self.call("create_session", {"name": "Backend"})
        self.call("send", {"instruction": "question"})
        waiting = self.wait_state("Backend", "waiting_input")
        question = waiting["questions"][0]
        result = self.call("answer", {"session": "Backend", "request_id": question["request_id"],
                                       "answers": {question["id"]: "Minimal"}})
        self.assertEqual(result["status"], "answered")
        self.wait_state("Backend", "idle")
        self.assertIn("Applied your answer", self.call("updates", {})["updates"][0]["spoken"])

    def test_doctor_is_cpu_only_does_not_create_manager_or_start_codex(self):
        result = subprocess.run([sys.executable, *self.argv, "--doctor"], env={**os.environ, **self.env},
                                capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["model_provider"], "test-local")
        self.assertEqual(info["profile"], "test")
        self.assertEqual(info["max_active"], 3)
        self.assertEqual(set(info["tools"]), EXPECTED)
        self.assertFalse((self.root / "rpc.jsonl").exists())
        self.assertFalse((self.root / "state").exists())

    def test_doctor_rejects_excess_workers_and_missing_workspace(self):
        for args in [["--max-active", "4"], ["--cwd", str(self.root / "missing")]]:
            result = subprocess.run([sys.executable, *self.argv, "--doctor", *args],
                                    env={**os.environ, **self.env}, capture_output=True, text=True, timeout=4)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
