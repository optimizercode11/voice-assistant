# Voice stability and Claude stop

Campaign `voice-stop-turns-20260920`, dedicated worktree based on39f6466.
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
  minimum120ms voice duration matches the recorder's acceptance threshold.
- Default auto-sleep0 with migration from the previous default5; explicit
  sleep settings remain configurable. Resume restores suspended AudioContext.
- Blocked native/gapless playback exposes recovery, failed requests retain the
  paused UI, and plain-JSON fallback consumes its response instead of resending.
- Black dark-theme background and separate working indicators for both agents.

The first full regression caught shared Claude tool schemas leaking stop into
Codex's advertisement; Codex now selects only methods it implements. The next
run found an echo-test source assertion tied to RAF scheduling; the assertion
now requires timer rescheduling while preserving the same350ms settle window.
The echo waveform simulations and rejection thresholds are unchanged.

Acceptance results and source identity will be appended after validation.
