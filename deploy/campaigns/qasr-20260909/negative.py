#!/usr/bin/env python3
"""Paired negative for the ASR swap: prove the bridge cannot mask a dead engine.

The dangerous failure mode of replacing a subprocess with an HTTP call is not a
wrong transcript, it is a 200 with an empty one -- the chatbot would answer
silence with confidence.  So the control must transcribe, and with the engine
gone the bridge must answer 503 with a name, never 200.

Runs the REAL speech_ui.py against a stub ASR origin, so what is under test is
the bridge's error mapping and its argument rules, not a mock of them.  It is a
GPU guarded run only because speech_ui.py insists on the authorized device.
"""

# deploy/site_config.py holds this host: absolute paths, ports, pins, GPU.  It is
# imported by name because the synced checkout has the same shape as the live one.
import pathlib as _pathlib, sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
import site_config as site
import array
import http.client
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

assert os.environ.get("CUDA_VISIBLE_DEVICES") == "2", "speech_ui.py requires the authorized device"

ROOT = str(Path(__file__).resolve().parents[3])       # the synced checkout
FFMPEG = shutil.which("ffmpeg") or os.environ.get("QASR_TEST_FFMPEG", str(site.FFMPEG))
if not os.path.exists(FFMPEG):
    raise SystemExit(f"negative: no ffmpeg on PATH and no fallback at {FFMPEG}")

STATE = {"alive": True}
CANNED = {"text": "The quick brown fox jumps over the lazy dog.",
          "raw_text": "language English<asr_text>The quick brown fox jumps over the lazy dog.",
          "audio_seconds": 2.0, "frames": 200, "chunks": 1, "tokens": 12,
          "frontend_ms": 3.0, "engine": {"total_ms": 40.0}}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not STATE["alive"]:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(CANNED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def tone_wav(seconds=2.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(rate)
        out.writeframes(array.array("h", (int(11000 * math.sin(2 * math.pi * 320 * i / rate))
                                          for i in range(int(seconds * rate)))).tobytes())
    return buf.getvalue()


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def call(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", "/stt", body, {"Content-Type": "audio/wav"})
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload


def start_bridge(port, extra):
    process = subprocess.Popen(
        ["python3", "-u", os.path.join(ROOT, "tools/speech_ui.py"), "--host", "127.0.0.1",
         "--port", str(port), "--ffmpeg", FFMPEG] + extra,
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process, process.stdout.read()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/stt/health")
            if conn.getresponse().status == 200:
                conn.close()
                return process, ""
            conn.close()
        except OSError:
            time.sleep(0.3)
    return process, "bridge did not become healthy"


def main():
    failures = []
    asr_port, bridge_port = free_port(), free_port()
    stub = HTTPServer(("127.0.0.1", asr_port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    process, log = start_bridge(bridge_port, ["--asr-url", f"http://127.0.0.1:{asr_port}"])
    if process.poll() is not None:
        print(f"NEGATIVE ABORTED bridge refused to start: {log[:400]}", flush=True)
        return 1
    status, payload = call(bridge_port, tone_wav())
    body = json.loads(payload) if payload else {}
    if status == 200 and "quick brown fox" in body.get("text", "").lower():
        print(f"CHECK PASS control status=200 text={body['text']!r}", flush=True)
    else:
        failures.append(f"control did not transcribe: {status} {payload[:200]!r}")
        print(f"CHECK FAIL control {status} {payload[:200]!r}", flush=True)

    # Sabotage: the engine is gone.  A masked failure looks like a 200 and "".
    STATE["alive"] = False
    status, payload = call(bridge_port, tone_wav())
    if status == 503 and b"unavailable" in payload.lower():
        print(f"CHECK PASS engine-down status=503 {payload[:120]!r}", flush=True)
    else:
        failures.append(f"a dead engine was masked: {status} {payload[:200]!r}")
        print(f"CHECK FAIL engine-down {status} {payload[:200]!r}", flush=True)
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    process.wait(timeout=30)
    stub.shutdown()

    # Sabotage: two backends at once is ambiguous, so the bridge must refuse it.
    process, log = start_bridge(bridge_port, ["--asr-url", f"http://127.0.0.1:{asr_port}",
                                              "--asr-bin", FFMPEG, "--model", ROOT,
                                              "--tokenizer", ROOT])
    stopped = process.wait(timeout=30)
    if stopped != 0 and "asr-url" in log:
        print(f"CHECK PASS both-backends refused rc={stopped} {log.strip().splitlines()[-1][:120]!r}",
              flush=True)
    else:
        failures.append(f"the bridge accepted two ASR backends at once: rc={stopped} {log[:200]!r}")
        print(f"CHECK FAIL both-backends rc={stopped} {log[:200]!r}", flush=True)

    if failures:
        print("GATE FAIL negative " + " | ".join(failures), flush=True)
        return 1
    print("GATE PASS negative (control transcribed; a dead engine and a double "
          "backend are both refused by name)", flush=True)
    return 0


sys.exit(main())
