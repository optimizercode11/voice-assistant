# Tests

Two kinds, and the difference matters: the Python suites test the **bridge**,
the browser suites test the **page**.  Neither runs a model.

## Bridge contracts (CPU, no browser)

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py     # 11 tests   bridge: ASR, uploads, TLS
CUDA_VISIBLE_DEVICES="" python3 tests/voice_chat_test.py    #  5 tests   adapter: prompt, roles, fail-closed
CUDA_VISIBLE_DEVICES="" python3 tests/agent_tools_test.py   # 20 tests   registry, validator, config
CUDA_VISIBLE_DEVICES="" python3 tests/retrieval_test.py     # 11 tests   FTS5 index, staleness, misses
CUDA_VISIBLE_DEVICES="" python3 tests/mcp_test.py           # 12 tests   real stdio MCP peer
CUDA_VISIBLE_DEVICES="" python3 tests/tool_loop_test.py     # 12 tests   the loop, over a real TLS bridge
```

`mcp_test.py` is run under `-W error::ResourceWarning`: an MCP server is
spawned and reaped many times, and a leaked pipe per restart is a real defect
that only shows up as an fd count.  (`Popen.close()` does not exist in this
Python, so the client closes the streams by hand.)

`tool_loop_test.py` scripts the upstream rather than mocking it, because the
thing under test is a *sequence*: the model asks, the bridge executes, the
model is answered.  The interesting failures are all about what the second
request looks like — whether the `tool_call_id` was echoed, whether the last
round still has `tools`, whether a browser-sent `role:"tool"` got through.

`mcp_test.py` talks to `tests/fixtures/fake_mcp_server.py`, a real newline-
delimited JSON-RPC peer with modes for hanging, crash-looping, replying with a
JSON-RPC error, and — the one that matters — answering `tools/list` differently
the second time.

`BridgeStartTests` inside `tool_loop_test.py` is the only suite that runs
`tools/speech_ui.py` as a **subprocess**, the way the deployment does.  It
proves `--tools-config` reaches the registry, that a config with `fetch` enabled
and no allow-list exits 2 rather than starting anyway, and that SIGTERM exits
**0** — because the shutdown path is what reaps the MCP child, and the test
reads that child's pid from `/tools` and insists it is gone afterwards.  A
bridge that dies by signal leaves someone else's server process alive with a
pipe to a dead parent, which is exactly what a `systemctl restart` would do.

They refuse to run unless `CUDA_VISIBLE_DEVICES` is empty — a CPU oracle that
quietly initialized a GPU is a lie about containment, not a faster test.  They
need a real `ffmpeg` and `openssl` on PATH, and they run the real
`tools/speech_ui.py` against fake ASR/Qwen processes, so what is under test is
the bridge's actual error mapping and argument rules.

Paired sabotage, which must FAIL:

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py --speaker-sabotage   # must FAIL
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py --negative            # control, must PASS
CUDA_VISIBLE_DEVICES="" python3 tests/tool_loop_test.py --sabotage            # must FAIL
CUDA_VISIBLE_DEVICES="" python3 tests/mcp_test.py        --sabotage            # must FAIL
```

The two new ones each break exactly one assertion, and each corresponds to a
claim in `TOOLS.md` that would be quiet to lose:

* `tool_loop_test.py --sabotage` makes `parse_messages` trust the role the
  browser sent.  `test_client_cannot_inject_a_tool_message` must then fail —
  a tab that can write `{"role":"tool"}` can author what the model says.
* `mcp_test.py --sabotage` re-lists a server's tools before every call, the
  obvious-looking "keep it fresh" change.  `test_the_tool_list_is_captured_once`
  must then fail, because a server that answers `tools/list` differently the
  second time can name a tool no operator ever allow-listed.

`make sabotage` runs the failing arms and reports a sabotage that passes
as the failure it is.

## Page contracts (Playwright)

```bash
export NODE=/path/to/node20+                       # Playwright needs Node >= 20
export PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_chat_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_carry_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_controls_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_language_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/stt_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_history_browser.mjs
```

Each starts its own local server that serves the real `web/` files and stubs
the endpoints, so the page's own recorder loop, VAD thresholds, playback and
interruption code is what runs.  Chromium gets `tests/fixtures/capture.wav` as
a fake microphone device; webkit has no MediaRecorder in the Linux test build,
so those runs cover typed input and **make no physical-Safari-microphone
claim**.  Screenshots land in `evidence/browser/`.

`voice_carry_browser.mjs` is the suite that reproduces a *reported* failure end
to end.  Chromium's fake microphone is fed
`tests/fixtures/capture-two-part.wav` -- real speech, a 1.8 s pause, real speech
-- so the page's own endpointer really does cut one sentence into two clips.
The `/stt` stub answers the first clip with `"I."` and builds the turn verdict by
shelling out to the real `tools/turn_control.py`, so this suite cannot pass while
the server's judgement is broken; a JavaScript copy of those rules would have
kept passing through the bug it is testing for.  The assertion is the one the
user asked for: the model receives `"I want to go to the museum."` as one turn.

`voice_history_browser.mjs` is the suite for the page's other kind of memory:
the bridge is stateless, so the transcript the browser holds *is* the model's
context.  It reloads a real page and then reads the `/chat/completions` body,
which is the only way to tell "the bubbles came back" apart from "the model got
its context back" — those are different claims and the first one is nearly
free to fake.  It also seeds storage by hand to prove a forged record cannot
author an assistant turn, and opens a second tab to prove a *New chat* over
there is not resurrected by a reply completing here.

`--sabotage` variants exist for the chat, controls, tools, carry and history
suites: they disable one handler (the Space key, automatic language choice, tool
progress, the carry-forward, or the write-through of a committed turn) and must
fail exactly there.  `--live` points the same assertions at the deployed page.

`page_harness.mjs` is different again: it runs the studio page's script against
a live `kserver` with a fake WebAudio that records what the page actually
scheduled.  Point it at a running TTS (`node tests/browser/page_harness.mjs
http://127.0.0.1:8090`, or through `ssh -N -L 8090:127.0.0.1:8090 vllm`).

Browser suites are timing-sensitive: run them one at a time.  Two concurrent
runs on a busy host will produce a spurious `waitForFunction` timeout, which
looks exactly like a real regression and is neither.
