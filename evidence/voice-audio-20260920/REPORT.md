# Missing voice investigation — 2026-09-20

The server-side speech path passed a fresh check. The user's browser symptom is
not yet identified; server success does not establish audible playback on their
device.

- The user services `voice-stack-gpu4.service` and `q38-server.service` are active,
  with main PIDs 177991 and 1839 and zero automatic restarts.
- The latest user request at 18:48 UTC successfully reached `/stt`,
  `/chat/completions`, and two `/tts` requests using `af_bella` at speed 1.2.
- At 18:52 UTC, a guarded CPU HTTP driver requested that same voice and speed.
  It returned 145244 bytes of mono 24 kHz PCM WAV: 72600 frames, 3.025 seconds,
  normalized RMS 0.046416933760250356. The resident ASR transcribed the clip
  exactly: “The quick brown fox jumps over the lazy dog.”
- The live TTS/ASR guard passed. No services were restarted or source changed
  during this investigation.
- Local and deployed page assets match: `chat.js` SHA256
  `863d5346ac320cbc11153eadb2a9352dc97d7404e298c213a8c7cb982e95be10`,
  `chat.html` SHA256
  `d857e2ac2a122b9b7d62499f31a2b7929dac101812eac9a05bcba45ed3ad7cc0`.

Live evidence: `../guarded-voice-audio-20260920-20260920T185207Z-2820572.json`
and the corresponding `.log`. Browser test results are recorded separately in
this directory; those exercise a test browser, not the user's audio device.

The UI provides **Play reply / Resume audio** for blocked or suspended playback.
Its default silence timeout pauses microphone capture after four seconds and
provides **Resume listening**; setting the timeout to zero keeps listening.
Neither is established as the cause of the reported symptom.
