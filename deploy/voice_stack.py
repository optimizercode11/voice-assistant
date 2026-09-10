#!/usr/bin/env python3
"""Restore the pinned GPU-2 voice stack; invoked by the guarded systemd unit."""
import array
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'run'

# Every absolute path, port and hash pin lives in site_config.py: this file is
# the stack's behaviour, that file is this host.  The names below are kept as
# they were so the launch blocks still read as one story.
#
# The resident Qwen3-ASR server (qwen3-asr engine, campaign qasr-serve-20260909)
# carries its own RELEASE.json that binds the binary to the source fingerprint
# it was built from, so the supervisor verifies a self-describing release
# instead of a hash hand-copied in, and a stale binary fails here rather than
# at 3am.  Qwen/TTS/vvasr predate that convention and stay pinned by hash.
import site_config as site

LANG = site.LANGUAGES
PRIOR = site.PRIOR
CERT = site.CERT
QMODEL = site.LLM_MODEL
AMODEL = site.ASR_MODEL
QASR = site.QASR_RELEASE
QASR_MODEL = site.QASR_MODEL
QASR_ENV = site.QASR_ENV
QASR_PORT = site.QASR_PORT
QBINARY = site.LLM_BINARY
TBINARY = site.TTS_BINARY
ABINARY = site.ASR_BINARY
FFMPEG = site.FFMPEG
SOCK = site.G2P_SOCKET
PINNED = site.PINS


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inputs():
    return list(PINNED) + [FFMPEG, CERT, LANG/'runtime/languages-manifest.json',
        QMODEL/'model.safetensors', QMODEL/'config.json', QMODEL/'q38_tok.bin',
        QMODEL/'chat_template.jinja', AMODEL/'config.json', AMODEL/'model.safetensors.index.json',
        *sorted(AMODEL.glob('model-*.safetensors')), site.ASR_TOKENIZER,
        site.TTS_CONFIG, site.TTS_WEIGHTS, site.TTS_REF_S,
        *sorted(site.TTS_VOICES.glob('*.npy')),
        *sorted((ROOT/'deploy').glob('*.py')), *sorted((ROOT/'deploy').glob('*.sh')),
        *sorted((ROOT/'deploy').glob('*.service')),
        site.QASR_WORKER, QASR/'RELEASE.json', site.QASR_SERVE,
        QASR/'tools/canonical.py', site.QASR_FRONTEND/'qwen_asr/__init__.py',
        QASR_MODEL/'model.safetensors', QASR_MODEL/'config.json',
        QASR_MODEL/'tokenizer_config.json', QASR_MODEL/'vocab.json', QASR_MODEL/'merges.txt',
        QASR_MODEL/'chat_template.json', QASR_MODEL/'preprocessor_config.json',
        QASR_ENV/'transformers/__init__.py',
        ROOT/'tools/g2p_sidecar.py', ROOT/'tools/speech_ui.py', ROOT/'tools/voice_chat.py',
        ROOT/'tools/agent_config.py', ROOT/'tools/agent_tools.py', ROOT/'tools/retrieval.py',
        ROOT/'tools/mcp_client.py', ROOT/'tools/voicectl.py',
        ROOT/'web/index.html', ROOT/'web/chat.html', ROOT/'web/chat.js',
        # A capability config is an input only when this site actually named one.
        *([ROOT / site.TOOLS_CONFIG] if site.TOOLS_CONFIG else [])]


def request(path, body=None, port=None, expected=200):
    port = site.HTTPS_PORT if port is None else port
    client = (http.client.HTTPSConnection('127.0.0.1', port,
        context=ssl.create_default_context(cafile=str(CERT)), timeout=90) if port == site.HTTPS_PORT else
        http.client.HTTPConnection('127.0.0.1', port, timeout=90))
    try:
        client.request('GET' if body is None else 'POST', path, body)
        response = client.getresponse()
        data = response.read()
        assert response.status == expected, (path, response.status, data[:600])
        return data
    finally:
        client.close()


def audio(data):
    with wave.open(io.BytesIO(data)) as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 24000)
        pcm = array.array('h', reader.readframes(reader.getnframes()))
    rms = math.sqrt(sum(float(x)*x for x in pcm)/len(pcm))/32768
    assert len(pcm) > 2400 and rms > .005, (len(pcm), rms)
    return {'frames': len(pcm), 'rms': rms}


def smoke():
    assert json.loads(request('/health', port=site.LLM_PORT))['status'] == 'ok'
    assert json.loads(request('/chat/health'))['available']
    stt_health = json.loads(request('/stt/health'))
    assert not stt_health['busy'] and stt_health['backend'] == 'qasr', stt_health
    for url, file in [('/chat', 'web/chat.html'), ('/chat.js', 'web/chat.js'), ('/', 'web/index.html')]:
        assert request(url) == (ROOT/file).read_bytes(), url
    langs = json.loads(request('/languages'))
    assert {row['code'] for row in langs['languages']} == set('abefhipjz')
    spoken = request('/tts?format=wav&language=a&voice=af_heart&speed=1.2',
        b'The quick brown fox jumps over the lazy dog.')
    transcript = json.loads(request('/stt', spoken))['text']
    assert 'quick brown fox' in transcript.lower(), transcript
    def ask(messages):
        return json.loads(request('/chat/completions', json.dumps({'messages': messages})))['text']
    reply = ask([{'role': 'user', 'content': transcript}])
    assert len(reply) > 5 and '<think>' not in reply
    output = request('/tts?format=wav&language=a&voice=af_heart&speed=1.2', reply.encode())
    hindi = request('/tts?format=wav&language=h&voice=hf_alpha&speed=1.2', 'नमस्ते दुनिया।'.encode())
    messages = [{'role': 'user', 'content': 'What is two plus two? Just the answer, please.'}]
    first = ask(messages)
    assert first.strip().lower().strip('.') in ('4', 'four'), first
    messages += [{'role': 'assistant', 'content': first},
        {'role': 'user', 'content': 'Actually I meant three plus two. Just the answer.'}]
    second = ask(messages)
    assert second.strip().lower().strip('.') in ('5', 'five'), second
    # A valid control above must pass before malformed requests are rejected.
    request('/stt', b'not audio', expected=400)
    request('/chat/completions', json.dumps({'messages': []}), expected=400)
    stats = json.loads(request('/stats'))
    assert stats['queued'] == stats['inflight'] == 0
    asr = json.loads(request('/stt', spoken))
    result = {'transcript': transcript, 'reply': reply, 'english': audio(output),
        'hindi': audio(hindi), 'context_correction': [first, second],
        'asr': {'backend': 'qasr', 'engine': asr.get('engine'),
                'frontend_ms': asr.get('frontend_ms'), 'frames': asr.get('frames')},
        'tls_verified': True,
        'source': os.environ.get('GUARD_SOURCE_FP'), 'pgid': os.getpgrp(),
        'gpu': 2, 'time': time.time()}
    (RUN/'acceptance.json').write_text(json.dumps(result, indent=2)+'\n')
    print('VOICE STACK ACCEPTANCE PASS', json.dumps(result), flush=True)


def supervise():
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == site.GPU, f'authorized on GPU {site.GPU} only'
    children = []
    stopping = False
    owns_socket = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def launch(name, args, env):
        with (RUN/(name+'.log')).open('ab') as log:
            child = subprocess.Popen([str(x) for x in args], cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children.append((name, child))
        return child
    def wait_for(predicate, seconds=120):
        deadline = time.monotonic()+seconds
        while not predicate():
            assert not stopping and time.monotonic() < deadline, 'startup timed out or stopped'
            for name, child in children:
                assert child.poll() is None, f'{name} exited; inspect run/{name}.log'
            time.sleep(.2)
    def healthy(port, path):
        try:
            request(path, port=port)
            return True
        except OSError:
            return False
    try:
        for binary, expected in PINNED.items():
            assert digest(binary) == expected, f'binary changed: {binary}'
        release = json.loads((QASR/'RELEASE.json').read_text())
        assert digest(QASR/'qasr_worker') == release['qasr_worker_sha256'], 'qasr_worker changed'
        assert digest(QASR/'tools/qasr_serve.py') == release['qasr_serve_sha256'], 'qasr_serve changed'
        inventory = json.loads((LANG/'runtime/languages-manifest.json').read_text())
        for row in inventory:
            assert digest(row['path']) == row['sha256'], f'runtime changed: {row["path"]}'
        if Path(SOCK).exists():
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(SOCK)
                except ConnectionRefusedError:
                    Path(SOCK).unlink()
                else:
                    raise RuntimeError('G2P socket already has a listener')
        for port in site.PORTS:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('0.0.0.0', port))
        env = dict(os.environ, OMP_NUM_THREADS='4', HF_HUB_OFFLINE='1')
        launch('g2p', [site.G2P_PYTHON, 'tools/g2p_sidecar.py',
            '--cpu', '--languages', 'abefhipjz', '--socket', SOCK],
            dict(env, CUDA_VISIBLE_DEVICES='', PYTHONPATH=str(LANG/'runtime/python')))
        wait_for(lambda: Path(SOCK).is_socket())
        owns_socket = True
        launch('qwen', [QBINARY, '-m', QMODEL, '--port', str(site.LLM_PORT), '--ctx', '16384',
            '--slots', '1', '--kv-quant', '--cache-ram', '8192', '--cache-min', '256',
            '--cache-log', '--temp', '0.6', '--think-budget', '256', '--answer-budget', '512',
            '--hide-think'], dict(env, Q38_ATTN_SPLIT='768', Q38_QK8='1', Q38_LONGGRAPH='1'))
        wait_for(lambda: healthy(site.LLM_PORT, '/health'))
        launch('tts', [TBINARY, '--weights', site.TTS_WEIGHTS, '--config', site.TTS_CONFIG,
            '--ref-s', site.TTS_REF_S, '--voices', site.TTS_VOICES,
            '--g2p-socket', SOCK, '--g2p-multilingual', '--web', 'web', '--bind', '0.0.0.0',
            '--port', str(site.TTS_PORT), '--device', '0', '--max-batch', '16', '--stream-lead', '1'],
            dict(env, KOKORO_LSTM_TENSOR='bf16x6', KOKORO_LSTM_COALESCED='1', KOKORO_LSTM_PROFILE='0'))
        wait_for(lambda: healthy(site.TTS_PORT, '/health'))
        # The ASR swap, in one line: the bridge no longer spawns a transcriber
        # per upload, it POSTs to a server that already holds the checkpoint.
        # vvasr stays on disk and speech_ui.py still supports --asr-bin, so
        # rollback is this block's inverse, not a rebuild.  ffmpeg stays in the
        # bridge: the certified path decodes to 24 kHz and resamples with the
        # pinned librosa, exactly as the oracle did.
        launch('stt', [site.QASR_PYTHON, '-u', str(site.QASR_SERVE),
            '--model', QASR_MODEL, '--worker', str(QASR/'qasr_worker'), '--ctx', '2048',
            '--host', '127.0.0.1', '--port', str(QASR_PORT), '--max-tokens', '256'],
            dict(env, PYTHONPATH=f'{QASR_ENV}:{site.QASR_FRONTEND}'))

        def stt_ready():
            try:
                body = json.loads(request('/health', port=QASR_PORT))
            except (OSError, AssertionError, ValueError):
                return False
            return bool(body.get('available')) and not body.get('sabotage')

        wait_for(stt_ready, seconds=300)
        bridge = ['python3', '-u', 'tools/speech_ui.py', '--host', '0.0.0.0',
            '--port', site.HTTP_PORT, '--https-port', site.HTTPS_PORT,
            '--tls-cert', CERT, '--tls-key', site.KEY,
            '--ffmpeg', FFMPEG, '--asr-url', f'http://127.0.0.1:{QASR_PORT}',
            '--llm-url', f'http://127.0.0.1:{site.LLM_PORT}']
        if site.TOOLS_CONFIG:
            # Refusing to start on a bad capability config is deliberate: a
            # half-loaded tool set is worse than a bridge that will not come up.
            bridge += ['--tools-config', str(ROOT / site.TOOLS_CONFIG)]
        launch('bridge', bridge, env)
        wait_for(lambda: healthy(site.HTTPS_PORT, '/stt/health'))
        (RUN/'children.json').write_text(json.dumps({name: child.pid for name, child in children}))
        (RUN/'ready').write_text(str(os.getpid()))
        while not stopping:
            for name, child in children:
                assert child.poll() is None, f'{name} exited; restarting stack'
            time.sleep(1)
    finally:
        (RUN/'ready').unlink(missing_ok=True)
        for name, child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for name, child in reversed(children):
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                print(f'{name} did not stop after SIGTERM; no SIGKILL used', flush=True)
        if owns_socket:
            Path(SOCK).unlink(missing_ok=True)
        (RUN/'stack.pid').unlink(missing_ok=True)


def start():
    RUN.mkdir(exist_ok=True)
    (RUN/'ready').unlink(missing_ok=True)
    with (RUN/'supervisor.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, '-u', __file__, 'supervise'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    (RUN/'stack.pid').write_text(str(child.pid)+'\n')
    try:
        deadline = time.monotonic()+240
        while not (RUN/'ready').exists():
            assert child.poll() is None, 'supervisor exited; inspect run/supervisor.log'
            assert time.monotonic() < deadline, 'stack startup timed out'
            time.sleep(.2)
        smoke()
        assert child.poll() is None
        print('VOICE STACK READY supervisor=', child.pid, flush=True)
    except BaseException:
        child.terminate()
        child.wait(timeout=90)
        raise


if __name__ == '__main__':
    os.chdir(ROOT)
    action = sys.argv[1]
    if action == 'inputs':
        print('\n'.join(str(p) for p in inputs()))
    elif action == 'supervise':
        supervise()
    elif action == 'start':
        start()
    else:
        raise SystemExit('expected inputs, supervise or start')
