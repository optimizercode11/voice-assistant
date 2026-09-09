# Campaign `qasr-deploy-20260909` — ASR becomes a resident server

**Status: closed and green — in the engine repo, not here.**  Recorded under
`qasr-deploy-20260909-b` with profile `gpu-bugfix`; the manifests are in
`/mnt/inference-engine/kokoro/evidence/`.  This directory is the ported
mechanism, kept so the deploy can be repeated or learned from.

## What it changed

Before, the bridge spawned a transcriber **per request**: two uploads, two
processes, two model loads.  After, it POSTs to `qasr_serve`, which already
holds the checkpoint.  `reproduce.py` observes the before-state by executing the
byte-for-byte backup of the previous `speech_ui.py` and counting the PIDs one
call spawns — expected `rc=2`, because that is the defect being removed.

`negative.py` is the one worth reading: the dangerous failure mode of replacing
a subprocess with an HTTP call is not a wrong transcript, it is a **200 with an
empty one** — the assistant answering silence with confidence.  Control
transcribes; with the engine gone the bridge must answer 503 with a name.

## Before re-running this

* `expected.json` still records the hashes **that campaign installed**.  The
  extraction moved the supervisor's constants into `site_config.py`, so
  `voice_stack.py` no longer matches, and `site_config.py` has no entry at all.
  `deploy.py` therefore **refuses**.  That is the intended fail-closed
  behaviour: do not edit `expected.json` to make it pass.
* A re-run is a **new campaign**: choose a new ID, generate a new
  `expected.json` from the files you are actually installing, and run
  `run_gate.sh` end to end.
* Labels 3 and 5 (`negative`, `gpu-acceptance`) need the authorized device.
  Labels 1, 2 and 4 are CPU guarded runs — including the deploy itself, on
  purpose (`deploy.py` explains why).
