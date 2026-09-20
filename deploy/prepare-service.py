"""Verify existing speech assets and record the staged source identity (CPU only)."""
import json
import os
from pathlib import Path
import site_config as site
import voice_stack as stack

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
for path in stack.inputs():
    assert path.is_file(), f'missing input: {path}'
for path, expected in site.PINS.items():
    assert stack.digest(path) == expected, f'binary changed: {path}'
release = json.loads((site.QASR_RELEASE/'RELEASE.json').read_text())
for path, key in [(site.QASR_WORKER, 'qasr_worker_sha256'),
                  (site.QASR_SERVE, 'qasr_serve_sha256'),
                  (site.QASR_RELEASE/'tools/canonical.py', 'canonical_sha256')]:
    assert stack.digest(path) == release[key], f'ASR release changed: {path}'
for row in release['model_files']:
    assert stack.digest(row['path']) == row['sha256'], f'ASR model changed: {row["path"]}'
for row in json.loads((site.LANGUAGES/'runtime/languages-manifest.json').read_text()):
    assert stack.digest(row['path']) == row['sha256'], f'language runtime changed: {row["path"]}'
head, fingerprint = os.environ['GUARD_GIT_HEAD'], os.environ['GUARD_SOURCE_FP']
assert len(fingerprint) == 64 and all(c in '0123456789abcdef' for c in head + fingerprint)
(stack.ROOT/'deploy/source.env').write_text(f'GUARD_GIT_HEAD={head}\nGUARD_SOURCE_FP={fingerprint}\n')
print('Speech asset hashes verified; staged source fingerprint:', fingerprint)
