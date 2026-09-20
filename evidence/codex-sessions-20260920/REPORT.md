# Named Codex sessions: deployment and validation

Implemented and deployed on 2026-09-20 as `voice-codex-sessions-20260920`.
The intended workflow is a voice coordinator controlling named Codex coding
sessions while handling routine computer tasks through direct workspace tools.

## Implemented behavior

- Ten tools: create_session, list_sessions, select_session, send, status,
  steer, interrupt, answer, updates, archive_session.
- Codex App Server transport with persistent thread IDs, explicit q38f model
  identity, bounded requests, and no automatic replay of uncertain actions.
- SQLite session state and selection per browser conversation; isolated
  default directories under `/mnt/voice-workspace/sessions/`.
- Three active managed workers maximum; nested agents disabled; overlapping
  active directories rejected. Unrelated external CLI sessions are outside
  this manager's admission accounting.
- Browser session cards, explicit selection, named spoken updates, exact
  turn targeting, stale-event protection, and conversation isolation.
- Direct file/shell tools remain available without invoking a coding agent.

## Evidence

| Check | Result / evidence |
| --- | --- |
| CPU regression | 117 tests passed with `CUDA_VISIBLE_DEVICES='' make test-chat test-loop test-codex test-events`; [log](cpu-regression.log) |
| Configuration and ownership | `make check-site`: 19 site checks and two deployment ownership tests passed |
| Browser sessions | `voice_codex_sessions_browser.mjs` passed cards, selection, exact turn routing, conversation separation, event deduplication, and malicious-name rendering |
| Existing browser speech | `voice_push_browser.mjs` and `voice_audio_output_browser.mjs` passed, including nonzero element/gapless output after cached-page restoration; [review](manager-review.md) |
| Candidate real models | Nine Qwen tool-routing turns plus real Codex tasks; [acceptance](live-acceptance.json) |
| Deployed real models | Eight Qwen tool-routing turns plus a real Codex task; [acceptance](deployed-check.json) |
| Deployed browser | HTTPS page and empty session panel loaded without JavaScript errors; [result](deployed-browser.json) |
| Speech startup | Exact English transcription, nonzero English/Hindi synthesis, context correction, TLS verification; [result](deployed-speech.json) |
| GPU startup containment | PASS, clean containment; [manifest](../guarded-voice-codex-sessions-20260920-20260920T193133Z-488230.json) |

Candidate acceptance created Backend and Tests in different directories and
native threads. Both implemented and tested code. Steering Backend targeted
its active turn; interrupting Backend left Tests working. Restarting the
manager retained selection and resumed Backend's same native thread on the
next instruction. Routine file reading called `mcp__workspace__read_file`.

Deployed acceptance exercised creation, selection, sending, status, direct
reading, and archiving. Independent conversation selections stayed separate.
The coordinator independently imported
`/mnt/voice-workspace/sessions/s_b46b4d269c19/probe.py` and verified
`square(-3) == 9` and `square(0) == 0`. Archiving both probe sessions removed
them from discovery and retained their project files. No probe tasks remain
active. Candidate and deployed execution guards both reported PASS:
[candidate](../guarded-codex-sessions-20260920-20260920T192539Z-2951565.json),
[deployed](../guarded-voice-codex-deployed-check-20260920-20260920T193300Z-2982220.json).

Real tool-routing checks supplied text to the voice HTTP path; they were not
a human microphone-to-speaker listening test. Speech and browser output were
checked separately. Interactive question routing was validated with the fake
App Server, including overlapping requests and exact request/question IDs;
the live coding tasks did not require a user question. The deployed browser
check ran after cleanup, so actual card selection is covered by the browser
fixture and deployed HTTP checks rather than that empty-panel screenshot test.

## Deployment and current availability

Deployment root:
`vllm:~/qwen36/voice-stack/voice-codex-sessions-20260920`.
Startup source fingerprint:
`3ce06259100ebd912492839262aa083e378e6c9f994bfc5db477c7dd36d3f383`.
The new stack uses GPU4. Promotion left the independent Q38 PID1839 unchanged.
The previous deployment and rollback unit are retained; see the runbook.

At 19:33:39 UTC, after deployed acceptance completed, systemd logged an
explicit stop of the independent `q38-server.service`. A separate campaign,
`q3827-decode-density-gpu2-20260920`, was using GPU2. Subsequent inspection
found Q38 failed and port 8080 absent; the new `voice-stack-gpu4.service`
remained active (MainPID488306), with its tools bridge on ports 8093/8094 and
speech services on 8090/8095. An experimental Q38 server was instead listening
on its own port 18192. We did not stop that experiment or restart Q38 into its
GPU allocation. Current voice-model availability is therefore blocked by
independent host activity, despite the completed passing acceptance.

Browser reload preserves ongoing work. Restarting the manager interrupts
active tasks; the next send resumes saved history, not the old running
process. These tools manage sessions they create and do not attach to arbitrary
already-running terminal sessions. Workspace paths are defaults, not a security
sandbox; tools retain the project account's permissions.
