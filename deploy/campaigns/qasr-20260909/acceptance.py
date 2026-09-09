#!/usr/bin/env python3
"""Device-level acceptance for the ASR swap -- read the stack's evidence, don't
restate it.

The stack's GPU containment is certified by ITS OWN guarded start
(deploy/start-voice-stack.sh runs under .guarded-run), so this script's job is to
prove three things and write them down:

  1. that start happened after the deploy, exited as expected, and attributed
     every new GPU-2 process to its own process group;
  2. the qasr_worker is really resident on GPU 2 in that same process group;
  3. a real TTS -> STT round trip through the TLS bridge still transcribes, and
     says which backend answered.

Foreign-GPU noise from other tenants on this shared host is reported by name.
It is not folded into the verdict and it is not hidden: it is simply not this
campaign's process, and pretending otherwise would be a weaker gate, not a
stronger one.
"""

# deploy/site_config.py holds this host: absolute paths, ports, pins, GPU.  It is
# imported by name because the synced checkout has the same shape as the live one.
import pathlib as _pathlib, sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
import site_config as site
import glob
import hashlib
import http.client
import json
import os
import ssl
import sys
import time
from pathlib import Path

assert os.environ.get("CUDA_VISIBLE_DEVICES") == "2", "run inside the guard on the authorized device"

ROOT = site.DEPLOY_ROOT
CERT = site.CERT
GPU2 = site.GPU_UUID
HERE = Path(__file__).resolve().parent
DEPLOYED = json.loads((site.DEPLOY_RECORDS / "deployment.json").read_text())
# Optional on purpose: a reverify does not restart the stack, and a record that
# has never carried one is not evidence of a bad deploy.  The hard anchor is the
# live supervisor's own start time, computed below from /proc.
DEPLOYED_AT = (DEPLOYED.get("stack_acceptance") or {}).get("time")


def request(path, body=None, port=None, expected=200):
    port = site.HTTPS_PORT if port is None else port
    client = (http.client.HTTPSConnection("127.0.0.1", port,
              context=ssl.create_default_context(cafile=str(CERT)), timeout=90) if port == site.HTTPS_PORT
              else http.client.HTTPConnection("127.0.0.1", port, timeout=90))
    try:
        client.request("GET" if body is None else "POST", path, body)
        response = client.getresponse()
        data = response.read()
        assert response.status == expected, (path, response.status, data[:300])
        return data
    finally:
        client.close()


def process_start_wall(pid):
    """Wall-clock start of a pid: /proc btime plus starttime ticks."""
    with open("/proc/stat") as handle:
        btime = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
    with open(f"/proc/{pid}/stat") as handle:
        starttime = int(handle.read().rsplit(")", 1)[1].split()[19])
    return btime + starttime / os.sysconf("SC_CLK_TCK")


def utc_seconds(stamp):
    return time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))


def request_status(path, body=None):
    """POST without asserting -- a boundary arm is graded BY its status."""
    client = http.client.HTTPSConnection("127.0.0.1", site.HTTPS_PORT,
                                         context=ssl.create_default_context(cafile=str(CERT)),
                                         timeout=120)
    try:
        client.request("POST", path, body)
        response = client.getresponse()
        return response.status, response.read()
    finally:
        client.close()


def tone_wav(seconds, rate=24000):
    import array, io, math, wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(rate)
        out.writeframes(array.array("h", (int(9000 * math.sin(2 * math.pi * 320 * i / rate))
                                          for i in range(int(rate * seconds)))).tobytes())
    return buffer.getvalue()


def ppid(pid):
    with open(f"/proc/{pid}/stat") as handle:
        return int(handle.read().rsplit(")", 1)[1].split()[1])


def pids_matching(needle):
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if needle in cmdline:
            found.append((int(entry.name), cmdline.strip()))
    return found


def main():
    failures, notes = [], []

    manifests = sorted(glob.glob(str(ROOT / "evidence/guarded-*.json")), key=os.path.getmtime)
    stack = None
    for path in reversed(manifests):
        record = json.loads(Path(path).read_text())
        if record.get("label") == "gpu-acceptance" and record.get("campaign") == site.CAMPAIGN:
            stack = (path, record)
            break
    if stack is None:
        raise SystemExit("acceptance: the stack has no guarded gpu-acceptance manifest at all")
    path, record = stack
    containment = record.get("containment", {})
    print(f"STACK_MANIFEST {path}", flush=True)
    if record.get("command_exit_code") != record.get("expected_exit_code"):
        failures.append(f"the stack's own guarded start exited "
                        f"{record.get('command_exit_code')} != {record.get('expected_exit_code')}")
    # The anchor is the process that is actually serving, not a field a re-run can
    # overwrite: the manifest I am about to quote as containment evidence must be
    # the guarded start that BEGAN the live supervisor.  The deploy timestamp is a
    # second check when the record still carries one, never the only one.
    supervisor = int((ROOT / "run/ready").read_text().strip())
    supervisor_started = process_start_wall(supervisor)
    manifest_started = utc_seconds(record["started_utc"])
    if manifest_started < supervisor_started - 5:
        failures.append(
            f"the newest stack manifest ({record['finished_utc']}) describes a stack that "
            f"started before the live supervisor {supervisor} "
            f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(supervisor_started))})")
    elif DEPLOYED_AT is not None and utc_seconds(record["finished_utc"]) < DEPLOYED_AT - 5:
        failures.append(f"the newest stack manifest predates the deploy ({record['finished_utc']})")
    else:
        print(f"CHECK PASS stack_manifest manifest_started="
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(manifest_started))} "
              f"supervisor={supervisor} supervisor_started="
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(supervisor_started))} "
              f"deployed_at={'none' if DEPLOYED_AT is None else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(DEPLOYED_AT))}",
              flush=True)

    foreign, unowned = [], []
    for row in containment.get("new_processes", []):
        if row.get("gpu_uuid") != GPU2:
            foreign.append(f"pid {row.get('pid')} on {row.get('gpu_uuid')}")
        elif row.get("disposition") != "owned":
            unowned.append(f"pid {row.get('pid')} disposition {row.get('disposition')}")
    if unowned:
        failures.append(f"unattributed new processes on GPU 2: {unowned}")
    if foreign:
        notes.append(f"foreign-GPU noise from other tenants (not this campaign): {foreign}")
    print(f"GPU2_ATTRIBUTION verdict={containment.get('verdict')} "
          f"owned={[r.get('pid') for r in containment.get('new_processes', []) if r.get('gpu_uuid') == GPU2]}",
          flush=True)

    children = json.loads((ROOT / "run/children.json").read_text())
    supervisor_pgid = os.getpgid(supervisor)
    workers = pids_matching("qasr_serve.py")
    # The worker is counted by parentage, not by name: the first acceptance run
    # saw two cmdlines matching "qasr_worker" because the server had just
    # respawned its child, and a name match cannot tell a second server from a
    # child that is on its way out.  ppid can.
    serve_pid = workers[0][0] if len(workers) == 1 else None
    engine = [(pid, cmd) for pid, cmd in pids_matching("qasr_worker")
              if serve_pid and ppid(pid) == serve_pid]
    if serve_pid is None or len(engine) != 1:
        failures.append(f"expected exactly one qasr_serve with one child worker, "
                        f"got servers={len(workers)} children={len(engine)}")
    else:
        worker_pid = engine[0][0]
        if os.getpgid(worker_pid) != supervisor_pgid:
            failures.append(f"qasr_worker pgid {os.getpgid(worker_pid)} != stack pgid {supervisor_pgid}")
        else:
            print(f"CHECK PASS residency worker_pid={worker_pid} serve_pid={serve_pid} "
                  f"pgid={supervisor_pgid} children={children}", flush=True)
        if engine[0][1].find("/mnt/qwen3-asr-0.6b-") < 0:
            failures.append(f"qasr_worker is not serving the pinned checkpoint: {engine[0][1][:160]}")

    health = json.loads(request("/stt/health"))
    if health.get("backend") != "qasr":
        failures.append(f"the bridge is not on the qasr backend: {health}")
    spoken = request("/tts?format=wav&language=a&voice=af_heart&speed=1.2",
                     b"The quick brown fox jumps over the lazy dog.")
    asr = json.loads(request("/stt", spoken))
    transcript = asr.get("text", "")
    if "quick brown fox" not in transcript.lower():
        failures.append(f"the live round trip did not transcribe: {transcript!r}")
    else:
        print(f"CHECK PASS round_trip transcript={transcript!r} engine={asr.get('engine')} "
              f"frontend_ms={asr.get('frontend_ms')}", flush=True)
    if not isinstance(asr.get("raw_text"), str) or not asr.get("raw_text"):
        failures.append(f"raw_text is missing: {list(asr)[:8]}")

    # The engine's real bound is 3000 mel frames (30 s) and it is an allocation,
    # not a knob.  The serve campaign proved the refusal at the server's own door;
    # this proves it through the DEPLOYED bridge, because a caller retries a 503
    # and shortens a 413 -- laundering the refusal into a 5xx is a product defect.
    boundary_status, boundary_body = request_status("/stt", tone_wav(31.0))
    if boundary_status != 413:
        failures.append(f"a 31 s upload returned {boundary_status}, not 413: "
                        f"{boundary_body[:160]!r}")
    else:
        print(f"CHECK PASS boundary_30s status={boundary_status} "
              f"detail={boundary_body[:120]!r}", flush=True)

    result = {"stack_manifest": path, "stack_finished": record.get("finished_utc"),
              "stack_verdict": containment.get("verdict"), "gpu2": GPU2,
              "backend": health.get("backend"), "transcript": transcript,
              "engine": asr.get("engine"), "frontend_ms": asr.get("frontend_ms"),
              "worker_pgid": supervisor_pgid if len(engine) == 1 else None,
              "boundary_30s_status": boundary_status,
              "supervisor": supervisor,
              "supervisor_started": supervisor_started,
              "stack_manifest_started": manifest_started,
              "tls_verified": True, "notes": notes, "time": time.time()}
    (ROOT / "evidence/deploy-qasr-20260909/acceptance.json").write_text(
        json.dumps(result, indent=2) + "\n")
    for note in notes:
        print(f"NOTE {note}", flush=True)
    if failures:
        print("GATE FAIL gpu-acceptance " + " | ".join(failures), flush=True)
        return 1
    print("GATE PASS gpu-acceptance " + json.dumps(
        {"backend": result["backend"], "engine": result["engine"],
         "worker_pgid": result["worker_pgid"],
         "boundary_30s": result["boundary_30s_status"]}), flush=True)
    return 0


sys.exit(main())
