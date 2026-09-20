"""Exercise direct workspace tools through their deployed stdio MCP boundary."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import mcp_client

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

SERVER = str(Path(__file__).resolve().parents[1] / 'tools' / 'mcp_workspace.py')


class WorkspaceMCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mcp-workspace-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cwd = self.root / 'workspace'
        self.cwd.mkdir()
        self.server, self.tools = self.start_server()

    def start_server(self, *options, env=None):
        server = mcp_client.MCPServer(
            'workspace', sys.executable, [SERVER, '--cwd', str(self.cwd), *options],
            {'CUDA_VISIBLE_DEVICES': '', **(env or {})}, 12.0, [], [])
        self.addCleanup(server.stop)
        tools = {tool.name: tool for tool in server.start()}
        return server, tools

    def call(self, name, arguments, *, error=False, server=None, tools=None, cap=4000):
        server, tools = server or self.server, tools or self.tools
        raw, is_error = server.call(tools[name], arguments)
        self.assertEqual(is_error, error, raw)
        self.assertLessEqual(len(raw), cap, 'the complete JSON text must fit the output cap')
        payload = json.loads(raw)
        self.assertIsInstance(payload, dict)
        return payload

    def test_discovery_marks_mutating_tools(self):
        self.assertEqual(set(self.tools), {
            'read_file', 'write_file', 'make_directory', 'list_directory', 'run_shell'})
        rows = self.server.request('tools/list', {})['tools']
        for row in rows:
            with self.subTest(tool=row['name']):
                self.assertEqual(row['annotations']['readOnlyHint'],
                                 row['name'] in ('read_file', 'list_directory'))

    def test_create_write_read_append_list_and_overwrite(self):
        self.call('make_directory', {'path': 'nested/deep'})
        path = 'nested/deep/notes.txt'
        self.call('write_file', {'path': path, 'content': 'alpha\nbeta\n'})
        self.assertEqual(self.call('read_file', {'path': path})['text'], 'alpha\nbeta\n')
        self.call('write_file', {'path': path, 'content': 'omega\n', 'append': True})
        self.assertEqual((self.cwd / path).read_text(), 'alpha\nbeta\nomega\n')
        page = self.call('read_file', {'path': path, 'offset': 2, 'limit': 1})
        self.assertEqual(page['text'], 'beta\n')
        entries = self.call('list_directory', {'path': 'nested/deep'})['entries']
        self.assertIn({'name': 'notes.txt', 'type': 'file'}, entries)
        self.call('write_file', {'path': path, 'content': 'replacement'})
        self.assertEqual((self.cwd / path).read_text(), 'replacement')

    def test_paths_and_content_are_literal_unicode(self):
        name = "héllo 'quoted'; $(touch injected).txt"
        content = 'café ☃ 漢字\n"quoted" \\ slash\t tab\n$(touch injected)'
        self.call('write_file', {'path': name, 'content': content})
        self.assertEqual(self.call('read_file', {'path': name})['text'], content)
        self.assertEqual((self.cwd / name).read_text(), content)
        self.assertFalse((self.cwd / 'injected').exists())

    def test_absolute_parent_and_home_paths_are_supported(self):
        outside = self.root / 'outside.txt'
        self.call('write_file', {'path': str(outside), 'content': 'outside cwd'})
        self.assertEqual(self.call('read_file', {'path': '../outside.txt'})['text'], 'outside cwd')
        server, tools = self.start_server(env={'HOME': str(self.root)})
        self.assertEqual(self.call('read_file', {'path': '~/outside.txt'},
                                   server=server, tools=tools)['text'], 'outside cwd')

    def test_directory_parents_flag_is_observed(self):
        self.call('make_directory', {'path': 'missing/child', 'parents': False}, error=True)
        self.assertFalse((self.cwd / 'missing').exists())
        self.call('make_directory', {'path': 'single', 'parents': False})
        self.assertTrue((self.cwd / 'single').is_dir())
        self.assertIn('single', [entry['name'] for entry in self.call('list_directory', {})['entries']])

    def test_shell_reports_streams_exit_status_and_cwd(self):
        result = self.call('run_shell', {'command': 'pwd; printf stdout; printf stderr >&2'})
        self.assertEqual(result['stdout'], str(self.cwd) + '\nstdout')
        self.assertEqual(result['stderr'], 'stderr')
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['timed_out'])
        self.assertFalse(result['truncated'])
        (self.cwd / 'sub').mkdir()
        result = self.call('run_shell', {'command': 'pwd', 'cwd': 'sub'})
        self.assertEqual(result['stdout'].strip(), str(self.cwd / 'sub'))
        result = self.call('run_shell', {'command': 'pwd', 'cwd': str(self.root)})
        self.assertEqual(result['stdout'].strip(), str(self.root))
        self.assertEqual(self.call('run_shell', {'command': 'pwd'})['stdout'].strip(), str(self.cwd))

    def test_nonzero_shell_exit_preserves_diagnostics(self):
        result = self.call('run_shell', {'command': 'printf partial; printf problem >&2; exit 7'},
                           error=True)
        self.assertEqual(result['stdout'], 'partial')
        self.assertEqual(result['stderr'], 'problem')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['timed_out'])

    def test_timeout_kills_background_descendants_before_delayed_write(self):
        marker = self.cwd / 'survived-timeout'
        command = '(sleep 2; printf survived > survived-timeout) & printf started; wait'
        started = time.monotonic()
        result = self.call('run_shell', {'command': command, 'timeout_seconds': 0.3}, error=True)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result['timed_out'])
        self.assertIn('started', result['stdout'])
        time.sleep(2.1)
        self.assertFalse(marker.exists(), 'a descendant must not survive the command timeout')
        self.assertEqual(self.call('run_shell', {'command': 'printf alive'})['stdout'], 'alive')

    def test_fast_read_answers_while_another_chat_runs_shell(self):
        (self.cwd / 'answer.txt').write_text('ready')
        command = 'printf started > command-started; sleep 2; printf finished'
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.call, 'run_shell', {'command': command})
            deadline = time.monotonic() + 3
            while not (self.cwd / 'command-started').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.cwd / 'command-started').exists())
            started = time.monotonic()
            self.assertEqual(self.call('read_file', {'path': 'answer.txt'})['text'], 'ready')
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(pending.done(), 'the fast read must not queue behind the shell command')
            self.assertEqual(pending.result(timeout=5)['stdout'], 'finished')

    def test_busy_server_rejects_mutation_without_executing_it_later(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(self.call, 'run_shell', {
                'command': f'printf started > busy-{index}; sleep 2'}) for index in range(2)]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all((self.cwd / f'busy-{index}').exists() for index in range(2)):
                    break
                time.sleep(0.01)
            self.assertTrue(all((self.cwd / f'busy-{index}').exists() for index in range(2)))
            started = time.monotonic()
            self.call('make_directory', {'path': 'should-not-be-queued'}, error=True)
            self.assertLess(time.monotonic() - started, 1)
            for result in pending:
                result.result(timeout=5)
        self.call('list_directory', {})
        self.assertFalse((self.cwd / 'should-not-be-queued').exists())

    def test_server_shutdown_terminates_active_shell_descendants(self):
        server, tools = self.start_server()
        command = ('printf started > shutdown-started; '
                   '(sleep 2; printf survived > survived-shutdown) & wait')
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(server.call, tools['run_shell'], {'command': command})
            deadline = time.monotonic() + 3
            while not (self.cwd / 'shutdown-started').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.cwd / 'shutdown-started').exists())
            server.stop()
            try:
                pending.result(timeout=5)
            except mcp_client.MCPError:
                pass  # Stopping the transport may cancel the pending response.
        time.sleep(2.1)
        self.assertFalse((self.cwd / 'survived-shutdown').exists(),
                         'server shutdown must not orphan an active command')

    def test_transport_eof_terminates_active_shell_descendants(self):
        server, tools = self.start_server()
        command = ('printf started > eof-started; '
                   '(sleep 2; printf survived > survived-eof) & wait')
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(server.call, tools['run_shell'], {'command': command})
            deadline = time.monotonic() + 3
            while not (self.cwd / 'eof-started').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.cwd / 'eof-started').exists())
            server.process.stdin.close()
            server.process.wait(timeout=1.5)
            try:
                pending.result(timeout=3)
            except mcp_client.MCPError:
                pass  # Closing the transport may cancel the pending response.
        time.sleep(2.1)
        self.assertFalse((self.cwd / 'survived-eof').exists(),
                         'transport EOF must not orphan an active command')

    def test_invalid_argument_shapes_do_not_crash_server(self):
        for arguments in ('not an object', [], 0, False, None):
            with self.subTest(arguments=arguments):
                result = self.server.request('tools/call', {'name': 'read_file', 'arguments': arguments})
                self.assertTrue(result.get('isError'), result)
        self.assertIsInstance(self.call('list_directory', {})['entries'], list)

    def test_invalid_types_bounds_and_unknown_keys_are_rejected(self):
        (self.cwd / 'existing.txt').write_text('valid readable file')
        cases = [
            ('read_file', {}), ('read_file', {'path': 7}),
            ('read_file', {'path': 'existing.txt', 'offset': 0}),
            ('read_file', {'path': 'existing.txt', 'offset': True}),
            ('read_file', {'path': 'existing.txt', 'limit': 0}),
            ('read_file', {'path': 'existing.txt', 'limit': 2001}),
            ('read_file', {'path': 'existing.txt', 'limit': 1.5}),
            ('read_file', {'path': 'existing.txt', 'extra': 'ignored?'}),
            ('write_file', {'path': 'x'}),
            ('write_file', {'path': 'x', 'content': 7}),
            ('write_file', {'path': 'x', 'content': 'x', 'append': 'false'}),
            ('make_directory', {'path': 'x', 'parents': 1}),
            ('list_directory', {'path': None}),
            ('run_shell', {'command': ''}),
            ('run_shell', {'command': ['echo', 'bad']}),
            ('run_shell', {'command': 'true', 'cwd': 7}),
            ('run_shell', {'command': 'true', 'timeout_seconds': 0}),
            ('run_shell', {'command': 'true', 'timeout_seconds': 8.1}),
            ('run_shell', {'command': 'true', 'timeout_seconds': True}),
            ('run_shell', {'command': 'true', 'timeout_seconds': '1'}),
        ]
        for name, arguments in cases:
            with self.subTest(tool=name, arguments=arguments):
                self.call(name, arguments, error=True)
        self.assertFalse((self.cwd / 'x').exists())

    def test_unknown_tool_is_an_error_and_server_stays_alive(self):
        result = self.server.request('tools/call', {'name': 'imaginary', 'arguments': {}})
        self.assertTrue(result.get('isError'), result)
        self.call('list_directory', {})

    def test_special_files_and_directories_are_rejected_without_blocking(self):
        fifo = self.cwd / 'pipe'
        os.mkfifo(fifo)
        for path in ('pipe', '.'):
            for name, extra in (('read_file', {}), ('write_file', {'content': 'no'})):
                with self.subTest(tool=name, path=path):
                    started = time.monotonic()
                    self.call(name, {'path': path, **extra}, error=True)
                    self.assertLess(time.monotonic() - started, 2)
        (self.cwd / 'fifo-link').symlink_to(fifo)
        self.call('write_file', {'path': 'fifo-link', 'content': 'no'}, error=True)

    def test_read_and_shell_output_caps_keep_json_valid(self):
        server, tools = self.start_server('--max-output-chars', '512')
        content = '"\\\t☃\n' * 2000
        (self.cwd / 'large.txt').write_text(content)
        result = self.call('read_file', {'path': 'large.txt'}, server=server, tools=tools, cap=512)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['text'])
        code = 'import sys; sys.stdout.write("x" * 100000); sys.stderr.write("y" * 100000)'
        result = self.call('run_shell', {'command': shlex.join([sys.executable, '-c', code])},
                           server=server, tools=tools, cap=512)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['timed_out'])
        self.assertIn('stdout', result)
        self.assertIn('stderr', result)

    def test_large_directory_output_is_capped_json(self):
        for index in range(100):
            (self.cwd / f'entry-{index:03d}-with-a-long-name.txt').touch()
        server, tools = self.start_server('--max-output-chars', '512')
        result = self.call('list_directory', {}, server=server, tools=tools, cap=512)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['entries'])
        self.assertLess(len(result['entries']), 100)

    def test_read_byte_cap_and_write_utf8_byte_limit(self):
        server, tools = self.start_server('--max-read-bytes', '32', '--max-write-bytes', '32')
        (self.cwd / 'large.txt').write_text('z' * 200)
        result = self.call('read_file', {'path': 'large.txt'}, server=server, tools=tools)
        self.assertTrue(result['byte_capped'])
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text']), 32)
        self.call('write_file', {'path': 'bounded', 'content': 'é' * 16}, server=server, tools=tools)
        self.call('write_file', {'path': 'bounded', 'content': 'é' * 17}, error=True,
                  server=server, tools=tools)
        self.assertEqual((self.cwd / 'bounded').read_text(), 'é' * 16,
                         'an oversized write must be rejected before truncating the file')


class WorkspaceCLITests(unittest.TestCase):
    def run_server(self, *arguments):
        return subprocess.run([sys.executable, SERVER, *arguments], input='',
                              capture_output=True, text=True, timeout=10)

    def test_explicit_existing_cwd_is_required(self):
        result = self.run_server()
        self.assertEqual(result.returncode, 2)
        self.assertIn('--cwd', result.stderr)
        self.assertEqual(result.stdout, '')
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_server('--cwd', str(Path(directory) / 'missing'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_doctor_reports_cwd_without_starting_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_server('--cwd', directory, '--doctor')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(directory, result.stdout)

    def test_transport_eof_finishes_accepted_file_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'existing.txt').write_text('read me')
            calls = [('write_file', {'path': 'new.txt', 'content': 'keep me'}),
                     ('read_file', {'path': 'existing.txt'})]
            requests = ''.join(json.dumps({
                'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}) + '\n'
                for index, (name, arguments) in enumerate(calls, 1))
            result = subprocess.run([sys.executable, SERVER, '--cwd', directory],
                                    input=requests, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = {row['id']: row['result'] for row in
                       (json.loads(line) for line in result.stdout.splitlines())}
            self.assertEqual(set(replies), {1, 2})
            self.assertFalse(replies[1]['isError'])
            self.assertFalse(replies[2]['isError'])
            self.assertEqual((root / 'new.txt').read_text(), 'keep me')
            self.assertEqual(json.loads(replies[2]['content'][0]['text'])['text'], 'read me')


if __name__ == '__main__':
    unittest.main(verbosity=2)
