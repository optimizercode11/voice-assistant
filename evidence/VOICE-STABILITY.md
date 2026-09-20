# Voice stability and Claude stop

Campaign `voice-stop-turns-20260920`, dedicated worktree based on 39f6466.
User requested explicit Claude stop, reliable speech endpoint/recovery and
black background. Source ownership split across Claude MCP, browser and root
semantic/pause/deployment lanes; an independent read-only review reproduced
playback and duplicate-POST failures against a frozen source copy.

Reproduction: prior semantic judge classifies `42` as filler-only and holds it
until further speech. Browser review reproduces blocked nonfinal autoplay
hanging without retry and JSON fallback repeating the original POST. All
historical reproduction files are kept; prior source is preserved separately.

Deployment remains `voice-stack-gpu4.service`, now from
`~/qwen36/voice-stack/voice-stop-turns-20260920`. The update preserves approvals
and the previous voice unit, validates GPU4 ownership and compares Q38 PID and
Q36 service state before/after. Claude's forced SSH entrypoint stays at the
local repo path; only reviewed source is integrated, with unrelated dirty files
preserved. No real Claude session is started by the test suite.

Implementation includes:
- Managed Claude stop, two-second graceful shutdown before bounded escalation,
  clearing queued work, fresh start only on send, stale result suppression and
  PID start-time checks. Unrelated sessions are not matched globally.
- Explicit current-user intent before pause_listening may close the microphone.
  Stopping Claude Code is separate from stopping microphone capture.
- Numeric and non-Latin answers use the acoustic endpoint; English fragments
  remain preserved, and pure filler is discarded without losing carried words.
- Held text flushes after 2.2s additional quiet and Send now sends held text.
- Acoustic sampling uses a 25ms timer independently of animation frames; the
  minimum 120ms voice duration matches the recorder's acceptance threshold.
- Default auto-sleep 0 with migration from the previous default 5; explicit
  sleep settings remain configurable. Resume restores suspended AudioContext.
- Blocked native/gapless playback exposes recovery, failed requests retain the
  paused UI, and plain-JSON fallback consumes its response instead of resending.
- Black dark-theme background and separate working indicators for both agents.

The first full regression caught shared Claude tool schemas leaking stop into
Codex's advertisement; Codex now selects only methods it implements. The next
run found an echo-test source assertion tied to RAF scheduling; the assertion
now requires timer rescheduling while preserving the same 350ms settle window.
The echo waveform simulations and rejection thresholds are unchanged.

## Accepted deployment

Source commit `e24198e`; source fingerprint
`55aeaa1fefacad891bbe71217a1871e2e2b709730da4893e4c2165f72dd44807`.
Integrated into `/mnt/voice-assistant` main by fast-forward; the unrelated
`deploy/voice-tools-bridge.service` edit retained exact SHA256
`ec28665dcaa86f53aaf349a5e4356079fe0a2460a2fd421a10a803ebe1170c76`.

- 262 Python tests in 17 suites, JavaScript echo/barge/speech contracts and
  all 16 browser suites passed on the accepted source. Chromium and WebKit
  cover native playback, controls and language/STT paths; focused new recovery
  cases run in Chromium with deterministic audio/state fault injection.
- Both semantic rollback and missing-held-flush sabotage failed at the intended
  assertions while their controls passed. Failed earlier integration runs are
  retained in this campaign.
- Visual inspection and CSS assertion confirm a black default background.
- Deployed at 2026-09-20 06:02 UTC. Voice service active/enabled, zero restarts,
  MainPID3238379 and guarded PGID3238372. Fresh GPU4 acceptance CLEAN.
  Q38 guard MainPID2996026 stayed running; Q36 inactive/disabled.
- Fresh speech round trip transcribed the fox pangram exactly, produced English
  and Hindi audio, and preserved a Four→Five correction.
- The live stop request called `mcp__claude__stop` successfully in81ms, returned
  “Done. Claude Code is stopped. The microphone stays on.” and no pause control.
  Claude MCP version1.1 is ready with the stop tool; Codex advertises only its
  implemented methods. No real Claude Code instance was started by validation.
- Live Hindi audio transcribed to “मुझे समय बताओ।” and returned complete=true,
  hold=false and discard=false. The browser receives an immediate completion
  decision for this utterance.
- LAN HTTPS page and script bytes match the accepted local files exactly.
- `scripts/guardrail-check . --profile gpu-bugfix --campaign
  voice-stop-turns-20260920` returned GREEN.

Browser audio fixtures and fault injection verify state recovery; physical
microphone, room noise and device-specific browser suspension still need
observation on the user's device. Claude stop owns its session group and
identifiable descendants; it deliberately does not globally search for
previously reparented daemon processes outside that group.
