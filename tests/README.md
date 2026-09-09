# Tests

Two kinds, and the difference matters: the Python suites test the **bridge**,
the browser suites test the **page**.  Neither runs a model.

## Bridge contracts (CPU, no browser)

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py     # 11 tests
CUDA_VISIBLE_DEVICES="" python3 tests/voice_chat_test.py    #  5 tests
```

They refuse to run unless `CUDA_VISIBLE_DEVICES` is empty — a CPU oracle that
quietly initialized a GPU is a lie about containment, not a faster test.  They
need a real `ffmpeg` and `openssl` on PATH, and they run the real
`tools/speech_ui.py` against fake ASR/Qwen processes, so what is under test is
the bridge's actual error mapping and argument rules.

Paired sabotage, which must FAIL:

```bash
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py --speaker-sabotage   # must FAIL
CUDA_VISIBLE_DEVICES="" python3 tests/speech_ui_test.py --negative            # control, must PASS
```

`make sabotage` runs the failing arms and reports a sabotage that passes
as the failure it is.

## Page contracts (Playwright)

```bash
export NODE=/path/to/node20+                       # Playwright needs Node >= 20
export PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_chat_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_controls_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/voice_language_browser.mjs
CUDA_VISIBLE_DEVICES="" $NODE tests/browser/stt_browser.mjs
```

Each starts its own local server that serves the real `web/` files and stubs
the endpoints, so the page's own recorder loop, VAD thresholds, playback and
interruption code is what runs.  Chromium gets `tests/fixtures/capture.wav` as
a fake microphone device; webkit has no MediaRecorder in the Linux test build,
so those runs cover typed input and **make no physical-Safari-microphone
claim**.  Screenshots land in `evidence/browser/`.

`--sabotage` variants exist for the chat and controls suites: they disable one
handler (the Space key, or automatic language choice) and must fail exactly
there.  `--live` points the same assertions at the deployed page.

`page_harness.mjs` is different again: it runs the studio page's script against
a live `kserver` with a fake WebAudio that records what the page actually
scheduled.  Point it at a running TTS (`node tests/browser/page_harness.mjs
http://127.0.0.1:8090`, or through `ssh -N -L 8090:127.0.0.1:8090 vllm`).

Browser suites are timing-sensitive: run them one at a time.  Two concurrent
runs on a busy host will produce a spurious `waitForFunction` timeout, which
looks exactly like a real regression and is neither.
