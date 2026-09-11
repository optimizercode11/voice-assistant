"""The read-only filesystem MCP server, driven as a real subprocess peer.

Why subprocess and not import: the containment guarantees are properties of a
*process* with a particular argv, not of a function.  Importing mcp_files and
calling tool_read_file() would test the logic while leaving the actual
deployment -- a child spawned by the bridge with --root X -- untested.

The adversarial cases here are the point.  A file tool that works is easy; the
claim worth testing is that a model saying "read escape" or "read ../etc/passwd"
does not get /etc/passwd.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import mcp_client

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

SERVER = str(Path(__file__).resolve().parents[1] / 'tools' / 'mcp_files.py')
SABOTAGE = '--sabotage' in sys.argv

# The paired negative for "containment is decided after symlink resolution".
# The tempting refactor is "we already rejected '..' and absolute paths, so a
# plain join is enough" -- which is exactly the hole, because the argument
# layer never sees a symlink.  Both halves of that one claim go together here:
# resolve without realpath, and trust the pre-open check instead of asking the
# descriptor where it actually landed.  Applied only to this one server
# instance, so the sabotage breaks the escape tests and nothing else.
SABOTAGE_WRAPPER = '''
import os, stat, sys
sys.path.insert(0, {tools!r})
import mcp_files

def resolve(self, root_name=None, relative=""):
    name, real = self.pick(root_name)
    rel = mcp_files._clean_relative(relative)
    if not rel:
        return name, real
    probe = os.path.abspath(os.path.join(real, rel))     # never resolves the link
    self.contain(real, probe)
    return name, probe

def open_read(roots, path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))   # trusts the pre-check
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        raise mcp_files.Refused("that path is a directory; use list_dir")
    if not stat.S_ISREG(info.st_mode):
        raise mcp_files.Refused("not a regular file")
    return fd, info.st_size

mcp_files.Roots.resolve = resolve
mcp_files._open_read = open_read
raise SystemExit(mcp_files.main())
'''


def desk() -> Path:
    """A small tree with the traps a real checkout has: a link out, a binary, a .git."""
    root = Path(tempfile.mkdtemp(prefix='mcp-files-desk-'))
    (root / 'a.txt').write_text('alpha\nbeta gamma\nomega\n')
    # Comfortably over the 64 KiB read cap, so byte_capped is observable rather
    # than theoretical.  Kept to one word per line: prose here would satisfy the
    # two-word regex below and turn that assertion into a count of this file.
    (root / 'big.txt').write_text(''.join(f'filler{n}\n' for n in range(20000)))
    (root / 'words.txt').write_text('one\none two\none two three\n')
    (root / 'bin.dat').write_bytes(b'x\x00y\x00z\n')
    (root / 'sub').mkdir()
    (root / 'sub' / 'hidden.md').write_text('# secret\n')
    (root / '.git').mkdir()
    (root / '.git' / 'config').write_text('[core]\n\trepositoryformatversion = 0\n')
    (root / 'escape').symlink_to('/etc/passwd')
    (root / 'etclink').symlink_to('/etc')
    (root / 'sub' / 'up').symlink_to('..')
    return root


def serve(roots, timeout=20.0):
    command = [sys.executable, SERVER] if not SABOTAGE else [sys.executable, str(_wrapper())]
    for entry in roots:
        command += ['--root', str(entry)]
    return mcp_client.MCPServer('files', command[0], command[1:], {}, timeout, [], [])


def _wrapper() -> Path:
    directory = Path(tempfile.mkdtemp(prefix='mcp-files-sabotage-'))
    script = directory / 'sabotaged.py'
    script.write_text(SABOTAGE_WRAPPER.format(tools=str(Path(SERVER).parent)))
    return script


class FileMCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desk = desk()
        cls.server = serve([cls.desk])
        cls.tools = {tool.name: tool for tool in cls.server.start()}

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def call(self, name, arguments):
        text, is_error = self.server.call(self.tools[name], arguments)
        if is_error:
            return None, text
        try:
            return json.loads(text), text
        except ValueError:
            return None, text

    def refuse(self, name, arguments):
        """Assert the call is refused, and that nothing sensitive came back with the refusal."""
        payload, text = self.call(name, arguments)
        # Deliberately not repr()ing the payload: when this fails because a file really
        # did leak, repr() would copy that file into the CI log.
        self.assertIsNone(payload, f'{name}({arguments}) should have been refused')
        self.assertIn('tool error:', text)
        self.assertNotIn('root:x:', text, 'the refusal must not carry /etc/passwd through')
        return text

    # ------------------------------------------------------------------ surface

    def test_tools_listed_are_read_only(self):
        self.assertEqual(sorted(self.tools), ['find', 'grep', 'list_dir', 'read_file', 'roots'])
        joined = ' '.join(sorted(self.tools))
        for forbidden in ('write', 'delete', 'remove', 'mkdir', 'move', 'rename', 'chmod', 'edit'):
            self.assertNotIn(forbidden, joined, f'a read-only server must not offer {forbidden}')

    def test_roots_reports_what_is_exposed(self):
        payload, _ = self.call('roots', {})
        self.assertEqual(len(payload['roots']), 1)
        self.assertEqual(os.path.realpath(str(self.desk)), payload['roots'][0]['path'])
        self.assertIn('.git', payload['ignore'])

    # --------------------------------------------------------------------- happy

    def test_list_dir_hides_dotfiles_by_default(self):
        payload, _ = self.call('list_dir', {})
        names = [entry['name'] for entry in payload['entries']]
        self.assertIn('a.txt', names)
        self.assertNotIn('.git', names, 'hidden entries need include_hidden')

    def test_read_file_returns_numbered_lines(self):
        payload, _ = self.call('read_file', {'path': 'a.txt'})
        self.assertEqual(payload['lines_total'], 3)
        self.assertIn('2\tbeta gamma', payload['text'])

    def test_read_file_pages_with_offset(self):
        payload, _ = self.call('read_file', {'path': 'a.txt', 'offset': 3, 'limit': 1})
        self.assertEqual(payload['lines_returned'], 1)
        self.assertIn('omega', payload['text'])
        self.assertNotIn('alpha', payload['text'])

    def test_grep_reports_file_and_line_number(self):
        payload, _ = self.call('grep', {'pattern': 'mma$'})
        self.assertEqual(payload['count'], 1)
        self.assertEqual(payload['matches'][0]['file'], 'a.txt')
        self.assertEqual(payload['matches'][0]['line'], 2)

    def test_grep_ignore_case_and_glob(self):
        exact = self.call('grep', {'pattern': 'ALPHA'})[0]
        self.assertEqual(exact['count'], 0)
        loose = self.call('grep', {'pattern': 'ALPHA', 'ignore_case': True})[0]
        self.assertEqual(loose['count'], 1)
        missed = self.call('grep', {'pattern': 'alpha', 'glob': '*.md'})[0]
        self.assertEqual(missed['count'], 0)

    def test_grep_regex_metacharacters_are_regex(self):
        scoped = {'path': 'words.txt'}
        self.assertEqual(self.call('grep', {'pattern': r'^\w+\s+\w+$', **scoped})[0]['count'], 1,
                         'anchors and \s must be regex, not literals: one line has exactly two words')
        self.assertEqual(self.call('grep', {'pattern': r'\w+ three$', **scoped})[0]['count'], 1)
        self.assertEqual(self.call('grep', {'pattern': '^one two three$', **scoped})[0]['count'], 1)

    def test_find_by_glob(self):
        payload, _ = self.call('find', {'glob': '*.md'})
        self.assertEqual([row['path'] for row in payload['results']], ['sub/hidden.md'])

    # -------------------------------------------------------------- containment

    def test_dotdot_refused(self):
        self.refuse('read_file', {'path': '../etc/passwd'})
        self.refuse('read_file', {'path': 'sub/../../etc/passwd'})
        self.refuse('grep', {'pattern': 'root', 'path': '../..'})

    def test_absolute_and_home_refused(self):
        self.refuse('read_file', {'path': '/etc/passwd'})
        self.refuse('read_file', {'path': '~/../../etc/passwd'})
        self.refuse('read_file', {'path': r'C:\Windows\win.ini'})

    def test_symlink_to_file_cannot_escape(self):
        text = self.refuse('read_file', {'path': 'escape'})
        self.assertIn('outside the configured root', text)

    def test_symlink_to_directory_cannot_escape(self):
        self.refuse('read_file', {'path': 'etclink/passwd'})
        self.refuse('read_file', {'path': 'sub/up/up/up/etc/passwd'})

    def test_walk_never_follows_links_out(self):
        """grep over the whole root must not read through `escape` into /etc/passwd."""
        payload, _ = self.call('grep', {'pattern': 'root:.*:0:0'})
        self.assertIsNotNone(payload, 'the search itself must succeed')
        self.assertEqual(payload['count'], 0)
        self.assertEqual([row['file'] for row in payload['matches']], [])

    def test_dot_git_is_not_walkable(self):
        payload, _ = self.call('grep', {'pattern': 'repositoryformatversion'})
        self.assertEqual(payload['count'], 0, '.git is pruned from the walk')
        found = self.call('find', {'glob': 'config'})[0]
        self.assertEqual(found['count'], 0)

    def test_binary_refused_without_content(self):
        payload, _ = self.call('read_file', {'path': 'bin.dat'})
        self.assertTrue(payload['binary'])
        self.assertEqual(payload['text'], '')

    def test_grep_skips_binary_and_keeps_going(self):
        payload, _ = self.call('grep', {'pattern': 'alpha'})
        self.assertGreaterEqual(payload['binary_or_unreadable_skipped'], 1, 'bin.dat was skipped')
        self.assertEqual(payload['count'], 1, 'one binary must not end the search')

    def test_directory_says_use_list_dir(self):
        text = self.refuse('read_file', {'path': 'sub'})
        self.assertIn('list_dir', text)

    # -------------------------------------------------------------------- caps

    def test_read_byte_cap_is_reported_not_silent(self):
        payload, _ = self.call('read_file', {'path': 'big.txt'})
        self.assertIsNotNone(payload, 'a capped read must still return parseable JSON')
        self.assertTrue(payload['byte_capped'], 'a capped read must say so')
        self.assertTrue(payload['truncated'])

    def test_a_huge_result_is_trimmed_not_sliced(self):
        """The output cap must never hand back JSON that does not parse.

        Slicing the serialized string is the tempting implementation and it is
        the bug: the model receives a document that is not JSON, so an
        over-long answer becomes an unreadable one.
        """
        raw = self.server.call(self.tools['read_file'], {'path': 'big.txt'})[0]
        json.loads(raw)                                   # must not raise
        self.assertLessEqual(len(raw), 24000)

    def test_grep_match_cap(self):
        payload, _ = self.call('grep', {'pattern': 'filler', 'max_matches': 5})
        self.assertLessEqual(payload['count'], 5)
        self.assertTrue(payload['hit_limit'])

    # ------------------------------------------------------------- robustness

    def test_invalid_regex_is_an_error_not_a_crash(self):
        self.refuse('grep', {'pattern': '('})
        payload, _ = self.call('grep', {'pattern': 'alpha'})
        self.assertEqual(payload['count'], 1, 'the server must still answer afterwards')

    def test_unknown_tool_is_an_error_not_a_crash(self):
        text = self.refuse('no_such_tool', {}) if 'no_such_tool' in self.tools else None
        payload, _ = self.call('grep', {'pattern': 'alpha'})
        self.assertEqual(payload['count'], 1)


class FileMCPProcessTests(unittest.TestCase):
    """Startup refusals: these are argv behaviour, so they run the binary directly."""

    def run_server(self, argv, timeout=15):
        return subprocess.run([sys.executable, SERVER] + argv, input='', capture_output=True,
                              text=True, timeout=timeout)

    def test_no_roots_refuses_to_start(self):
        result = self.run_server([])
        self.assertEqual(result.returncode, 2)
        self.assertIn('no --root', result.stderr)
        self.assertNotIn('/', result.stdout, 'it must not serve anything first')

    def test_missing_root_directory_refuses(self):
        result = self.run_server(['--root', '/definitely/not/a/directory'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('not a directory', result.stderr)

    def test_doctor_names_every_exposed_directory(self):
        tree = desk()
        result = self.run_server(['--root', str(tree), '--doctor'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(os.path.realpath(str(tree)), result.stdout)
        self.assertIn('.git', result.stdout)
        self.assertIn('not implemented', result.stdout)

    def test_a_scope_refusal_points_at_request_directory_only_when_grants_exist(self):
        # Measured live 2026-09-11: refused with "absolute paths are not
        # accepted", the model tried an invented tool name and never reached
        # request_directory.  With a grants file the refusal names the way out;
        # without one there is no such tool, so it must not be promised.
        tree = desk()
        grants = Path(tempfile.mkdtemp(prefix='mcp-files-grants-')) / 'approvals.json'
        grants.write_text('{"pending": [], "granted": []}')
        with_grants = mcp_client.MCPServer('files', sys.executable,
                                           [SERVER, '--root', str(tree), '--roots-file', str(grants)],
                                           {}, 15.0, [], [])
        without = mcp_client.MCPServer('files', sys.executable, [SERVER, '--root', str(tree)], {}, 15.0, [], [])
        try:
            tools = {tool.name: tool for tool in with_grants.start()}
            for arguments in ({'path': '/etc'}, {'path': '../up'}, {'path': 'x', 'root': 'nope'}):
                text, is_error = with_grants.call(tools['list_dir'], arguments)
                self.assertTrue(is_error, arguments)
                self.assertIn('request_directory', text, arguments)
            text, _ = with_grants.call(tools['roots'], {})
            self.assertIn('request_directory', json.loads(text)['note'])
            # A refusal that is not about scope (a bad regex) stays a plain refusal.
            text, is_error = with_grants.call(tools['grep'], {'pattern': '('})
            self.assertTrue(is_error)
            self.assertNotIn('request_directory', text)
            plain = {tool.name: tool for tool in without.start()}
            text, is_error = without.call(plain['list_dir'], {'path': '/etc'})
            self.assertTrue(is_error)
            self.assertNotIn('request_directory', text, 'no grants file, no such tool to point at')
            text, _ = without.call(plain['roots'], {})
            self.assertNotIn('note', json.loads(text))
        finally:
            with_grants.stop()
            without.stop()

    def test_several_roots_must_be_named_by_the_caller(self):
        first, second = desk(), desk()
        instance = serve([first, second])
        try:
            listed = {tool.name: tool for tool in instance.start()}
            text, is_error = instance.call(listed['list_dir'], {})
            self.assertTrue(is_error, 'with two roots an unnamed call is a guess about which one')
            self.assertIn('name which one', text)
            text, is_error = instance.call(listed['list_dir'], {'root': os.path.basename(
                os.path.realpath(str(second)))})
            self.assertFalse(is_error, text)
        finally:
            instance.stop()

    def test_garbage_on_stdin_does_not_kill_the_server(self):
        instance = serve([desk()])
        try:
            listed = {tool.name: tool for tool in instance.start()}
            for junk in ('', '   ', 'not json', '[1,2,3]', '{"no_id": true}'):
                instance._write({'jsonrpc': '2.0', 'method': 'log', 'params': {'m': junk}})
            payload = json.loads(instance.call(listed['grep'], {'pattern': 'alpha'})[0])
            self.assertEqual(payload['count'], 1, 'still answering after malformed input')
        finally:
            instance.stop()

    def test_arguments_must_be_an_object(self):
        instance = serve([desk()])
        try:
            instance.start()
            result = instance.request('tools/call', {'name': 'grep', 'arguments': 'not-an-object'},
                                      timeout=10)
            self.assertTrue(result.get('isError'), 'a malformed call is a refusal, not a hang')
            self.assertIn('arguments must be an object', str(result))
        finally:
            instance.stop()


if __name__ == '__main__':
    sys.argv = [value for value in sys.argv if value != '--sabotage']
    unittest.main(verbosity=2)
