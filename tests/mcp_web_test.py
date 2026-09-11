"""The web-browsing MCP server, driven as a real subprocess peer.

Why subprocess and not import: "the model cannot reach this machine" is a
property of a *process* with a particular argv, not of a function.  Importing
mcp_web and calling tool_read_page() would leave the deployment -- a child
spawned by the bridge with the policy in its argv -- untested.

The awkward part is deliberate.  The server refuses loopback, and the fixture
site lives on loopback, so the functional instance is started through a
wrapper that widens exactly one predicate -- `_is_public_address` -- to admit
127.0.0.0/8 and nothing else.  A second, unpatched instance proves the refusals
are real.  Both are the same file with the same argv shape the bridge uses.
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import mcp_client

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

HERE = Path(__file__).resolve().parent
SERVER = str(HERE.parent / 'tools' / 'mcp_web.py')
DDG_FIXTURE = (HERE / 'fixtures' / 'web' / 'ddg.html').read_bytes()
SABOTAGE = '--sabotage' in sys.argv

# The loopback wrapper admits 127.0.0.0/8 so the fixture can be reached, and
# NOTHING else: 169.254.169.254 stays refused, which is what the redirect test
# relies on.  With --sabotage it also removes the per-hop check on redirects --
# the tempting refactor "the first URL was vetted, the redirect target is the
# server's business" -- and test_redirect_into_private_range_is_refused must
# then go red, because that refactor is precisely a door from a public page
# into the private network.
WRAPPER = '''
import socket, sys
from urllib.parse import urlsplit
sys.path.insert(0, {tools!r})
import mcp_web

_public = mcp_web._is_public_address
mcp_web._is_public_address = lambda address: address.is_loopback or _public(address)

if {sabotage!r}:
    _check = mcp_web._check_url
    def lax(url, policy, hop=0):
        if hop == 0:
            return _check(url, policy, hop)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts, host, socket.getaddrinfo(host, None)[0][4][0]   # never re-vetted
    mcp_web._check_url = lax

raise SystemExit(mcp_web.main())
'''


def _wrapper() -> Path:
    directory = Path(tempfile.mkdtemp(prefix='mcp-web-wrapper-'))
    script = directory / 'loopback.py'
    script.write_text(WRAPPER.format(tools=str(Path(SERVER).parent), sabotage=SABOTAGE))
    return script


# ------------------------------------------------------------------ the fixture site
PAGE = b'''<!doctype html><html><head><meta charset="utf-8"><title>Fixture &amp; Friends</title>
<style>body { color: red }</style><script>var secret = "SCRIPT_CONTENT";</script></head>
<body><h1>Welcome</h1><p>Tom &amp; Jerry say &eacute;l&eacute;phant &lt;3.</p>
<nav><a href="/final">Final page</a> <a href="/final#top">Final page again</a>
<a href="https://example.com/abs">Absolute</a> <a href="mailto:x@y.z">Mail</a>
<a href="javascript:alert(1)">JS</a></nav>
<noscript>NOSCRIPT_CONTENT</noscript>
<p>Second paragraph.</p></body></html>'''

CHALLENGE = b'''<html><head><title>DuckDuckGo</title></head><body>
<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<p>Please complete the following challenge to confirm this search was made by a human.</p>
</body></html>'''

OPENSEARCH = json.dumps(["tarra rum pump", ["Ta Ra Rum Pum", "Ta Ra Rum Pum (soundtrack)"],
                         ["2007 Indian film", "Album"],
                         ["https://en.wikipedia.org/wiki/Ta_Ra_Rum_Pum",
                          "https://en.wikipedia.org/wiki/Ta_Ra_Rum_Pum_(soundtrack)"]]).encode()

SEARXNG = json.dumps({"query": "x", "results": [
    {"title": "One", "url": "https://one.example/", "content": "first  result"},
    {"title": "Two", "url": "https://two.example/", "content": "second"},
    {"title": "Bad", "url": "ftp://three.example/", "content": "not web"}]}).encode()


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, kind='text/html; charset=utf-8', status=200, headers=()):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        self.server.posts += 1
        self._send(b'no', 'text/plain', 405)

    def do_GET(self):
        path, _, query = self.path.partition('?')
        self.server.hits.append(path)
        self.server.headers_seen.append(dict(self.headers))
        base = f'http://127.0.0.1:{self.server.server_port}'
        if path == '/page':
            return self._send(PAGE)
        if path == '/many-links':
            links = ''.join(f'<a href="/link/{n}">link {n}</a> ' for n in range(60))
            return self._send(f'<html><body>{links}</body></html>'.encode())
        if path == '/chain1':
            return self._redirect('/chain2')
        if path == '/chain2':
            return self._redirect(base + '/final')
        if path == '/final':
            return self._send(b'<html><head><title>Final</title></head><body>Final destination.</body></html>')
        if path == '/to-metadata':
            return self._redirect('http://169.254.169.254/latest/meta-data/')
        if path == '/to-secret':
            return self._redirect(base + '/secret')
        if path == '/secret':
            return self._send(b'<html><body>SECRET_PAGE</body></html>')
        if path == '/loop':
            return self._redirect('/loop')
        if path == '/big':
            return self._send(b'<html><body>' + b'word ' * 400_000 + b'</body></html>')
        if path == '/image.png':
            return self._send(b'\x89PNG\r\n\x1a\n' + b'\x00' * 64, 'image/png')
        if path == '/gz':
            return self._send(gzip.compress(b'<html><title>Zipped</title><body>GZIP_BODY</body></html>'),
                              headers=[('Content-Encoding', 'gzip')])
        if path == '/cp1252':
            return self._send('<html><body>café “quoted”</body></html>'.encode('cp1252'),
                              'text/html; charset=windows-1252')
        if path == '/text.txt':
            return self._send(b'plain  text\n\nwith <b>tags</b> kept', 'text/plain')
        if path == '/data.json':
            return self._send(b'{"a": 1}', 'application/json')
        if path == '/html/':
            return self._send(DDG_FIXTURE)
        if path == '/challenge/':
            return self._send(CHALLENGE)
        if path == '/api.php':
            return self._send(OPENSEARCH, 'application/json; charset=utf-8')
        if path == '/searx/search':
            return self._send(SEARXNG, 'application/json')
        return self._send(b'not found', 'text/plain', 404)


def site() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    server.hits, server.headers_seen, server.posts = [], [], 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def serve(argv, patched=True, timeout=25.0):
    command = [sys.executable, str(_wrapper()) if patched else SERVER] + list(argv)
    return mcp_client.MCPServer('web', command[0], command[1:], {}, timeout, [], [])


class WebMCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = site()
        cls.base = f'http://127.0.0.1:{cls.site.server_port}'
        common = ['--host-interval', '0', '--timeout', '3',
                  '--search-url', cls.base + '/html/', '--wikipedia-url', cls.base + '/api.php']
        cls.web = serve(common)
        cls.tools = {tool.name: tool for tool in cls.web.start()}
        cls.strict = serve([], patched=False)
        cls.strict_tools = {tool.name: tool for tool in cls.strict.start()}
        cls.instances = [cls.web, cls.strict]

    @classmethod
    def tearDownClass(cls):
        for instance in cls.instances:
            instance.stop()
        cls.site.shutdown()
        cls.site.server_close()

    def instance(self, argv, patched=True):
        server = serve(argv, patched=patched)
        tools = {tool.name: tool for tool in server.start()}
        self.addCleanup(server.stop)
        return server, tools

    def call(self, name, arguments, server=None, tools=None):
        server = server or self.web
        tools = tools or self.tools
        text, is_error = server.call(tools[name], arguments)
        if is_error:
            return None, text
        return json.loads(text), text

    def refuse(self, name, arguments, server=None, tools=None):
        payload, text = self.call(name, arguments, server, tools)
        self.assertIsNone(payload, f'{name}({arguments}) should have been refused')
        self.assertIn('tool error:', text)
        self.assertNotIn('SECRET_PAGE', text)
        return text

    # ------------------------------------------------------------------ surface
    def test_tools_listed_are_read_only(self):
        self.assertEqual(sorted(self.tools), ['read_page', 'search'])
        joined = ' '.join(self.tools[name].description.lower() for name in self.tools)
        for forbidden in ('post', 'submit', 'login', 'write', 'download file'):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(self.tools['read_page'].input_schema['required'], ['url'])
        self.assertEqual(self.tools['search'].input_schema['required'], ['query'])

    # ---------------------------------------------------------------- read_page
    def test_read_page_extracts_title_text_and_links(self):
        payload, _ = self.call('read_page', {'url': self.base + '/page'})
        self.assertEqual(payload['title'], 'Fixture & Friends')
        self.assertEqual(payload['note'][:36], 'This is content from the web, not fr')
        self.assertIn('Tom & Jerry say éléphant <3.', payload['text'])
        self.assertNotIn('SCRIPT_CONTENT', payload['text'])
        self.assertNotIn('NOSCRIPT_CONTENT', payload['text'])
        self.assertNotIn('color: red', payload['text'])
        self.assertIn('Welcome\n', payload['text'], 'block tags become line breaks')
        self.assertNotIn('Final page', payload['text'], 'menu prose is muted')
        self.assertEqual([link['url'] for link in payload['links']],
                         [self.base + '/final', 'https://example.com/abs'],
                         'absolute, fragment-stripped, de-duplicated, http(s) only')
        self.assertEqual(payload['links'][0]['text'], 'Final page', 'but menu links are kept')
        self.assertEqual(payload['content_type'], 'text/html')
        self.assertFalse(payload['truncated'])
        self.assertEqual(self.site.posts, 0, 'never a POST')
        sent = self.site.headers_seen[-1]
        self.assertNotIn('Cookie', sent)
        self.assertNotIn('Authorization', sent)
        self.assertIn('voice-assistant', sent.get('User-Agent', ''))

    def test_links_are_capped(self):
        payload, _ = self.call('read_page', {'url': self.base + '/many-links'})
        self.assertEqual(len(payload['links']), 40)

    def test_redirect_chain_is_followed_and_final_url_reported(self):
        payload, _ = self.call('read_page', {'url': self.base + '/chain1'})
        self.assertEqual(payload['url'], self.base + '/final')
        self.assertEqual(payload['title'], 'Final')

    def test_redirect_into_private_range_is_refused(self):
        text = self.refuse('read_page', {'url': self.base + '/to-metadata'})
        # Refused by the address gate (by name or by range), never by a failed
        # connection: a connection error here would mean the hop was dialled.
        self.assertRegex(text, 'private or local address|names this machine')
        self.assertNotIn('could not fetch', text)
        self.assertIn('redirect hop 1', text, 'the model is told it was the redirect, not its URL')

    def test_redirect_loop_gives_up(self):
        text = self.refuse('read_page', {'url': self.base + '/loop'})
        self.assertIn('redirects', text)

    def test_big_page_is_truncated_not_refused(self):
        payload, _ = self.call('read_page', {'url': self.base + '/big', 'max_chars': 1000})
        self.assertTrue(payload['truncated'])
        self.assertEqual(len(payload['text']), 1000)
        self.assertEqual(payload['next_start'], 1000)
        self.assertIn('bytes_note', payload, 'a page over the byte cap says so')
        self.assertGreater(payload['chars_total'], 1000)

    def test_paging_with_start(self):
        first, _ = self.call('read_page', {'url': self.base + '/page', 'max_chars': 500})
        page, _ = self.call('read_page', {'url': self.base + '/page', 'max_chars': 500, 'start': 8})
        self.assertEqual(page['text'], first['text'][8:])
        self.assertEqual(page['start'], 8)

    def test_png_is_refused_without_content(self):
        text = self.refuse('read_page', {'url': self.base + '/image.png'})
        self.assertIn('image/png, not text', text)
        self.assertNotIn('PNG', text)

    def test_gzip_is_handled(self):
        payload, _ = self.call('read_page', {'url': self.base + '/gz'})
        self.assertEqual(payload['title'], 'Zipped')
        self.assertIn('GZIP_BODY', payload['text'])

    def test_windows_1252_is_decoded(self):
        payload, _ = self.call('read_page', {'url': self.base + '/cp1252'})
        self.assertIn('café “quoted”', payload['text'])

    def test_plain_text_and_json_come_back_raw(self):
        payload, _ = self.call('read_page', {'url': self.base + '/text.txt'})
        self.assertIn('with <b>tags</b> kept', payload['text'])
        self.assertEqual(payload['links'], [])
        payload, _ = self.call('read_page', {'url': self.base + '/data.json'})
        self.assertEqual(payload['content_type'], 'application/json')
        self.assertEqual(json.loads(payload['text']), {'a': 1})

    def test_bad_arguments_are_refusals(self):
        self.refuse('read_page', {})
        text = self.refuse('read_page', {'url': self.base + '/page', 'max_chars': 'lots'})
        self.assertIn('max_chars must be an integer', text)
        self.assertIn('404', self.refuse('read_page', {'url': self.base + '/missing'}))

    # ------------------------------------------------------------ the address gate
    def test_local_and_private_targets_are_refused_by_the_real_server(self):
        for url in (self.base + '/secret', 'http://localhost/', 'http://10.0.0.1/',
                    'http://169.254.169.254/latest/meta-data/', 'http://[::1]/',
                    'http://192.168.1.1/', 'http://metadata.google.internal/',
                    'http://printer.local/', 'http://db.internal/', 'http://[::ffff:127.0.0.1]/'):
            text = self.refuse('read_page', {'url': url}, self.strict, self.strict_tools)
            self.assertTrue('private or local' in text or 'this machine' in text, f'{url}: {text}')
        self.assertEqual(self.site.hits.count('/secret'), 0, 'the fixture was never dialled')

    def test_credentials_and_other_schemes_are_refused(self):
        self.assertIn('credentials', self.refuse('read_page', {'url': 'http://user:pw@example.com/'},
                                                 self.strict, self.strict_tools))
        for url in ('ftp://example.com/', 'file:///etc/passwd', 'gopher://example.com/', 'example.com'):
            self.assertIn('only http and https', self.refuse('read_page', {'url': url},
                                                             self.strict, self.strict_tools))

    def test_search_is_gated_the_same_way(self):
        # The search backend URL is a fetch like any other: an unpatched server
        # pointed at loopback refuses it, so a config cannot turn search into a
        # local scanner.
        server, tools = self.instance(['--search-url', self.base + '/html/', '--host-interval', '0',
                                       '--wikipedia-url', self.base + '/api.php', '--timeout', '3'],
                                      patched=False)
        text = self.refuse('search', {'query': 'anything'}, server, tools)
        self.assertIn('private or local', text)

    def test_allow_list_restricts(self):
        server, tools = self.instance(['--allow-host', 'example.com', '--host-interval', '0', '--timeout', '3'])
        text = self.refuse('read_page', {'url': self.base + '/page'}, server, tools)
        self.assertIn('not on this server\'s allow list', text)

    def test_deny_list_wins(self):
        server, tools = self.instance(['--allow-host', '127.0.0.1', '--deny-host', '127.0.0.1',
                                       '--host-interval', '0', '--timeout', '3'])
        text = self.refuse('read_page', {'url': self.base + '/page'}, server, tools)
        self.assertIn('deny list', text)

    def test_per_minute_cap(self):
        server, tools = self.instance(['--per-minute', '3', '--host-interval', '0', '--timeout', '3'])
        for _ in range(3):
            payload, text = self.call('read_page', {'url': self.base + '/final'}, server, tools)
            self.assertIsNotNone(payload, text)
        text = self.refuse('read_page', {'url': self.base + '/final'}, server, tools)
        self.assertIn('last minute', text)

    # ------------------------------------------------------------------- search
    def test_ddg_results_are_parsed_and_ads_dropped(self):
        payload, _ = self.call('search', {'query': 'ta ra rum pum film'})
        self.assertEqual(payload['backend'], 'ddg')
        self.assertEqual([row['url'] for row in payload['results']],
                         ['https://en.wikipedia.org/wiki/Ta_Ra_Rum_Pum',
                          'https://www.imdb.com/title/tt0833553/',
                          'https://www.example.org/review/ta-ra-rum-pum'])
        self.assertEqual(payload['results'][0]['title'], 'Ta Ra Rum Pum - Wikipedia')
        self.assertIn('Saif Ali Khan & Rani Mukerji', payload['results'][0]['snippet'])
        self.assertNotIn('<b>', payload['results'][0]['snippet'])
        self.assertEqual(payload['results'][1]['snippet'], "A racing driver's family faces hard times after a crash.")
        joined = json.dumps(payload)
        self.assertNotIn('Sponsored', joined)
        self.assertNotIn('example-shop', joined)
        self.assertIn('/html/', self.site.hits[-1])

    def test_max_results_limits_search(self):
        payload, _ = self.call('search', {'query': 'ta ra rum pum film', 'max_results': 1})
        self.assertEqual(len(payload['results']), 1)

    def test_challenge_page_falls_back_to_wikipedia(self):
        server, tools = self.instance(['--search-url', self.base + '/challenge/', '--host-interval', '0',
                                       '--wikipedia-url', self.base + '/api.php', '--timeout', '3'])
        payload, _ = self.call('search', {'query': 'tarra rum pump'}, server, tools)
        self.assertEqual(payload['backend'], 'wikipedia (fallback)')
        self.assertIn('rate-limited', payload['fallback'])
        self.assertEqual(payload['results'][0]['title'], 'Ta Ra Rum Pum')
        self.assertEqual(payload['results'][0]['url'], 'https://en.wikipedia.org/wiki/Ta_Ra_Rum_Pum')
        self.assertEqual(payload['results'][0]['snippet'], '2007 Indian film')

    def test_searxng_backend(self):
        server, tools = self.instance(['--search', 'searxng=' + self.base + '/searx', '--host-interval', '0',
                                       '--timeout', '3'])
        payload, text = self.call('search', {'query': 'one two'}, server, tools)
        self.assertIsNotNone(payload, text)
        self.assertEqual(payload['backend'], 'searxng=' + self.base + '/searx')
        self.assertEqual([row['url'] for row in payload['results']], ['https://one.example/', 'https://two.example/'])
        self.assertEqual(payload['results'][0]['snippet'], 'first result')

    def test_search_query_bounds(self):
        self.assertIn('at least 2', self.refuse('search', {'query': 'a'}))
        self.assertIn('longer than', self.refuse('search', {'query': 'x' * 301}))
        self.assertIn('max_results must be an integer', self.refuse('search', {'query': 'ab', 'max_results': '5'}))

    # ----------------------------------------------------------------- protocol
    def test_unknown_tool_is_an_error_not_a_crash(self):
        result = self.web.request('tools/call', {'name': 'post_form', 'arguments': {}}, timeout=10)
        self.assertTrue(result.get('isError'))
        self.assertIn('unknown tool', str(result))

    def test_arguments_must_be_an_object(self):
        result = self.web.request('tools/call', {'name': 'search', 'arguments': 'not-an-object'}, timeout=10)
        self.assertTrue(result.get('isError'))
        self.assertIn('arguments must be an object', str(result))

    def test_garbage_on_stdin_does_not_kill_the_server(self):
        for junk in ('', '   ', 'not json', '[1,2,3]', '{"no_id": true}'):
            self.web._write({'jsonrpc': '2.0', 'method': 'log', 'params': {'m': junk}})
        payload, _ = self.call('read_page', {'url': self.base + '/final'})
        self.assertEqual(payload['title'], 'Final', 'still answering after malformed input')


class WebMCPProcessTests(unittest.TestCase):
    def run_server(self, argv, timeout=15):
        return subprocess.run([sys.executable, SERVER] + argv, input='', capture_output=True,
                              text=True, timeout=timeout)

    def test_doctor_names_the_policy(self):
        result = self.run_server(['--doctor', '--allow-host', 'en.wikipedia.org', '--deny-host', 'evil.example',
                                  '--per-minute', '12'])
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ('search, read_page', 'en.wikipedia.org', 'evil.example', '12/minute',
                         'loopback', 'every redirect hop', 'GET only', 'not implemented'):
            self.assertIn(expected, result.stdout)

    def test_there_is_no_allow_private_flag(self):
        result = self.run_server(['--allow-private'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized', result.stderr)

    def test_unknown_search_backend_refuses_to_start(self):
        result = self.run_server(['--search', 'google'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('unknown --search backend', result.stderr)


if __name__ == '__main__':
    sys.argv = [value for value in sys.argv if value != '--sabotage']
    unittest.main(verbosity=2)
