"""Exercise supervisor ownership with fake processes; no models or GPU initialized."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
import site_config as site
import voice_stack as stack

if '--sabotage' in sys.argv:
    sys.argv.remove('--sabotage')
    source = (ROOT / 'deploy/voice_stack.py').read_text()
    anchor = "        # q38-server.service owns the LLM on GPU 2. Never launch or stop it here."
    assert anchor in source
    source = source.replace(anchor, "        launch('qwen', [QBINARY], env)\n" + anchor)
    exec(compile(source, stack.__file__, 'exec'), stack.__dict__)


class DeploymentTests(unittest.TestCase):
    def test_supervisor_owns_only_speech_and_bridge(self):
        children = []
        class Child:
            def __init__(self, argv, **kwargs):
                self.argv, self.env = argv, kwargs['env']
                self.pid = 100 + len(children)
                self.terminated = False
                children.append(self)
            def poll(self): return None
            def terminate(self): self.terminated = True
            def wait(self, **kwargs): return 0
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == 'RELEASE.json':
                return json.dumps({'qasr_worker_sha256': 'fake', 'qasr_serve_sha256': 'fake'})
            if path.name == 'languages-manifest.json': return '[]'
            return original_read(path, *args, **kwargs)
        def request(path, **kwargs):
            return json.dumps({'available': True, 'weights': site.QASR_WEIGHTS,
                               'model': 'Qwen3-ASR-1.7B'}).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(stack, 'RUN', root), patch.object(stack, 'SOCK', str(root/'g2p.sock')), \
                 patch.object(stack, 'PINNED', {}), patch.object(stack, 'digest', return_value='fake'), \
                 patch.object(Path, 'read_text', read), patch.object(Path, 'is_socket', return_value=True), \
                 patch.object(stack.socket, 'socket'), patch.object(stack.signal, 'signal'), \
                 patch.object(stack, 'request', side_effect=request), \
                 patch.object(stack.subprocess, 'Popen', side_effect=Child), \
                 patch.object(stack.time, 'sleep', side_effect=KeyboardInterrupt), \
                 patch.dict(os.environ, CUDA_VISIBLE_DEVICES=site.GPU):
                with self.assertRaises(KeyboardInterrupt): stack.supervise()
        self.assertEqual(len(children), 4, 'must not start an LLM on the speech GPU')
        self.assertFalse(any(str(site.LLM_BINARY) in child.argv for child in children))
        self.assertTrue(all(child.terminated for child in children), 'stop must reap every owned service')
        self.assertEqual(children[0].env['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(children[-1].env['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(children[1].env['CUDA_VISIBLE_DEVICES'], '4')
        self.assertEqual(children[2].env['CUDA_VISIBLE_DEVICES'], '4')
        bridge = children[-1].argv
        self.assertEqual(bridge[bridge.index('--llm-url') + 1], 'http://127.0.0.1:8080')

    def test_external_llm_not_in_owned_resources(self):
        self.assertNotIn(site.LLM_PORT, site.PORTS)
        self.assertNotIn(site.LLM_BINARY, stack.inputs())
        unit = (ROOT / 'deploy' / site.UNIT).read_text()
        self.assertNotIn('Conflicts=q38-server.service', unit)
        self.assertIn('WantedBy=default.target', unit)

if __name__ == '__main__': unittest.main(verbosity=2)
