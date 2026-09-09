#!/usr/bin/env python3
"""Reproduce the defect this campaign exists to remove: one process, and one
model load, per transcription request.

EXPECTED rc=2.  It does not read a config file or a git diff -- it runs the
bridge code that was live before this campaign (the byte-for-byte copy deploy.py
backed up before installing over it, hash-bound in expected.json's sibling) and
counts how many processes one call to transcribe() spawns.  Two calls, two PIDs,
one per request: that is the thing being removed, observed rather than asserted.

Why the backup rather than the live file: the deploy already landed, and the
honest question is what the previous code did, not what the tree says now.  The
backup is the previous code, and it is checked against the hash deploy.py
recorded when it wrote it.
"""

# deploy/site_config.py holds this host: absolute paths, ports, pins, GPU.  It is
# imported by name because the synced checkout has the same shape as the live one.
import pathlib as _pathlib, sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
import site_config as site
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

assert os.environ.get("CUDA_VISIBLE_DEVICES") == "", "reproduce is a CPU guarded run"

REPO = Path(__file__).resolve().parents[3]                    # the synced checkout
LIVE = site.DEPLOY_ROOT
BACKUP = site.DEPLOY_RECORDS / "backup/speech_ui.py"
RECORD = Path(tempfile.gettempdir()) / "qasr-reproduce-spawns.log"
FFMPEG = os.environ.get("QASR_TEST_FFMPEG") or str(site.FFMPEG)

STUB_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$$" >> @RECORD@
wav=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --wav) wav="${2:?}"; shift 2 ;;
    *) shift ;;
  esac
done
python3 - "$wav" @CHUNK@ <<'PY'
import sys, wave
wav, chunk_samples = sys.argv[1], int(sys.argv[2])
with wave.open(wav, "rb") as reader:
    chunks = -(-reader.getnframes() // chunk_samples)
for index in range(chunks):
    print('{"chunk":%d,"name":"%s","text":"hello","tokens":[1]}' % (index, wav))
PY
"""

if not BACKUP.exists():
    print(f"REPRODUCE FAILED FOR THE WRONG REASON: no pre-deploy copy at {BACKUP}", flush=True)
    sys.exit(3)
if not Path(FFMPEG).exists():
    print(f"REPRODUCE FAILED FOR THE WRONG REASON: no ffmpeg at {FFMPEG}", flush=True)
    sys.exit(3)

source = BACKUP.read_text()
if "--asr-url" in source:
    print("REPRODUCE NOT OBSERVED: the pre-deploy copy already had a resident path", flush=True)
    sys.exit(1)

# The pre-deploy bridge imports voice_chat at module scope, so the synced tree
# must carry it.  The first campaign-b run found this out the hard way: the old
# remote directory still held files left by earlier labels, and a fresh campaign
# directory has only what --file names.  Name it in run_gate.sh, do not lean on
# whatever a previous run happened to leave behind.
sys.path.insert(0, str(REPO / "tools"))
spec = importlib.util.spec_from_file_location("previous_speech_ui", BACKUP)
previous = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(previous)
except ImportError as error:
    print(f"REPRODUCE FAILED FOR THE WRONG REASON: the pre-deploy copy cannot be "
          f"imported in the synced tree ({error}); add the missing module to the "
          f"--file/--input list in run_gate.sh", flush=True)
    sys.exit(3)

# The stub has to satisfy the PRE-DEPLOY parser, not a convenient one: that code
# requires one JSON chunk per 70400 decoded samples, indexed in order, and names
# the exact wav it was handed (speech_ui.py:106-111).  A stub that fakes the name
# dies in its own shape guard and the reproduce never reaches the thing being
# counted -- which is what the first two attempts did.
CHUNK_SAMPLES = 70400
stub = Path(tempfile.gettempdir()) / "qasr-reproduce-asr"
stub.write_text(STUB_TEMPLATE.replace("@RECORD@", str(RECORD))
                           .replace("@CHUNK@", str(CHUNK_SAMPLES)))
stub.chmod(0o755)
RECORD.write_text("")

config = SimpleNamespace(asr_bin=stub, model=REPO, tokenizer=REPO, ffmpeg=FFMPEG,
                         tts_url="http://127.0.0.1:1", llm_url=None)
import array
import io
import math
import wave
buf = io.BytesIO()
with wave.open(buf, "wb") as out:
    out.setnchannels(1), out.setsampwidth(2), out.setframerate(24000)
    out.writeframes(array.array("h", (int(11000 * math.sin(2 * math.pi * 320 * i / 24000))
                                      for i in range(24000 * 2))).tobytes())
upload = buf.getvalue()

pids = []
for attempt in (1, 2):
    with tempfile.TemporaryDirectory() as directory:
        try:
            result = previous.transcribe(config, upload, directory, lambda: False)
        except Exception as error:                      # noqa: BLE001
            print(f"REPRODUCE FAILED FOR THE WRONG REASON: the previous bridge refused "
                  f"the stub's own reply ({type(error).__name__}: {error}); the spawn "
                  f"count was never reached", flush=True)
            sys.exit(3)
    pids.append(int(RECORD.read_text().split()[-1]))
    print(f"REQUEST {attempt} spawned pid={pids[-1]} text={result['text']!r}", flush=True)

if len(set(pids)) == 2 and pids[0] != pids[1]:
    print(f"REPRODUCE OBSERVED two requests, two processes ({pids[0]} then {pids[1]}): "
          f"the previous bridge starts a fresh ASR, and reloads its model, per request",
          flush=True)
    sys.exit(2)
print(f"REPRODUCE FAILED FOR THE WRONG REASON: pids={pids}", flush=True)
sys.exit(3)
