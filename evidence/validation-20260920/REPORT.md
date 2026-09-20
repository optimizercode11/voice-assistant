# Voice assistant validation — 2026-09-20

Fresh rerun at 17:41–17:43 UTC: all validation groups passed.
Tested source fingerprint:
`5543c553463b5455ab4a17c47856e3856462faf1cff9b87d4ca07fd5b06742fc`.
No application source changes, service restart, model launch or deployment
were performed. Live requests used the already-running services.

## CPU tests

170 unit/integration tests passed, plus 19 deployment configuration checks.

| Group | Tests |
|---|---:|
| Workspace MCP | 22 |
| Read-only files MCP | 30 |
| Voice chat | 7 |
| Compaction | 9 |
| HTTP/TLS tool loop | 23 |
| Tool registry/configuration | 25 |
| MCP transport and concurrency | 14 |
| Directory approvals | 38 |
| Deployment process ownership | 2 |

Logs and command results are in `workspace-cpu.log`, `bridge-cpu.log`,
`registry-cpu.log`, and their corresponding `*-summary.json` files.
Two non-failing warnings remain: the existing invalid `\s` escape at
`tests/mcp_files_test.py:179`, and an unclosed subprocess-output file in the
broken-config bridge startup test. Neither warning is counted as a clean,
warning-free run; all test assertions passed.

## Deployed assistant

- Six environment cases passed: three fresh home-directory questions, cwd,
  account, and a chat containing the prior `/home/voice` claim. Each used
  a successful direct shell call and returned the independently verified value.
  HOME is `/home/ambudsharma`; cwd is `/mnt/voice-workspace`; user is
  `ambudsharma`. See `home-live.json`.
- Three live requests exercised directory creation, file writing, listing,
  reading, and shell execution. All five workspace tools succeeded without
  Claude/Codex calls. The created file's actual bytes were checked on codex,
  independently of the model's answer. See `workspace-live.json` and
  `file-verification.json`.
- Fresh English speech→ASR transcription matched the fox pangram. English and
  Hindi synthesis, Four→Five conversational correction, TLS verification,
  and malformed-input controls passed. See `speech-live.json`.
- Five key deployed source/config files match the tested local bytes. Voice
  MainPID177991 and Q38 MainPID1839 remained unchanged with zero restarts;
  Q36 remained inactive. See `deployment-check.json`.

## Simulated unavailable tool

Two additional cases used the real resident model and the current adapter/prompt,
with an isolated registry whose workspace shell always returns an unavailable
error. This did not disable or modify the production workspace connection.
Both a fresh chat and one containing the wrong earlier path attempted the tool,
received the simulated failure, and answered that the home directory could not
be verified. Neither invented a path. Responses were inspected directly;
`unavailable-tool.json` preserves the exact results and error metadata.

The guarded live and failure-injection run records are:

- `../guarded-voice-validation-20260920-20260920T174149Z-2567918.json`
- `../guarded-voice-validation-20260920-20260920T174300Z-2572438.json`

These results establish behavior for the tested cases, not a guarantee that
prompt-level instructions eliminate every future hallucination. Physical
microphone/speaker acoustics on a user's device were not tested.
