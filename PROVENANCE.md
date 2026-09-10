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
