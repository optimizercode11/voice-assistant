# Four-second idle pause (2026-09-20)

Campaign: `voice-idle4-20260920`.
Source fingerprint: `4a1d7d307cd6e4513bd349a3211147bd717108e2af3fb0144e91b0b4907e479e`.

The idle timeout now defaults to four seconds. Browsers with previous default
values (zero or five seconds) migrate once; other custom timeouts survive, and
new explicit choices including zero persist. Existing held-speech and resume
behavior is unchanged.

CPU-only Chromium verification, using guarded-run on local CPUs 0–3 and 4–7:
- Reproduce: the default-four assertion failed on the previous zero default.
- Positive: the actual silent microphone pauses at the default timeout, disables
  tracks without an upload, and resumes after agent updates, Space, and typed
  replies. Zero still keeps listening. The timing assertion allows scheduling
  overhead and checks that it does not pause before three seconds.
- Negative: removing microphone auto-pause fails the same behavioral test.
- Regression: existing speech-recovery browser suite passes, including migration
  from old zero/five defaults, custom three seconds, and persistence of new
  explicit zero/five values.

`scripts/guardrail-check . --profile bugfix --campaign voice-idle4-20260920`
returned GREEN. Corresponding guarded manifests and logs are retained here.

Local source only: deployment was held because the owner reported shutting down
`vllm`. No remote commands, service restarts, or GPU execution were performed.
The live profile remains `voice-stop-turns-20260920` until a later deployment.
