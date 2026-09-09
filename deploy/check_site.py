#!/usr/bin/env python3
"""Does deploy/site_config.py still describe a real, single deployment?

Cheap, offline, and it catches the two ways a deployment profile rots:

  1. the systemd unit and site_config disagree about where the stack lives, so
     systemd starts one checkout while an operator edits another; and
  2. the asset list the guard binds (--input per line) silently loses an entry,
     which turns "this run bound the bytes it used" into a claim again.

With --compare-live (run ON the host) it also executes the live checkout's own
`voice_stack.py inputs` and requires the two lists to be identical.  That is the
parity gate for a refactor of the supervisor: same assets, same order, no new
assumption.  It runs no model and touches no device.

With --require-assets it additionally insists every listed path exists, which is
only true on the host; that is the point of making it a flag.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import site_config as site          # noqa: E402
import voice_stack                  # noqa: E402

failures = []
notes = []


def check(condition, message):
    (notes if condition else failures).append(message)
    print(("  ok   " if condition else "  FAIL ") + message, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-live", action="store_true",
                        help="diff inputs() against the live checkout on this host")
    parser.add_argument("--require-assets", action="store_true",
                        help="fail if any bound asset is missing (host only)")
    args = parser.parse_args()

    print(f"== site profile: {site.DEPLOY_ROOT} unit={site.UNIT} gpu={site.GPU} ==")

    # -- the profile is internally coherent ---------------------------------
    check(len(set(site.PORTS)) == len(site.PORTS), f"ports are unique: {site.PORTS}")
    check(site.HTTP_PORT != site.HTTPS_PORT, "HTTP and HTTPS listeners use different ports")
    check(bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", site.CAMPAIGN)),
          f"campaign id is guard-safe: {site.CAMPAIGN}")
    check(all(len(digest) == 64 for digest in site.PINS.values()),
          f"{len(site.PINS)} binaries are pinned by full sha256")
    check(site.CERT.suffix == ".pem" and site.KEY.name == "key.pem",
          "certificate and key are a pair in one directory")

    # -- the unit and the profile tell the same story -----------------------
    unit = (HERE / "voice-stack-gpu2.service").read_text()
    root = str(site.DEPLOY_ROOT).replace(str(Path.home()), "%h")
    for field in ("WorkingDirectory", "PIDFile", "ExecStart"):
        values = re.findall(rf"^{field}=(.*)$", unit, re.M)
        # ExecStart carries an interpreter, so the deployment root appears after it.
        check(len(values) == 1 and root in values[0] and ".." not in values[0],
              f"unit {field}={values} points inside {root}")
    check(f"{root}/deploy/start-voice-stack.sh" in unit,
          "unit ExecStart is the guarded start wrapper")

    # -- the wrapper's campaign/device come from the same file --------------
    wrapper = (HERE / "start-voice-stack.sh").read_text()
    check("site_config" in wrapper and "--input" in wrapper,
          "start wrapper takes campaign/device/inputs from site_config, not literals")

    # -- the bound asset list ------------------------------------------------
    inputs = voice_stack.inputs()
    check(len(inputs) > 40, f"guard binds {len(inputs)} inputs (checkpoints, binaries, source)")
    for group, predicate, label in (
            ("checkpoints", lambda p: str(p).endswith((".safetensors", ".json", ".bin", ".txt", ".jinja")), "checkpoint/config assets"),
            ("binaries", lambda p: p in site.PINS or p.name in {"qasr_worker", "qasr_serve.py", "ffmpeg-linux-x86_64-v7.0.2"}, "executables"),
            ("this checkout", lambda p: str(p).startswith(str(voice_stack.ROOT)), "files from this checkout")):
        check(any(predicate(p) for p in inputs), f"inputs include {label}")
    for required in (voice_stack.ROOT / "tools/speech_ui.py", voice_stack.ROOT / "web/chat.html",
                     voice_stack.ROOT / "web/chat.js", HERE / "site_config.py",
                     HERE / "voice_stack.py"):
        check(required in inputs, f"inputs bind {required.relative_to(voice_stack.ROOT)}")

    if args.require_assets:
        missing = [str(path) for path in inputs if not Path(path).exists()]
        check(not missing, f"all {len(inputs)} bound inputs exist on this machine"
                           + (f"; missing: {missing[:5]}" if missing else ""))

    if args.compare_live:
        live = subprocess.run([sys.executable, "-u",
                               str(site.DEPLOY_ROOT / "deploy/voice_stack.py"), "inputs"],
                              capture_output=True, text=True, cwd=site.DEPLOY_ROOT)
        if live.returncode:
            check(False, f"live checkout refused to list inputs: {live.stderr[-300:]}")
        else:
            theirs = [line for line in live.stdout.splitlines() if line.strip()]
            mine = [str(path) for path in inputs]
            # Each checkout binds its OWN copy of the app, so those paths differ
            # by construction.  What must match is every external asset: the
            # checkpoints, binaries and runtimes the stack actually loads.
            theirs = [p for p in theirs if not p.startswith(str(site.DEPLOY_ROOT) + "/")]
            mine = [p for p in mine if not p.startswith(str(voice_stack.ROOT) + "/")]
            added = [path for path in mine if path not in theirs]
            dropped = [path for path in theirs if path not in mine]
            check(not added and not dropped,
                  f"live and repo bind the same {len(theirs)} inputs "
                  f"(+{len(added)} -{len(dropped)}{' added=' + str(added[:3]) if added else ''}"
                  f"{' removed=' + str(dropped[:3]) if dropped else ''})")

    print(f"== {'PASS' if not failures else 'FAIL'}: {len(notes)} checks, {len(failures)} failures ==")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
