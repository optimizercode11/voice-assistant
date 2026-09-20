#!/usr/bin/env python3
"""Check environment answers against the real workspace account (CPU driver)."""
import argparse
import json
from pathlib import Path
import ssl
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
import agent_config
import agent_tools
import mcp_client
import voice_chat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='store_true', help='Use the staged prompt with the resident LLM.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    server = mcp_client.MCPServer('workspace', 'ssh', ['-T', 'codex-workspace'], timeout=10)
    try:
        tools = {tool.name: tool for tool in server.start()}
        raw, error = server.call(tools['run_shell'], {
            'command': 'printf "%s\\n" "$HOME"; pwd; id -un'})
        actual = json.loads(raw)
        assert not error and actual['exit_code'] == 0, actual
        home, cwd, user = actual['stdout'].splitlines()
    finally:
        server.stop()
    cases = [(f'home-{n}', [{'role': 'user', 'content': "What's your home directory?"}], home)
             for n in range(1, 4)]
    cases += [
        ('cwd', [{'role': 'user', 'content': "What's your current working directory?"}], cwd),
        ('account', [{'role': 'user', 'content': 'What user account do your computer tools run under?'}], user),
        ('earlier-wrong-answer', [
            {'role': 'user', 'content': "What's your home directory?"},
            {'role': 'assistant', 'content': 'My home directory is /home/voice.'},
            {'role': 'user', 'content': "What's your home directory?"}], home),
    ]
    evidence = {'environment': {'home': home, 'cwd': cwd, 'user': user},
                'candidate': args.candidate, 'cases': [], 'passed': False}
    registry = None
    try:
        if args.candidate:
            registry = agent_tools.Registry.build(agent_config.load(ROOT / 'config/host.toml'), root=ROOT)
            assert not registry.mcp_failures, registry.mcp_failures
        for name, messages, expected in cases:
            body = json.dumps({'messages': messages}).encode()
            if registry is not None:
                result = voice_chat.turn('http://127.0.0.1:8080', body, lambda: False,
                                         registry=registry, limits=registry.config.limits)
            else:
                request = urllib.request.Request('https://127.0.0.1:8094/chat/completions', data=body,
                                                 headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=120) as response:
                    result = json.load(response)
            rows = result.get('tools', [])
            ok = (expected in result.get('text', '') and bool(rows)
                  and all(row.get('source') == 'mcp:workspace' and row.get('ok') for row in rows)
                  and any(row.get('name') == 'mcp__workspace__run_shell' for row in rows))
            evidence['cases'].append({'name': name, 'messages': messages, 'expected': expected,
                                      'response': result, 'passed': ok})
            print(name, 'PASS' if ok else 'FAIL', result.get('text'), flush=True)
        evidence['passed'] = all(case['passed'] for case in evidence['cases'])
    finally:
        if registry is not None:
            registry.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
