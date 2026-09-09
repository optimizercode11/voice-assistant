#!/usr/bin/env python3
"""Swap the live GPU-2 voice stack's ASR from a per-request vvasr spawn to the
resident qasr_serve -- with a rollback that runs on any failure, including a
crash, and a verification that reads the stack's own evidence instead of trusting
this script's own opinion.

WHY THIS RUNS AS A CPU GUARDED RUN
    Installing files and asking systemd to restart a unit are not GPU commands.
    The stack's GPU work happens inside ITS OWN guarded start
    (deploy/start-voice-stack.sh -> .guarded-run -> voice_stack.py), so its
    containment is certified by that run's manifest, which acceptance.py reads.
    Restarting it from inside a GPU guarded run would instead make the stack's
    own PIDs look like foreign processes appearing on the card mid-run.

WHAT IS BOUND BEFORE ANYTHING IS TOUCHED
    expected.json names the qasr release's source fingerprint and git head, and
    the sha256 of both files being installed.  A release built from a different
    tree than the one whose gates are quoted is the whole reason this repo treats
    binaries as stale by default, so it is checked before the live tree is.
"""

# deploy/site_config.py holds this host: absolute paths, ports, pins, GPU.  It is
# imported by name because the synced checkout has the same shape as the live one.
import pathlib as _pathlib, sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
import site_config as site

import hashlib
import http.client
import json
import os
import ssl
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

assert os.environ.get("CUDA_VISIBLE_DEVICES") == "", "deploy is a CPU guarded run"

ROOT = site.DEPLOY_ROOT                           # the live checkout on the host
UNIT = site.UNIT
PORTS = site.PORTS
CERT = site.CERT
HERE = Path(__file__).resolve().parent            # this campaign directory
REPO = HERE.parents[2]                            # the synced voice-assistant checkout
EXPECTED = json.loads((HERE / "expected.json").read_text())
# Written beside the thing it changed, not inside the rsync'd campaign dir: the
# rollback copies have to survive a re-sync, and guarded-hostrun only pulls the
# guard's own manifests back, so a deployment record that lives only in the
# campaign dir is unreadable by the next run and unbindable by the next gate.
RUN = site.DEPLOY_RECORDS
RUN.mkdir(parents=True, exist_ok=True)

# Source and destination are the same relative path: the checkout in the synced
# campaign dir IS the shape of the live checkout, so an install is a copy, not a
# rename.  voice_stack.py and site_config.py travel together -- the supervisor
# imports the profile, so installing one without the other is a broken stack.
TARGETS = {REPO / "tools/speech_ui.py": ROOT / "tools/speech_ui.py",
           REPO / "deploy/voice_stack.py": ROOT / "deploy/voice_stack.py",
           REPO / "deploy/site_config.py": ROOT / "deploy/site_config.py"}


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def systemctl(*args, check=True):
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"systemctl {' '.join(args)}: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def tls_request(path, body=None, expected=200):
    client = http.client.HTTPSConnection("127.0.0.1", 8092,
                                         context=ssl.create_default_context(cafile=str(CERT)),
                                         timeout=90)
    try:
        client.request("GET" if body is None else "POST", path, body)
        response = client.getresponse()
        data = response.read()
        assert response.status == expected, (path, response.status, data[:200])
        return data
    finally:
        client.close()


def supervisor_pid():
    """The supervisor, from run/ready, falling back to /proc.

    The unit is currently `failed` (StartLimitBurst tripped at 06:09:58) while
    its processes are still alive -- systemd logged five "remains running after
    unit stopped" lines and left them in the cgroup.  So "is the stack running"
    cannot be answered from systemctl, and a restart that assumes it can would
    start a second stack against ports the first one still holds.
    """
    ready = ROOT / "run/ready"
    if ready.exists():
        try:
            pid = int(ready.read_text().strip())
            if Path(f"/proc/{pid}").exists():
                return pid
        except ValueError:
            pass
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            line = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "voice_stack.py supervise" in line:
            return int(entry.name)
    return None


def ports_free():
    import socket
    held = []
    for port in PORTS:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                held.append(port)
    return held


def stop_stack(seconds=240):
    """SIGTERM the supervisor and wait for the whole stack to let go.

    The supervisor's own handler terminates children in reverse order and waits,
    and the repo rule is no SIGKILL as a first action -- so this asks once and
    waits, and refuses to proceed rather than escalating into a 30 GiB model's
    teardown.
    """
    pid = supervisor_pid()
    if pid is None:
        print("STOP nothing to stop (no supervisor)", flush=True)
    else:
        print(f"STOP SIGTERM supervisor {pid}", flush=True)
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        gone = pid is None or not Path(f"/proc/{pid}").exists()
        if gone and not ports_free():
            return
        time.sleep(1)
    held = ports_free()
    alive = supervisor_pid()
    if alive or held:
        raise RuntimeError(f"stack did not stop in {seconds}s (supervisor={alive}, ports={held}); "
                           "refusing to start a second stack or to escalate to SIGKILL")


def start_unit():
    systemctl("reset-failed", UNIT, check=False)   # the burst counter is why it is 'failed'
    systemctl("start", UNIT)


def stack_state():
    return {"active": systemctl("is-active", UNIT, check=False) or "unknown",
            "enabled": systemctl("is-enabled", UNIT, check=False) or "unknown",
            "main_pid": systemctl("show", UNIT, "-p", "MainPID", "--value", check=False) or "0"}


def wait_ready(after_mtime, seconds=600):
    """Ready means the supervisor wrote run/ready AND wrote a fresh acceptance.json."""
    deadline = time.monotonic() + seconds
    acceptance = ROOT / "run/acceptance.json"
    while time.monotonic() < deadline:
        state = stack_state()
        if (ROOT / "run/ready").exists() and acceptance.exists() \
                and acceptance.stat().st_mtime > after_mtime and state["active"] == "active":
            result = json.loads(acceptance.read_text())
            if result.get("time", 0) > after_mtime:
                return result
        time.sleep(2)
    raise RuntimeError(f"stack did not become ready in {seconds}s: {stack_state()}")


def restore(backup, stack_was_running):
    print("ROLLBACK restoring the previous ASR path", flush=True)
    for _, live in TARGETS.items():
        shutil.copyfile(backup / live.name if (backup / live.name).exists() else
                        backup / live.parent.name / live.name, live)
    if stack_was_running:
        stop_stack()
        start_unit()
    return stack_state()


def main():
    release = Path(EXPECTED["qasr_release"])
    manifest = json.loads((release / "RELEASE.json").read_text())
    bound = {"source_fingerprint": manifest["source_fingerprint"], "git_head": manifest["git_head"]}
    for key, want in bound.items():
        if EXPECTED.get(key) != want:
            raise SystemExit(f"release is not the certified one: {key} {want} != "
                             f"{EXPECTED.get(key)} (campaign {EXPECTED.get('campaign')})")
    if digest(release / "qasr_worker") != manifest["qasr_worker_sha256"]:
        raise SystemExit("release binary does not match its own manifest")
    for source, live in TARGETS.items():
        want = EXPECTED["install"].get(str(live))
        got = digest(source)
        if want != got:
            raise SystemExit(f"{source} sha256 {got} != expected {want}")
        if not live.exists():
            raise SystemExit(f"live file is missing, refusing to invent it: {live}")

    if all(digest(live) == EXPECTED["install"].get(str(live)) for _, live in TARGETS.items()):
        # The install already landed (a re-run of a completed campaign, which is
        # what guardrail-check forces once any label needs re-recording).  Do NOT
        # back these files up as the rollback -- they are the new ones -- just
        # re-verify the live stack and re-record what it says.
        print("ALREADY DEPLOYED the live files already match the expected hashes; "
              "re-verifying instead of restarting", flush=True)
        spoken = tls_request("/tts?format=wav&language=a&voice=af_heart&speed=1.2",
                             b"The quick brown fox jumps over the lazy dog.")
        asr = json.loads(tls_request("/stt", spoken))
        health = json.loads(tls_request("/stt/health"))
        transcript = asr.get("text", "")
        if health.get("backend") != "qasr":
            raise SystemExit(f"the live stack is not on the qasr backend: {health}")
        if "quick brown fox" not in transcript.lower():
            raise SystemExit(f"the live round trip did not transcribe: {transcript!r}")
        prior = {}
        if (RUN / "deployment.json").exists():
            prior = json.loads((RUN / "deployment.json").read_text())
        # Carry the original install forward instead of overwriting it.  A reverify
        # does not restart the stack, so the stack's guarded start -- the
        # containment evidence acceptance.py reads -- still belongs to the deploy
        # that actually installed these bytes.  Rewriting the record without it is
        # what made the next gate unreadable (KeyError: stack_acceptance): the
        # re-run destroyed the only thing that bound the running stack to this
        # install, which is the same failure dbe1f4a fixed one layer out.
        result = {"unit": UNIT, "reverified": True, "health": health, "asr": asr,
                  "transcript": transcript, "supervisor": supervisor_pid(),
                  "installed": {str(live): digest(live) for _, live in TARGETS.items()},
                  "bound": {"source_fingerprint": EXPECTED["source_fingerprint"],
                            "git_head": EXPECTED["git_head"]},
                  "stack_acceptance": prior.get("stack_acceptance"),
                  "deployed_at": prior.get("deployed_at", prior.get("time")),
                  "tls_verified": (prior.get("stack_acceptance") or {}).get("tls_verified"),
                  "time": time.time()}
        if result["stack_acceptance"] is None:
            print("NOTE the prior deploy record carries no stack_acceptance, so this "
                  "record cannot anchor the stack's GPU containment to an install; "
                  "gpu-acceptance falls back to the live supervisor's start time",
                  flush=True)
        (RUN / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
        print("DEPLOY PASS " + json.dumps({"reverified": True, "transcript": transcript,
                                           "engine": asr.get("engine")}), flush=True)
        return 0

    before = stack_state()
    # "active" is the wrong question here: the unit is failed while its stack
    # still serves, so what rollback must reproduce is whether the stack was up.
    was_running = supervisor_pid() is not None
    backup = RUN / "backup"
    if backup.exists():
        # A re-run of a campaign that already landed.  The rollback in that
        # directory is the PRE-deploy code -- the very thing reproduce.py executes
        # and the only way back to vibevoice-asr -- so it is never overwritten.
        # It may be reused only while the live files are still the ones this
        # campaign installed; anything else and the rollback no longer describes
        # the system, which is the case worth refusing over.
        prior = {}
        if (RUN / "deployment.json").exists():
            prior = json.loads((RUN / "deployment.json").read_text()).get("installed", {})
        foreign = [str(live) for _, live in TARGETS.items()
                   if digest(live) != prior.get(str(live))]
        if foreign:
            raise SystemExit(f"refusing to reuse the rollback at {backup}: these live files "
                             f"are not the ones this campaign installed: {foreign}")
        print(f"REUSING the existing rollback at {backup} (pre-deploy code, untouched)",
              flush=True)
    else:
        backup.mkdir(parents=True)
        for _, live in TARGETS.items():
            shutil.copyfile(live, backup / live.name)
    (RUN / "state-before.json").write_text(
        json.dumps({"systemd": before, "supervisor": supervisor_pid(),
                    "stack_was_running": was_running, "ports_held": ports_free()},
                   indent=2) + "\n")
    print(f"BACKUP {backup} -> {[p.name for p in sorted(backup.iterdir())]}", flush=True)

    started = time.time()
    success = False
    try:
        for source, live in TARGETS.items():
            temp = live.with_name(live.name + ".qasr-new")
            temp.write_bytes(source.read_bytes())
            shutil.copystat(live, temp)
            temp.replace(live)
            print(f"INSTALLED {live} sha256={digest(live)}", flush=True)
        print(f"RESTART stop + start {UNIT}", flush=True)
        stop_stack()
        start_unit()
        acceptance = wait_ready(started)
        if acceptance.get("asr", {}).get("backend") != "qasr":
            raise RuntimeError(f"stack came up on the wrong ASR backend: {acceptance.get('asr')}")
        transcript = acceptance["transcript"]
        if "quick brown fox" not in transcript.lower():
            raise RuntimeError(f"the stack's own round trip did not transcribe: {transcript!r}")
        result = {"unit": UNIT, "before": before, "after": stack_state(),
                  "release": str(release), "bound": bound, "deployed_at": started,
                  "installed": {str(live): digest(live) for _, live in TARGETS.items()},
                  "stack_acceptance": acceptance, "tls_verified": acceptance["tls_verified"]}
        (RUN / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
        success = True
        print("DEPLOY PASS " + json.dumps({"asr": acceptance["asr"],
                                           "transcript": transcript,
                                           "after": result["after"]}), flush=True)
    except BaseException as error:
        print(f"DEPLOY FAILED {type(error).__name__}: {error}", flush=True)
        try:
            print(f"ROLLBACK STATE {json.dumps(restore(backup, was_running))}", flush=True)
        except BaseException as rollback_error:
            print(f"ROLLBACK FAILED {rollback_error}; the stack needs a human", flush=True)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
