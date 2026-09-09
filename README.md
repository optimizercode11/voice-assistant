# voice-assistant — a live speech-to-speech conversation with Qwen

Point a browser at it, press **Start conversation**, talk, and the answer comes
back as speech.  Nothing is typed unless you want it to be, and nothing leaves
your own server.

This is the application layer only.  The three models it orchestrates are
separate engines in `/mnt/inference-engine`; this repo holds the microphone-to-
speaker loop, the browser page, the HTTP/TLS bridge, and the supervisor that
starts and certifies the whole stack.

```
   your voice ──▶ browser ──▶ STT ──▶ Qwen3.8-27B ──▶ Kokoro TTS ──▶ your ears
                     ▲            (this repo owns every arrow)   │
                     └──────────── listen again ◀────────────────┘
```

| Piece | What it is | Lives in |
|---|---|---|
| `/chat` page | microphone capture, pause detection, playback, interruption | this repo (`web/`) |
| speech bridge | one same-origin HTTP/TLS door for STT, chat, TTS | this repo (`tools/`) |
| conversation adapter | system prompt, message rules, timeouts, cancellation | this repo (`tools/`) |
| stack supervisor | starts/verifies/rolls back all five processes | this repo (`deploy/`) |
| Kokoro-82M TTS engine | `kserver`, CUDA continuous batching | `inference-engine/kokoro` |
| Qwen3-ASR-0.6B engine | `qasr_serve` + `qasr_worker`, resident | `inference-engine/qwen3-asr` |
| Qwen3.8-27B engine | `q38_27_server`, NVFP4, host-state cache | `inference-engine/q38-27-nvfp4` |

## Where things are

```
tools/     speech_ui.py (the bridge) · voice_chat.py (the LLM adapter) · g2p_sidecar.py
web/       chat.html + chat.js (the assistant) · index.html (the speech studio)
deploy/    voice_stack.py (supervisor) · site_config.py (THIS host) · systemd unit
tests/     bridge + adapter suites (CPU, no model) · browser suites · fixtures/
scripts/   the guarded-run / guardrail-check harness this repo is certified with
```

## Run the tests (no GPU, no model, no network)

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py    # 11 bridge contracts
CUDA_VISIBLE_DEVICES="" python3 tests/voice_chat_test.py   #  5 chat contracts
python3 deploy/check_site.py                               # deployment profile
make test-browser                                          # 4 suites × chromium+webkit
```

`make check` runs everything that works offline.  The browser suites need a
Playwright install and a Node ≥ 20; see `tests/README.md`.

## Deploy and operate

The stack runs as one user service on the GPU host:

```bash
ssh vllm 'systemctl --user status voice-stack-gpu2'
```

`RUNBOOK.md` covers install, restart, rollback, the evidence gates, and what to
look at when the answer comes back silent.  `ARCHITECTURE.md` explains the
request path and the limits that are deliberate rather than accidental.

## Current deployment

Live at **https://192.168.228.113:8092/chat** on GPU 2 of `vllm`, installed at
`vllm:~/qwen36/voice-stack/voice-restore-20260908/`.  `PROVENANCE.md` records
where every file in this repo came from, which campaign certified it, and how
the copies here were verified against what is actually running.
