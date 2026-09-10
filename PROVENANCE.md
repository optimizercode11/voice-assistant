# Provenance

Extracted on 2026-09-09 from `/mnt/inference-engine` at commit `890e8e4`
("Merge the qasr deploy lane: the GPU-2 voice stack's ASR is the resident
server").  Nothing here was rewritten from memory: every file was copied from
the engine repo or from the live deployment tree, then adapted, then verified.

## Byte-for-byte against what is actually running

The live tree is `vllm:~/qwen36/voice-stack/voice-restore-20260908/`.  These
files are **identical** to it (sha256, verified during the extraction):

| File | sha256 |
|---|---|
| `tools/speech_ui.py` | `751e7efbf5441da678dfb989727d38d99dc321d58b7a84f86852622ac7a287be` |
| `tools/voice_chat.py` | `7f5df75fd2c592691c5357db1d09ad820608738b3b590636911025773543e3e3` |
| `tools/g2p_sidecar.py` | `325999b23b736cc951bc37ce437868ac4caf5e8d6eab353b54ab8ff4c49fe811` |
| `web/chat.html` | `c288c84bfaee7282b8401ad91b98c9fbdda7a567857a5a9e394f9466b1f8b5c4` |
| `web/chat.js` | `77046d69ae7b87d005375871574e070e32d2c64a9670fab8baec8e4d747b39f3` |
| `web/index.html` | `5292b9a2fbe09c13a6bf4cc3c0002b3b5defc14869a670a6105c3e9930ba98a0` |

The directory names `tools/` and `web/` are kept on purpose: the bridge, the
tests and the deploy scripts all resolve assets relative to the checkout, and
matching the live tree exactly is worth more than a prettier layout.

## Changed during the extraction

| File | From | Change |
|---|---|---|
| `deploy/voice_stack.py` | `kokoro/evidence/qasr-deploy/voice_stack.py` (live `deploy/voice_stack.py`) | Constants moved to `site_config.py`; behaviour unchanged. |
| `deploy/site_config.py` | — | New: every absolute path, port, pin and GPU that was scattered across the supervisor and the campaign scripts. |
| `deploy/campaigns/qasr-20260909/*.py` | `kokoro/evidence/qasr-deploy/` | Import the site profile instead of repeating host paths; `deploy.py` installs `voice_stack.py` **and** `site_config.py`. |
| `deploy/{start-voice-stack.sh,voice-stack-gpu2.service,install-service.sh}` | live tree (`deploy-install.sh` → `install-service.sh`) | Campaign, device, CPU set and paths come from `site_config.py`; install refuses to run outside `DEPLOY_ROOT`. |
| `tests/*.py` | `kokoro/tools/*_test.py` | Path bootstrap only. |
| `tests/browser/*.mjs` | `kokoro/tools/*_browser.mjs`, `page_harness.mjs` | Playwright resolved through `$PLAYWRIGHT`; fixtures from `tests/fixtures/`; screenshots to `evidence/browser/`. |
| `tests/fixtures/microphone.wav` | `kokoro/evidence/recording/microphone.wav` (3.25 s) | Upload body for the STT stubs. |
| `tests/fixtures/capture.wav` | `kokoro/evidence/voicechat/microphone.wav` (13.25 s) | Chromium's fake microphone device. These are two different recordings; collapsing them makes the pause detector never close a turn. |
| `tests/fixtures/capture-two-part.wav` | generated from `tests/fixtures/capture.wav` by `tests/fixtures/make_two_part_capture.py` | Two real speech bursts lifted out of that recording, 1.8 s apart, so the endpointer cuts one sentence in half. Kept as a generator because the *timing structure* is the fixture and a blob of samples cannot be reviewed for it. |
| `tests/browser/voice_carry_browser.mjs` | — | New: reproduces the reported "it only sends `I`" failure end to end, and its `--sabotage` arm is that failure. |
| `scripts/{guarded-run,guarded-hostrun,guardrail-check,worktree-fingerprint}` | `inference-engine/scripts/` | Vendored so evidence binds *this* tree; see `scripts/VENDORED.md`. |

## Verification actually performed during the extraction

* `CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py` — 11/11 pass.
* `CUDA_VISIBLE_DEVICES="" python3 tests/voice_chat_test.py` — 5/5 pass.
* `tests/browser/{voice_controls,voice_chat,voice_language,stt}_browser.mjs` —
  pass on chromium **and** webkit.
* Paired sabotages fail for the intended reason: `voice_controls_browser.mjs
  --sabotage` (keyboard handler) and `speech_ui_test.py --speaker-sabotage`.
* `python3 deploy/check_site.py` — 19/19 offline.
* On the host, through the guard (`repo-extract-20260909`, `--cpu --cpus 24-27
  --nice 10`): `check_site.py --compare-live --require-assets` — **PASS, 21
  checks**, all 98 bound inputs present, and the refactored supervisor binds
  **the same 86 external assets as the live one, +0 −0**.  That is the parity
  gate for the `site_config.py` extraction: same assets, no new assumption.
  Manifest: `evidence/guarded-repo-extract-20260909-*.json`.
* The four campaign scripts import against the new profile and stop at their
  own guard assertions.

Not re-run from this repo: the GPU labels of any campaign.  They need the
authorized device, and the closed campaign's evidence stays in the engine repo.

## What stayed behind, and why

`/mnt/inference-engine` keeps its copies under `kokoro/tools/`, `kokoro/web/`
and `kokoro/evidence/`.  They are **not** deleted: `worktree-fingerprint` binds
evidence to a source fingerprint, so removing those files would silently turn
every recorded green gate for the voice campaigns into unverifiable history.
The engine repo also keeps the engines themselves — `kserver`, `qasr_serve`,
`q38_27_server` — which this repo consumes as pinned binaries, never rebuilds.

## The capability layer (tools, RAG, MCP) — written here, not yet deployed

Everything under `tools/agent_*.py`, `tools/retrieval.py`, `tools/mcp_client.py`,
`tools/voicectl.py`, `config/`, `TOOLS.md` and the six suites that cover it is
**new work in this repo, not a copy of anything certified**.  It has never run
against a real model, a real GPU or the live host.  What backs it is offline and
reproducible:

```bash
make check       # 71 contracts: 11 bridge, 5 adapter, 20 registry/config,
                 # 11 RAG, 12 MCP, 12 loop, 19 site checks
make sabotage    # 5 paired negatives, each must FAIL; all 5 refuse
make test-browser  # 5 suites × chromium/webkit
```

No GPU, no model, no network: the LLM upstreams are scripted fakes and the MCP
peer is `tests/fixtures/fake_mcp_server.py`, a real stdio JSON-RPC process.

**The live deployment does not run this code.**  `https://192.168.228.113:8092/chat`
still serves the pre-tools `chat.html`/`chat.js` and starts the bridge without
`--tools-config`, because `site_config.py:TOOLS_CONFIG` is empty.  That is
deliberate, and it has a consequence an operator must not discover by surprise:

* `check_site.py --compare-live --require-assets` will now **fail** on the
  `web/` assets, because the page served by the host predates the tool-progress
  UI.  That is the gate doing its job — local and live have genuinely diverged —
  and it stays red until the tree is deployed.
* Deploying it means restarting the bridge, which is GPU-affecting on the device
  named in `site_config.py`.  No device was authorized for this work, so no
  deploy was attempted and no host parity run is claimed here.

Two behaviours changed in files the live host also runs, both deliberate:

1. `speech_ui.py` now requires `CUDA_VISIBLE_DEVICES=2` only on the **native
   vvasr** path.  With `--asr-url` the bridge is `http.server` plus ffmpeg and
   never initialises a device, so the old unconditional check was a demand to
   *claim* a GPU rather than to use one — and it made the bridge impossible to
   start or test on a machine with none.  The live stack launches under the
   guard, which sets the variable anyway, so its containment claim is unchanged.
2. `SIGTERM` now exits **0** through the clean shutdown path instead of dying by
   signal, so the bridge reaps its MCP children on `systemctl stop`.  With no
   MCP servers configured this is a no-op for the live stack.

## Deployed beside the live stack — campaign `voice-tools-20260910`

**https://192.168.228.113:8094/chat** — the tools bridge, on GPU 2, running this
repo's code with tools + RAG + MCP enabled.

It is a *second* bridge, not a replacement. The GPU holds 27719 of 32607 MiB, so
a second Qwen is arithmetically impossible; the new bridge reuses the resident
q38 `:8080`, qasr `:8095` and Kokoro `:8090` and listens on 8093/8094. The
certified assistant at `:8092` was never restarted — its children are the same
five PIDs (`400639/401257/401527/401652/402137`) they were before this work.

```bash
systemctl --user {status,restart,stop} voice-tools-bridge.service   # on vllm
# tree: ~/qwen36/voice-stack/voice-tools-20260910
```

### Verified against the real models, not fakes

| | |
|---|---|
| `regression` | `make check` on the host under the guard — 71 contracts + 19 site checks, rc=0 |
| `positive` | index built (6 docs → 50 chunks); `doctor --probe`: MCP server `stack` up |
| real tool call | q38 emitted `tool_calls` (`call_4_0`) for `search_notes`; bridge ran FTS5 in 9 ms; second generation answered grounded in `RUNBOOK.md` |
| real MCP call | model called `mcp__stack__health` (9 ms) and relayed what qasr reports about itself |
| real STT | `microphone.wav` → resident qasr on device 2 → 44 ms, correct transcript |
| real TTS | Kokoro returned a 154 KB WAV through the bridge |
| shutdown | after 4 service restarts, exactly one `mcp_stack_status.py` remains, parented to the live bridge — SIGTERM reaping holds in production |

Manifests: `evidence/guarded-voice-tools-20260910-*.json`.

### This is not the certified stack

`voice-stack-gpu2` remains the certified deployment and still runs the
pre-tools page. `check_site.py --compare-live --require-assets` still fails on
the `web/` assets, correctly: local and live have genuinely diverged. Promoting
this tree to `:8092` is a separate, authorized decision, not a side effect.

### Two defects only testing the real URL could find

1. **The TLS listener had no registry.** `main()` built the secure
   `SpeechServer` with the shared locks but not the capabilities, so
   `:8094/chat/health` reported `{"tools": []}` while `:8093` reported all
   three. Since browsers refuse `getUserMedia` on an insecure origin, the
   listener the microphone can use was the broken one — a page that looked
   deployed and had no tools. 71 green contracts could not see it: every
   in-process test builds its own `SpeechServer`, and the subprocess test
   asserted against the plain HTTP port.
2. **The health MCP server probed an HTTPS port over plain HTTP**, so the model
   faithfully reported a bridge outage that did not exist. A health tool that
   lies is worse than no health tool.

Both are recorded because they are the argument for deploying before believing,
not because they are interesting in themselves.

### Read-only file browsing over MCP (same campaign)

`tools/mcp_files.py` adds `list_dir` / `read_file` / `grep` / `find` / `roots`
as a second MCP server.  The bridge's tool count goes 2 -> 8, and
`doctor --probe` reports `files  up  5 tools  restarts=0`.

Two findings came out of building it that were not the request:

1. **The output cap produced invalid JSON.**  The first implementation enforced
   `max_output_chars` by slicing the serialized string, so any large result
   reached the model as a document that no longer parses: a "too long" guard
   that turned a big answer into a corrupt one.  It was caught by a test that
   only existed because the fixture file happened to be big enough to trip the
   cap.  `_fit()` now trims the payload -- drop rows, shorten the text field --
   and reports `rows_dropped`, so the response is always valid JSON.
2. **The containment check is load-bearing, and only the sabotage proves it.**
   Under the tempting refactor "we already rejected `..` and absolute paths, so
   a plain join is enough", `read_file("escape")` where `escape -> /etc/passwd`
   returns the real `/etc/passwd`.  The argument layer never sees a symlink;
   only `realpath()` after resolution does.  `make sabotage` runs that arm and
   requires it to fail.

Verified offline: 29 tests in `tests/mcp_files_test.py` against a real
subprocess peer, `make check` green, `make sabotage` refusing for the right
reason.  The shipped `config/host.toml` exposes exactly one root -- the
deployed tree, checked for credentials and key material -- not `$HOME`.

## Letting the user interrupt the reply — 2026-09-10

The request was "use WebRTC AEC3 so I can interrupt the model mid-response".
AEC3 was already being *requested* — `getUserMedia` has passed
`echoCancellation:true` since the first commit. The page muted the microphone
for the whole reply, so there was nothing to cancel and nobody listening.

Three defects came out of building the gate, none of them from reading the code:

1. **`armGlow()` asked the media element for `createMediaElementSource`.** That
   method belongs to `AudioContext`, so the capability check was one no browser
   can pass: `typeof player.createMediaElementSource === 'undefined'` and
   `typeof ctx.createMediaElementSource === 'function'`, measured in headless
   Chromium. The graph was therefore never built, which silently cost three
   things at once — the orb glow while the assistant speaks, the echo reference
   the gate needs, and barge-in itself.
   *The test that hid it:* `voice_barge_browser.mjs` asserted the page refuses
   barge-in with "the reply is not routed through WebAudio" and explained the
   refusal as a headless-browser limitation. Headless Chromium reaches
   `AudioContext.state === 'running'` normally. The suite was grading the bug as
   correct behaviour, which is what a test for the wrong property buys.
   It now asserts the opposite: a device that cancels keeps a live microphone
   **and** moves the glow meter during playback, and a device that reports no
   cancellation is refused with the microphone muted.
2. **`listen()` read `playbackEndedAt` as a flag.** It is a timestamp. Asking
   only whether it is non-zero charged the 350 ms AEC settle window to every
   clip forever after the first reply, instead of to the one clip that follows
   a reply. The window is now computed from elapsed time.
3. **Two sabotage arms were green for the wrong reason.** `make sabotage` reads
   exit 0 as "the mutation escaped" and non-zero as "correctly refused".
   `barge_gate_test.mjs --sabotage` caught its sabotage — four FAIL lines — and
   then exited 0, so the target reported the echo floor as decorative. The same
   inverted convention sat in four browser suites, where the "sabotage escaped"
   branch exited 1 and would have been read as a refusal. Every arm now carries
   a `--dead-sabotage` self-check: apply a no-op mutation through the real
   sabotage path and require the run to finish its assertions and exit 0. That
   is the only way to tell an arm that caught a regression from an arm whose
   anchor stopped matching the source. `make sabotage-selftest` runs them all.

### Measured, offline, against a waveform

`tests/echo_path_test.mjs` extracts the gate, the watcher, the clock and the
hold from `web/chat.js` verbatim and drives them with the committed reply
delayed 10–40 ms into the microphone at a sweep of return losses.

| | |
|---|---|
| held interruptions from echo | none through `g = 0.5` (= `BARGE.echoGain`), across 31 delays and a spectrally smoothed path |
| first held interruption | `g = 0.6`, i.e. the AEC3-failure case, earliest at 928 ms |
| positive gate frames | begin at `g = 0.2`; the 220 ms hold rejects them |
| near-end speech over echo | interrupts at 224 ms in all 186 combinations |
| AGC pump on the reply tail | 0 trips with the settle window; 1 trip at +240 ms with `settleMs = 0` |
| device reporting no AEC | refused before the microphone is enabled; the identical uncancelled waveform does interrupt when armed |

**Not certified:** no speaker-to-microphone test was run. A headless browser has
no acoustic echo path, and the waveform test models the residual after
cancellation rather than cancellation itself. `RUNBOOK.md` carries the 30-second
manual check that this cannot replace.

### Deployed: campaign `voice-barge-20260910-a`, 2026-09-10

Shipped to the CPU bridge on GPU-2 host `vllm` at 04:37 UTC, from head
`9e7fa48` / source fingerprint `544d7780…`. The GPU stack was never touched:
`voice-stack-gpu2.service` kept `MainPID=629273` and its `03:29:25 UTC` start
timestamp across the whole campaign, which is the check that distinguishes a
CPU-bridge ship from an accidental 30 GiB restart.

The bridge went from 9 tools to 10 (`request_directory`) and the notes index
from 60 to 104 chunks, because the promoted docs are four times the size of the
ones the old index was built from. Deployed `web/chat.js` hashes to
`bbe6265e…`, the same digest `tests/echo_path_test.mjs` prints for the source it
extracts the gate from, so the bytes the waveform test certified are the bytes
serving `https://192.168.228.113:8094/chat`.

**Still not certified:** the manual speaker-to-microphone check. The claim on
this page is that echo does not interrupt and a person does, measured offline;
whether a real room with a real speaker agrees is unmeasured, and the
`BARGE.echoGain = 0.5` threshold is the knob that check will adjudicate.

## ASR upgrade — `qasr-fp8-20260910`

The live GPU2 ASR at8095 now uses Qwen3-ASR-1.7B with `--weights fp8`.
Release: `~/qwen36/qasr-fp8/qasr-fp8-20260910`; official checkpoint:
`~/qwen36/qasr-1p7b/qasr-1p7b-20260910/model`. Decoder matrix weights use E4M3;
audio, activations and norms stay BF16. This site profile selects the release,
passes precision explicitly, binds both checkpoint shards and their index, and
requires the expected model/precision in worker readiness.

Engine build commit19df0ff; source fingerprint
`f7c031287f21ffe10c127f09a3dcdaa48eb6405872d01dad059eb7b0a4278c76`;
worker SHA256 `3fa883b1b67c26d5c3e5810c69020cbcfbf39145026a3d99d7878d6bdbdd9980`.
Engine perf guardrail GREEN. Seven native and live HTTP transcript cases exact,
including the production256-token cap; boundary checks and compute-sanitizer
PASS. Resident worker3912MiB versus5760MiB for BF16. Best-of-six engine latency
is5.1-43.6% higher; this deployment trades latency for memory.

The engine campaign applied the matching ASR changes to the existing legacy
supervisor and restarted `voice-stack-gpu2.service` through its GPU2 guard.
Stack startup and live speech round trip passed; ASR server770710/worker771157,
PGID761361. The tools bridge MainPID714008 and MCP children714036/714038 stayed
running. Its interruption build and browser assets were not redeployed here.
The0.6B release remains available for rollback. Full deployment/rollback records
are in `/mnt/inference-engine/qwen3-asr-1.7b/evidence/fp8-deploy/`.
`deploy/check_site.py`:19/19 offline checks pass for this profile.
