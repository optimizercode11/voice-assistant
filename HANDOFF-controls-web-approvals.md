# Handoff: pause-listening control, a web-browsing MCP server, and making the file-permission request actually happen

Status: **shipped 2026-09-11** as campaign `voice-controls-20260911-a` from
head e64e54d; the live measurements are in `PROVENANCE.md` and the executed
steps in `RUNBOOK.md`. Kept as the record of what was measured before and
after. Two gaps remain open and are NOT fixed here: unprompted, the model still
answers a fact it feels sure of without checking (Saif Ali Khan's parents,
wrong again, no tool called -- `HANDOFF-wikipedia-mcp.md` Finding 2); and asked
for the *parent* of its configured root it lists the root it has instead of
asking for the parent.

## What was measured on the live bridge (:8094, 2026-09-11, before any change)

Probe: `probe_tools.py` (three prompts through `/chat/completions`, NDJSON).

| Prompt | Tools called | What came back |
|---|---|---|
| "Hang on, I need to take a phone call. Stop listening for a bit." | none | "I'll pause listening. Let me know when you're ready to resume." **The page kept listening.** |
| "Please have a look at the folder /home/…/voice-stack and tell me what's in it." | `mcp__files__roots` | Round 2 (last, no tools offered) returned the literal text `<tool_call><function=mcp__files__read>…` and the bridge **spoke it**. `request_directory` was never called. `/approvals` stayed empty. |
| "Who are Saif Ali Khan's parents?" | none | Correct this run (Sharmila Tagore, Mansoor Ali Khan). |

Search front-ends reachable from the host with a browser User-Agent (`probe_search.sh`):
`html.duckduckgo.com` 200 with results, `lite.duckduckgo.com` 200 with results,
Wikipedia opensearch 200. Bing/Brave/Mojeek answer 200 but serve a challenge page.

## Deliverables

1. **`pause_listening` tool** (builtin, `[builtins] pause_listening = true`). The
   tool result carries `meta.control = {"pause_listening": true}`; `turn()`
   aggregates controls into the answer (`controls`), the bridge forwards them,
   and the page enters a `paused` phase after the reply instead of `listen()`.
   Resuming is a **human action only** (Resume listening button / Space while
   paused). There is deliberately no `resume_listening` tool: a model that can
   re-open the microphone defeats the reason the user paused it.
2. **`tools/mcp_web.py`**: stdlib-only stdio MCP server, `search` + `read_page`.
   GET only, DNS pinned to the address that passed the private-range check,
   allow-list re-checked at every redirect hop, text-only, byte/char caps,
   per-host rate limit, untrusted-content framing. `tests/mcp_web_test.py` with
   a sabotage arm (drop the per-hop redirect check → the redirect-into-loopback
   test must FAIL).
3. **File-permission request actually reachable**: `_speakable` refuses
   `<tool_call` text (the leak above); a capability manifest in the system
   prompt names each tool's purpose; the file server's refusals point at
   `request_directory`; `rounds` raised so roots → request → answer fits.

## Ownership while in flight
- main session: `tools/agent_tools.py`, `tools/voice_chat.py`, `tools/speech_ui.py`,
  `tools/mcp_files.py`, `tools/agent_config.py`, `web/chat.js`, `web/chat.html`,
  `config/*.toml`, `Makefile`, `TOOLS.md`, `README.md`, tests other than the web suite.
- fork: `tools/mcp_web.py`, `tests/mcp_web_test.py`, `tests/fixtures/web/*` only.
- Nobody commits but the main session.

## Progress log
- 2026-09-11 09:20 measured the three gaps above; DDG html works from the host.
- 09:45 server side landed: `pause_listening` builtin + `control` on tool rows,
  `controls` on the answer (NDJSON and plain JSON), `<tool_call` gate in
  `_speakable`, `Registry.manifest()` in the system prompt, file-server scope
  refusals point at `request_directory` (only with a grants file),
  MAX_TOOL_ROUNDS 4, host rounds 3, host.toml: `pause_listening = true`,
  `[fetch] enabled = false`, `web` MCP server, `purpose` on every server.
  Suites: agent_tools 22, mcp_files 30, tool_loop 19 — all green.
- 10:05 page landed: `paused` state, `holdListening()` enforced inside
  `listen()`, Resume button + Space, typed turns return to Paused.
  `voice_pause_browser.mjs` green (real looping fake mic: 0 uploads over
  16.25 s while paused; resume → next automatic turn); its sabotage arm
  (hold removed) fails on the third upload, as designed.
- 10:12 fork delivered `tools/mcp_web.py` + `tests/mcp_web_test.py` (30 tests,
  ~1 s; sabotage arm: exactly `test_redirect_into_private_range_is_refused`
  red). Live from the host: `search("ta ra rum pum film")` → ddg, 828 ms,
  first hit en.wikipedia.org/wiki/Ta_Ra_Rum_Pum; `read_page` of that article
  169–260 ms, opens on the article text after nav/header/footer muting.
  Main session tightened `_is_public_address` with `is_global`.
  `voicectl doctor --probe` on host.toml: 12 tools, stack/web/files all up.
- 10:30 `make check` PASS (19 checks, 0 failures; 10 Python suites + node
  tests + check-site). `make sabotage` exit 0: every arm "correctly refused",
  including the two new ones (`mcp_web_test.py --sabotage`,
  `voice_pause_browser.mjs --sabotage`). `make test-browser` running.
- 10:40 `make test-browser` 11 suites green (chromium + webkit). Committed e64e54d.
- 09:22-09:24 UTC (host clock) shipped as `voice-controls-20260911-a`: stage
  PASS; first install aborted on its own `is-active` gate while a concurrent
  campaign restarted the GPU stack (nothing touched); install-2 PASS; restart
  PASS. Bridge PID 103298, 12 tools. Live probes: pause control delivered,
  folder request filed + declined, web search correct; parents question still
  unchecked (open), parent-of-root near-miss (open).
- 09:36-09:47 UTC user report "Something went wrong" / "TTS is down": TTS was
  up (109 KB WAV in 60 ms through the bridge; Chromium played it); the real
  failure was the round budget (3) on a multi-step folder question -> 502
  "ran out of room" -> page ended the session. Hotfix 2097863 shipped as
  `voice-controls-20260911-b` (bridge PID 112567): last-round note to the
  model, rounds 5, page keeps the session on a bridge refusal, "Stop." no
  longer pauses. Both re-measured live: pass.
- DONE. Superseded NEXT (kept for the record): ship per
  RUNBOOK §"Ship the current app to the CPU bridge" (restart only
  voice-tools-bridge.service); re-run `probe_tools.py` on :8094 and record
  whether the model now calls `pause_listening` / `request_directory` /
  `mcp__web__search`.
