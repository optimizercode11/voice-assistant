# Architecture

## The one diagram that matters

```
browser (web/chat.html + chat.js)
   │  getUserMedia → MediaRecorder → webm/opus blob
   │  amplitude + silence window decides "I've finished speaking"
   ▼
speech bridge  tools/speech_ui.py            HTTP :8091   HTTPS :8092 (microphone needs TLS)
   ├── POST /stt              → qasr_serve :8095        (Qwen3-ASR-0.6B, resident)
   ├── POST /chat/completions → q38_27_server :8080     (Qwen3.8-27B NVFP4)
   ├── POST /tts, GET /languages|/voices|/stats|/health → kserver :8090 (Kokoro-82M)
   ├── GET  /tools, /chat/health         → what this bridge will let the model do
   └── GET  /chat, /chat.js, /           → the pages themselves

        inside one POST /chat/completions, if tools are configured:
        Qwen ⇄ tools  ──┬── retrieval.py   SQLite FTS5 (BM25) over your own files
                        ├── mcp_client.py  stdio JSON-RPC to servers you named
                        └── agent_tools.py now / fetch_url / the validator
        …up to limits.rounds generations, then one speakable answer.
```

The original service, `voice-stack-gpu2.service`, starts five processes in
dependency order through `deploy/voice_stack.py`:

| # | Process | Why it exists |
|---|---|---|
| 1 | `g2p_sidecar.py` | CPU multilingual phonemization over a unix socket. The TTS engine takes phonemes; G2P is a separate lane so it never holds a GPU lock. |
| 2 | `q38_27_server` | ctx 16384, one slot, Q8 KV, MTP off, 8 GiB host state cache. |
| 3 | `kserver` | Kokoro TTS with continuous batching and segment-level streaming. |
| 4 | `qasr_serve` | Holds the ASR checkpoint resident, so a request costs the engine's own ~40–100 ms instead of a process spawn plus a model load. |
| 5 | `speech_ui.py` | The only thing the browser talks to. |

The separately managed `voice-tools-bridge.service` runs another CPU bridge
on HTTP 8093 / HTTPS 8094 from `voice-tools-20260910`. It reuses the same
resident model endpoints and loads `config/host.toml`. Its wrapper is
`deploy/start-tools-bridge.sh`, guarded with `--cpu --cpus 24-27 --nice 10`.
An app update targets this service; it does not require a GPU-stack restart.

## The tool loop lives on the server, and that is a security boundary

A tool result is an assertion about the world that the model must believe.  If
the browser could supply one, any page able to drive this endpoint could make
Qwen state anything it liked out loud — a prompt injection with a Content-Type.
So `voice_chat.turn()` runs the entire loop inside a single request: the page
sends only `user`/`assistant` text exactly as before, and receives *progress*
events plus one answer.  Progress is display data and is never fed back.

`tools/agent_tools.py` is the single choke point.  A builtin, a retrieval query
and an MCP tool all reach the model as the same OpenAI tool object and all come
back through the same `execute()`, so no path gets to skip the JSON-Schema
validator, the per-call deadline, or the result-size clip.  Failures are
returned to the model as `"tool error: …"` rather than raised as HTTP 500s — a
model that passed `{"zone": 3}` should be told in one sentence and allowed to
correct itself, not lose the generation that just cost the most.

MCP deserves the suspicion: an MCP server is someone else's program that names
its own tools.  The list is captured once at startup and never re-read mid-turn,
`allow`/`deny` are applied before the model sees a name, names are folded into
`mcp__<server>__<tool>` in the engine's own grammar, the child inherits six
environment variables and nothing else, and a wedged server is SIGTERM'd before
it is killed.  `TOOLS.md` has the full table.

## Why one origin

The bridge is the only origin the browser sees.  That is not tidiness: the
model ports have no authentication, and a page that could reach `:8080`
directly is a page where any other site on the LAN can drive a 27B model.  The
bridge enforces same-origin on every POST, and microphone capture additionally
requires the TLS listener, because browsers refuse `getUserMedia` on an
insecure origin.

## Contracts the bridge owns (and the engines do not)

`/chat/completions`
* The **system prompt is server-owned** (`tools/voice_chat.py`). Client messages
  are appended after it and cannot replace it — a browser tab cannot redefine
  the assistant's behaviour.
* Messages must alternate `user`/`assistant`, end with `user`, be non-empty,
  ≤ 8000 characters each, ≤ 101 messages. Anything else is a 400, not a
  silently truncated conversation.
* `reasoning_effort: none`, `max_tokens: 384`, one reply ≤ 1 MiB, 65 s deadline.
  A truncated reply, a reply containing `think` tags, or an empty reply is an
  error — the assistant refuses to speak half a sentence.
* One generation at a time (`chat_lock`); a second request gets 503 with a
  reason rather than queueing into a timeout.
* Cancellation closes the upstream socket, so **Interrupt** actually stops the
  turn instead of paying for a reply nobody will hear.

`/stt`
* ≤ 20 MiB and ≤ 120 s, decoded by ffmpeg to 24 kHz mono s16 **before** the
  engine sees it.  The browser's webm/opus is not something any ASR frontend
  reads, and the certified path is decode-then-resample exactly as the oracle
  did it.
* One transcription in flight (`asr_lock`, shared between the HTTP and HTTPS
  listeners): the engine has one arena.
* A dead or wedged engine is a 503 with a name.  It is never a 200 with an
  empty transcript — that is the failure mode this bridge is most careful
  about, because the chatbot would answer silence with confidence.
* Speaker headers and `[Silence]` markers are stripped for speech; `raw_text`
  is returned unchanged so the UI can show what was actually heard.

`/tts`
* The bridge forwards chunked responses. The current `/chat` page waits for
  the full audio blob before playback (`web/chat.js::runTurn`, `response.blob()`),
  so that page does not obtain first-segment streaming latency.

## Turn-taking and barge-in

`web/chat.js::listen()` records a clip until roughly 1 s of silence after
speech, or the literal `20000` ms ceiling. **Send now** bypasses the speech
threshold and the fragment hold. The bridge returns a completeness judgment
from `tools/turn_control.py`; the page retains unfinished words in `heldText`
and combines them with a later clip. This remains energy-and-pause detection,
not a neural VAD.

The browser's echo canceller removes speaker return from microphone capture.
AEC3 is the libwebrtc implementation: it processes capture against a render
reference. The app requests `echoCancellation: true` and
`noiseSuppression: true`; it neither implements AEC3 nor proves that a
particular browser/device uses that implementation. The settings check proves
only that the browser reports echo cancellation enabled. See the
[WebRTC AEC3 interface](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/aec3/echo_canceller3.h)
and [capture constraints](https://www.w3.org/TR/mediacapture-streams/#dom-mediatrackconstraintset-echocancellation).

On top of cancellation, `web/chat.js::startBargeWatch()` re-enables the mic
track during a reply, measures its RMS and the WebAudio playback RMS, and
requires `mic > max(BARGE.floor, playback * BARGE.echoGain)` for
`BARGE.holdMs = 220` ms after `BARGE.settleMs = 350` ms. Invalid/missing
numeric inputs fail closed. This watcher does not record the interruption's
opening words: `interruptReply()` cancels the request, pauses playback, then
starts a new recorder through `listen()`. Continue or repeat the replacement
request once the page says **Listening**.

`getUserMedia` requests `autoGainControl: !bargeWanted()` in `web/chat.js`.
With barge-in selected, gain control is off: automatic amplification of quiet
capture/residual echo would undermine the fixed RMS comparison. This is a
constraint request, not a measurement of the device's actual gain behavior.

`bargeAvailable()` requires the first audio track's settings to report
`echoCancellation === true`. False, missing settings, or an omitted value
refuses voice interruption. Some Bluetooth and raw Linux capture paths fall
into this category; there is no device-name blacklist. The exact messages in
`web/chat.js::startBargeWatch()` are shown in `web/chat.html`'s `barge-note`:

| Refusal | Page text |
|---|---|
| No playback analyser | `Unavailable: the reply is not routed through WebAudio.` |
| Cancellation not reported true | `Unavailable on this audio device: no echo cancellation.` |

The WebAudio refusal is checked first. Audio can still play with barge-in
unavailable. Uncheck **Interrupt replies by speaking**, end the conversation,
and start it again to acquire the mic with the new constraints. The selection
is saved in this browser. With the feature off or refused, the microphone
stays muted during the reply. **Interrupt reply** or Space remains available
while busy, including during thinking/synthesis; Space inside editable fields
and controls retains its ordinary meaning.

### Limits and current defects

| Limit | Source and controlling value |
|---|---|
| Voice interruption is armed only while `phase === 'speaking'`. Talking during `thinking` or `synthesizing` does not interrupt. | `web/chat.js::startBargeWatch()`, `phase` and the literal `'speaking'`. |
| A reply that has just stopped costs the next clip up to one 350 ms settle window before its energy counts toward endpointing. It is not a 350 ms guarantee of end-to-end interrupt latency, and recording itself already runs during this window. | `web/chat.js::listen()`, `BARGE.settleMs = 350`, `sincePlayback`, `playbackEndedAt`. The window is computed from elapsed time, so a clip long after playback pays none; the first version read the timestamp as a flag and charged every later clip, which `tests/echo_path_test.mjs` now forbids. |
| Only three successive unfinished clips are held. A fourth breath that ends another unfinished clip dispatches the accumulated partial sentence. | `web/chat.js::runTurn()`, `fragmentHolds < 3` (literal bound, no named maximum constant). |
| The page's echo model is one scalar gain, not an adaptive filter. Room delay, nonlinear loudspeakers, differing input/output gains, and quiet speech can defeat it. | `web/chat.js::nearEndSpeech()`, `BARGE.echoGain = 0.5`, `BARGE.floor = 0.020`, `BARGE.holdMs = 220`. |
| A browser without `AudioContext.createMediaElementSource` gets no playback reference, and barge-in then refuses rather than guess. | `web/chat.js::armGlow()`, `ctx.createMediaElementSource`, `playbackAnalyser`. The API belongs to `AudioContext`, not to the media element; see the [Web Audio API](https://www.w3.org/TR/webaudio/#dom-audiocontext-createmediaelementsource). The first version asked `player.createMediaElementSource`, a check no browser can pass, which silently disabled the speaking glow and barge-in everywhere — the reason `voice_barge_browser.mjs` now measures the glow instead of accepting a refusal. |

Replies are synthesized after generation completes (the engine is non-streaming
for chat); the page waits for the TTS response blob, so first-audio latency
includes generation, synthesis and transfer. History lives in
this browser tab and is sent with each turn; the engine's own checkpoint cache
does the reuse.  Context overflow fails loudly with "start a new chat" rather
than quietly dropping old turns.

The full assistant text is committed before playback (`web/chat.js::runTurn`,
`committed`), so cutting audio does not trim the transcript/history to the
words actually heard. Automated gate/browser tests cannot establish acoustic
echo performance; use the manual check in [RUNBOOK.md](RUNBOOK.md).

## Deliberate limits, written down

* One Qwen slot, one ASR arena, one TTS batch: this is a conversation, not a
  multi-tenant service.
* Sampled per-process peaks on the 32607 MiB card: Qwen 23372 MiB, ASR 5268 MiB,
  TTS 1288 MiB — a conservative sum of samples (~29.2 GiB), not a worst-case
  guarantee.
* The bridge is a Python `ThreadingHTTPServer`.  It is the right size for one
  conversation and the wrong tool for a hundred.
