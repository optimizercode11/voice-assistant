# deploy/

How this checkout becomes a running service, and how it stays honest about what
it is running.  Operations (status, logs, restart, rollback, troubleshooting)
are in **`../RUNBOOK.md`**.

| File | Role |
|---|---|
| `site_config.py` | **This host**: checkpoints, accepted binaries + SHA-256 pins, ports, TLS, GPU, deployment root, campaign id. The only file with absolute paths in it. |
| `voice_stack.py` | The supervisor. `inputs` prints what a guarded start binds; `start` verifies then launches and smoke-tests; `supervise` is the long-lived parent. |
| `start-voice-stack.sh` | What systemd runs: enters `.guarded-run` with the device/CPU set from `site_config.py`, then `voice_stack.py start`. |
| `voice-stack-gpu2.service` | The user unit. Must agree with `site_config.py`; `check_site.py` enforces it. |
| `install-service.sh` | Initial GPU-stack installation: stops the legacy unit and **starts `voice-stack-gpu2.service`**. Never use for a tools-bridge-only update. |
| `voice-tools-bridge.service` | CPU bridge on 8093/8094, with a separate working directory. Reuses the resident GPU-stack models. |
| `start-tools-bridge.sh` | Systemd's CPU wrapper: `.guarded-run --label bridge-start --cpu --cpus 24-27 --nice 10`, then `speech_ui.py --asr-url … --llm-url … --tools-config config/host.toml`. |
| `check_site.py` | Offline profile check; `--compare-live` compares external input **paths**, excluding checkout-local files. `--require-assets` checks path existence on the host. Neither hashes deployed web assets. |
| `source.env.example` | Copy to `source.env` (gitignored) with the head + source fingerprint of the installed tree. |
| `campaigns/` | Per-campaign deploy/gate scripts, with their own status notes. |

## The two-file rule

`voice_stack.py` imports `site_config.py`.  Installing one without the other
produces a supervisor that cannot start, so anything that copies the supervisor
must copy the profile with it — `campaigns/qasr-20260909/deploy.py` does.

## The order a start happens in

The following describes a **GPU-stack** start. Shipping `web/` and `tools/`
uses the separate [CPU bridge sequence](../RUNBOOK.md#ship-the-current-app-to-the-cpu-bridge):
read-only preflight → isolated guarded staging → fresh bridge snapshot →
copy → rebuild the notes index → restart only `voice-tools-bridge.service` →
health/hash/manual checks. `scripts/guarded-hostrun` requires a relative
`--dest` ending in `/<campaign>`; use the fresh stage directory in the runbook,
not the existing live directory with a reused campaign ID.
It writes on the host even if its payload is a check, so it is not a read-only
inspection tool.

The existing `rollback/` on the original stack and the old ASR campaign in
`campaigns/` are GPU-stack recovery material. They are not a tools-bridge
rollback. The runbook records their observed locations and creates a separate
snapshot for the app update. The sequence was source-reviewed and inspected
read-only on 2026-09-10; no deployment or rollback was executed for that review.

```
verify pins → verify release manifests → verify runtime inventory
  → refuse if any port or the G2P socket is already held
  → g2p → qwen → tts → asr → bridge            (each waits for its own health)
  → real round trip: TTS ▸ STT ▸ chat ▸ TTS, plus Hindi and malformed controls
  → write run/ready, hand the PID to systemd
```

Nothing is launched before the binaries it will use have been hashed, and the
service is not considered up until it has spoken and been understood.
