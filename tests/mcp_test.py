"""The MCP client against a real stdio peer -- a subprocess, not a mock.

The client speaks a wire protocol, so every test here starts a process that
also speaks it (tests/fixtures/fake_mcp_server.py) and asks for the failure
shapes that a third-party server can actually produce.
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
FIXTURE = str(Path(__file__).resolve().parent / 'fixtures' / 'fake_mcp_server.py')
SABOTAGE = '--sabotage' in sys.argv

# Paired negative for "the tool list is captured once".  Re-listing before
# every call is the obvious-looking convenience change, and it is the hole: a
# server that answers tools/list differently the second time can name a tool
# the operator never allow-listed, and the bridge will run it.  Applied to one
# instance inside one test, so the sabotage breaks exactly one assertion.
import re as _re
_ORIGINAL_CALL = mcp_client.MCPServer.call


def relisting(self, tool, arguments, timeout=None):
    listed = self.request("tools/list", {}, timeout=self.timeout)
    fresh = []
    for row in (listed or {}).get("tools", []) or []:
        safe = _re.sub(r"[^A-Za-z0-9_-]", "_", row["name"].replace("__", "_"))
        fresh.append(mcp_client.MCPTool(self.name, row["name"], f"mcp__{self.name}__{safe}"[:64],
                                        row.get("description", ""), row.get("inputSchema") or {}))
    self.tools = fresh
    return _ORIGINAL_CALL(self, tool, arguments, timeout)


def server(mode='ok', **kwargs):
    args = [FIXTURE] + ([mode] if mode != 'ok' else [])
    return mcp_client.MCPServer('desk', sys.executable, args, kwargs.pop('env', {}),
                                kwargs.pop('timeout', 5.0), kwargs.get('allow', []), kwargs.get('deny', []))


class MCPTests(unittest.TestCase):
    def test_initialize_then_list_then_call(self):
        instance = server()
        try:
            tools = instance.start()
            self.assertEqual(instance.server_info['name'], 'fake-mcp')
            self.assertEqual([tool.tool_name for tool in tools],
                             ['mcp__desk__get_time', 'mcp__desk__window_title', 'mcp__desk__dangerous_write'])
            self.assertEqual(tools[0].server, 'desk', 'every tool must carry its provenance')
            text, is_error = instance.call(tools[0], {'zone': 'UTC'})
            self.assertEqual((text, is_error), ('12:34 in UTC', False))
            self.assertEqual(instance.status()['state'], 'ready')
        finally:
            instance.stop()
        self.assertFalse(instance.status()['running'])

    def test_starting_twice_reuses_the_same_child(self):
        instance = server()
        try:
            first = instance.start()
            pid = instance.process.pid
            self.assertIs(instance.start(), first)
            self.assertEqual(instance.process.pid, pid, 'a second start must not double-spawn')
        finally:
            instance.stop()

    def test_allow_and_deny_are_applied_before_the_model_sees_anything(self):
        denied = server(deny=['dangerous_write'])
        allowed = server(allow=['get_time'])
        try:
            self.assertEqual([t.name for t in denied.start()], ['get_time', 'window.title'])
            self.assertEqual([t.name for t in allowed.start()], ['get_time'])
        finally:
            denied.stop(); allowed.stop()

    def test_a_hanging_server_costs_its_timeout_not_the_turn(self):
        instance = server('hang', timeout=1.0)
        instance.start()
        started = time.monotonic()
        with self.assertRaises(mcp_client.MCPError):
            instance.call(instance.tools[0], {'zone': 'UTC'}, timeout=1.0)
        self.assertLess(time.monotonic() - started, 4)
        instance.stop()
        self.assertFalse(instance.status()['running'], 'a hung child must be reaped')

    def test_a_jsonrpc_error_surfaces_its_message(self):
        instance = server('rpc_error')
        instance.start()
        try:
            with self.assertRaises(mcp_client.MCPError) as caught:
                instance.call(instance.tools[0], {'zone': 'UTC'})
            self.assertIn('upstream fixture failure', str(caught.exception))
        finally:
            instance.stop()

    def test_a_server_that_dies_mid_call_is_reported_not_swallowed(self):
        instance = server('crash')
        instance.start()
        with self.assertRaises(mcp_client.MCPError):
            instance.call(instance.tools[0], {'zone': 'UTC'})
        instance.stop()

    def test_the_tool_list_is_captured_once(self):
        # A server that renames its tools after listing them must not be able to
        # offer something the operator never allow-listed.
        instance = server('renames')
        if SABOTAGE:
            instance.call = lambda tool, arguments, timeout=None: relisting(instance, tool, arguments, timeout)
        try:
            captured = [t.name for t in instance.start()]
            self.assertIn('get_time', captured)
            self.assertNotIn('sneaky', [t.name for t in instance.tools], 'the second list must be ignored')
            ghost = mcp_client.MCPTool('desk', 'sneaky', 'mcp__desk__sneaky', 'appeared later')
            with self.assertRaises(mcp_client.MCPError):
                instance.call(ghost, {})
        finally:
            instance.stop()

    def test_restart_is_bounded(self):
        instance = server('crash')
        instance.start()
        for _ in range(4):
            try:
                instance.call(instance.tools[0], {'zone': 'UTC'})
            except mcp_client.MCPError:
                pass
        self.assertLessEqual(instance.restarts, mcp_client.MAX_RESTARTS, 'a crash-looping server must not respawn forever')
        instance.stop()

    def test_a_command_that_does_not_exist_is_a_clean_failure(self):
        instance = mcp_client.MCPServer('ghost', '/nonexistent/mcp-server-xyz', [])
        with self.assertRaises(mcp_client.MCPError):
            instance.start()
        instance.stop()

    def test_the_registry_turns_mcp_tools_into_ordinary_tools(self):
        config = agent_config.load(None)
        config.mcp = [agent_config.MCPServerConfig(name='desk', command=sys.executable, args=[FIXTURE],
                                                   deny=['dangerous_write'], timeout_seconds=5.0)]
        registry = agent_tools.Registry.build(config)
        try:
            self.assertEqual(registry.names(), ['mcp__desk__get_time', 'mcp__desk__window_title', 'now'])
            spec = [s for s in registry.specs() if s['function']['name'] == 'mcp__desk__get_time'][0]
            self.assertEqual(spec['type'], 'function')
            self.assertEqual(spec['function']['parameters']['properties']['zone']['enum'], ['UTC', 'Asia/Kolkata'])
            result = registry.execute('mcp__desk__get_time', '{"zone":"Asia/Kolkata"}')
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.content, '12:34 in Asia/Kolkata')
            self.assertEqual(result.source, 'mcp:desk')
            bad = registry.execute('mcp__desk__get_time', '{"zone":"Mars"}')
            self.assertFalse(bad.ok, 'the declared enum must be enforced before the server is called')
            self.assertIn('must be one of', bad.error)
            self.assertEqual(registry.status()['mcp'][0]['tools'], 2)
        finally:
            registry.close()
        self.assertFalse(registry.session.servers[0].status()['running'], 'close must reap the child')

    def test_a_dead_mcp_server_degrades_the_assistant_instead_of_killing_it(self):
        config = agent_config.load(None)
        config.mcp = [agent_config.MCPServerConfig(name='ghost', command='/nonexistent/mcp-server-xyz'),
                      agent_config.MCPServerConfig(name='desk', command=sys.executable, args=[FIXTURE],
                                                   timeout_seconds=5.0)]
        registry = agent_tools.Registry.build(config)
        try:
            self.assertIn('now', registry.names())
            self.assertIn('mcp__desk__get_time', registry.names())
            self.assertTrue(any('ghost' in note for note in registry.notes), registry.notes)
            self.assertEqual(registry.mcp_failures[0]['server'], 'ghost')
        finally:
            registry.close()

    def test_the_child_gets_a_small_environment(self):
        # An MCP server is someone else's program; it must not inherit every
        # credential in the bridge's environment just because it is a child.
        probe = Path(tempfile.mkdtemp()) / 'env_probe_server.py'   # never written into the repo
        probe.write_text('import json, os, sys\n'
                         'for raw in sys.stdin:\n'
                         '    m = json.loads(raw) if raw.strip() else None\n'
                         '    if not m or m.get("id") is None: continue\n'
                         '    if m["method"] == "initialize": r = {"protocolVersion":"2025-03-26","serverInfo":{"name":"probe"}}\n'
                         '    elif m["method"] == "tools/list": r = {"tools":[{"name":"peek","inputSchema":{"type":"object","properties":{}},"description":"d"}]}\n'
                         '    else: r = {"content":[{"type":"text","text":json.dumps({k: v for k, v in os.environ.items()})}]}\n'
                         '    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":r}) + "\\n"); sys.stdout.flush()\n')
        instance = mcp_client.MCPServer('probe', sys.executable, [str(probe)], {'MCP_TOKEN': 'secret'}, 5.0)
        try:
            instance.start()
            seen = json.loads(instance.call(instance.tools[0], {})[0])
            self.assertEqual(seen.get('MCP_TOKEN'), 'secret', 'configured env must arrive')
            self.assertNotIn('AWS_SECRET_ACCESS_KEY', seen)
            self.assertNotIn('SSH_AUTH_SOCK', seen)
            self.assertIn('PATH', seen)
        finally:
            instance.stop()


if __name__ == '__main__':
    sys.argv = [value for value in sys.argv if value != '--sabotage']
    unittest.main(verbosity=2)
