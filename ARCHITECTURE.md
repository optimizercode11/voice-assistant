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

Five processes, started in dependency order by `deploy/voice_stack.py`:

| # | Process | Why it exists |
|---|---|---|
| 1 | `g2p_sidecar.py` | CPU multilingual phonemization over a unix socket. The TTS engine takes phonemes; G2P is a separate lane so it never holds a GPU lock. |
| 2 | `q38_27_server` | ctx 16384, one slot, Q8 KV, MTP off, 8 GiB host state cache. |
| 3 | `kserver` | Kokoro TTS with continuous batching and segment-level streaming. |
| 4 | `qasr_serve` | Holds the ASR checkpoint resident, so a request costs the engine's own ~40–100 ms instead of a process spawn plus a model load. |
| 5 | `speech_ui.py` | The only thing the browser talks to. |

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
* Proxied, including `Transfer-Encoding: chunked`, so the first segment of a
  long reply plays while the rest is still being synthesized.

## Turn-taking, honestly

Capture stops on a ~1 s amplitude pause, capped at 30 s per turn, with a
**Send now** button that bypasses the threshold for quiet speech.  This is
energy-and-pause detection, **not** a neural VAD and **not** full-duplex
acoustic barge-in: the microphone is muted while the assistant is thinking and
speaking, and **Interrupt** (or Space) stops playback and resumes capture.

Replies are synthesized after generation completes (the engine is non-streaming
for chat), so first-audio latency is generation + synthesis.  History lives in
this browser tab and is sent with each turn; the engine's own checkpoint cache
does the reuse.  Context overflow fails loudly with "start a new chat" rather
than quietly dropping old turns.

## Deliberate limits, written down

* One Qwen slot, one ASR arena, one TTS batch: this is a conversation, not a
  multi-tenant service.
* Sampled per-process peaks on the 32607 MiB card: Qwen 23372 MiB, ASR 5268 MiB,
  TTS 1288 MiB — a conservative sum of samples (~29.2 GiB), not a worst-case
  guarantee.
* The bridge is a Python `ThreadingHTTPServer`.  It is the right size for one
  conversation and the wrong tool for a hundred.
