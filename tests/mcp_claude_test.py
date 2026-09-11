"""The Claude Code MCP server, driven as a real subprocess peer over a fake `claude`.

Why subprocess and not import: the claims here are about a *process* -- that
`send` returns before the work is done, that a dying child is reported rather
than hung on, that nothing but JSON-RPC ever reaches the server's stdout.  The
fixture on the other side (tests/fixtures/fake_claude.py) speaks the CLI's own
stream-json shapes, so the parser is exercised against the real event format.

The sabotage arm removes the one property the voice path depends on: that
`updates` returns the spoken line ONLY, and the Markdown report only when asked.
The tempting refactor is "always include detail so the local model has full
context"; with --sabotage the wrapper applies it, and exactly one test --
test_updates_carry_no_code_unless_asked -- must go red, because that refactor
is a speech engine reading a code block aloud.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import agent_config
import agent_tools
import mcp_client

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE.parent / 'tools'
SERVER = str(TOOLS_DIR / 'mcp_claude.py')
FAKE = str(HERE / 'fixtures' / 'fake_claude.py')
SABOTAGE = '--sabotage' in sys.argv

WRAPPER = '''
import sys
sys.path.insert(0, {tools!r})
import mcp_claude
_updates = mcp_claude.Session.get_updates
mcp_claude.Session.get_updates = lambda self, detail: _updates(self, True)   # "always give full context"
sys.argv = ["mcp_claude.py"] + sys.argv[1:]
raise SystemExit(mcp_claude.main())
'''


def server(**kwargs):
    if SABOTAGE:
        wrapper = tempfile.NamedTemporaryFile('w', suffix='_sabotage.py', delete=False)
        wrapper.write(WRAPPER.format(tools=str(TOOLS_DIR)))
        wrapper.close()
        script = wrapper.name
    else:
        script = SERVER
    args = [script, '--claude', FAKE, '--cwd', str(HERE)] + kwargs.pop('args', [])
    return mcp_client.MCPServer('claude', sys.executable, args, kwargs.pop('env', {}), kwargs.pop('timeout', 8.0))


def parse(text):
    return json.loads(text)


def wait_idle(instance, tools, seconds=10.0):
    """Poll `updates` until the session is idle; return the last response."""
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        text, _ = instance.call(tools['updates'], {})
        last = parse(text)
        if last['state'] != 'working' and last['new']:
            return last
        time.sleep(0.1)
    raise AssertionError(f'session never finished: {last}')


class ClaudeServerTests(unittest.TestCase):
    def setUp(self):
        self.instance = server()
        self.tools = {tool.name: tool for tool in self.instance.start()}

    def tearDown(self):
        self.instance.stop()

    def send(self, instruction):
        text, is_error = self.instance.call(self.tools['send'], {'instruction': instruction})
        self.assertFalse(is_error, text)
        return parse(text)

    def test_the_two_tools_and_their_folded_names(self):
        self.assertEqual(sorted(self.tools), ['send', 'updates'])
        self.assertEqual(sorted(t.tool_name for t in self.tools.values()),
                         ['mcp__claude__send', 'mcp__claude__updates'])
        self.assertEqual(self.instance.server_info['name'], 'voice-claude-code')

    def test_send_returns_before_the_work_is_done(self):
        started = time.monotonic()
        reply = self.send('slow 1.5')
        self.assertLess(time.monotonic() - started, 1.0, 'send must not wait for the turn')
        self.assertEqual(reply['status'], 'working')
        self.assertTrue(reply['new_session'])
        time.sleep(0.4)
        text, _ = self.instance.call(self.tools['updates'], {})
        mid = parse(text)
        self.assertEqual(mid['state'], 'working')
        self.assertFalse(mid['new'])
        self.assertEqual(mid['working_on'], 'slow 1.5')
        self.assertGreaterEqual(mid['so_far']['commands'], 1, 'the live counters must show progress')
        self.assertIn('Still working', mid['note'])
        done = wait_idle(self.instance, self.tools)
        self.assertEqual(done['state'], 'idle')
        self.assertEqual(len(done['updates']), 1)
        row = done['updates'][0]
        self.assertEqual(row['spoken'], 'I waited as asked and edited one file.')
        self.assertEqual(row['activity']['files_edited'], 1)
        self.assertFalse(row['is_error'])
        self.assertNotIn('detail', row, 'a finished row carries the spoken line, never the report')
        text, _ = self.instance.call(self.tools['updates'], {})
        again = parse(text)
        self.assertFalse(again['new'], 'an update is delivered once')
        self.assertEqual(again['updates'], [])

    def test_updates_carry_no_code_unless_asked(self):
        self.send('code')
        done = wait_idle(self.instance, self.tools)
        flat = json.dumps(done)
        self.assertNotIn('```', flat, 'the voice path must never see a code block it did not ask for')
        self.assertNotIn('voice_chat.py', flat)
        self.assertNotIn('detail', done)
        self.assertEqual(done['updates'][0]['spoken'],
                         'I fixed the manifest bug in the chat module and the tests pass. Nothing else changed.')
        self.assertEqual(done['updates'][0]['activity'],
                         {'commands': 1, 'files_edited': 1, 'files_read': 1, 'other_tools': 0, 'last_tool': 'Bash'})
        text, _ = self.instance.call(self.tools['updates'], {'detail': True})
        asked = parse(text)
        self.assertIn('```python', asked['detail'], 'asked for, the report is handed over whole')
        self.assertIn('tools/voice_chat.py:312', asked['detail'])
        self.assertEqual(asked['detail_of_seq'], 1)
        self.assertNotIn('SPOKEN:', asked['detail'], 'the spoken line is not part of the report')

    def test_a_reply_without_a_spoken_line_still_speaks_plainly(self):
        self.send('nospoken')
        done = wait_idle(self.instance, self.tools)
        spoken = done['updates'][0]['spoken']
        self.assertTrue(spoken)
        for mark in ('`', '#', '**', '```', '\n'):
            self.assertNotIn(mark, spoken, f'markdown {mark!r} leaked into speech: {spoken!r}')
        self.assertIn('The bridge clips at 6000 chars', spoken)
        self.assertNotIn('make test\n', spoken)
        self.assertLessEqual(len(spoken), 600)

    def test_the_last_spoken_line_wins_over_a_quoted_mark(self):
        self.send('quote')
        done = wait_idle(self.instance, self.tools)
        self.assertEqual(done['updates'][0]['spoken'], 'I read the instructions back to you and changed nothing.')

    def test_the_instruction_reaches_claude_verbatim(self):
        odd = 'restart the tools bridge -- not the GPU stack -- and tell me if 8094 answers "ok"'
        self.send(odd)
        done = wait_idle(self.instance, self.tools)
        self.assertEqual(done['updates'][0]['spoken'], f'You said {odd}')
        self.assertEqual(done['updates'][0]['instruction'], odd)

    def test_instructions_queue_behind_a_turn_in_flight(self):
        first = self.send('slow 1')
        second = self.send('second thing')
        self.assertEqual(first['queued_behind'], 0)
        self.assertEqual(second['status'], 'queued')
        self.assertEqual(second['queued_behind'], 1)
        text, _ = self.instance.call(self.tools['updates'], {})
        self.assertEqual(parse(text).get('queued'), 1)
        deadline = time.monotonic() + 10
        rows = []
        while time.monotonic() < deadline and len(rows) < 2:
            text, _ = self.instance.call(self.tools['updates'], {})
            rows += parse(text)['updates']
            time.sleep(0.1)
        self.assertEqual([row['seq'] for row in rows], [1, 2])
        self.assertEqual([row['instruction'] for row in rows], ['slow 1', 'second thing'])
        self.assertEqual(rows[1]['spoken'], 'You said second thing')

    def test_a_dying_child_is_reported_and_the_next_send_starts_afresh(self):
        self.send('die')
        done = wait_idle(self.instance, self.tools)
        row = done['updates'][0]
        self.assertTrue(row['is_error'])
        self.assertIn('exit code 3', row['spoken'])
        self.assertEqual(done['state'], 'not started')
        self.assertEqual(done['last_exit_code'], 3)
        reply = self.send('hello again')
        self.assertTrue(reply['new_session'], 'after a death the next instruction gets a fresh child')
        done = wait_idle(self.instance, self.tools)
        self.assertEqual(done['updates'][0]['spoken'], 'You said hello again')

    def test_an_error_result_is_an_error_update(self):
        self.send('error')
        done = wait_idle(self.instance, self.tools)
        self.assertTrue(done['updates'][0]['is_error'])
        self.assertIn('error', done['updates'][0]['spoken'].lower())

    def test_before_any_send_updates_is_honest_about_it(self):
        text, is_error = self.instance.call(self.tools['updates'], {'detail': True})
        self.assertFalse(is_error)
        state = parse(text)
        self.assertEqual(state['state'], 'not started')
        self.assertFalse(state['new'])
        self.assertEqual(state['detail'], '')


class PushTests(unittest.TestCase):
    """With --push a finished turn is a notification up the wire, and is then delivered, not unread."""

    def start(self, push):
        instance = server(args=['--push'] if push else [])
        heard = []
        instance.on_notification = lambda name, method, params: heard.append((name, method, params))
        tools = {tool.name: tool for tool in instance.start()}
        return instance, tools, heard

    def test_a_finished_turn_is_notified_and_not_repeated(self):
        instance, tools, heard = self.start(push=True)
        try:
            instance.call(tools['send'], {'instruction': 'code'})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(heard) < 2:
                time.sleep(0.05)
            self.assertEqual([method for _, method, _ in heard],
                             ['notifications/voice/working', 'notifications/voice/update'],
                             'a working notice when the job starts, one update when it finishes')
            self.assertEqual(heard[0][2], {'instruction': 'code'})
            name, method, params = heard[1]
            self.assertEqual((name, method), ('claude', 'notifications/voice/update'))
            self.assertEqual(params['spoken'],
                             'I fixed the manifest bug in the chat module and the tests pass. Nothing else changed.')
            self.assertEqual(params['instruction'], 'code')
            self.assertEqual(params['activity']['files_edited'], 1)
            self.assertIn('```python', params['detail'], 'the report rides the push, clipped, for the page to show')
            text, _ = self.instance_updates(instance, tools)
            asked = parse(text)
            self.assertFalse(asked['new'], 'a pushed update is delivered; asking again must not repeat it')
            self.assertEqual(asked['state'], 'idle')
            text, _ = instance.call(tools['updates'], {'detail': True})
            self.assertIn('```python', parse(text)['detail'], 'the report is still there for whoever asks')
        finally:
            instance.stop()

    def test_without_push_nothing_is_notified_and_updates_still_works(self):
        instance, tools, heard = self.start(push=False)
        try:
            instance.call(tools['send'], {'instruction': 'plain'})
            done = wait_idle(instance, tools)
            self.assertEqual(done['updates'][0]['spoken'], 'You said plain')
            self.assertEqual(heard, [])
        finally:
            instance.stop()

    @staticmethod
    def instance_updates(instance, tools):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text, is_error = instance.call(tools['updates'], {})
            if parse(text)['state'] != 'working':
                return text, is_error
            time.sleep(0.05)
        raise AssertionError('never idle')


class ProcessHygieneTests(unittest.TestCase):
    def test_child_stdout_noise_never_reaches_the_wire(self):
        instance = server(env={'FAKE_CLAUDE_STDOUT_NOISE': '1'})
        try:
            tools = {tool.name: tool for tool in instance.start()}
            instance.call(tools['send'], {'instruction': 'noisy'})
            done = wait_idle(instance, tools)
            self.assertEqual(done['updates'][0]['spoken'], 'You said noisy')
            self.assertEqual(instance.status()['state'], 'ready')
        finally:
            instance.stop()

    def test_stopping_the_server_reaps_the_claude_child(self):
        instance = server()
        try:
            tools = {tool.name: tool for tool in instance.start()}
            instance.call(tools['send'], {'instruction': 'slow 30'})
            time.sleep(0.5)
            server_pid = instance.process.pid
            children = subprocess.run(['pgrep', '-P', str(server_pid)], capture_output=True, text=True).stdout.split()
            self.assertTrue(children, 'the fake claude should be running under the server')
        finally:
            instance.stop()
        time.sleep(0.5)
        for pid in children:
            self.assertFalse(Path(f'/proc/{pid}').exists(), f'child {pid} outlived the server')

    def test_doctor_refuses_a_missing_binary_and_accepts_a_present_one(self):
        missing = subprocess.run([sys.executable, SERVER, '--doctor', '--claude', '/nonexistent/claude'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(missing.returncode, 1)
        self.assertIn('NOT FOUND', missing.stdout)
        present = subprocess.run([sys.executable, SERVER, '--doctor', '--claude', FAKE, '--cwd', str(HERE),
                                  '--model', 'fable', '--add-dir', '/srv'], capture_output=True, text=True, timeout=30)
        self.assertEqual(present.returncode, 0, present.stdout + present.stderr)
        self.assertIn('fake claude', present.stdout)
        self.assertIn('--permission-mode bypassPermissions', present.stdout)
        self.assertIn('--model fable --add-dir /srv', present.stdout)

    def test_the_registry_offers_both_tools_with_the_operators_purpose(self):
        config = agent_config.load(None)
        config.mcp = [agent_config.MCPServerConfig(
            name='claude', command=sys.executable, args=[SERVER, '--claude', FAKE, '--cwd', str(HERE)],
            purpose='hand work to Claude Code and hear how it went', timeout_seconds=8.0)]
        registry = agent_tools.Registry.build(config)
        try:
            self.assertIn('mcp__claude__send', registry.names())
            self.assertIn('mcp__claude__updates', registry.names())
            self.assertIn('- mcp__claude__send, mcp__claude__updates: hand work to Claude Code and hear how it went',
                          registry.manifest())
            result = registry.execute('mcp__claude__send', '{"instruction":"through the registry"}')
            self.assertTrue(result.ok, result.error)
            self.assertIn('"status"', result.content)
            bad = registry.execute('mcp__claude__send', '{"instruction":""}')
            self.assertFalse(bad.ok, 'minLength is enforced before the server is called')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                got = registry.execute('mcp__claude__updates', '{}')
                self.assertTrue(got.ok, got.error)
                if json.loads(got.content)['new']:
                    break
                time.sleep(0.1)
            self.assertEqual(json.loads(got.content)['updates'][0]['spoken'], 'You said through the registry')
        finally:
            registry.close()


if __name__ == '__main__':
    unittest.main(argv=[a for a in sys.argv if a != '--sabotage'], verbosity=1)
