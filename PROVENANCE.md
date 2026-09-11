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

## The turn that answered nothing, and the silence that hid it

A conversation ran for seventeen turns and then stopped mid-reply. Every server
said otherwise: the bridge logged 87 responses, 86 of them 200 (the 404 was a
favicon), no traceback anywhere; the ASR logged every clip; the GPU stack had not
restarted. The page asked for a transcript, asked for a reply, and then asked for
nothing at all — no TTS, no further poll — and its connection went away.

The last line of `run/qwen.log` says what happened:

```
[chat] prompt 3646 tok (3613 cached) prefill 279 t/s | gen 0 tok 0.0 t/s | reason 0 tok, body 0 tok | stop
```

**Zero tokens, `stop`, HTTP 200.** As far as the protocol is concerned an empty
completion is still a completion, so the failure had nowhere to surface except
the one place nobody was watching.

`web/chat.js` then did the two worst possible things at once. It committed
`{role:'assistant', content:''}` into history, which would have replayed an empty
turn into every later prompt, and it went on to ask the TTS to speak silence. The
exception that followed was caught by the turn's `catch`, which calls
`stopSession()` — and `stopSession()` sets `active = false` and stops the
microphone tracks. A refusal to answer was therefore indistinguishable, from the
user's chair, from the assistant dying.

An empty reply is now an error rather than a turn: never committed, never sent to
the TTS, and carrying `keepSession` so the microphone stays open and the next
sentence needs no second click on Start. `tests/browser/voice_think_browser.mjs`
asserts in a real browser that after an empty reply the transcript did not grow,
nothing reached `/tts`, the next prompt contains no empty assistant message, and
the following turn works.

**Not reproduced:** the zero-token generation itself. It is one observation in one
engine run at 3646 prompt tokens, and nothing here makes it less likely to recur —
only no longer fatal. Whether the prefix cache or the prompt length caused it is a
question for the engine, not the page.

## Saying so: thinking aloud

A tool round costs a second full generation — visible in `qwen.log` as a fresh
`prompt N tok (0 cached)` after a `tc ok N` — so the gap between the last word of
a question and the first word of an answer can run to several seconds. Silence
there is indistinguishable from a hang, which is exactly how the empty-reply bug
above presented.

The page now speaks during that gap, and the wording comes from the tool the
server actually reported running (`search_notes` → "Let me check your notes.",
`fetch_url` → "Let me look that up."), never from a guess. Three properties are
asserted rather than intended:

| | |
|---|---|
| fast reply, no tools | **no** acknowledgment — a 60 ms answer is never padded with a fake "one moment" |
| slow reply, no tools | the 900 ms deadline says "One moment."; the answer still arrives |
| slow reply, two tool rounds | exactly **one** acknowledgment for the turn |
| answer arrives | the acknowledgment is dropped if it has not started, stopped if it has — it never delays the reply |
| switch off | both routes to the mouth honour it (the first version gated only the timer, and the test caught it) |

The acknowledgment is deliberately **not** phase `speaking`, so barge-in stays
disarmed for it: a room cannot interrupt its own filler and be credited with
interrupting the reply. Space still stops everything.

### What is actually slow, measured on the live bridge

Synthesised speech is not the regression. Timed against `https://127.0.0.1:8094/tts`
with the stack idle:

| reply length | time to first byte |
|---|---|
| 32 chars | 0.098 s |
| 108 chars | 0.290 s |
| 441 chars | 0.925 s |
| 774 chars | 1.163 s |

That is a flat ~1.5–2.1 ms per character, i.e. roughly 500 characters a second,
with no fixed cost worth mentioning. What changed is the **length** of the
answers: the same `qwen.log` shows plain turns generating 50–85 tokens and
tool turns generating 116–178 for the final answer. A tool turn is therefore a
second full generation *and* a three-to-four-times-longer thing to synthesise and
to listen to. The acknowledgment addresses the part of that which is fixable in
the page; the length is a prompt decision, not a latency bug.

## Not waiting for the whole reply — `voice-pipeline-tts-20260910`

The section above concluded that synthesis speed was not the regression, and it
was right: the engine holds a flat ~2 ms per character. It stopped one step short,
though, and the gap the user kept describing as "TTS feels slower" lives in that
step.

The page used to issue **one** `/tts` request for the entire answer and then play
it. The engine returns no audio until it has synthesized the whole request, so
time-to-first-word was `generation + synthesis(whole answer)`. On a tool turn —
116–178 tokens, roughly 700–900 characters — that is **1.2 s of silence after the
words are already on the screen**. The text appears instantly; the voice lingers
a second and a half behind it. That interval is the entire complaint, and it is a
buffering policy, not an engine.

`web/chat.js` now splits the reply at sentence boundaries and issues one request
per sentence, with two in flight. The first word is audible after ~0.1 s and the
rest of the answer is synthesized while the voice is already saying the first
part. `SPEECH_CHUNK` holds the three knobs: `minChars` (24) merges fragments too
short to be worth a request, `maxChars` (420) bounds a run-on clause, `prefetch`
(2) is how far ahead of the mouth it asks — enough that the voice never waits,
few enough that a long answer does not stampede a GPU already carrying a 27B
model and an ASR worker.

Three things were load-bearing and are now gated:

* **Nothing may be lost at a seam.** The chunks rejoin to the original
  word-for-word. A hard cut goes after punctuation, then onto a space, and only
  through a word as a last resort — a word cut in half is worse than the pause
  the feature set out to remove. `tests/speech_chunk_test.mjs` asserts this over
  twelve shapes, including a 900-character string with no punctuation at all.
* **The first request must be small.** Asserted directly, not inferred from a
  screenshot.
* **The requests must overlap.** `tests/browser/voice_tts_browser.mjs` runs a
  fake engine that spends 400 ms on any request, then asserts the second sentence
  reached the server before the first finished. The paired sabotage keeps every
  sentence and every byte of audio and merely fetches them one at a time: the
  reply sounds identical and the assertion goes red at 492 ms. That is the whole
  point — the feature is only the overlap.

Two page behaviours had to survive the change. The glow and the barge-in gate are
torn down when a clip ends, so `playReply` now takes a `final` flag and keeps both
up between sentences of the same reply; without it the orb blinks out and
interruption disarms at every comma. `startBargeWatch` became re-entrant, because
the element is briefly paused at each seam and the loop bails out on that.

The acknowledgment from the previous section gets exactly as much air as the first
sentence took to synthesize, and is cut at the last moment before real speech
begins. That is a deliberate change of moment: with synthesis costing 1.2 s the
filler usually finished on its own, and with it costing 0.1 s it would otherwise
be clipped mid-syllable on almost every turn.

A synthesis failure no longer ends the conversation. The words are already on the
screen and the microphone still works, so a lost sentence carries `keepSession`
and the turn stays open — the same lesson the empty-reply fix taught.

## The conversation moved into the browser — 2026-09-10

Deployed as campaign `voice-history-20260910-a` from head `5b039de`; the served
`chat.js` is `0ea4e4a5fa64e55b428be6dad8a3dddbd7e46038e635ff2fbe4ddd286a10d5b3`,
byte-identical to this checkout. GPU 2 was not touched: PID `761368`, active
since `05:02:04 UTC` before the campaign and after it.

A thread about one film restarted itself with "I'm not sure what you mean by
Carlton", which read as a model that was not doing knowledge-aware context
processing. The mechanism underneath is duller and is worth stating precisely:
`tools/voice_chat.py` keeps no session and `parse_messages` takes the role from
*position*, so the transcript in `web/chat.js` is the model's entire context, and
a page reload deleted it. **A reload is therefore a mechanism that reproduces
that symptom exactly -- it is not a proven cause of it.** No log was recovered
for the turn in question, so this is a candidate explanation with a fix, not a
diagnosis. What is established is the general failure: with no persistence, any
reload mid-thread leaves the model a first question to reason about.

That is why the fix is in the page and not on the server. A server-side session
would need an identity to hang state on, a lifetime, and a privacy statement, to
re-learn something the browser already holds — and it would still be the same
stateless bridge underneath. So `web/chat.js` now owns one more contract: **the
page is the model's memory**, and it says so in the header rather than implying
it remembers more than it sends.

Three constants were measured against the deployed bridge, not read off the
source, and the first draft of the comments had one of them wrong (it said 100;
the ceiling is 101):

| Constant | Value | Measured on `:8094` |
| --- | --- | --- |
| `SEND_TURNS` | 40 turns = 81 messages sent | 101 messages → 200, 102 → 400 |
| `MAX_MESSAGE` | 8000 characters | 8000 → 200, 8001 → 400 |
| `STORE_TURNS` / `STORE_BYTES` | 200 turns / 1.2 MB | localStorage budget, trimmed proportionally |

`transcript` is the single source of truth and `history` is derived from it, so
the model can never be shown a turn the browser would lose on refresh, nor forget
one it kept. A roll-off repaints for the same reason: a bubble that a refresh
would delete is a lie on screen.

Two defects were caught by the new suite rather than by reading the code. The
ownership rule — a tab may write only into the conversation whose id it owns —
was initially too strict, and **"New chat" could never take the key back**: it
cleared the screen and left the transcript on disk, so the next reload resurrected exactly what the user had just erased. Claiming the key is now an explicit
act. The other was the byte trim: dropping one turn per attempt could exhaust the
retry budget on a large record and exit **without writing anything** while the
counter still read "saved in this browser". The trim is now proportional.

`tests/browser/voice_history_browser.mjs` asserts nine claims by reading the
`/chat/completions` bodies and the stored record, never a screenshot, because
"the bubbles came back" and "the model got its context back" are different claims
and the first is nearly free to fake. Its sabotage keeps every bubble, every
request and every byte of audio and merely stops writing the turn down; it fails
at the first assertion, and `make sabotage-selftest` proves the arm is capable of
failing. The suite also seeds storage by hand to prove a forged record cannot
author an assistant turn or exceed the per-message ceiling, and opens a second tab
to prove a *New chat* over there is not resurrected by a reply finishing here.

What is **not** claimed: the reload step of the 30-second manual check was not
performed on real hardware, and nothing here says anything about a room. The
transcript now persists in the browser profile, which is a change in where user
data lives; the page footnote says so plainly, and **New chat** and clearing site
data are the two ways it goes away.

### Conversation list, barge-in across sentences, and the reply's seams (2026-09-11)

Three defects reported from use, one day after the pipelined reply shipped.
*"What are you doing?" always held*: the turn judge tested its open-word list
before terminal punctuation, and `doing`/`to`/`from`/`for` are on that list, so
common questions were held three times each until "Send now"; a question mark
now outranks the list, a full stop still does not (`tests/turn_control_test.py`).
*Interruption stopped working on multi-sentence replies*: every clip's `play`,
`pause` and `ended` re-armed the 350 ms AEC settle window and restarted the
voiced counter, so the gate was closed for most of a reply made of short
sentences; the whole reply is now one playback for the gate (`replySeam`).
*Hiccups between sentences*: one request per sentence meant one connection,
one serialized G2P pass and one GPU step per sentence, and a source swap the ear
hears at every full stop; the page now sends the first sentence alone and the
rest as a few requests that grow geometrically against the previous clip's
playback, so the engine batches the bulk and a reply has one or two seams.
The conversation list that had been left uncommitted on 2026-09-10 crashed the
page at load (it bound a button the page did not have); it is finished here —
index plus per-chat keys, migration of the single-chat record, switching,
deleting, deep links — and `voice_history_browser.mjs` grew from nine claims to
eleven. Supersedes the ownership paragraph above: a tab writes into the chat it
has open, and "New chat" no longer erases anything.

Shipped to the CPU bridge as campaign `voice-fixes-20260911-a` from head 507e514
(fingerprint 3e36b7bc5eec…): stage, install and restart all PASS under the guard
(`evidence/guarded-voice-fixes-20260911-a-*`). The bridge is PID 59644 since
08:08:44 UTC, NRestarts=0; `voice-stack-gpu2.service` kept PID 53262 and its
07:37:54 UTC start across the campaign. Served `chat.js`
`24a3d8cff9c2f537ed63750cc80df2c4cccaaac3c74f890844d0fe83a9e29b24` and `chat`
`6a898c5488cc1750ec4417a39973c245d28cfc967eb5c8d57118cb320d7953cb` over TLS
equal this checkout; `check_site.py --compare-live` PASS 20/20 on the live tree.
The 30-second manual microphone check is owed, as before. Rollback snapshot
stays at `~/qwen36/voice-stage/voice-fixes-20260911-a/rollback/` until it passes.

### Speaking while the model is still talking (2026-09-11)

Measured on the live stack: a first sentence synthesizes in 65 ms and a whole
paragraph in 255 ms, the bridge adds 2 ms, and the model answers a typical
question in about 2 s with a time-to-first-token of up to 1.9 s -- yet the page
did not speak until the whole answer had arrived, because the bridge called the
model with `stream: false` and forwarded only the final `answer`. The silence
the user heard was generation, not synthesis. The bridge now streams the
model's prose as `delta` events (tool-call and reasoning text withheld; a plain
JSON body from an engine that ignores `stream` is still accepted) and the page
speaks each sentence as it completes, taking the next group late so the engine
still batches. Gates: four streaming cases in `tests/tool_loop_test.py` and a
streamed turn in `voice_tts_browser.mjs` that asserts the first sentence was
requested before the answer event existed. The transcript still commits only
the `answer`.

Shipped to the CPU bridge as campaign `voice-stream-20260911-a` from head 47cb7e3:
stage, install and restart PASS under the guard (`evidence/guarded-voice-stream-20260911-a-*`).
Bridge PID 61188 since 08:29:37 UTC, NRestarts=0; `voice-stack-gpu2.service` kept
PID 53262 / 07:37:54 UTC. Served `chat.js`
`94caa8c20978a5a0a35cbf88b0c39615f2608233a2fe74d628e12aa2f730a695` equals this
checkout; `check_site.py --compare-live` PASS 20/20. Measured on the live bridge
right after: first prose delta 0.10 s after the question, full answer at 0.96 s
(284 chars, 51 deltas). The 30-second manual microphone check is still owed.

