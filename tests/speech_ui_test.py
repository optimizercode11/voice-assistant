#!/usr/bin/env python3
"""HTTP/decoder/process tests with real ffmpeg and an explicit fake ASR process.

No model runs: the child refuses CUDA visibility. This checks the bridge's
contract, not speech recognition accuracy.
"""
import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave

# The bridge lives in tools/ and is imported by name, exactly as the deployed
# tree imports it; running the suite from tests/ only needs that on the path.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import speech_ui


def audio(seconds=1, rate=44100, channels=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x00\x00" * int(seconds * rate) * channels)
    return output.getvalue()


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Kokoro-Language", "f")
        if "broken=1" in self.path:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"2\r\n\x00\x01\r\n")
            return  # Deliberately omit the chunk terminator.
        self.end_headers()
        self.wfile.write(b"\x00")
        self.wfile.flush()
        self.wfile.write(b"\x01\x02\x03")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"voices":["default"]}')

    def log_message(self, *_):
        pass


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == "", "run with CPU guard"
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.fixture = cls.root / "asr"
        cls.fixture.write_text('''#!/usr/bin/env python3
import json, os, sys, time, wave
from pathlib import Path
assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
mode = Path(__file__).with_name("mode").read_text()
Path(__file__).with_name("pid").write_text(str(os.getpid()))
if mode == "slow": time.sleep(10)
if mode == "exit": sys.exit(1)
with wave.open(args["--wav"], "rb") as reader:
    assert reader.getnchannels() == 1 and reader.getframerate() == 24000
    assert reader.getsampwidth() == 2
    count = (reader.getnframes() + 70400 - 1) // 70400
for i in range(count - (mode == "missing")):
    print(json.dumps({"name":args["--wav"], "chunk":i, "tokens":[1] * (256 if mode == "truncated" else 1), "text":"Bonjour <script>世界</script>."}))
''')
        cls.fixture.chmod(0o755)
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.upstream.seen = []
        cls.config = SimpleNamespace(page=Path(__file__).resolve().parents[1] / "web/index.html",
                                    tts_url=f"http://127.0.0.1:{cls.upstream.server_port}",
                                    ffmpeg="ffmpeg", asr_bin=cls.fixture, model=cls.root,
                                    tokenizer=cls.root / "tokenizer")
        cls.server = speech_ui.SpeechServer(("127.0.0.1", 0), cls.config)
        cert, key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
                        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        cls.client_tls = ssl.create_default_context(cafile=str(cert))
        cls.secure = speech_ui.SpeechServer(("127.0.0.1", 0), cls.config,
                                            tls=tls, asr_lock=cls.server.asr_lock)
        cls.config.https_port = cls.secure.server_port
        for server in (cls.server, cls.secure, cls.upstream):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.secure, cls.upstream):
            server.shutdown()
            server.server_close()
        cls.directory.cleanup()

    def setUp(self):
        (self.root / "mode").write_text("ok")

    def request(self, path, body=None, headers=None, *, secure=False):
        connection = (http.client.HTTPSConnection("127.0.0.1", self.secure.server_port, timeout=15,
                                                context=self.client_tls) if secure else
                      http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15))
        try:
            connection.request("GET" if body is None else "POST", path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_page_health_proxy(self):
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="stt-record"', body)
        self.assertTrue(json.loads(self.request("/stt/health")[2])["available"])
        route = "/tts?language=f&voice=ff_siwis&speed=1&stream=1"
        status, headers, body = self.request(route, "Bonjour".encode())
        self.assertEqual((status, body), (200, b"\x00\x01\x02\x03"))
        self.assertEqual(headers["X-Kokoro-Language"], "f")
        self.assertEqual(self.upstream.seen[-1], (route, b"Bonjour"))

    def test_native_speaker_and_silence_markers(self):
        self.assertEqual(speech_ui.transcript_text('Unchanged speech.'), 'Unchanged speech.', 'plain-text control')
        raw = ' \n Speaker 0:The quick brown fox jumps over the lazy dog.[Silence]'
        self.assertEqual(speech_ui.transcript_text(raw), 'The quick brown fox jumps over the lazy dog.')
        self.assertEqual(speech_ui.transcript_text(' \n Speaker 0:[Silence]'), '')
        self.assertEqual(speech_ui.transcript_text('Speaker 2:Bonjour.\n Speaker 3:世界。'), 'Bonjour.\n世界。')
        self.assertEqual(speech_ui.transcript_text('The words Speaker 0: are a label.'), 'The words Speaker 0: are a label.')

    def test_degenerate_asr_tail_is_folded_not_spoken(self):
        """The engine sometimes decodes past the end of the utterance.

        These strings are captured from the live qasr server against known
        ground truth (PROVENANCE.md), not invented: the words before the run
        are correct and the run is one token repeated.  Left in, the assistant
        reads it aloud -- "ory.ory.ory.ory" -- which is what this guards.
        """
        captured = [
            ('Let us do some museums and history museums tomorrow...............',
             'Let us do some museums and history museums tomorrow.'),
            ("Let's do some museums........ory.ory.ory.ory.ory.ory.ory.ory",
             "Let's do some museums."),
            ('The museum is open until nine in the evening,,, is why we always go then.......',
             'The museum is open until nine in the evening, is why we always go then.'),
        ]
        for raw, wanted in captured:
            self.assertEqual(speech_ui.transcript_text(raw), wanted, raw)
        # Controls: repetition a person actually produced must survive, because
        # "no no no" is content and a hallucinated tail is not.
        for keep in ('No no no, that is not what I meant.', 'Haha that was funny.',
                     'I said very very clearly, stop.', 'Yes.'):
            self.assertEqual(speech_ui.transcript_text(keep), keep, keep)

    def test_resampling_and_complete_unicode_output(self):
        status, _, body = self.request("/stt", audio(3.5))
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(result["audio_seconds"], 3.5)
        self.assertEqual(result["text"], "Bonjour <script>世界</script>." * 2)
        self.assertEqual(result["raw_text"], result["text"])

    def test_truncated_tts_stream_is_a_transport_failure(self):
        with self.assertRaises(http.client.IncompleteRead):
            self.request("/tts?stream=1&broken=1", b"test")

    def test_browser_recording_formats(self):
        source = self.root / "input.wav"
        source.write_bytes(audio())
        for extension, codec in (("webm", "libopus"), ("m4a", "aac")):
            target = self.root / f"recording.{extension}"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                            "-i", str(source), "-c:a", codec, "-threads", "1", "-y", str(target)], check=True)
            status, _, body = self.request("/stt", target.read_bytes())
            self.assertEqual(status, 200, (extension, body))

    def test_tls_upload_and_shared_busy_limit(self):
        status, _, body = self.request('/stt/health', secure=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['https_port'], self.secure.server_port)
        self.assertEqual(self.request('/stt', audio(), secure=True)[0], 200)
        self.server.asr_lock.acquire()
        try:
            self.assertEqual(self.request('/stt', audio(), secure=True)[0], 503)
        finally:
            self.server.asr_lock.release()
        self.assertEqual(self.request('/tts?stream=1', b'test', secure=True)[2], b'\x00\x01\x02\x03')

    def test_tls_disconnect_releases_asr(self):
        (self.root / 'mode').write_text('slow')
        (self.root / 'pid').unlink(missing_ok=True)
        raw = socket.create_connection(('127.0.0.1', self.secure.server_port))
        connection = self.client_tls.wrap_socket(raw, server_hostname='localhost')
        body = audio()
        connection.sendall(f'POST /stt HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        deadline = time.monotonic() + 5
        while not (self.root / 'pid').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.root / 'pid').exists(), 'TLS upload reaches native process')
        pid = int((self.root / 'pid').read_text())
        connection.close()
        deadline = time.monotonic() + 3
        while self.server.asr_lock.locked() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.server.asr_lock.locked())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_audio_and_limits(self):
        self.assertEqual(self.request("/stt", b"not audio")[0], 400)
        self.assertEqual(self.request("/stt", audio(120.2, 24000, 1))[0], 400)
        self.assertEqual(self.request("/stt", b"")[0], 413)
        self.assertEqual(self.request("/stt", b"x", {"Content-Length": str(speech_ui.MAX_BYTES + 1)})[0], 413)
        self.assertEqual(self.request("/stt", b"x", {"Origin": "https://foreign.invalid"})[0], 403)
        # Input must not be interpreted as a playlist that reads another file.
        self.assertEqual(self.request("/stt", b"#EXTM3U\nfile:///etc/passwd\n")[0], 400)

    def test_busy_and_lock_recovery(self):
        self.server.asr_lock.acquire()
        try:
            self.assertEqual(self.request("/stt", audio())[0], 503)
        finally:
            self.server.asr_lock.release()
        self.assertEqual(self.request("/stt", audio())[0], 200)

    def test_incomplete_truncated_and_failed_children(self):
        self.assertEqual(self.request("/stt", audio())[0], 200, "paired passing control")
        for mode in ("missing", "truncated", "exit"):
            (self.root / "mode").write_text(mode)
            status, _, body = self.request("/stt", audio())
            self.assertEqual(status, 502, (mode, body))
            self.assertNotIn(str(self.root).encode(), body)

    def test_disconnect_terminates_owned_asr_and_releases_slot(self):
        (self.root / "mode").write_text("slow")
        (self.root / "pid").unlink(missing_ok=True)
        connection = socket.create_connection(("127.0.0.1", self.server.server_port))
        body = audio()
        connection.sendall(f"POST /stt HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        deadline = time.monotonic() + 5
        while not (self.root / "pid").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.root / "pid").exists())
        pid = int((self.root / "pid").read_text())
        connection.close()
        deadline = time.monotonic() + 3
        while self.server.asr_lock.locked() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.server.asr_lock.locked())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--negative", action="store_true")
    parser.add_argument("--speaker-sabotage", action="store_true")
    args = parser.parse_args()
    if args.speaker_sabotage:
        speech_ui.transcript_text = lambda raw: raw
        suite = unittest.TestSuite([BridgeTests("test_native_speaker_and_silence_markers"),
                                    BridgeTests("test_degenerate_asr_tail_is_folded_not_spoken")])
    else:
        suite = unittest.TestSuite([BridgeTests("test_incomplete_truncated_and_failed_children")]) if args.negative else unittest.defaultTestLoader.loadTestsFromTestCase(BridgeTests)
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
