"""Voice routing through real MCP/manager processes and a scripted Codex peer.

The scripted model deliberately fabricates conversation IDs. The bridge must
replace them with the browser's routing context before executing any tool.
No real model, GPU, project directory or production session is touched.
"""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / 'tools'
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))
import agent_config
import agent_tools
import voice_chat
from tool_loop_test import Upstream, answered, call, calling

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''


class CodexVoiceLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = {name: self.root / name.lower() for name in ('Backend', 'Tests')}
        for project in self.projects.values():
            project.mkdir()
        self.rpc_log = self.root / 'rpc.jsonl'
        profile = self.root / 'voice-test.config.toml'
        profile.write_text('model = "fake-codex"\nmodel_provider = "fake"\n'
                           '[model_providers.fake]\nname = "Fake"\nbase_url = "http://127.0.0.1:1/v1"\n')
        config = agent_config.load(None)
        config.limits.rounds = 3
        config.mcp = [agent_config.MCPServerConfig(
            name='codex', command=sys.executable,
            args=[str(TOOLS / 'mcp_codex_sessions.py'), '--codex', str(HERE / 'fixtures/fake_codex_app_server.py'),
                  '--profile', 'voice-test', '--codex-home', str(self.root), '--cwd', str(self.root),
                  '--state-dir', str(self.root / 'state')],
            env={'CUDA_VISIBLE_DEVICES': '', 'FAKE_CODEX_RPC_LOG': str(self.rpc_log)}, timeout_seconds=8),
            agent_config.MCPServerConfig(name='workspace', command=sys.executable,
                args=[str(TOOLS / 'mcp_workspace.py'), '--cwd', str(self.root)], timeout_seconds=8)]
        self.registry = agent_tools.Registry.build(config)
        self.assertEqual(len([n for n in self.registry.names() if n.startswith('mcp__codex__')]), 10)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def turn(self, name, arguments, *, context='chat-1', text='Manage the coding session', extra_calls=()):
        self.upstream.requests = []
        self.upstream.started = threading.Event()
        self.upstream.script = [calling([call(name, arguments), *extra_calls]), answered('Acknowledged.')]
        result = voice_chat.turn(f'http://127.0.0.1:{self.upstream.server_port}',
            json.dumps({'context_id': context, 'messages': [{'role': 'user', 'content': text}]}).encode(),
            lambda: False, registry=self.registry, limits=self.registry.config.limits)
        messages = [m for m in self.upstream.requests[-1]['messages'] if m.get('role') == 'tool']
        values = []
        for message in messages:
            try:
                values.append(json.JSONDecoder().raw_decode(message['content'])[0])
            except ValueError:
                values.append(message['content'])
        return result, values

    def codex(self, name, arguments=None, **kwargs):
        return self.turn('mcp__codex__' + name, arguments or {}, **kwargs)

    def create(self, name, context='chat-1'):
        result, values = self.codex('create_session', {'name': name, 'cwd': str(self.projects[name])}, context=context)
        self.assertTrue(result['tools'][0]['ok'], result)
        self.assertEqual(values[0]['session']['cwd'], str(self.projects[name]))
        return values[0]['session']

    def status(self, name, context='chat-1'):
        result, values = self.codex('status', {'session': name}, context=context)
        self.assertTrue(result['tools'][0]['ok'], result)
        return values[0]['session']

    def wait_state(self, name, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.status(name)
            if result['state'] == state:
                return result
            time.sleep(.02)
        self.fail(f'{name} never reached {state}: {result}')

    def test_named_sessions_use_exact_directories_and_context_selections(self):
        backend = self.create('Backend')
        tests = self.create('Tests', context='chat-2')
        self.codex('select_session', {'session': 'Backend'}, context='chat-1')
        first, values = self.codex('send', {'instruction': 'slow backend task'}, context='chat-1')
        self.assertTrue(first['tools'][0]['ok'], first)
        self.assertEqual(values[0]['session']['session_id'], backend['session_id'])
        second, values = self.codex('send', {'instruction': 'slow test task'}, context='chat-2')
        self.assertTrue(second['tools'][0]['ok'], second)
        self.assertEqual(values[0]['session']['session_id'], tests['session_id'])
        self.assertNotEqual(self.status('Backend')['thread_id'], self.status('Tests')['thread_id'])
        logged = [json.loads(line) for line in self.rpc_log.read_text().splitlines()]
        starts = [rpc['params']['cwd'] for rpc in logged if rpc.get('method') == 'thread/start']
        self.assertEqual(starts, [str(self.projects['Backend']), str(self.projects['Tests'])])

    def test_fabricated_model_context_cannot_redirect_selected_session(self):
        backend = self.create('Backend', context='chat-1')
        self.create('Tests', context='chat-2')
        result, values = self.codex('send', {'instruction': 'slow intended backend', 'context_id': 'chat-2'}, context='chat-1')
        self.assertTrue(result['tools'][0]['ok'], result)
        self.assertEqual(values[0]['session']['session_id'], backend['session_id'])
        self.assertEqual(self.status('Tests')['state'], 'idle')
        # A forged selection also must not change the other conversation.
        self.codex('select_session', {'session': 'Tests', 'context_id': 'chat-2'}, context='chat-1')
        _, values = self.codex('list_sessions', context='chat-1')
        self.assertEqual(values[0]['selected_session_id'], self.status('Tests')['session_id'])
        _, values = self.codex('list_sessions', context='chat-2')
        self.assertEqual(values[0]['selected_session_id'], self.status('Tests')['session_id'])

    def test_simple_file_read_uses_workspace_without_starting_codex(self):
        (self.root / 'note.txt').write_text('Direct workspace marker\n')
        result, values = self.turn('mcp__workspace__read_file', {'path': 'note.txt'}, text='Read note.txt')
        self.assertEqual([t['name'] for t in result['tools']], ['mcp__workspace__read_file'])
        self.assertTrue(result['tools'][0]['ok'], result)
        self.assertIn('Direct workspace marker', json.dumps(values))
        self.assertFalse(self.rpc_log.exists(), 'An ordinary file read launched Codex')

    def test_list_and_status_report_actual_manager_state(self):
        backend = self.create('Backend')
        self.codex('send', {'instruction': 'slow backend task'})
        _, values = self.codex('list_sessions')
        self.assertEqual(values[0]['selected_session_id'], backend['session_id'])
        self.assertEqual(values[0]['active'], 1)
        self.assertEqual(values[0]['sessions'][0]['state'], 'working')
        status = self.status('Backend')
        self.assertEqual(status['working_on'], 'slow backend task')
        self.assertTrue(status['turn_id'])
        self.assertTrue(status['thread_id'])

    def test_unknown_session_and_busy_session_errors_reach_model(self):
        result, values = self.codex('send', {'session': 'Imaginary', 'instruction': 'do something'})
        self.assertFalse(result['tools'][0]['ok'])
        self.assertIn('No unique managed Codex session', values[0])
        self.assertFalse(self.rpc_log.exists())
        self.create('Backend')
        self.codex('send', {'instruction': 'slow backend task'})
        result, values = self.codex('send', {'instruction': 'accidental second task'})
        self.assertFalse(result['tools'][0]['ok'])
        self.assertIn('busy', values[0])
        self.assertIn('steer', values[0])
        self.assertEqual(self.status('Backend')['working_on'], 'slow backend task')

    def test_interrupt_cancels_only_named_task_and_never_pauses_microphone(self):
        self.create('Backend')
        self.create('Tests', context='chat-2')
        self.codex('send', {'session': 'Backend', 'instruction': 'slow backend task'})
        self.codex('send', {'session': 'Tests', 'instruction': 'slow test task'}, context='chat-2')
        result, values = self.codex('interrupt', {'session': 'Backend'}, text='Stop the Backend task',
                                   extra_calls=[call('pause_listening', {})])
        self.assertTrue(result['tools'][0]['ok'], result)
        self.assertFalse(result['tools'][1]['ok'], result)
        self.assertEqual(result['controls'], {})
        self.wait_state('Backend', 'interrupted')
        self.assertEqual(self.status('Tests')['state'], 'working')

    def test_steering_preserves_active_turn_identity(self):
        self.create('Backend')
        self.codex('send', {'instruction': 'slow backend task'})
        before = self.status('Backend')
        result, values = self.codex('steer', {'instruction': 'Preserve the existing public API'})
        self.assertTrue(result['tools'][0]['ok'], result)
        self.assertEqual(values[0]['status'], 'steered')
        self.assertEqual(values[0]['session']['turn_id'], before['turn_id'])
        logged = [json.loads(line) for line in self.rpc_log.read_text().splitlines()]
        steers = [rpc['params'] for rpc in logged if rpc.get('method') == 'turn/steer']
        self.assertEqual(steers[0]['expectedTurnId'], before['turn_id'])


if __name__ == '__main__':
    unittest.main()
