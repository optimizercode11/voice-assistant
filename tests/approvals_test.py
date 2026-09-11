#!/usr/bin/env python3
"""Directory grants -- the one capability here that can widen while it runs.

Everything else in this repo is fixed at config time, so a bug can only ever
expose what an operator already approved.  This capability is different: the
assistant asks for a folder mid-conversation and a person says yes.  A bug here
exposes whatever the model can *talk someone into* approving, so the tests are
written as an adversary that controls the model and the request text.

The load-bearing claim, and the one the sabotage arm attacks:

    request() records a question.  Nothing that the model can cause to happen
    turns it into a grant.  approve() is reachable only from the page.

Everything else here is downstream of that, which is why "the tool works" is a
minority of these tests and "the tool cannot grant" is the one that matters.
"""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import atexit
import shutil
import tempfile
import threading
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import agent_config
import agent_tools
import approvals
import mcp_client
import speech_ui

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

SERVER = str(Path(__file__).resolve().parents[1] / 'tools' / 'mcp_files.py')
SABOTAGE = '--sabotage' in sys.argv
sys.argv = [a for a in sys.argv if a != '--sabotage']


if SABOTAGE:
    # The paired negative for the claim above: make request() grant what it
    # records, exactly the "convenience" refactor that would make every other
    # control in approvals.py decorative.  It must break the tests that assert it.
    _original_request = approvals.Store.request

    def _granting_request(self, path, reason=""):
        record = _original_request(self, path, reason)
        if not record.get("already_granted"):
            self.approve(record["id"])
        return record

    approvals.Store.request = _granting_request


def workspace() -> Path:
    """A home-shaped tree: an ordinary project, a credential folder, a link out."""
    base = Path(tempfile.mkdtemp(prefix='approvals-'))
    # 2026-09-11: 1311 of these (79 MB each) were found filling /tmp on the dev
    # VM -- every sabotage/selftest run leaked one.  Remove it when the process ends.
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    (base / 'projects' / 'notes').mkdir(parents=True)
    (base / 'projects' / 'notes' / 'meeting.md').write_text('# meeting\n')
    (base / 'projects' / 'api_key.txt').write_text('sk-demo\n')
    (base / 'projects' / 'empty').mkdir()
    (base / 'elsewhere').mkdir()
    (base / 'elsewhere' / 'tax.txt').write_text('the tax notes\n')
    return base


def config_for(root: Path, enabled: bool = True):
    config = agent_config.AgentConfig(
        approvals=agent_config.ApprovalsConfig(enabled=enabled, file=Path('var/approvals.json')))
    return config


def registry_for(root: Path, enabled: bool = True):
    config = config_for(root, enabled)
    return agent_tools.Registry(config, agent_tools.Context(config, root))


class RefusalTests(unittest.TestCase):
    """What no click can approve, checked against the realpath rather than the string."""

    @classmethod
    def setUpClass(cls):
        cls.base = workspace()

    def test_the_obvious_disasters_are_never_grantable(self):
        for path in ['/', '/etc', '/etc/ssh', '/proc/self', '/var/log', '/usr', '/root',
                     str(Path.home()), str(Path.home() / '.ssh'), str(Path.home() / '.aws'),
                     str(Path.home() / '.config' / 'gcloud')]:
            self.assertTrue(approvals.refusal(path), f'{path} should be refused')

    def test_ordinary_directories_are_grantable(self):
        for path in [self.base / 'projects', self.base / 'projects' / 'notes', '/tmp', '/mnt']:
            self.assertEqual(approvals.refusal(path), '', f'{path} should be grantable')

    def test_a_symlink_cannot_walk_into_a_refused_directory(self):
        # The argument layer would happily accept "projects/ssh" -- it is a plain
        # relative name.  Only realpath() knows it is ~/.ssh.
        link = self.base / 'projects' / 'ssh'
        link.symlink_to(Path.home() / '.ssh', target_is_directory=True)
        self.assertTrue(approvals.refusal(link), 'a link into ~/.ssh must be refused by its target')

    def test_dotslash_tricks_resolve_to_the_same_refusal(self):
        home = str(Path.home())
        self.assertEqual(approvals.refusal(home + '/projects/../.ssh'),
                         approvals.refusal(home + '/.ssh'))

    def test_describe_reports_the_blast_radius_before_the_click(self):
        report = approvals.describe(self.base / 'projects')
        self.assertEqual(report['realpath'], os.path.realpath(str(self.base / 'projects')))
        self.assertEqual(report['files'], 2)
        self.assertIn('api_key.txt', report['hints'],
                      'a credential filename must be visible before the grant, not after')
        self.assertEqual(approvals.describe(self.base / 'nope')['error'], 'not a directory')

    def test_describe_is_bounded(self):
        noisy = self.base / 'noisy'
        noisy.mkdir()
        for index in range(approvals.MAX_WALK + 50):
            (noisy / f'f{index}.txt').write_text('x')
        report = approvals.describe(noisy)
        self.assertTrue(report['truncated'], 'describe() must say it stopped rather than imply it saw all')
        self.assertEqual(report['files'], approvals.MAX_WALK)


class StoreTests(unittest.TestCase):
    """The file itself: what a request may do, and what only a click may do."""

    def setUp(self):
        self.base = workspace()
        self.store = approvals.Store(self.base / 'var' / 'approvals.json')

    def test_request_records_pending_and_grants_nothing(self):
        # THE load-bearing assertion in this file.  If this ever passes while
        # granted() is non-empty, the model can read any folder it can name.
        record = self.store.request(self.base / 'projects', 'to read the meeting notes')
        self.assertFalse(record['already_granted'])
        self.assertEqual(self.store.granted(), [], 'request() must not grant')
        self.assertEqual(self.store.granted_roots(), [])
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.pending()[0]['reason'], 'to read the meeting notes')

    def test_asking_twice_files_one_request(self):
        self.store.request(self.base / 'projects', 'first')
        self.store.request(self.base / 'projects', 'second')
        self.assertEqual(len(self.store.pending()), 1, 'a nagging model must not bury the queue')

    def test_request_refuses_what_no_click_could_approve(self):
        with self.assertRaises(approvals.ApprovalError):
            self.store.request('/', 'everything')
        with self.assertRaises(approvals.ApprovalError):
            self.store.request(Path.home() / '.ssh', 'keys')
        self.assertEqual(self.store.pending(), [])

    def test_request_refuses_a_file_and_a_vanished_directory(self):
        with self.assertRaises(approvals.ApprovalError):
            self.store.request(self.base / 'projects' / 'api_key.txt', 'a file is not a folder')
        with self.assertRaises(approvals.ApprovalError):
            self.store.request(self.base / 'gone', 'not there')

    def test_approve_grants_the_realpath_not_the_string(self):
        record = self.store.request(self.base / 'projects' / 'notes' / '..', 'relative ask')
        self.store.approve(record['id'])
        self.assertEqual(self.store.granted_roots(),
                         [os.path.realpath(str(self.base / 'projects'))])

    def test_a_click_cannot_approve_a_denied_path_even_if_the_file_says_so(self):
        # The request was filed earlier and the file is a plain file on disk.
        # approve() re-checks, so hand-editing a pending entry to name /etc
        # does not turn the Approve button into a way to read /etc.
        self.store._write({'pending': [{'id': 'hand', 'realpath': '/etc', 'files': 0,
                                        'truncated': False, 'hints': [], 'error': '',
                                        'reason': '', 'asked_at': 0}], 'granted': []})
        with self.assertRaises(approvals.ApprovalError):
            self.store.approve('hand')
        self.assertEqual(self.store.granted(), [])

    def test_decline_removes_a_request_without_granting_it(self):
        record = self.store.request(self.base / 'projects')
        self.store.decline(record['id'])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.granted(), [], 'declining must not be a way to approve')

    def test_revoke_takes_access_away_and_says_so_when_there_was_none(self):
        record = self.store.request(self.base / 'projects')
        self.store.approve(record['id'])
        self.store.revoke(self.base / 'projects')
        self.assertEqual(self.store.granted_roots(), [])
        with self.assertRaises(approvals.ApprovalError):
            self.store.revoke(self.base / 'projects')

    def test_unknown_ids_are_an_error_not_a_default(self):
        for method in ('approve', 'decline'):
            with self.assertRaises(approvals.ApprovalError):
                getattr(self.store, method)('nope')

    def test_the_grants_file_is_private_and_leaves_no_temporary(self):
        self.store.request(self.base / 'projects')
        location = self.base / 'var' / 'approvals.json'
        self.assertEqual(oct(location.stat().st_mode & 0o777), '0o600',
                         'the file records which of the user\u2019s folders are exposed')
        self.assertFalse(location.with_suffix('.json.tmp').exists())

    def test_a_corrupt_grants_file_refuses_rather_than_silently_losing_grants(self):
        location = self.base / 'var' / 'approvals.json'
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text('{"pending": [', encoding='utf-8')
        with self.assertRaises(approvals.ApprovalError):
            self.store.pending()

    def test_granted_roots_drops_a_directory_that_vanished(self):
        record = self.store.request(self.base / 'elsewhere')
        self.store.approve(record['id'])
        self.assertEqual(len(self.store.granted()), 1)
        (self.base / 'elsewhere' / 'tax.txt').unlink()
        (self.base / 'elsewhere').rmdir()
        self.assertEqual(self.store.granted_roots(), [],
                         'a dead root must not crash a read-only server')
        self.assertEqual(len(self.store.granted()), 1, 'the history still says it was approved')


class BuiltinTests(unittest.TestCase):
    """The half the model can touch.  It may ask, and the reply must not let it
    believe the asking worked."""

    def setUp(self):
        self.base = workspace()
        self.registry = registry_for(self.base)

    def test_the_tool_only_exists_when_an_operator_turned_it_on(self):
        self.assertIn('request_directory', self.registry.names())
        self.assertNotIn('request_directory', registry_for(self.base, False).names())

    def test_calling_it_never_grants(self):
        result = self.registry.execute('request_directory',
                                       {'path': str(self.base / 'projects'), 'reason': 'notes'})
        self.assertTrue(result.ok)
        self.assertEqual(agent_tools.approvals_store(config_for(self.base), self.base).granted(), [])

    def test_the_reply_opens_with_not_granted_and_forbids_claiming_access(self):
        result = self.registry.execute('request_directory', {'path': str(self.base / 'projects')})
        self.assertTrue(result.content.startswith('NOT GRANTED'), result.content)
        for phrase in ('you cannot read that folder', 'do not say you have access'):
            self.assertIn(phrase, result.content)
        self.assertFalse(result.meta['granted'])

    def test_a_denied_path_is_refused_in_words_the_model_can_act_on(self):
        result = self.registry.execute('request_directory', {'path': str(Path.home() / '.ssh')})
        self.assertFalse(result.ok)
        self.assertIn('.ssh', result.error)
        self.assertEqual(self.registry.execute('request_directory', {'path': '/'}).error,
                         'the whole filesystem cannot be approved')

    def test_relative_paths_resolve_against_the_assistant_not_the_caller(self):
        result = self.registry.execute('request_directory', {'path': 'projects'})
        self.assertEqual(result.meta['realpath'], os.path.realpath(str(self.base / 'projects')))

    def test_an_already_approved_folder_says_so_instead_of_filing_a_second_ask(self):
        first = self.registry.execute('request_directory', {'path': str(self.base / 'projects')})
        store = agent_tools.approvals_store(config_for(self.base), self.base)
        store.approve(store.pending()[0]['id'])
        again = self.registry.execute('request_directory', {'path': str(self.base / 'projects')})
        self.assertTrue(again.ok)
        self.assertIn('already approved', again.content)
        self.assertNotIn('NOT GRANTED', again.content)
        self.assertEqual(len(store.pending()), 0)
        self.assertTrue(first.content.startswith('NOT GRANTED'))

    def test_it_is_not_labelled_read_only_because_it_writes_a_request(self):
        self.assertFalse(self.registry.tools['request_directory'].read_only)


class FileServerTests(unittest.TestCase):
    """The grants file as a *running* server sees it, one real MCP peer at a time."""

    def setUp(self):
        self.base = workspace()
        self.desk = self.base / 'desk'
        self.desk.mkdir()
        (self.desk / 'a.txt').write_text('alpha\n')
        self.extra = self.base / 'elsewhere'
        self.grants = self.base / 'var' / 'approvals.json'
        self.store = approvals.Store(self.grants)

    def peer(self):
        server = mcp_client.MCPServer(
            'files', sys.executable,
            [SERVER, '--root', str(self.desk), '--roots-file', str(self.grants)],
            {}, 25.0, [], [])
        return server, {tool.name: tool for tool in server.start()}

    def call(self, server, tools, name, arguments):
        text, is_error = server.call(tools[name], arguments)
        return (None, text) if is_error else (json.loads(text), text)

    def test_a_grant_reaches_the_running_server_without_a_restart(self):
        server, tools = self.peer()
        try:
            payload, _ = self.call(server, tools, 'roots', {})
            self.assertEqual([name for name, _ in [(r['name'], r['path']) for r in payload['roots']]],
                             ['desk'])
            _, refused = self.call(server, tools, 'list_dir', {'root': 'elsewhere'})
            self.assertIn('unknown root', refused)

            record = self.store.request(self.extra, 'the tax notes')
            self.assertEqual(self.store.granted(), [], 'the ask must not be the grant')
            _, still = self.call(server, tools, 'list_dir', {'root': 'elsewhere'})
            self.assertIn('unknown root', still, 'a pending request must not open a root')

            self.store.approve(record['id'])
            payload, _ = self.call(server, tools, 'roots', {})
            self.assertEqual(sorted(r['name'] for r in payload['roots']), ['desk', 'elsewhere'],
                             'the same process must see the click')
            payload, _ = self.call(server, tools, 'read_file', {'root': 'elsewhere', 'path': 'tax.txt'})
            self.assertIn('tax notes', payload['text'])
        finally:
            server.stop()

    def test_a_revocation_stops_working_without_a_restart(self):
        server, tools = self.peer()
        try:
            self.store.approve(self.store.request(self.extra)['id'])
            payload, _ = self.call(server, tools, 'read_file', {'root': 'elsewhere', 'path': 'tax.txt'})
            self.assertIsNotNone(payload)
            self.store.revoke(self.extra)
            payload, text = self.call(server, tools, 'read_file', {'root': 'elsewhere', 'path': 'tax.txt'})
            self.assertIsNone(payload, 'a revoked root stayed readable until a restart')
        finally:
            server.stop()

    def test_a_symlink_still_cannot_escape_a_granted_root(self):
        server, tools = self.peer()
        try:
            self.store.approve(self.store.request(self.extra)['id'])
            (self.extra / 'out').symlink_to('/etc/passwd')
            payload, text = self.call(server, tools, 'read_file', {'root': 'elsewhere', 'path': 'out'})
            self.assertIsNone(payload)
            self.assertNotIn('root:x:', text, 'the refusal must not carry the file through')
        finally:
            server.stop()

    def test_a_denied_entry_in_the_grants_file_is_skipped_not_served(self):
        # Something other than the page wrote this file.  The server re-applies
        # the deny-list rather than trusting what it finds in it.
        self.store._write({'pending': [], 'granted': [{'realpath': '/etc', 'granted_at': 0}]})
        server, tools = self.peer()
        try:
            payload, _ = self.call(server, tools, 'roots', {})
            paths = [row['path'] for row in payload['roots']]
            self.assertNotIn('/etc', paths)
            self.assertTrue(any('/etc' in note for note in payload['granted_notes']),
                            'the skip must be reported, not silent')
        finally:
            server.stop()

    def test_an_unreadable_grants_file_keeps_the_reviewed_roots(self):
        server, tools = self.peer()
        try:
            self.grants.parent.mkdir(parents=True, exist_ok=True)
            self.grants.write_text('{"granted": [', encoding='utf-8')
            payload, _ = self.call(server, tools, 'roots', {})
            self.assertEqual([row['path'] for row in payload['roots']],
                             [os.path.realpath(str(self.desk))],
                             'a corrupt file must not widen or empty the roots')
            payload, _ = self.call(server, tools, 'read_file', {'path': 'a.txt'})
            self.assertIn('alpha', payload['text'], 'the reviewed root must keep working')
        finally:
            server.stop()

    def test_the_grants_file_can_never_be_the_only_root(self):
        completed = subprocess.run([sys.executable, SERVER, '--roots-file', str(self.grants)],
                                   capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 2,
                         'a file server whose scope is decided by a mutable file must not start')


class HttpRouteTests(unittest.TestCase):
    """The page's half.  This is the only path in the deployment that can grant."""

    @classmethod
    def setUpClass(cls):
        cls.base = workspace()
        cls.registry = registry_for(cls.base)
        config = types.SimpleNamespace(page=cls.base / 'chat.html', llm_url=None, https_port=None,
                                       tts_url='http://127.0.0.1:1', asr_url='http://127.0.0.1:1',
                                       host='127.0.0.1', port=0)
        cls.server = speech_ui.SpeechServer(('127.0.0.1', 0), config, registry=cls.registry)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.registry.close()

    def setUp(self):
        # Every test in this class shares one bridge and one grants file, so a
        # grant made by one would otherwise decide the outcome of the next --
        # including the ones whose whole job is to prove nothing was granted.
        approvals.Store(self.base / 'var' / 'approvals.json')._write({'pending': [], 'granted': []})

    def request(self, method, body=None, origin=None, path='/approvals'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        headers = {}
        if origin:
            headers['Origin'] = origin
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, {}

    def store(self):
        return agent_tools.approvals_store(config_for(self.base), self.base)

    def test_a_deployment_without_the_capability_shows_an_empty_queue(self):
        # A second listener, because the claim is about this route on a bridge
        # whose config never turned the feature on -- not about the one above.
        quiet = registry_for(self.base, False)
        config = types.SimpleNamespace(page=self.base / 'chat.html', llm_url=None, https_port=None,
                                       tts_url='http://127.0.0.1:1', asr_url='http://127.0.0.1:1',
                                       host='127.0.0.1', port=0)
        server = speech_ui.SpeechServer(('127.0.0.1', 0), config, registry=quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for method, body in (('GET', None), ('POST', {'decision': 'approve', 'id': 'x'})):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
                headers = {'Content-Type': 'application/json'} if body else {}
                connection.request(method, '/approvals',
                                   body=json.dumps(body) if body else None, headers=headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload, {'enabled': False, 'pending': [], 'granted': []},
                                 f'{method} on a disabled bridge must be a no-op, not a grant')
        finally:
            server.shutdown()
            server.server_close()
            quiet.close()

    def test_reading_the_queue_does_not_grant_anything(self):
        self.request('GET')
        self.assertEqual(self.store().granted(), [])

    def test_a_click_grants_and_the_queue_comes_back_clean(self):
        self.store().request(self.base / 'projects', 'notes')
        pending = self.store().pending()
        self.assertEqual(len(pending), 1)
        status, payload = self.request('POST', {'decision': 'approve', 'id': pending[0]['id']})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.store().granted_roots(),
                         [os.path.realpath(str(self.base / 'projects'))])
        self.assertEqual(payload['pending'], [])

    def test_a_click_cannot_name_a_directory_to_grant(self):
        # There is no body shape that says "read /etc": approve takes an id only.
        status, payload = self.request('POST', {'decision': 'approve', 'path': '/etc'})
        self.assertEqual(status, 400)
        self.assertEqual(self.store().granted(), [])

    def test_an_unknown_decision_a_stale_id_and_a_stray_shape_are_all_400(self):
        self.assertEqual(self.request('POST', {'decision': 'grant-everything', 'id': 'x'})[0], 400)
        self.assertEqual(self.request('POST', {'decision': 'approve', 'id': 'stale'})[0], 400)
        self.assertEqual(self.request('POST', [1, 2])[0], 400, 'a JSON array is not an approval')
        self.assertEqual(self.store().granted(), [], 'none of those may grant')

    def test_decline_and_revoke_round_trip(self):
        record = self.store().request(self.base / 'projects')
        self.assertEqual(self.request('POST', {'decision': 'decline', 'id': record['id']})[0], 200)
        self.assertEqual(self.store().pending(), [])
        self.assertEqual(self.store().granted(), [])
        record = self.store().request(self.base / 'projects')
        self.store().approve(record['id'])
        self.assertEqual(self.request('POST', {'decision': 'revoke',
                                               'path': approvals.canonical(self.base / 'projects')})[0], 200)
        self.assertEqual(self.store().granted_roots(), [])

    def test_a_foreign_page_can_neither_read_nor_click(self):
        self.assertEqual(self.request('GET', origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.request('POST', {'decision': 'approve', 'id': 'x'},
                                      origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.store().granted(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
