"""Browser session controls reach the manager directly and retain context."""
import http.client
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import agent_tools
import speech_ui

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''


class Registry:
    def __init__(self):
        self.calls = []
        self.selected = {}
        self.failure = None
        self.sessions = [{'session_id': 'api', 'name': 'Backend', 'cwd': '/projects/api', 'state': 'working'},
                         {'session_id': 'web', 'name': 'Frontend', 'cwd': '/projects/web', 'state': 'idle'}]

    def names(self):
        return ['mcp__codex__list_sessions', 'mcp__codex__select_session']

    def execute(self, name, arguments, deadline=None):
        self.calls.append((name, arguments, deadline))
        if self.failure is not None:
            return self.failure
        context = arguments['context_id']
        if name.endswith('__select_session'):
            session = next((row for row in self.sessions if arguments['session'] in (row['session_id'], row['name'])), None)
            if session is None:
                return agent_tools.ToolResult(False, '', error='Session does not exist')
            self.selected[context] = session['session_id']
        return agent_tools.ToolResult(True, json.dumps({'sessions': self.sessions,
                                                       'selected_session_id': self.selected.get(context),
                                                       'private': 'not browser data'}))


class CodexRoutesTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.server = speech_ui.SpeechServer(('127.0.0.1', 0), SimpleNamespace(), registry=self.registry)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method='GET', path='/codex/sessions?context_id=chat-1', body=None, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_list_is_direct_and_sanitized_without_model_configuration(self):
        self.registry.sessions[0]['credential'] = 'private'
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(result['enabled'])
        self.assertEqual(result['context_id'], 'chat-1')
        self.assertEqual(len(result['sessions']), 2)
        self.assertNotIn('private', result)
        self.assertNotIn('credential', result['sessions'][0])
        self.assertEqual(self.registry.calls[0][:2], ('mcp__codex__list_sessions', {'context_id': 'chat-1'}))
        self.assertIsInstance(self.registry.calls[0][2], float)

    def test_selection_is_scoped_to_conversation(self):
        status, result = self.request('POST', '/codex/sessions/select', {'context_id': 'chat-1', 'session': 'Backend'})
        self.assertEqual(status, 200)
        self.assertEqual(result['selected_session_id'], 'api')
        self.assertEqual(self.request()[1]['selected_session_id'], 'api')
        self.assertIsNone(self.request(path='/codex/sessions?context_id=chat-2')[1]['selected_session_id'])

    def test_cross_origin_read_and_write_cannot_reach_manager(self):
        for method, path, body in [('GET', '/codex/sessions', None),
                                    ('POST', '/codex/sessions/select', {'session': 'api'})]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method, path, body, 'https://other.example')[0], 403)
        self.assertEqual(self.registry.calls, [])

    def test_invalid_contexts_and_selection_do_not_reach_manager(self):
        for path in ['/codex/sessions?context_id=', '/codex/sessions?context_id=a&context_id=b',
                     '/codex/sessions?context_id=a%00b', '/codex/sessions?context_id=' + 'a' * 81]:
            self.assertEqual(self.request(path=path)[0], 400)
        for body in [[], {'context_id': []}, {'session': ''}, {'session': 'a\n'}, {'session': 'a' * 121}]:
            self.assertEqual(self.request('POST', '/codex/sessions/select', body)[0], 400)
        self.assertEqual(self.registry.calls, [])

    def test_disabled_manager_is_graceful(self):
        self.server.registry = None
        self.assertEqual(self.request(), (200, {'enabled': False, 'sessions': [],
                                               'selected_session_id': None, 'context_id': 'chat-1'}))

    def test_failed_and_malformed_manager_results_are_explicit_errors(self):
        self.registry.failure = agent_tools.ToolResult(False, '', error='Codex disconnected')
        self.assertEqual(self.request(), (503, {'error': 'Codex disconnected'}))
        self.registry.failure = agent_tools.ToolResult(True, '{truncated')
        self.assertEqual(self.request()[0], 502)

    def test_pagination_fetches_all_sessions_in_same_context_and_deadline(self):
        calls = []

        def paged(name, arguments, deadline):
            calls.append((name, arguments, deadline))
            offset = arguments.get('offset', 0)
            return agent_tools.ToolResult(True, json.dumps({
                'sessions': [self.registry.sessions[offset]],
                'selected_session_id': 'api', 'next_offset': 1 if offset == 0 else None}))

        self.registry.execute = paged
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertEqual([s['session_id'] for s in result['sessions']], ['api', 'web'])
        self.assertEqual([c[1] for c in calls], [{'context_id': 'chat-1'}, {'context_id': 'chat-1', 'offset': 1}])
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(result['selected_session_id'], 'api')

    def test_invalid_pagination_is_rejected_without_unbounded_calls(self):
        self.registry.failure = agent_tools.ToolResult(True, json.dumps({
            'sessions': [self.registry.sessions[0]], 'next_offset': 0}))
        self.assertEqual(self.request()[0], 502)
        self.assertEqual(len(self.registry.calls), 1)


if __name__ == '__main__':
    unittest.main()
