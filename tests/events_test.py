"""The bridge speaks first: /events, over the real bridge process.

The claim under test is a sequence across three processes -- the Claude Code
server (over a fake `claude`) finishes a turn and writes a JSON-RPC
notification up its pipe; the bridge's MCP client hands it to the registry's
subscriber; the bridge fans it out to every page holding `/events` open -- so
the honest test starts `tools/speech_ui.py` the way the deployment does, with a
`--tools-config` that names the real server, drives one `send` through a
scripted model, and reads the server-sent event as a browser would.

The sabotage arm removes the method whitelist in `Events.on_notification`: the
tempting generality "forward whatever the server notifies".  With it, an MCP
server's *log line* (`notifications/message`, which any server may emit) with a
`spoken` field would be read aloud by the page.  Exactly one test --
test_only_the_update_method_is_spoken -- must go red.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'tools'))
sys.path.insert(0, str(HERE))

import mcp_client
from tool_loop_test import Upstream, answered, calling, socket_free_port

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
SABOTAGE = '--sabotage' in sys.argv
TOOLS = HERE.parent / 'tools'
FAKE_CLAUDE = HERE / 'fixtures' / 'fake_claude.py'
FAKE_MCP = HERE / 'fixtures' / 'fake_mcp_server.py'

WRAPPER = '''
import sys
sys.path.insert(0, {tools!r})
import speech_ui
if {sabotage!r}:
    _strict = speech_ui.Events.on_notification
    def lax(self, server, method, params):     # "forward whatever the server says"
        _strict(self, server, speech_ui.Events.METHOD if isinstance(params, dict) and params.get("spoken") else method, params)
    speech_ui.Events.on_notification = lax
sys.argv = ["speech_ui.py"] + sys.argv[1:]
speech_ui.main()
'''

CONFIG = '''
[limits]
rounds = 3

[[mcp.server]]
name = "claude"
purpose = "hand work to Claude Code and hear how it went"
command = "{python}"
args = ["{server}", "--claude", "{fake}", "--cwd", "{cwd}", "--push"]
timeout_seconds = 8

[[mcp.server]]
name = "desk"
purpose = "a fixture that speaks first with a log line"
command = "{python}"
args = ["{fake_mcp}", "notifies"]
timeout_seconds = 5
'''


def send_call(instruction):
    return calling([{'id': 'call_1', 'type': 'function',
                     'function': {'name': 'mcp__claude__send',
                                  'arguments': json.dumps({'instruction': instruction})}}])


class EventStream:
    """A minimal EventSource: one GET, events parsed as they arrive.

    http.client hands the socket to the response for a stream with no length,
    so the read timeout is fixed at connect time and a quiet stream surfaces
    as `None` from next_event(), which is what "nothing was pushed" looks like.
    """

    def __init__(self, port, origin=None, timeout=15.0):
        self.connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        headers = {'Accept': 'text/event-stream'}
        if origin:
            headers['Origin'] = origin
        self.connection.request('GET', '/events', headers=headers)
        self.response = self.connection.getresponse()

    def next_event(self):
        fields = {}
        try:
            while True:
                line = self.response.fp.readline()
                if not line:
                    return None
                line = line.decode('utf-8').rstrip('\n')
                if not line:
                    if 'data' in fields:
                        return {'event': fields.get('event', 'message'), 'id': fields.get('id'),
                                'data': json.loads(fields['data'])}
                    fields = {}
                    continue
                if line.startswith(':'):
                    continue                                   # keepalive
                key, _, value = line.partition(':')
                fields[key] = value.lstrip()
        except (socket.timeout, TimeoutError):
            return None

    def next_update(self):
        """The next finished-turn event, skipping the working notice that precedes it."""
        while True:
            event = self.next_event()
            if event is None or event['event'] == 'update':
                return event

    def close(self):
        try:
            self.response.close()
        except OSError:
            pass
        self.connection.close()


class EventsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.upstream.requests, cls.upstream.script, cls.upstream.started = [], [], threading.Event()
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        config = cls.root / 'host.toml'
        config.write_text(CONFIG.format(python=sys.executable, server=TOOLS / 'mcp_claude.py', fake=FAKE_CLAUDE,
                                        cwd=cls.root, fake_mcp=FAKE_MCP))
        wrapper = cls.root / 'bridge_wrapper.py'
        wrapper.write_text(WRAPPER.format(tools=str(TOOLS), sabotage=SABOTAGE))
        cls.port = socket_free_port()
        command = [sys.executable, str(wrapper), '--host', '127.0.0.1', '--port', str(cls.port),
                   '--asr-url', 'http://127.0.0.1:1', '--llm-url', f'http://127.0.0.1:{cls.upstream.server_port}',
                   '--page', str(Path('web/index.html').resolve()), '--tools-config', str(config)]
        cls.bridge = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      env={**os.environ, 'CUDA_VISIBLE_DEVICES': ''})
        cls.lines = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = cls.bridge.stdout.readline()
            if not line or cls.bridge.poll() is not None:
                break
            cls.lines.append(line.strip())
            if 'Speech UI listening' in line:
                break
        assert cls.bridge.poll() is None, f'bridge exited early: {cls.lines}'
        threading.Thread(target=lambda: [cls.lines.append(l.strip()) for l in cls.bridge.stdout], daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.terminate()
        try:
            cls.bridge.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.bridge.kill()
            cls.bridge.wait(timeout=5)
        cls.bridge.stdout.close()
        cls.upstream.shutdown(); cls.upstream.server_close(); cls.temp.cleanup()

    def setUp(self):
        self.upstream.requests, self.upstream.started = [], threading.Event()

    def get(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, json.loads(body) if body else None

    def chat(self, text):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        connection.request('POST', '/chat/completions', body=json.dumps({'messages': [{'role': 'user', 'content': text}]}),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_health_says_the_bridge_can_speak_first(self):
        status, health = self.get('/chat/health')
        self.assertEqual(status, 200)
        self.assertTrue(health['events'])
        self.assertIn('mcp__claude__send', health['tools'])

    def test_a_finished_turn_is_pushed_to_an_open_page(self):
        stream = EventStream(self.port)
        try:
            self.assertEqual(stream.response.status, 200)
            self.assertIn('text/event-stream', stream.response.getheader('Content-Type'))
            self.upstream.script = [send_call('push me a note'), answered('Asked. It will say when it is done.')]
            status, reply = self.chat('ask claude to push me a note')
            self.assertEqual(status, 200, reply)
            self.assertEqual([tool['name'] for tool in reply['tools']], ['mcp__claude__send'])
            notice = stream.next_event()
            while notice is not None and notice['data'].get('instruction') != 'push me a note':
                notice = stream.next_event()          # an earlier test's leftovers, if any
            self.assertIsNotNone(notice, 'the page is told the agent has the job')
            self.assertEqual(notice['event'], 'working')
            self.assertEqual(notice['data']['instruction'], 'push me a note')
            event = stream.next_event()
            self.assertIsNotNone(event, 'the finished turn must arrive without anyone asking')
            self.assertEqual(event['event'], 'update')
            self.assertEqual(event['data']['spoken'], 'You said push me a note')
            self.assertEqual(event['data']['server'], 'claude')
            self.assertEqual(event['data']['instruction'], 'push me a note')
            self.assertFalse(event['data']['is_error'])
            self.assertEqual(set(event['data']), {'type', 'server', 'spoken', 'instruction', 'is_error', 'seconds', 'activity', 'detail', 'id'},
                             'only the whitelisted fields cross')
            self.assertEqual(event['data']['detail'], 'Echo: push me a note', 'the report rides along for the page to SHOW')
        finally:
            stream.close()

    def test_a_pushed_update_is_delivered_not_unread(self):
        stream = EventStream(self.port)
        try:
            self.upstream.script = [send_call('second note'), answered('Asked.')]
            self.chat('again')
            self.assertIsNotNone(stream.next_update())
            # Now "any news?": the model calls updates and must be told there is none.
            self.upstream.requests = []
            self.upstream.script = [calling([{'id': 'call_2', 'type': 'function',
                                              'function': {'name': 'mcp__claude__updates', 'arguments': '{}'}}]),
                                    # The last tool result carries the bridge's "no more rounds"
                                    # note after the JSON, so decode only the leading object.
                                    lambda requests: answered(json.dumps(json.JSONDecoder().raw_decode(
                                        requests[-1]['messages'][-1]['content'])[0]))]
            status, reply = self.chat('any news?')
            self.assertEqual(status, 200, reply)
            seen = json.loads(reply['text'])
            self.assertFalse(seen['new'], 'a pushed update must not be repeated when asked')
            self.assertEqual(seen['updates'], [])
        finally:
            stream.close()

    def test_with_no_page_open_the_update_waits_for_the_next_one(self):
        time.sleep(1.0)      # the bridge notices a closed page within half a second; earlier tests just closed theirs
        self.upstream.script = [send_call('nobody listening'), answered('Asked.')]
        self.chat('go')
        time.sleep(1.0)                                     # the fake finishes in well under this
        stream = EventStream(self.port, timeout=5)
        try:
            event = stream.next_update()
            self.assertIsNotNone(event, 'a turn that finished with no page open is handed to the next page')
            self.assertEqual(event['data']['spoken'], 'You said nobody listening')
        finally:
            stream.close()
        # ...and only once: a second page does not hear the afternoon again.
        again = EventStream(self.port, timeout=2.5)
        try:
            self.assertIsNone(again.next_event())
        finally:
            again.close()

    def test_only_the_update_method_is_spoken(self):
        stream = EventStream(self.port, timeout=2.5)
        try:
            # The fixture server emits `notifications/message` with a spoken
            # field on every tools/call.  That is a log line, not a finished
            # turn, and the page must never hear it.
            self.upstream.script = [calling([{'id': 'call_3', 'type': 'function',
                                              'function': {'name': 'mcp__desk__get_time', 'arguments': '{"zone":"UTC"}'}}]),
                                    answered('It is 12:34.')]
            status, reply = self.chat('what time is it?')
            self.assertEqual(status, 200, reply)
            self.assertEqual(reply['tools'][0]['name'], 'mcp__desk__get_time')
            self.assertIsNone(stream.next_event(), 'a log line from an MCP server was forwarded to the speaker')
        finally:
            stream.close()

    def test_two_pages_both_hear_it(self):
        first, second = EventStream(self.port), EventStream(self.port)
        try:
            self.upstream.script = [send_call('both of you'), answered('Asked.')]
            self.chat('tell both')
            for stream in (first, second):
                event = stream.next_update()
                self.assertIsNotNone(event)
                self.assertEqual(event['data']['spoken'], 'You said both of you')
        finally:
            first.close(); second.close()

    def test_cross_origin_listeners_are_refused(self):
        stream = EventStream(self.port, origin='https://evil.example')
        try:
            self.assertEqual(stream.response.status, 403)
        finally:
            stream.close()

    def test_a_dropped_page_is_forgotten(self):
        stream = EventStream(self.port)
        self.assertIsNotNone(stream.response.status)
        stream.close()
        time.sleep(0.5)
        self.upstream.script = [send_call('after the drop'), answered('Asked.')]
        status, _ = self.chat('go')
        self.assertEqual(status, 200)
        fresh = EventStream(self.port, timeout=3)
        try:
            # The dropped subscriber was the only one at publish time... unless
            # the bridge noticed the drop.  Either outcome is fine for the person
            # (the update reaches the next page or was already given to a live
            # one); what must not happen is the bridge wedging on a dead socket.
            fresh.next_update()
            status, health = self.get('/chat/health')
            self.assertEqual(status, 200, 'the bridge still answers after a subscriber vanished')
        finally:
            fresh.close()


if __name__ == '__main__':
    unittest.main(argv=[a for a in sys.argv if a != '--sabotage'], verbosity=1)
