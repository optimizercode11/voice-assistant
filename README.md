# voice-assistant — a live speech-to-speech conversation with Qwen

Point a browser at it, press **Start conversation**, talk, and the answer comes
back as speech. Nothing is typed unless you want it to be. Voice processing
runs on your own server; optional tools can reach configured external sources.

This is the application layer only.  The three models it orchestrates are
separate engines in `/mnt/inference-engine`; this repo holds the microphone-to-
speaker loop, the browser page, the HTTP/TLS bridge, and the supervisor that
starts and certifies the whole stack.

```
   your voice ──▶ browser ──▶ STT ──▶ Qwen3.8-27B ──▶ Kokoro TTS ──▶ your ears
                     ▲            (this repo owns every arrow)   │
                     └──────────── listen again ◀────────────────┘

                        Qwen can ask, mid-turn, to look something up:
                        ┌── your own notes (SQLite FTS5 / BM25)
                        ├── MCP servers you named in a config
                        └── an allowlisted web page
                        The loop runs on the server; the browser only hears
                        "looking that up…" and the answer.  See TOOLS.md.
```

| Piece | What it is | Lives in |
|---|---|---|
| `/chat` page | microphone capture, pause detection, playback, interruption | this repo (`web/`) |
| speech bridge | one same-origin HTTP/TLS door for STT, chat, TTS | this repo (`tools/`) |
| conversation adapter | system prompt, message rules, the tool loop, timeouts, cancellation | this repo (`tools/`) |
| capability layer | tool registry, BM25 retrieval, MCP client, TOML config | this repo (`tools/`, `TOOLS.md`) |
| stack supervisor | starts/verifies/rolls back all five processes | this repo (`deploy/`) |
| Kokoro-82M TTS engine | `kserver`, CUDA continuous batching | `inference-engine/kokoro` |
| Qwen3-ASR-0.6B engine | `qasr_serve` + `qasr_worker`, resident | `inference-engine/qwen3-asr` |
| Qwen3.8-27B engine | `q38_27_server`, NVFP4, host-state cache | `inference-engine/q38-27-nvfp4` |

## Where things are

```
tools/     speech_ui.py (the bridge) · voice_chat.py (the loop) · agent_tools.py (registry)
           retrieval.py (FTS5 RAG) · mcp_client.py (stdio MCP) · agent_config.py · voicectl.py
web/       chat.html + chat.js (the assistant) · index.html (the speech studio)
config/    assistant.toml (capabilities: tools, corpus, MCP servers)
deploy/    voice_stack.py (supervisor) · site_config.py (THIS host) · systemd unit
tests/     bridge, adapter, tools, RAG, MCP and loop suites (CPU, no model) · browser suites
scripts/   the guarded-run / guardrail-check harness this repo is certified with
```

## Run the tests (no GPU, no model, no network)

```bash
CUDA_VISIBLE_DEVICES="" make check                          # offline contracts
CUDA_VISIBLE_DEVICES="" make sabotage                       # paired negatives must FAIL
CUDA_VISIBLE_DEVICES="" make test-browser                   # chromium + webkit
```

Or one at a time:

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py     # 11 bridge contracts
CUDA_VISIBLE_DEVICES="" python3 tests/voice_chat_test.py    #  5 chat contracts
CUDA_VISIBLE_DEVICES="" python3 tests/agent_tools_test.py   # 20 registry + config contracts
CUDA_VISIBLE_DEVICES="" python3 tests/retrieval_test.py     # 11 RAG contracts
CUDA_VISIBLE_DEVICES="" python3 tests/mcp_test.py           # 12 MCP contracts, real stdio peer
CUDA_VISIBLE_DEVICES="" python3 tests/tool_loop_test.py     # 12 loop contracts over real TLS
CUDA_VISIBLE_DEVICES="" python3 deploy/check_site.py         # deployment profile
```

Nothing above touches a GPU, a model or the network — the LLM upstreams are
scripted fakes and the MCP peer is `tests/fixtures/fake_mcp_server.py`.  The
browser suites need a Playwright install and Node ≥ 20; see `tests/README.md`.

## Interrupting a reply

**Interrupt reply** (or Space outside a text field/control) stops a busy turn
and resumes listening during a voice conversation. **Interrupt replies by
speaking** is checked by default: when available, the page reopens the microphone
during playback and uses the browser's echo cancellation plus a sustained
speech gate to decide when to interrupt. Turning that checkbox off restores
the mode that mutes the microphone throughout a reply; end and restart the
conversation after changing it.

The browser supplies echo cancellation (AEC3 on browser paths using libwebrtc);
the page adds a comparison between microphone energy and the reply's waveform.
It requests `autoGainControl: false` when barge-in is wanted, because automatic
gain can lift residual echo above that gate. Devices reporting anything other
than `echoCancellation === true`, including some Bluetooth and raw Linux
capture paths, are refused. `barge-note` says **“Unavailable on this audio
device: no echo cancellation.”** A missing playback reference instead says
**“Unavailable: the reply is not routed through WebAudio.”** Device names alone
do not establish support.

Voice interruption is limited to `phase === 'speaking'` in `web/chat.js`
(`startBargeWatch`); talking during `thinking` or `synthesizing` does not cancel
the turn. The same file sets `BARGE.settleMs = 350`, `BARGE.holdMs = 220`,
`BARGE.floor = 0.020`, and the scalar echo estimate `BARGE.echoGain = 0.5`.
The first clip after playback pays a settle window. Fragment carry stops at
`fragmentHolds < 3`: a fourth unfinished clip still dispatches a partial.
See [Architecture](ARCHITECTURE.md#turn-taking-and-barge-in) for these limits
and the current WebAudio/settle defects; this checkout is **not yet verified
for acoustic barge-in on real hardware**.

## Deploy and operate

Two user services share the resident models on the GPU host:

```bash
ssh vllm 'systemctl --user status voice-tools-bridge.service voice-stack-gpu2.service --no-pager -l'
```

[RUNBOOK.md](RUNBOOK.md) gives the ordered staging, snapshot, copy, CPU bridge
restart, post-check and rollback commands, with the real read-only preflight
output. It also contains the 30-second microphone/speaker acceptance check.
`ARCHITECTURE.md` explains the
request path and the limits that are deliberate rather than accidental.
`TOOLS.md` covers tools, RAG and MCP — including what is off until you name it.

## Capabilities (tools, RAG, MCP)

Off in the generic configuration. The separate tools deployment explicitly
loads `config/host.toml`. To see what a config would allow, and what is broken:

```bash
python3 tools/voicectl.py --config config/assistant.toml doctor
python3 tools/voicectl.py --config config/assistant.toml index build
```

Then start the bridge with `--tools-config config/assistant.toml`.  A config the
bridge cannot fully understand is a refusal to start, not a warning.

## Current deployment

The tools bridge is **https://192.168.228.113:8094/chat**, installed at
`vllm:~/qwen36/voice-stack/voice-tools-20260910/`; it reuses GPU 2's models.
The original stack remains **https://192.168.228.113:8092/chat**, installed at
`vllm:~/qwen36/voice-stack/voice-restore-20260908/`. Updating this app means
restarting only `voice-tools-bridge.service`. `PROVENANCE.md` records
where every file in this repo came from, which campaign certified it, and how
the copies here were verified against what is actually running.
