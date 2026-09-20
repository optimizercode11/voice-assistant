# Home-directory answer grounding

User report: "What's your home directory?" received "/home/voice".
The reported reply is not an actual environment value. A fresh reproduction
instead answered "/mnt/voice-workspace" and made no tool calls, demonstrating
the same missing verification and confusion between HOME and cwd. No claim
is made that the fresh reproduction repeated the exact reported string.

Independent workspace MCP inspection returned:

- HOME: `/home/ambudsharma`
- cwd: `/mnt/voice-workspace`
- account: `ambudsharma`
- host: `codex`

The correction adds a generic system instruction to verify execution-environment
facts through the relevant host's tools, plus a host-specific instruction
mapping home/cwd/account questions to the direct workspace shell. Earlier
assistant statements are explicitly not evidence. No environment question is
hardcoded to a local username in the response implementation.

Both candidate and deployed checks passed six cases: three fresh identical
home questions, cwd, account, and an existing chat containing the incorrect
assistant claim. Every case included a successful `mcp__workspace__run_shell`
call and the independently measured expected value. No coding-agent tool was
called. Raw records: `HOME-GROUNDING-BEFORE.json`,
`HOME-GROUNDING-CANDIDATE.json`, and `HOME-GROUNDING-LIVE.json`.

The first candidate harness invocation used the adapter's arguments incorrectly
and failed; it did not change the live service. The harness was corrected and
the passing rerun retained alongside that failed evidence.

Deployed 2026-09-20 at 17:36 UTC from
`~/qwen36/voice-stack/voice-home-grounding-20260920`.
Source fingerprint:
`5543c553463b5455ab4a17c47856e3856462faf1cff9b87d4ca07fd5b06742fc`.
Voice supervisor MainPID177991; Q38 MainPID1839 unchanged. Fresh GPU4 guard
PASS/CLEAN: Kokoro PID178370 and QASR PID178715 belonged to PGID177980.
English/Hindi speech, ASR, TLS and conversation-correction startup checks passed.
The 25 registry/config, 7 chat and 9 compaction CPU tests passed.

This change improves tool use through instructions. It is not an application
gate that can guarantee every factual statement is supported, and these
passing checks are evidence for the tested prompts rather than a guarantee
against future hallucinations.
