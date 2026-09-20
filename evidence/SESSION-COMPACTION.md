# Voice sessions and compaction

Campaign: `voice-sessions-compact-20260920`. Dedicated branch/worktree based on
`43419fe`, with backend and frontend inspection/implementation lanes and a
coordinator review of concurrency, migration, deployment and evidence.

The voice app retains the independently managed Q38-27B model on GPU2 with
262,144-token context, two slots and a 32-request model queue. Kokoro and QASR
remain on GPU4. Q36 remains inactive and disabled.

Changes:
- IndexedDB archives replace automatic 60-chat/3MB eviction. No automatic chat
  deletion; storage errors retain the in-memory archive and offer JSON export.
- Atomic per-chat summary/boundary writes, revision checks that fork concurrent
  same-chat edits, and migration that retains old data until a committed write.
- Manual compaction summarizes older turns, preserves the full archive and keeps
  recent turns verbatim. Repeat compaction incorporates the prior summary.
- The bridge accepts up to 4MiB/4,001 messages, admits two simultaneous requests,
  queues 32 more with a five-minute wait limit, and cancels disconnected waiters.
- Concurrent MCP writes are serialized and response IDs remain per request.
- Generation budgets are 600 seconds. Tools are disabled during compaction;
  incomplete, empty, tool-call or reasoning output cannot become a summary.
- Source fingerprints now include the frontend and systemd unit, so their
  evidence becomes stale when those files change.

The original 121-message rejection was reproduced against the prior adapter.
Backend tests exercise real HTTP/TLS against a fake model, including admission,
queue overflow/cancellation, full history, input validation and summary output
failures. Browser checks cover more than 40 turns, 77 saved chats, reloads,
repeated compaction, cancellation, switching chats, independent tabs, concurrent
same-chat edits, export, quota failures and legacy migration. A paired sabotage
restores the old 101-message limit and must fail the large-history test.

Deployment root: `~/qwen36/voice-stack/voice-sessions-compact-20260920`.
`prepare-service.py` verifies speech assets and binds staged source.
`update-service.sh` preserves the previous unit and directory approvals, then
starts the new voice unit through its own GPU4 guard. Q38 PID and Q36 service
state are compared before/after; failed promotion restores the previous unit.

Browser histories remain local to each browser and origin. Directory grants
and external coding-agent tool sessions remain account-level. Summaries can
omit details; the original transcript is retained. The existing model still
has two active slots: more saved chats do not add GPU memory or active slots.

## Validation and deployed identity

Deployed at 2026-09-20 05:34 UTC. Source fingerprint:
`16b1cea978cdbfbd874e10bc802165e4af719b1e1f0e9fca34cb7bbc8b98b3ac`.

- 254 Python tests in 16 suites plus JavaScript contract checks passed.
- All 15 browser suites passed. The first browser attempt selected unavailable
  local CPUs 8–11 and did not execute tests. The next attempt timed out on
  WebKit native playback; an isolated diagnostic passed, followed by a complete
  clean rerun using CPUs 0–7. Failed manifests remain in this campaign.
- Restoring the old 101-message limit failed the large-history assertion as
  intended; the unmodified control passed.
- Fresh GPU4 acceptance passed with CLEAN containment. Supervisor MainPID
  `3158797`, guarded PGID `3158786`; Kokoro PID `3159320` and QASR PID `3159649`.
  Service is active/enabled with zero restarts. Q38 guard MainPID `2996026`
  (model PID `2996069`) remained unchanged; Q36 inactive/disabled.
- TLS speech round trip transcribed the fox pangram exactly, generated English
  and Hindi audio, and retained a Four→Five conversational correction.
- Live concurrent compactions retained separate `ORBIT-731` and `CEDAR-946`
  codes and the preference correction English→Hindi. Concurrent follow-up
  chats returned the exact respective code; malformed compaction was refused.
- HTTPS LAN health reports two active requests, queue limit32 and compaction
  enabled. Five MCP servers and the 240-chunk documentation index are ready.
- `scripts/guardrail-check . --profile gpu-bugfix --campaign
  voice-sessions-compact-20260920` returned GREEN.

The large full-context model's context configuration was inspected, not
rebenchmarked here. This campaign certifies app behavior and speech startup;
physical microphone/speaker acoustics still depend on the user's device.
