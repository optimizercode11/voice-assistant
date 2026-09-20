"""Stop only the managed fake Claude, discard work, and restart only on send."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_claude_test import FAKE, HERE, parse, server, wait_idle
import mcp_claude

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError('condition did not become true')


def running(pid):
    row = mcp_claude.Session._processes().get(pid)
    return row is not None and row[0] != 'Z'


class StopWireTests(unittest.TestCase):
    def setUp(self):
        self.peer = server(args=['--push'])
        self.notifications = []
        self.peer.on_notification = lambda name, method, params: self.notifications.append((method, params))
        self.tools = {tool.name: tool for tool in self.peer.start()}

    def tearDown(self):
        self.peer.stop()

    def call(self, tool, args=None):
        text, error = self.peer.call(self.tools[tool], args or {})
        self.assertFalse(error, text)
        return parse(text)

    def test_stop_discards_queue_keeps_mcp_available_and_never_replays(self):
        self.call('send', {'instruction': 'slow 30'})
        self.call('send', {'instruction': 'must never run'})
        self.call('send', {'instruction': 'must never run either'})
        child_pid = int(subprocess.check_output(['pgrep', '-P', str(self.peer.process.pid)]).strip())
        stopped = self.call('stop')
        self.assertEqual(stopped['status'], 'stopped')
        self.assertTrue(stopped['cancelled_work'])
        self.assertEqual(stopped['discarded_queued'], 2)
        self.assertFalse(running(child_pid))
        for _ in range(3):
            state = self.call('updates')
            self.assertEqual(state['state'], 'stopped')
            self.assertFalse(state['new'])
            self.assertNotIn('queued', state)
            time.sleep(0.05)
        self.assertEqual(self.peer.status()['state'], 'ready')
        self.assertFalse(any(method == mcp_claude.UPDATE_METHOD for method, _ in self.notifications))
        self.assertEqual(self.call('stop')['discarded_queued'], 0)
        new = self.call('send', {'instruction': 'fresh work'})
        self.assertTrue(new['new_session'])
        wait_until(lambda: any(method == mcp_claude.UPDATE_METHOD for method, _ in self.notifications))
        results = [row for method, row in self.notifications if method == mcp_claude.UPDATE_METHOD]
        self.assertEqual([row['instruction'] for row in results], ['fresh work'])

    def test_stop_without_child_and_idle_child_is_idempotent(self):
        for _ in range(2):
            result = self.call('stop')
            self.assertEqual(result['state'], 'stopped')
            self.assertFalse(result['cancelled_work'])
        self.call('send', {'instruction': 'short turn'})
        wait_until(lambda: self.call('updates')['state'] == 'idle')
        result = self.call('stop')
        self.assertFalse(result['cancelled_work'])
        self.assertEqual(result['state'], 'stopped')

    def test_owned_normal_and_detached_children_stop_unrelated_process_survives(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     start_new_session=True)
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.terminate())
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'owned.json'
                self.call('send', {'instruction': f'children {path}'})
                wait_until(lambda: path.exists() and path.stat().st_size > 0)
                owned = json.loads(path.read_text())
                self.assertTrue(all(running(pid) for pid in owned))
                result = self.call('stop')
                self.assertEqual(result['state'], 'stopped')
                self.assertTrue(all(not running(pid) for pid in owned), owned)
                self.assertIsNone(unrelated.poll(), 'stop must never kill unrelated processes')
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_term_resistant_child_is_stopped_after_grace_period(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pid'
            self.call('send', {'instruction': f'ignoreterm {path}'})
            wait_until(lambda: path.exists() and path.stat().st_size > 0)
            pid = int(path.read_text())
            started = time.monotonic()
            self.assertEqual(self.call('stop')['status'], 'stopped')
            self.assertGreaterEqual(time.monotonic() - started, 1.9, 'TERM gets a real grace period')
            self.assertFalse(running(pid))


class StaleReaderTests(unittest.TestCase):
    def test_old_events_cannot_mutate_or_dispatch_new_session(self):
        events, traces = [], []
        session = mcp_claude.Session(FAKE, str(HERE), [], 1000, lambda text: None,
                                   notify=lambda row, maximum: events.append(row),
                                   trace=lambda kind, text: traces.append((kind, text)))
        # Disable wire-only working notice, which otherwise prints on stdout.
        original = mcp_claude.notify_working
        mcp_claude.notify_working = lambda instruction: None
        self.addCleanup(setattr, mcp_claude, 'notify_working', original)
        self.addCleanup(session.stop)
        session.send('slow 30')
        old = session.process
        session.stop()
        session.send('slow 30')
        current = session.process
        wait_until(lambda: session.activity['commands'] == 1)
        before = (session.seq, session.session_id, dict(session.activity), len(traces), len(events))
        session._on_event({'type': 'system', 'subtype': 'init', 'session_id': 'stale'}, old)
        session._on_event({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Edit', 'input': {}}]}}, old)
        session._on_event({'type': 'result', 'subtype': 'success', 'result': 'SPOKEN: stale success'}, old)
        self.assertEqual(before, (session.seq, session.session_id, dict(session.activity), len(traces), len(events)))
        self.assertIs(session.process, current)
        self.assertEqual(session.in_flight, 'slow 30')


if __name__ == '__main__':
    unittest.main()
