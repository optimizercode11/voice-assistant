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
| `install-service.sh` | Install/enable the unit, with a rollback that restores the unit it replaces. |
| `check_site.py` | Does the profile still describe one real deployment? `--compare-live` and `--require-assets` on the host. |
| `source.env.example` | Copy to `source.env` (gitignored) with the head + source fingerprint of the installed tree. |
| `campaigns/` | Per-campaign deploy/gate scripts, with their own status notes. |

## The two-file rule

`voice_stack.py` imports `site_config.py`.  Installing one without the other
produces a supervisor that cannot start, so anything that copies the supervisor
must copy the profile with it — `campaigns/qasr-20260909/deploy.py` does.

## The order a start happens in

```
verify pins → verify release manifests → verify runtime inventory
  → refuse if any port or the G2P socket is already held
  → g2p → qwen → tts → asr → bridge            (each waits for its own health)
  → real round trip: TTS ▸ STT ▸ chat ▸ TTS, plus Hindi and malformed controls
  → write run/ready, hand the PID to systemd
```

Nothing is launched before the binaries it will use have been hashed, and the
service is not considered up until it has spoken and been understood.
