"""The registry and the config: what gets offered, and what refuses to start."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import agent_config
import agent_tools

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, text):
        path = self.root / 'assistant.toml'
        path.write_text(text)
        return path

    def test_no_file_named_means_the_conservative_defaults(self):
        config = agent_config.load('')
        self.assertEqual(config.builtins, {'now': True})
        self.assertFalse(config.retrieval.enabled)
        self.assertFalse(config.fetch.enabled)
        self.assertEqual(config.mcp, [])

    def test_a_named_file_that_is_missing_is_fatal(self):
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load(self.root / 'nope.toml')

    def test_an_unknown_key_is_fatal_rather_than_typo_tolerated(self):
        for text in ('[limitts]\nrounds = 2\n', '[retrieval]\nenabled = "yes"\n', 'bogus = 1\n',
                     '[[mcp.server]]\nname = "a"\ncommand = "x"\nextra = 1\n'):
            with self.assertRaises(agent_config.ConfigError, msg=text):
                agent_config.load(self.write(text))

    def test_fetch_without_an_allow_list_refuses_to_start(self):
        with self.assertRaises(agent_config.ConfigError) as caught:
            agent_config.load(self.write('[fetch]\nenabled = true\n'))
        self.assertIn('allow_hosts', str(caught.exception))

    def test_limits_are_clamped_not_trusted(self):
        config = agent_config.load(self.write('[limits]\nrounds = 99\nper_call_seconds = 100000\n'
                                              'result_chars = 99999999\nturn_seconds = 99999\n'))
        self.assertLessEqual(config.limits.rounds, agent_config.MAX_TOOL_ROUNDS)
        self.assertLessEqual(config.limits.per_call_seconds, 120)
        self.assertLessEqual(config.limits.result_chars, agent_config.MAX_TOOL_RESULT_CHARS)
        self.assertLessEqual(config.limits.turn_seconds, 600)

    def test_mcp_servers_are_named_checked_and_deduplicated(self):
        good = '[[mcp.server]]\nname = "desk"\ncommand = "/bin/true"\nargs = ["--stdio"]\n' \
               'env = { TOKEN = "x" }\nallow = ["a"]\nden = ["b"]\n'.replace('den', 'deny')
        self.assertEqual(len(agent_config.load(self.write(good)).mcp), 1)
        for text in ('[[mcp.server]]\nname = "bad name"\ncommand = "x"\n',
                     '[[mcp.server]]\nname = "a"\ncommand = ""\n',
                     '[[mcp.server]]\nname = "a"\ncommand = "x"\n[[mcp.server]]\nname = "a"\ncommand = "y"\n',
                     '[[mcp.server]]\nname = "a"\ncommand = "x"\nargs = "not a list"\n'):
            with self.assertRaises(agent_config.ConfigError, msg=text):
                agent_config.load(self.write(text))

    def test_the_environment_selects_the_file_and_a_broken_one_still_fails(self):
        path = self.write('[builtins]\nnow = false\n')
        os.environ['VOICE_TOOLS_CONFIG'] = str(path)
        self.addCleanup(os.environ.pop, 'VOICE_TOOLS_CONFIG', None)
        self.assertEqual(agent_config.load(None).builtins, {'now': False})
        os.environ['VOICE_TOOLS_CONFIG'] = str(self.root / 'gone.toml')
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load(None)

    def test_too_many_tools_is_refused_before_the_engine_sees_them(self):
        rows = ''.join(f'[[mcp.server]]\nname = "s{n}"\ncommand = "/bin/true"\n'
                       for n in range(agent_config.MAX_TOOLS + 1))
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load(self.write(rows))


class ValidatorTests(unittest.TestCase):
    def test_types_enums_and_bounds(self):
        cases = [
            ({'type': 'object', 'required': ['a']}, {}, ['missing required a']),
            ({'type': 'object', 'properties': {'a': {'type': 'integer'}}}, {'a': True},
             ['a must be integer, got bool']),
            ({'type': 'object', 'properties': {'a': {'type': 'number', 'minimum': 2}}}, {'a': 1},
             ['a must be >= 2']),
            ({'type': 'object', 'properties': {'a': {'type': 'string', 'enum': ['x']}}}, {'a': 'y'},
             ["a must be one of ['x']"]),
            ({'type': 'object', 'properties': {'a': {'type': 'array', 'items': {'type': 'string'},
                                                     'maxItems': 1}}}, {'a': ['x', 'y']},
             ['a has more than 1 items']),
            ({'type': 'object', 'properties': {'a': {'type': 'object',
                                                     'properties': {'b': {'type': 'string', 'pattern': '^d+$'}}}}},
             {'a': {'b': 'xy'}}, ['a.b does not match the required pattern']),
        ]
        for schema, value, expected in cases:
            problems = agent_tools.validate(schema, value)
            self.assertEqual(problems, expected, f'{schema} {value} -> {problems}')

    def test_unknown_keywords_are_ignored_but_known_ones_are_not(self):
        schema = {'$schema': 'http://json-schema.org/draft-07/schema#', 'additionalProperties': False,
                  'default': {}, 'type': 'object', 'properties': {'a': {'type': 'string'}}}
        self.assertEqual(agent_tools.validate(schema, {'a': 'ok', 'b': 1}), [])
        self.assertTrue(agent_tools.validate(schema, {'a': 3}))

    def test_a_tool_with_no_declared_schema_accepts_an_object(self):
        self.assertEqual(agent_tools.validate({}, {'anything': 1}), [])


class RegistryTests(unittest.TestCase):
    def test_defaults_offer_only_now(self):
        registry = agent_tools.Registry(agent_config.load(''))
        self.addCleanup(registry.close)
        self.assertEqual(registry.names(), ['now'])
        self.assertEqual(registry.specs()[0]['function']['name'], 'now')

    def test_a_disabled_builtin_is_absent_from_the_spec_list(self):
        config = agent_config.load('')
        config.builtins = {'now': False}
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        self.assertEqual(registry.names(), [])

    def test_unknown_tool_names_what_does_exist(self):
        registry = agent_tools.Registry(agent_config.load(''))
        self.addCleanup(registry.close)
        result = registry.execute('search_notes', '{}')
        self.assertFalse(result.ok)
        self.assertIn('no tool called', result.error)

    def test_malformed_arguments_are_the_models_to_fix(self):
        registry = agent_tools.Registry(agent_config.load(''))
        self.addCleanup(registry.close)
        for arguments in ('not json', '[1,2]', '"a string"', '{"zone": 4}'):
            result = registry.execute('now', arguments)
            self.assertFalse(result.ok, arguments)
            self.assertTrue(result.for_model().startswith('tool error:'), result.for_model())

    def test_a_hanging_handler_is_abandoned_within_its_budget(self):
        config = agent_config.load('')
        config.limits.per_call_seconds = 0.3
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        release = threading.Event()
        registry._add(agent_tools.Tool('stuck', 'x', {'type': 'object', 'properties': {}},
                                       lambda a, c: (release.wait(5), agent_tools.ToolResult(True, 'late'))[1],
                                       'builtin', 5.0))
        started = time.monotonic()
        result = registry.execute('stuck', {})
        self.assertFalse(result.ok)
        self.assertIn('abandoned', result.error)
        self.assertLess(time.monotonic() - started, 2)
        release.set()

    def test_a_handler_that_raises_is_a_tool_error_not_a_crash(self):
        registry = agent_tools.Registry(agent_config.load(''))
        self.addCleanup(registry.close)
        def boom(arguments, context):
            raise RuntimeError('kaput')
        registry._add(agent_tools.Tool('boom', 'x', {'type': 'object', 'properties': {}}, boom, 'builtin', 1.0))
        result = registry.execute('boom', {})
        self.assertFalse(result.ok)
        self.assertIn('RuntimeError', result.error)

    def test_results_are_clipped_to_the_context_budget(self):
        config = agent_config.load('')
        config.limits.result_chars = 200
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        registry._add(agent_tools.Tool('long', 'x', {'type': 'object', 'properties': {}},
                                       lambda a, c: agent_tools.ToolResult(True, 'y' * 5000), 'builtin', 1.0))
        result = registry.execute('long', {})
        self.assertLessEqual(len(result.content), 215)
        self.assertTrue(result.content.endswith('[…truncated]'))

    def test_search_notes_reports_a_missing_index_instead_of_inventing(self):
        config = agent_config.load('')
        config.retrieval.enabled = True
        config.retrieval.sources = [Path(self_made_up_dir())]
        config.retrieval.index = Path(tempfile.mkdtemp()) / 'nothing.sqlite'
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        result = registry.execute('search_notes', '{"query":"anything at all"}')
        self.assertFalse(result.ok)
        self.assertIn('notes are unavailable', result.error)
        self.assertIn('search_notes', registry.names(), 'the tool is offered; it reports honestly when empty')

    def test_fetch_url_is_off_unless_a_host_is_named(self):
        config = agent_config.load('')
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        self.assertNotIn('fetch_url', registry.names())
        config.fetch.enabled = True
        config.fetch.allow_hosts = ['example.com']
        registry = agent_tools.Registry(config)
        self.addCleanup(registry.close)
        self.assertIn('fetch_url', registry.names())
        for url, reason in (('https://elsewhere.invalid/x', 'allow_hosts'),
                            ('file:///etc/passwd', 'http(s)'),
                            ('http://example.com/', 'scheme')):
            result = registry.execute('fetch_url', json.dumps({'url': url}))
            self.assertFalse(result.ok, url)
            self.assertIn(reason, result.error)


def self_made_up_dir():
    return str(Path(tempfile.mkdtemp()) / 'absent')


if __name__ == '__main__':
    unittest.main(verbosity=2)
