# Browser audio graph audit — 2026-09-20

Confirmed a browser lifecycle defect using real Chromium and a nonzero 440 Hz WAV. The actual application output analyser measured RMS 0.194226688 before a persisted `pagehide`/`pageshow` cycle and RMS 0 after it. After the cycle the page still reported Speaking, the media player reported `paused=false`, and a newly created AudioContext reported running, but the playback graph was absent. The regression assertion failed with exit 1.

The existing `pagehide` handler unconditionally closed the AudioContext and discarded `mediaSource`. A page restored from the back/forward cache retains its original HTMLAudioElement. That element remains associated with its first MediaElementAudioSourceNode: creating a replacement source throws, and `armGlow` catches it and incorrectly assumes native playback remains available. The old source belongs to a closed context, so audio is silent. A graph that was never attached can fall back to native output; a graph already attached to a closed context cannot.

The reproduction dispatches persisted lifecycle events deterministically on the actual application page. It does not claim that Chromium admitted this particular test page to its back/forward cache, or that this is proven to be the user's current browser state. Headless browser signal measurements prove application graph output, not the user's hardware volume or device selection.

New regression: `tests/browser/voice_audio_output_browser.mjs`. It serves the real HTML and JS, supplies a nonzero one-second waveform, clicks the actual Send button before and after the lifecycle cycle, and measures the real playback analyser. It includes native element and gapless streamed reply paths. CPU-only command:

```sh
CUDA_VISIBLE_DEVICES='' PLAYWRIGHT=/tmp/fork-stability-browser-zu50jkow/node_modules/playwright/index.mjs /tmp/fork-stability-browser-zu50jkow/node_modules/node/bin/node tests/browser/voice_audio_output_browser.mjs
```

Baseline output:

```json
{"mode":"element","label":"before cached-page restore","peakRms":0.1942266882444893,"context":"running","graph":true,"elementTime":0.057288,"playerPaused":false,"stats":{"gapless":0,"element":0},"status":"Press Space to stop the reply."}
{"mode":"element","label":"after cached-page restore","peakRms":0,"context":"running","graph":false,"elementTime":0,"playerPaused":false,"stats":{"gapless":0,"element":0},"status":"Press Space to stop the reply."}
```

Related test gap: `voice_tts_browser.mjs` deliberately returns a silent WAV and verifies playback order/completion, so its prior success never established audible output. The new regression uses a nonzero waveform. Existing suspended-context recovery tests exercise explicit recovery but did not exercise cached page restoration or closed graph reuse.

Implementation details for the coordinator: preserve the context/media element association when `pagehide.persisted` is true, release recording resources and suspend instead; resume the preserved context on the subsequent user gesture before the `glowArmed` early return. A retained `updateSource` must also be cleared/reconnected after closing on pagehide.

After the coordinator's fix, the same command passed (exit 0, approximately five seconds). Native output measured RMS 0.193708899 before and 0.195054902 after restoration. Gapless output measured RMS 0.187922703 before and after restoration; the gapless clip count advanced from one to two. Both paths retained their original context and had a running output graph after the real Send gesture. Because the persisted pagehide handler suspends that context, this also validates gesture-driven resumption of an already armed graph.

```text
BROWSER AUDIO OUTPUT PASS: nonzero element and gapless output survives cached-page restoration
```

Expanded regression also advertises events support and instruments EventSource with an EventTarget fake. It asserts one initial live connection, that connection closed on pagehide, exactly one replacement live connection on persisted pageshow, and no duplicate connection on repeated pageshow. The expanded command passed again (exit 0): native RMS 0.193708899 → 0.193421249 and gapless RMS 0.194382309 → 0.194382309. No JavaScript page errors occurred.
