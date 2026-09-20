# Direct workspace tools deployment

Deployed 2026-09-20 at 17:13 UTC, authorized by the user's request to deploy.
Campaign: `voice-workspace-20260920`.
Source fingerprint: `f19e9a5decc8b0f31facd128127d4a2aa74ec51c010f1963a83d4f8e005a483b`.
Source includes the current uncommitted workspace changes; the unrelated local
`deploy/voice-tools-bridge.service` edit was preserved at SHA256
`ec28665dcaa86f53aaf349a5e4356079fe0a2460a2fd421a10a803ebe1170c76`.

The voice service uses `~/qwen36/voice-stack/voice-workspace-20260920` on vllm.
The new `codex-workspace` SSH alias has a dedicated key, authorized on codex
with a forced command running `/mnt/voice-assistant/tools/mcp_workspace.py`
and `--cwd /mnt/voice-workspace`. Previous SSH configuration/authorized_keys
were backed up with `.voice-workspace-20260920.bak` suffixes. No private key
was copied off the GPU host. Existing coding-agent aliases were retained.

The old service's boot guard failed because unrelated GPUs acquired new model
processes during startup. Speech acceptance had passed and old children were
still running, although systemd reported failed/MainPID0. The campaign's
`promote.sh` stopped only that unit's remaining cgroup processes with SIGTERM,
then used the normal snapshot/index/restart update procedure.

Validation:

- Guarded CPU preflight verified speech binaries/runtime/model assets,
  108 bound input files, the notes index, and all six MCP peers. The workspace
  peer advertises five tools; total offered tools: 22.
- Voice service active/enabled, MainPID109397, zero restarts. Fresh GPU4 guard
  passed CLEAN with Kokoro PID109797 and QASR PID110116 attributed to PGID109388.
- External Q38 guard MainPID1839 and model PID2389 stayed unchanged. Q36 remains
  inactive/disabled. Other GPU processes were unchanged.
- Live natural typed requests created a directory and file, listed/read it,
  and ran a shell byte comparison. All five calls used `mcp:workspace`, all
  succeeded, and none called Claude or Codex. Independent project-host byte
  inspection matched the expected content (SHA256
  `5b4b86d52a1286b634e51b478501ddcb1a6451069a0e3cee120220363b7993a9`).
- A request naming only a filename saved under `/mnt/voice-workspace`.
  Independent MCP readback and `pwd` verified the content and default cwd.
- English speech→ASR transcribed the fox pangram exactly; English and Hindi
  synthesis, conversation correction Four→Five, and malformed-input controls
  passed. Startup acceptance verified the TLS certificate.
- 22 direct-tool CPU tests pass with ResourceWarning treated as errors,
  including two new EOF regressions. The two GPU deployment ownership tests
  and the profile coherence check also passed.

Records:

- `WORKSPACE-LIVE-ACCEPTANCE.json`: live model prompts, responses, tool rows.
- `WORKSPACE-SPEECH-ACCEPTANCE.json`: speech/TLS startup acceptance.
- `WORKSPACE-DEFAULT-ACCEPTANCE.json`: default-location request and response.
- `guarded-voice-workspace-20260920-20260920T171230Z-2458067.json`: preflight.
- `guarded-voice-workspace-20260920-20260920T171257Z-2460012.json`: promotion.
- `guarded-voice-workspace-20260920-20260920T171259Z-109343.json`: GPU acceptance.
- `guarded-voice-workspace-20260920-20260920T171328Z-2462226.json`: live tools.
- `guarded-voice-workspace-20260920-20260920T171451Z-2467484.json`: default cwd.

The tests preserve uniquely named scratch files in the workspace for inspection.
Physical microphone/speaker acoustics were not tested from a user device.
Rollback remains the normal voice-unit restoration from this deployment's
`rollback/voice-unit.service`; it does not change the independent LLM service.
