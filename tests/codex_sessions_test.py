#!/usr/bin/env python3
"""Named-session acceptance against a real, CPU-only fake App Server process."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from codex_sessions import Manager, SessionError

FIXTURE = ROOT / "tests/fixtures/fake_codex_app_server.py"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "rpc.jsonl"
        self.state = self.root / "backend.json"
        self.environment = patch.dict(os.environ, {
            "CUDA_VISIBLE_DEVICES": "", "FAKE_CODEX_RPC_LOG": str(self.log),
            "FAKE_CODEX_STATE": str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.notifications = []
        self.manager = self.make_manager()

    def make_manager(self, **kwargs):
        manager = Manager(self.root / "state", self.root,
                          [sys.executable, str(FIXTURE), "app-server"],
                          model=kwargs.pop("model", "test-model"),
                          provider=kwargs.pop("provider", "test-local"),
                          profile=kwargs.pop("profile", "test-profile"),
                          notify=lambda method, params: self.notifications.append((method, params)), **kwargs)
        self.addCleanup(manager.close)
        return manager

    def create(self, name, **kwargs):
        return self.manager.create_session(name, **kwargs)["session"]

    def wait_state(self, name, state, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.manager.status(name)["session"]
            if result["state"] == state:
                return result
            time.sleep(0.01)
        self.fail(f"{name}: wanted {state}, got {self.manager.status(name)}")

    def rpc(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_create_select_and_contexts_are_isolated(self):
        backend = self.create("Backend", context_id="browser_a")
        frontend = self.create("Frontend", context_id="browser_b")
        self.assertNotEqual(backend["cwd"], frontend["cwd"])
        self.assertTrue(Path(backend["cwd"]).is_dir())
        self.assertEqual(self.manager.status(context_id="browser_a")["session"]["session_id"], backend["session_id"])
        self.assertEqual(self.manager.status(context_id="browser_b")["session"]["session_id"], frontend["session_id"])
        self.manager.select_session("backend", context_id="browser_b")
        self.assertEqual(self.manager.status(context_id="browser_b")["session"]["name"], "Backend")
        self.assertIsNone(self.manager.backend, "Creating/selecting sessions must not run inference")

    def test_missing_focus_and_inexact_names_rejected(self):
        self.create("Backend", context_id="one")
        self.create("Backend tests", context_id="two")
        for operation in (lambda: self.manager.send("fix it", context_id="empty"),
                          lambda: self.manager.select_session("Back"),
                          lambda: self.manager.create_session("BACKEND")):
            with self.assertRaises(SessionError):
                operation()

    def test_persistence_resumes_same_thread_and_profile(self):
        original = self.create("Backend", context_id="browser_a")
        first = self.manager.send("first task", "Backend")["session"]
        self.wait_state("Backend", "idle")
        self.manager.close()
        self.manager = self.make_manager()
        restored = self.manager.status(context_id="browser_a")["session"]
        self.assertEqual(restored["session_id"], original["session_id"])
        self.assertEqual(restored["thread_id"], first["thread_id"])
        self.assertEqual(restored["profile"], "test-profile")
        second = self.manager.send("second task", "Backend")["session"]
        self.assertEqual(second["thread_id"], first["thread_id"])
        self.assertNotEqual(second["turn_id"], first["turn_id"])
        self.wait_state("Backend", "idle")
        calls = self.rpc()
        self.assertEqual(sum(c.get("method") == "thread/start" for c in calls), 1)
        resumed = [c["params"] for c in calls if c.get("method") == "thread/resume"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["modelProvider"], "test-local")
        self.assertEqual(resumed[0]["model"], "test-model")
        self.assertEqual(resumed[0]["threadId"], first["thread_id"])

    def test_profile_change_cannot_silently_resume_old_thread(self):
        self.create("Backend")
        self.manager.send("first task", "Backend")
        self.wait_state("Backend", "idle")
        self.manager.close()
        self.manager = self.make_manager(model="different-model", provider="different-provider", profile="other-profile")
        before = sum(c.get("method") == "turn/start" for c in self.rpc())
        with self.assertRaises(SessionError):
            self.manager.send("second task", "Backend")
        self.assertEqual(sum(c.get("method") == "turn/start" for c in self.rpc()), before)

    def test_three_workers_parallel_and_fourth_rejected(self):
        for name in ["Backend", "Frontend", "Tests", "Docs"]:
            self.create(name)
        with concurrent.futures.ThreadPoolExecutor(3) as executor:
            futures = [executor.submit(self.manager.send, "slow task", name)
                       for name in ["Backend", "Frontend", "Tests"]]
            self.assertEqual([f.result()["status"] for f in futures], ["working"] * 3)
        self.assertEqual(self.manager.list_sessions()["active"], 3)
        with self.assertRaisesRegex(SessionError, "slots"):
            self.manager.send("slow task", "Docs")
        calls = [c for c in self.rpc() if c.get("method") == "thread/start"]
        self.assertTrue(all(c["params"]["config"]["features.multi_agent"] is False for c in calls))
        self.assertTrue(all(c["params"]["config"]["agents.max_threads"] == 1 for c in calls))

    def test_same_parent_and_symlink_directory_overlap_rejected(self):
        project = self.root / "project"
        child = project / "subdirectory"
        child.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(project, target_is_directory=True)
        self.create("Parent", cwd=str(project))
        self.create("Child", cwd=str(child))
        self.create("Alias", cwd=str(alias))
        self.manager.send("slow task", "Parent")
        for name in ["Child", "Alias"]:
            with self.assertRaisesRegex(SessionError, "overlapping"):
                self.manager.send("slow task", name)

    def test_busy_send_rejected_steer_target_and_interrupt_isolated(self):
        self.create("Backend")
        self.create("Frontend")
        backend = self.manager.send("slow task", "Backend")["session"]
        frontend = self.manager.send("slow task", "Frontend")["session"]
        with self.assertRaisesRegex(SessionError, "busy"):
            self.manager.send("new task", "Backend")
        self.manager.steer("preserve API", "Backend")
        steer = next(c["params"] for c in self.rpc() if c.get("method") == "turn/steer")
        self.assertEqual((steer["threadId"], steer["expectedTurnId"]), (backend["thread_id"], backend["turn_id"]))
        self.assertEqual(steer["input"][0]["text"], "preserve API")
        self.manager.interrupt("Backend")
        self.wait_state("Backend", "interrupted")
        remaining = self.manager.status("Frontend")["session"]
        self.assertEqual((remaining["state"], remaining["turn_id"]), ("working", frontend["turn_id"]))
        with self.assertRaises(SessionError):
            self.manager.steer("too late", "Backend")

    def test_stale_completion_cannot_finish_new_turn(self):
        self.create("Backend")
        first = self.manager.send("first task", "Backend")["session"]
        self.wait_state("Backend", "idle")
        second = self.manager.send("slow second task", "Backend")["session"]
        self.manager._notification("turn/completed", {"threadId": first["thread_id"],
                                  "turn": {"id": first["turn_id"], "status": "completed"}})
        current = self.manager.status("Backend")["session"]
        self.assertEqual((current["state"], current["turn_id"]), ("working", second["turn_id"]))

    def test_failed_task_is_not_completed_and_update_has_identity(self):
        row = self.create("Backend", context_id="browser_a")
        self.manager.send("fail task", "Backend", context_id="browser_a")
        failed = self.wait_state("Backend", "failed")
        self.assertTrue(failed["requires_attention"])
        updates = self.manager.updates("Backend", context_id="browser_a")
        event = updates["updates"][0]
        self.assertTrue(event["is_error"])
        self.assertEqual(event["state"], "failed")
        self.assertEqual(event["session_id"], row["session_id"])
        self.assertEqual(event["session_name"], "Backend")
        self.assertEqual(event["turn_id"], failed["turn_id"])
        self.assertIn("failed", event["spoken"])
        self.assertFalse(self.manager.updates("Backend", context_id="browser_a")["new"])
        self.assertTrue(self.manager.updates("Backend", context_id="browser_b")["new"])
        events = [params for _, params in self.notifications if params.get("spoken")]
        self.assertEqual(events[-1]["context_id"], "browser_a")

    def test_question_answer_requires_exact_session_request_and_question(self):
        self.create("Backend")
        self.create("Frontend")
        self.manager.send("question", "Backend")
        self.manager.send("question", "Frontend")
        backend = self.wait_state("Backend", "waiting_input")
        frontend = self.wait_state("Frontend", "waiting_input")
        question = backend["questions"][0]
        for session, request, answer in [("Frontend", question["request_id"], {"choice": "Minimal"}),
                                         ("Backend", "stale", {"choice": "Minimal"}),
                                         ("Backend", question["request_id"], {"wrong": "Minimal"})]:
            with self.assertRaises(SessionError):
                self.manager.answer(request, answer, session)
        self.manager.answer(question["request_id"], {"choice": "Minimal"}, "Backend")
        self.wait_state("Backend", "idle")
        self.assertEqual(self.manager.status("Frontend")["session"]["questions"], frontend["questions"])
        with self.assertRaises(SessionError):
            self.manager.answer(question["request_id"], {"choice": "Minimal"}, "Backend")

    def test_restarted_active_task_is_interrupted_not_complete(self):
        self.create("Backend")
        before = self.manager.send("slow task", "Backend")["session"]
        self.manager.close()
        self.manager = self.make_manager()
        restored = self.manager.status("Backend")["session"]
        self.assertEqual(restored["state"], "interrupted")
        self.assertEqual(restored["thread_id"], before["thread_id"])
        self.assertFalse(self.manager.updates("Backend")["new"])

    def test_answering_one_request_preserves_other_pending_questions(self):
        self.create("Backend")
        self.manager.send("multiquestion", "Backend")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            waiting = self.manager.status("Backend")["session"]
            if len(waiting["questions"]) == 2:
                break
            time.sleep(0.01)
        self.assertEqual(len(waiting["questions"]), 2)
        first, second = waiting["questions"]
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.manager.answer(first["request_id"], {first["id"]: "Minimal"}, "Backend")
        remaining = self.manager.status("Backend")["session"]
        self.assertEqual(remaining["state"], "waiting_input")
        self.assertEqual(remaining["questions"], [second])
        self.manager.answer(second["request_id"], {second["id"]: "All relevant tests"}, "Backend")
        self.wait_state("Backend", "idle")
        self.assertEqual(self.manager.status("Backend")["session"]["questions"], [])

    def test_large_update_pages_do_not_consume_unreturned_reports(self):
        self.create("Backend")
        identities = []
        for index in range(3):
            row = self.manager.send("slow task " + str(index), "Backend")["session"]
            identities.append(row["turn_id"])
            self.manager._notification("item/completed", {
                "threadId": row["thread_id"], "turnId": row["turn_id"],
                "item": {"type": "agentMessage", "id": "large-" + str(index),
                         "text": "Long report details. " * 250 + "\nSPOKEN: Report " + str(index) + "."}})
            self.manager._notification("turn/completed", {
                "threadId": row["thread_id"], "turn": {"id": row["turn_id"], "status": "completed"}})
        received = []
        for _ in range(4):
            page = self.manager.updates("Backend", detail=True)
            self.assertLessEqual(len(json.dumps(page)), 5400)
            if page["new"]:
                self.assertEqual(len(page["updates"]), 1)
                self.assertEqual(len(page["updates"][0]["detail"]), 3000)
            received.extend(event["turn_id"] for event in page["updates"])
            if not page["new"]:
                break
        self.assertEqual(received, identities)
        self.assertFalse(self.manager.updates("Backend", detail=True)["new"])

    def test_state_directory_has_one_owner(self):
        with self.assertRaisesRegex(SessionError, "Another session manager"):
            self.make_manager()

    def test_archive_preserves_files_removes_focus_and_keeps_history(self):
        created = self.create("Backend")
        marker = Path(created["cwd"]) / "keep.txt"
        marker.write_text("keep")
        started = self.manager.send("first task", "Backend")["session"]
        with self.assertRaises(SessionError):
            self.manager.archive_session("Backend")
        self.wait_state("Backend", "idle")
        self.manager.archive_session("Backend")
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(self.manager.list_sessions()["total"], 0)
        self.assertIsNone(self.manager.list_sessions()["selected_session_id"])
        self.assertTrue(json.loads(self.state.read_text())[started["thread_id"]]["archived"])


if __name__ == "__main__":
    unittest.main()
