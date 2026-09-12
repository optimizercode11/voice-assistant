"""The Codex MCP server, driven as a real subprocess peer over a fake `codex`.

Same claims as tests/mcp_claude_test.py, made about a different process shape:
Codex is one child per turn and the thread is continued with `exec resume`.
So the additional claims are that the second `send` resumes the first turn's
thread, that the profile reaches BOTH forms as `-c` overrides (resume rejects
`--profile`), and that the child's stdin is closed (the real CLI blocks on an
open one).  The fixture (tests/fixtures/fake_codex.py) asserts each of those
on argv and exits non-zero otherwise.

The sabotage arm is the same one: `updates` returning the report unasked; with
--sabotage exactly test_updates_carry_no_code_unless_asked must go red.
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
SERVER = str(TOOLS_DIR / 'mcp_codex.py')
FAKE = str(HERE / 'fixtures' / 'fake_codex.py')
SABOTAGE = '--sabotage' in sys.argv

PROFILE = '''model = "fake-local"
model_provider = "q38f"
model_reasoning_effort = "high"

[model_providers.q38f]
name = "fake local server"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
requires_openai_auth = false

[projects."/nowhere"]
trust_level = "trusted"
'''

WRAPPER = '''
import sys
sys.path.insert(0, {tools!r})
import mcp_codex
_updates = mcp_codex.Session.get_updates
mcp_codex.Session.get_updates = lambda self, detail: _updates(self, True)   # "always give full context"
sys.argv = ["mcp_codex.py"] + sys.argv[1:]
raise SystemExit(mcp_codex.main())
'''

_HOME = tempfile.mkdtemp(prefix='codex-home-')
Path(_HOME, 'q38f.config.toml').write_text(PROFILE)


def server(**kwargs):
    if SABOTAGE:
        wrapper = tempfile.NamedTemporaryFile('w', suffix='_sabotage.py', delete=False)
        wrapper.write(WRAPPER.format(tools=str(TOOLS_DIR)))
        wrapper.close()
        script = wrapper.name
    else:
        script = SERVER
    args = [script, '--codex', FAKE, '--profile', 'q38f', '--codex-home', _HOME, '--cwd', str(HERE)] + kwargs.pop('args', [])
    return mcp_client.MCPServer('codex', sys.executable, args, kwargs.pop('env', {}), kwargs.pop('timeout', 8.0))


def parse(text):
    return json.loads(text)


def wait_idle(instance, tools, seconds=10.0):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        text, _ = instance.call(tools['updates'], {})
        last = parse(text)
        if last['state'] != 'working' and last['new']:
            return last
        time.sleep(0.1)
    raise AssertionError(f'session never finished: {last}')


class CodexServerTests(unittest.TestCase):
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
                         ['mcp__codex__send', 'mcp__codex__updates'])
        self.assertEqual(self.instance.server_info['name'], 'voice-codex')
        self.assertIn('Codex', self.tools['send'].description)
        self.assertNotIn('Claude', self.tools['send'].description)

    def test_send_returns_before_the_work_is_done(self):
        started = time.monotonic()
        reply = self.send('slow 1.5')
        self.assertLess(time.monotonic() - started, 1.0, 'send must not wait for the turn')
        self.assertEqual(reply['status'], 'working')
        self.assertTrue(reply['new_session'])
        time.sleep(0.5)
        mid = parse(self.instance.call(self.tools['updates'], {})[0])
        self.assertEqual(mid['state'], 'working')
        self.assertFalse(mid['new'])
        self.assertEqual(mid['working_on'], 'slow 1.5')
        self.assertGreaterEqual(mid['so_far']['commands'], 1, 'a started command counts before it finishes')
        self.assertIn('Still working', mid['note'])
        done = wait_idle(self.instance, self.tools)
        self.assertEqual(done['state'], 'idle')
        row = done['updates'][0]
        self.assertEqual(row['spoken'], 'I waited as asked and edited one file.')
        self.assertEqual(row['activity']['commands'], 1, 'started and completed is one command, not two')
        self.assertEqual(row['activity']['files_edited'], 1)
        self.assertFalse(row['is_error'])
        self.assertNotIn('detail', row)
        again = parse(self.instance.call(self.tools['updates'], {})[0])
        self.assertFalse(again['new'], 'an update is delivered once')

    def test_the_second_send_resumes_the_first_turns_thread(self):
        self.send('thread')
        first = wait_idle(self.instance, self.tools)['updates'][0]['spoken']
        self.assertTrue(first.startswith('Fresh fake-thread-'), first)
        thread = first.split()[1]
        reply = self.send('thread')
        self.assertFalse(reply['new_session'], 'the thread continues')
        second = wait_idle(self.instance, self.tools)['updates'][0]['spoken']
        self.assertEqual(second, f'Resumed {thread}', 'later turns must resume the same Codex thread')

    def test_updates_carry_no_code_unless_asked(self):
        self.send('code')
        done = wait_idle(self.instance, self.tools)
        flat = json.dumps(done)
        self.assertNotIn('```', flat, 'the voice path must never see a code block it did not ask for')
        self.assertNotIn('voice_chat.py', flat)
        self.assertEqual(done['updates'][0]['spoken'],
                         'I fixed the manifest bug in the chat module and the tests pass. Nothing else changed.')
        asked = parse(self.instance.call(self.tools['updates'], {'detail': True})[0])
        self.assertIn('```python', asked['detail'])
        self.assertEqual(asked['detail_of_seq'], 1)

    def test_a_reply_without_a_spoken_line_still_speaks_plainly(self):
        self.send('nospoken')
        row = wait_idle(self.instance, self.tools)['updates'][0]
        self.assertNotIn('```', row['spoken'])
        self.assertNotIn('#', row['spoken'])
        self.assertIn('The bridge clips at 6000 chars', row['spoken'])

    def test_the_last_spoken_line_wins_over_a_quoted_mark(self):
        self.send('quote')
        row = wait_idle(self.instance, self.tools)['updates'][0]
        self.assertEqual(row['spoken'], 'I read the instructions back to you and changed nothing.')

    def test_the_instruction_reaches_codex_verbatim(self):
        self.send('please Rename the Thing, exactly like this')
        row = wait_idle(self.instance, self.tools)['updates'][0]
        self.assertEqual(row['spoken'], 'You said please Rename the Thing, exactly like this')

    def test_instructions_queue_behind_a_turn_in_flight(self):
        self.send('slow 1')
        reply = self.send('second one')
        self.assertEqual(reply['status'], 'queued')
        self.assertEqual(reply['queued_behind'], 1)
        deadline = time.monotonic() + 10
        rows = []
        while time.monotonic() < deadline and len(rows) < 2:
            got = parse(self.instance.call(self.tools['updates'], {})[0])
            rows += got['updates']
            time.sleep(0.1)
        self.assertEqual([row['spoken'] for row in rows],
                         ['I waited as asked and edited one file.', 'You said second one'])

    def test_a_dying_child_is_reported_and_the_next_send_starts_afresh(self):
        self.send('thread')
        wait_idle(self.instance, self.tools)
        self.send('die')
        row = wait_idle(self.instance, self.tools)['updates'][0]
        self.assertTrue(row['is_error'])
        self.assertIn('stopped unexpectedly', row['spoken'])
        self.assertIn('exit code 3', row['spoken'])
        reply = self.send('thread')
        self.assertTrue(reply['new_session'], 'after a death the next send starts a fresh thread')
        spoken = wait_idle(self.instance, self.tools)['updates'][0]['spoken']
        self.assertTrue(spoken.startswith('Fresh '), spoken)

    def test_a_failed_turn_is_an_error_update(self):
        self.send('error')
        row = wait_idle(self.instance, self.tools)['updates'][0]
        self.assertTrue(row['is_error'])
        self.assertIn('the model refused', row['spoken'])

    def test_before_any_send_updates_is_honest_about_it(self):
        got = parse(self.instance.call(self.tools['updates'], {})[0])
        self.assertEqual(got['state'], 'not started')
        self.assertFalse(got['new'])
        self.assertEqual(got['updates'], [])

    def test_an_unreadable_profile_is_an_error_update_not_a_hang(self):
        broken = tempfile.mkdtemp(prefix='codex-home-broken-')
        instance = server(args=['--codex-home', broken])
        try:
            tools = {tool.name: tool for tool in instance.start()}
            reply = parse(instance.call(tools['send'], {'instruction': 'anything'})[0])
            self.assertEqual(reply['status'], 'failed')
            got = parse(instance.call(tools['updates'], {})[0])
            self.assertTrue(got['updates'][0]['is_error'])
            self.assertIn('profile could not be read', got['updates'][0]['spoken'])
        finally:
            instance.stop()


class PushTests(unittest.TestCase):
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
            while time.monotonic() < deadline and not any(m == 'notifications/voice/update' for _, m, _ in heard):
                time.sleep(0.05)
            methods = [method for _, method, _ in heard]
            self.assertEqual(methods[0], 'notifications/voice/working')
            self.assertEqual(methods[-1], 'notifications/voice/update')
            self.assertEqual(methods.count('notifications/voice/update'), 1)
            self.assertEqual(heard[0][2], {'instruction': 'code'})
            traces = [(params['kind'], params['line']) for _, method, params in heard
                      if method == 'notifications/voice/trace']
            self.assertIn(('command', 'make test-chat'), traces, 'a command is traced when it starts')
            self.assertIn(('output', 'exit 0: 14 passed'), traces, 'and its output when it ends')
            self.assertIn(('edit', 'tools/voice_chat.py'), traces)
            self.assertTrue(any(kind == 'text' and line.startswith('I looked at the bridge') for kind, line in traces))
            name, method, params = heard[-1]
            self.assertEqual((name, method), ('codex', 'notifications/voice/update'))
            self.assertEqual(params['spoken'],
                             'I fixed the manifest bug in the chat module and the tests pass. Nothing else changed.')
            self.assertEqual(params['activity']['files_edited'], 1)
            self.assertEqual(params['activity']['commands'], 1)
            self.assertIn('```python', params['detail'])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                asked = parse(instance.call(tools['updates'], {})[0])
                if asked['state'] != 'working':
                    break
                time.sleep(0.05)
            self.assertFalse(asked['new'], 'a pushed update is delivered; asking again must not repeat it')
            self.assertEqual(asked['state'], 'idle')
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


class ProcessHygieneTests(unittest.TestCase):
    def test_child_stdout_noise_never_reaches_the_wire(self):
        instance = server(env={'FAKE_CODEX_STDOUT_NOISE': '1'})
        try:
            tools = {tool.name: tool for tool in instance.start()}
            instance.call(tools['send'], {'instruction': 'noisy'})
            done = wait_idle(instance, tools)
            self.assertEqual(done['updates'][0]['spoken'], 'You said noisy')
            self.assertEqual(instance.status()['state'], 'ready')
        finally:
            instance.stop()

    def test_stopping_the_server_reaps_the_codex_child(self):
        instance = server()
        try:
            tools = {tool.name: tool for tool in instance.start()}
            instance.call(tools['send'], {'instruction': 'slow 30'})
            time.sleep(0.5)
            children = subprocess.run(['pgrep', '-P', str(instance.process.pid)], capture_output=True, text=True).stdout.split()
            self.assertTrue(children, 'the fake codex should be running under the server')
        finally:
            instance.stop()
        time.sleep(0.5)
        for pid in children:
            self.assertFalse(Path(f'/proc/{pid}').exists(), f'child {pid} outlived the server')

    def test_doctor_reports_the_profile_the_access_and_both_argv_forms(self):
        missing = subprocess.run([sys.executable, SERVER, '--doctor', '--codex', '/nonexistent/codex',
                                  '--codex-home', _HOME], capture_output=True, text=True, timeout=30)
        self.assertEqual(missing.returncode, 1)
        self.assertIn('NOT FOUND', missing.stdout)
        present = subprocess.run([sys.executable, SERVER, '--doctor', '--codex', FAKE, '--codex-home', _HOME,
                                  '--cwd', str(HERE), '--add-dir', '/srv', '--push'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(present.returncode, 0, present.stdout + present.stderr)
        self.assertIn('fake codex', present.stdout)
        self.assertIn('-c model_provider="q38f"', present.stdout)
        self.assertIn('-c model_providers.q38f.base_url="http://127.0.0.1:1/v1"', present.stdout)
        self.assertNotIn('projects', present.stdout, 'trust tables are not flattened')
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', present.stdout)
        self.assertIn('exec resume', present.stdout)
        self.assertIn('--add-dir /srv', present.stdout)
        self.assertIn('PUSHED', present.stdout)
        unreadable = subprocess.run([sys.executable, SERVER, '--doctor', '--codex', FAKE, '--codex-home', '/nonexistent'],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(unreadable.returncode, 1)
        self.assertIn('UNREADABLE', unreadable.stdout)

    def test_the_registry_offers_both_tools_with_the_operators_purpose(self):
        config = agent_config.load('')
        config.mcp = [agent_config.MCPServerConfig(
            name='codex', command=sys.executable,
            args=[SERVER, '--codex', FAKE, '--profile', 'q38f', '--codex-home', _HOME, '--cwd', str(HERE)],
            purpose='hand work to Codex on the local model', timeout_seconds=8.0)]
        registry = agent_tools.Registry.build(config)
        try:
            self.assertEqual([name for name in registry.names() if name.startswith('mcp__codex')],
                             ['mcp__codex__send', 'mcp__codex__updates'])
            self.assertIn('- mcp__codex__send, mcp__codex__updates: hand work to Codex on the local model',
                          registry.manifest())
        finally:
            registry.close()


if __name__ == '__main__':
    unittest.main(argv=[a for a in sys.argv if a != '--sabotage'], verbosity=1)
