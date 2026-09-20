# Named Codex session manager review

Review scope: `tools/codex_sessions.py`, `tools/mcp_codex_sessions.py`, transport interactions in `tools/codex_app_server.py`, bridge result limits, and browser focus handling. This lane made no model calls and no remote calls. The manager was changing during review; findings below distinguish observed behavior from proposed/follow-up fixes.

## Findings and disposition

1. **Selection response integration mismatch — fixed in browser.** The real manager returns `{session,selected_session_id,context_id}` for selection; initial browser code expected a complete `sessions` array and hid its panel after a selection. Browser selection now merges the singular snapshot into existing cards and refreshes authoritative discovery. The browser fixture now returns the actual singular shape, and its regression passed.

2. **Compound startup timeout exceeded the outer deadline — coordinator fixed during review.** Initial cold send independently allowed 8 seconds each for initialization, thread start/resume, and turn start, while the registry allowed 10 seconds total. The coordinator changed send to a shared 8-second deadline passed through these requests. Admission and per-session state transitions remain serialized under the manager lock. An explicit start rejection can be marked failed; uncertain transport outcomes must remain unknown and must not be automatically resent.

3. **Large discovery results could become invalid JSON — coordinator/bridge follow-up.** Registry clips tool result text at 6000 characters. Initial list returned 10 full snapshots, including up to 4000 instruction characters each; the HTTP route subsequently JSON-decoded the clipped string. Coordinator now clips summary fields and budgets discovery pages to 4400 characters, with `next_offset`. Bridge lane implemented page collection (maximum 32 sessions, one shared 10-second deadline); pagination and malformed-pagination route tests passed. Selected sessions beyond the first page remain in the aggregate discovery response.

4. **Completion report truncation consumed unseen reports — reported to coordinator.** Initial updates returned five reports of up to 4500 detail characters each, then advanced the cursor for all five before registry clipping. Recommended bounded complete-JSON reports with cursor advancement only for returned records. A continuation signal allows explicit follow-up without silently dropping history.

5. **Concurrent input requests lost visible questions — reproduced and reported to coordinator.** Inject two valid `item/tool/requestUserInput` requests for one active turn, then answer the first exact request ID. Initial behavior produced `state=working`, `questions=[]`, while one pending request remained. Either reject overlapping requests explicitly or preserve every pending request and remain `waiting_input` until resolved. Question IDs must remain exact identities: reject duplicate/oversized IDs instead of truncating them.

## Focused reproduction

Final coordinator disposition: all five findings were resolved before
deployment. Discovery and update responses now fit the registry's complete
JSON budget, and update cursors advance only over returned events. Pending
questions retain their exact request/question identities and remain visible
until answered. Regressions
`test_large_update_pages_do_not_consume_unreturned_reports` and
`test_answering_one_request_preserves_other_pending_questions` both passed
in the final 117-test CPU run. The shared startup deadline and singular browser
selection fix are also present in the deployed source. The reproduction below
records the original defect, not current behavior.

A temporary SQLite manager, fake in-memory backend (`respond` no-op), and direct validated-shaped callbacks were used; no Codex subprocess or model was involved. Two questions were `question1` and `question2` in the same `thread1`/`turn1`. Answering request one yielded:

```json
{"scenario":"two unanswered requests in same turn; answer first","state":"working","visible_questions":[],"pending_requests":1}
```

## Confirmed design boundaries

- Three active **managed workers** are admitted, reserving one coordinating slot. Active states include uncertain and waiting-input states, so uncertainty does not silently free capacity. Thread configuration disables nested multi-agent execution and limits agent threads to one. The manager does **not** enumerate or control unrelated external Codex/Claude CLI processes, so a claim of a machine-wide four-session ceiling would be inaccurate without a shared admission authority.
- Exact thread and turn IDs gate incoming completion/activity notifications. Stale notifications cannot complete the next turn. Pending answers are checked against both the session and current turn.
- One process holds an exclusive state-directory lock. SQLite persists session identity, thread IDs, selection by conversation, events and update cursors. Restart marks previously active work interrupted and resumes its saved Codex thread on the next send; this is interrupted-work recovery, not transparent continuation of a live task.
- Work in overlapping project directories is refused among active managed sessions. This protects ordinary same-directory edits; it is not isolation against arbitrary shell actions or unrelated external editors.
- Browser focus is changed through manager selection only. Background working/trace/update events do not select a session. Cards refresh from the manager on session events. Explicit foreign-conversation updates are excluded from transcript/speech, and queued announcements are dropped when switching conversations.
- Browser pending messages are keyed by session ID and turn ID; a late completion cannot clear another session or a newer turn's pending message. Event IDs deduplicate repeated speech within the live page.

## Validation performed

All browser runs used `CUDA_VISIBLE_DEVICES=''`, Chromium `--disable-gpu`, and:

```sh
PLAYWRIGHT=/tmp/fork-stability-browser-zu50jkow/node_modules/playwright/index.mjs \
/tmp/fork-stability-browser-zu50jkow/node_modules/node/bin/node tests/browser/voice_codex_sessions_browser.mjs
```

Passed after the singular-selection-response fix: named cards, selection/context routing, two same-provider sessions, stale-turn pending isolation, attributed single speech, no focus stealing, reload/context separation, dropped old-context queued announcements, literal malicious names, and disabled/missing endpoint fallback.

Earlier in this implementation lane, `voice_push_browser.mjs` passed legacy Claude/Codex push, queued speech, history, and pause behavior. `voice_audio_output_browser.mjs` passed nonzero element and gapless output before/after cached-page restoration (measured peak RMS about 0.18–0.19). Existing audio fix remained intact. `git diff --check` passed for the UI changes.
