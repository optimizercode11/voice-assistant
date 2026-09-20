# Browser audio validation — 2026-09-20

Both existing suites passed (exit 0) using local Chromium with `--disable-gpu`, `CUDA_VISIBLE_DEVICES=''`, Node `/tmp/fork-stability-browser-zu50jkow/node_modules/node/bin/node`, and Playwright `/tmp/fork-stability-browser-zu50jkow/node_modules/playwright/index.mjs`. The Makefile's older `/tmp/kokoro-playback-browser` dependency directory was absent.

- `tests/browser/voice_recovery_browser.mjs`: passed held/brief turns, Send now, carried fragments, cancellation, wake gate, microphone pause recovery, suspended AudioContext recovery, autoplay retry, single POST behavior, agent indicators, and preference migration.
- `tests/browser/voice_tts_browser.mjs`: passed multi-sentence pipelining, complete ordered speech text, unsplit short answer, and streamed playback before answer completion. Six TTS requests; three streamed clips used gapless WebAudio. No JavaScript page errors.

Commands ran with those two environment variables and the stated Node binary. The TTS suite was copied into `browser-tts-runner.mjs` with only its screenshot output directory changed from `evidence/browser` to `evidence/voice-audio-20260920/browser-artifacts`; application and test source files were not changed.

These suites use deterministic synthetic HTTP responses and headless browser audio. They verify playback and recovery behavior, but cannot verify the user's device output volume, tab mute state, speaker selection, or current browser state.

Code inspection found existing explicit recovery controls: `Play reply / Resume audio` after WebAudio suspension or denied autoplay, and `Resume listening` after the configured idle pause (default four seconds). None is proven to explain the user's reported missing voice.
