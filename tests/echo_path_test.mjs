import fs from 'node:fs';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';

// Offline waveform evidence, not an AEC3 implementation or a browser/room test.
// g is the residual mic/playback amplitude ratio AFTER any echo cancellation.
// A "trip" means interruptReply(), including the page's sustained-speech hold.
// A positive nearEndSpeech frame is reported separately: delayed echo can open
// that predicate briefly without interrupting the reply.
//   CUDA_VISIBLE_DEVICES="" node tests/echo_path_test.mjs [--verbose] [--sabotage|--dead-sabotage]
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '', 'set CUDA_VISIBLE_DEVICES="" explicitly');
for (const arg of process.argv.slice(2)) {
  assert.ok(['--verbose', '--sabotage', '--dead-sabotage'].includes(arg), `unknown argument: ${arg}`);
}
const verbose = process.argv.includes('--verbose');
const sabotage = process.argv.includes('--sabotage');
const deadSabotage = process.argv.includes('--dead-sabotage');
const assertionsComplete = 'ASSERTIONS COMPLETE: echo path';
// A sabotage whose anchor no longer matches the page mutates nothing, and a
// mutation that changes nothing passes -- exactly like a sabotage that was
// genuinely caught.  --dead-sabotage runs the REAL sabotage path with a no-op
// replacement and requires the child to finish its assertions and exit 0, which
// is what proves this file is capable of failing.
if (deadSabotage && !sabotage) {
  const run = spawnSync(process.execPath, [process.argv[1], '--sabotage', '--dead-sabotage'],
    {encoding: 'utf8', timeout: 120000});
  process.stdout.write(run.stdout ?? '');
  process.stderr.write(run.stderr ?? '');
  assert.ifError(run.error);
  assert.equal(run.signal, null, 'a dead sabotage must finish normally');
  assert.equal(run.status, 0, 'an uncaught sabotage must exit zero so make rejects it');
  assert.ok(run.stdout.split('\n').includes(assertionsComplete),
    'the waveform assertions must run to the end');
  console.log('DEAD SABOTAGE PASS: the no-op escaped after the waveform assertions ran');
  process.exit(0);
}
const source = fs.readFileSync(new URL('../web/chat.js', import.meta.url), 'utf8');
const begin = source.indexOf('// BARGE-GATE-BEGIN');
const end = source.indexOf('// BARGE-GATE-END');
assert.ok(begin > 0 && end > begin, 'the gate markers moved or vanished');
const originalBlock = source.slice(source.indexOf('\n', begin) + 1, end);
let gateBlock = originalBlock;
if (sabotage) {
  const anchor = 'return mic > Math.max(BARGE.floor, playback * BARGE.echoGain);';
  assert.equal(gateBlock.split(anchor).length, 2, 'sabotage must replace exactly one gate rule');
  gateBlock = gateBlock.replace(anchor, deadSabotage ? anchor : 'return mic > BARGE.floor;');
  if (deadSabotage) assert.equal(gateBlock, originalBlock, 'a dead sabotage must leave the gate unchanged');
}
const {BARGE} = new Function(`${gateBlock}\nreturn {BARGE, nearEndSpeech};`)();

// These top-level functions end at a column-zero brace in the page. Fail on
// source drift rather than silently running a second copy of the hold/refusal.
function pageFunction(name) {
  const match = source.match(new RegExp(`^function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^\\}`, 'm'));
  assert.ok(match, `cannot extract page function ${name}`);
  return match[0];
}
const watchSource = pageFunction('startBargeWatch');
const availableSource = pageFunction('bargeAvailable');
const playbackSize = Number(source.match(/playbackAnalyser\.fftSize\s*=\s*(\d+)/)?.[1]);
const micSize = Number(source.match(/\banalyser\.fftSize\s*=\s*(\d+)/)?.[1]);
assert.equal(playbackSize, 1024, 'review the playback window if the page changes');
assert.equal(micSize, 2048, 'review the microphone window if the page changes');
assert.match(watchSource, /analyser\.getFloatTimeDomainData\(samples\)/);
assert.match(watchSource, /playbackAnalyser\.getByteTimeDomainData\(bytes\)/);

// Read RIFF chunks, including metadata and odd-byte padding; never assume a
// 44-byte WAV header. This committed fixture is mono PCM16 at 48 kHz, which is
// also the simulated AudioContext rate (no resampling or level normalization).
const wav = fs.readFileSync(new URL('fixtures/microphone.wav', import.meta.url));
function decodeWav(bytes) {
  assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
  assert.equal(bytes.toString('ascii', 8, 12), 'WAVE');
  const limit = bytes.readUInt32LE(4) + 8;
  assert.equal(limit, bytes.length, 'complete RIFF fixture');
  let fmt, pcm;
  for (let offset = 12; offset + 8 <= limit;) {
    const kind = bytes.toString('ascii', offset, offset + 4);
    const size = bytes.readUInt32LE(offset + 4);
    const start = offset + 8;
    assert.ok(start + size <= limit, `truncated ${kind} chunk`);
    if (kind === 'fmt ') {
      assert.ok(size >= 16);
      fmt = {
        format: bytes.readUInt16LE(start), channels: bytes.readUInt16LE(start + 2),
        rate: bytes.readUInt32LE(start + 4), align: bytes.readUInt16LE(start + 12),
        bits: bytes.readUInt16LE(start + 14),
      };
    }
    if (kind === 'data') { assert.equal(pcm, undefined, 'one data chunk'); pcm = bytes.subarray(start, start + size); }
    offset = start + size + size % 2;
  }
  assert.deepEqual(fmt, {format: 1, channels: 1, rate: 48000, align: 2, bits: 16});
  assert.ok(pcm?.length > 0 && pcm.length % fmt.align === 0);
  return {rate: fmt.rate, samples: Float32Array.from({length: pcm.length / 2}, (_, i) => pcm.readInt16LE(i * 2) / 32768)};
}
const {rate, samples: reply} = decodeWav(wav);
const fixtureHash = createHash('sha256').update(wav).digest('hex');
const sourceHash = createHash('sha256').update(source).digest('hex');
const sampleAt = ms => Math.round(ms * rate / 1000);
const durationMs = reply.length * 1000 / rate;
const tickMs = 16;
const gains = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6];
const delays = Array.from({length: 31}, (_, i) => i + 10);

// Causal, overlapping windows ending at the animation tick, zero-padded at
// startup. Playback uses the page's byte-domain quantization; mic uses floats.
// No FFT/window function is applied: the page reads TIME-domain analyser data.
function fillWindow(output, signal, timeMs, byteDomain) {
  const start = sampleAt(timeMs) - output.length;
  for (let i = 0; i < output.length; i++) {
    const value = signal[start + i] ?? 0;
    output[i] = byteDomain ? Math.max(0, Math.min(255, Math.floor(128 * (1 + value)))) : value;
  }
}
function rmsAt(signal, timeMs) {
  const frame = new Float32Array(micSize);
  fillWindow(frame, signal, timeMs, false);
  return Math.sqrt(frame.reduce((sum, x) => sum + x * x, 0) / frame.length);
}

// Execute the real watcher, clock, RMS, hold/reset and AEC refusal functions.
// The only fakes are WebAudio sample delivery, time, RAF and device settings.
const makePage = new Function('io', `${gateBlock}
${pageFunction('rmsOf')}
${availableSource}
${pageFunction('stopBargeWatch')}
${watchSource}
let active = true, phase = 'speaking';
let bargeRaf = 0, bargeVoiced = 0, bargeLast = 0;
let playbackStartedAt = io.epoch, playbackEndedAt = 0;
const {stream, analyser, playbackAnalyser, player, performance,
       requestAnimationFrame, cancelAnimationFrame} = io;
// The gapless scheduler (2026-09-11) is idle in this harness: the reply here is
// the media element, exactly as the watcher's element branch sees it.
const gapless = io.gapless ?? {playing: false};
const $ = () => io.note;
const bargeWanted = () => true;
const originalGate = nearEndSpeech;
nearEndSpeech = (...args) => {
  const accepted = originalGate(...args);
  io.observe(...args, accepted);
  return accepted;
};
const originalStop = stopBargeWatch;
stopBargeWatch = () => { io.onStop(bargeVoiced); originalStop(); };
function interruptReply() { io.onInterrupt(); }
return {
  BARGE, startBargeWatch,
  endedAt(value) { playbackEndedAt = value; },
  voiced() { return bargeVoiced; },
};`);

function simulate(micSignal, {
  playbackSignal = reply, stopMs = durationMs, startAtMs = 0,
  endMs = durationMs + 400, settleMs, echoCancellation = true,
} = {}) {
  const frames = [], trips = [];
  const track = {enabled: false, getSettings: () => ({echoCancellation})};
  let nowMs = startAtMs, pending = null, nextRaf = 0, lastStoppedHold = 0, maxHeldMs = 0;
  const io = {
    epoch: 1000, note: {textContent: ''},
    player: {paused: false, ended: false},
    stream: {getTracks: () => [track], getAudioTracks: () => [track]},
    analyser: {fftSize: micSize, getFloatTimeDomainData: out => fillWindow(out, micSignal, nowMs, false)},
    playbackAnalyser: {fftSize: playbackSize, getByteTimeDomainData: out => fillWindow(out, playbackSignal, nowMs, true)},
    performance: {now: () => io.epoch + nowMs},
    requestAnimationFrame: callback => { pending = callback; return ++nextRaf; },
    cancelAnimationFrame: () => { pending = null; },
    observe: (mic, playback, since, accepted) => frames.push({timeMs: nowMs, mic, playback, since, accepted}),
    onStop: held => { lastStoppedHold = held; maxHeldMs = Math.max(maxHeldMs, held); },
    onInterrupt: () => trips.push({...frames.at(-1), heldMs: lastStoppedHold}),
  };
  const page = makePage(io);
  if (settleMs !== undefined) page.BARGE.settleMs = settleMs;
  const updatePlayback = () => {
    if (nowMs >= stopMs) { io.player.ended = true; page.endedAt(io.epoch + stopMs); }
  };
  updatePlayback();
  const armed = page.startBargeWatch();
  for (nowMs = startAtMs + tickMs; nowMs <= endMs && pending; nowMs += tickMs) {
    updatePlayback();
    const callback = pending; pending = null; callback();
    maxHeldMs = Math.max(maxHeldMs, page.voiced());
  }
  return {frames, trips, maxHeldMs, armed, micEnabled: track.enabled, note: io.note.textContent};
}

// No generated files: all derived samples are constructed here on every run.
function echoPath(signal, g, delayMs) {
  const delay = sampleAt(delayMs);
  return Float32Array.from({length: signal.length + delay}, (_, n) => g * (signal[n - delay] ?? 0));
}
// Additional spectral-smearing control. A unity-DC-gain causal moving average
// cannot amplify the signal; the required pure delayed-copy sweep stays intact.
const smoothedReply = Float32Array.from(reply, (_, n) =>
  ((reply[n] ?? 0) + (reply[n - 1] ?? 0) + (reply[n - 2] ?? 0) + (reply[n - 3] ?? 0)) / 4);
function mix(a, b) {
  const result = Float32Array.from({length: Math.max(a.length, b.length)}, (_, n) => (a[n] ?? 0) + (b[n] ?? 0));
  assert.ok(result.every(value => Math.abs(value) < 1), 'fixture mixing must not clip');
  return result;
}
function nearSpeech(lengthMs = 320) {
  // A continuous voiced portion of the SAME recording, shifted independently
  // of the echo. Enter in the reply's quiet gap to measure the hold from a reset.
  // 5 ms fades avoid treating splice clicks as the person's first phoneme.
  const onset = sampleAt(608), from = sampleAt(680), length = sampleAt(lengthMs), fade = sampleAt(5);
  return Float32Array.from({length: onset + length}, (_, n) => {
    const k = n - onset;
    if (k < 0 || k >= length) return 0;
    return 2 * reply[from + k] * Math.min(1, k / fade, (length - 1 - k) / fade);
  });
}

let failures = 0;
function check(label, body) {
  try { body(); console.log(`ok  ${label}`); }
  catch (error) { failures++; console.error(`FAIL ${label}: ${error.message}`); }
}
console.log(`fixture microphone.wav: ${rate} Hz, ${durationMs} ms, sha256=${fixtureHash}`);
console.log(`web/chat.js sha256=${sourceHash}`);
console.log(`frames: ${tickMs} ms ticks, playback=${playbackSize} bytes, mic=${micSize} floats; BARGE=${JSON.stringify(BARGE)}`);
console.log('trip = held interruptReply(); positive frames are reported separately');
if (sabotage) console.log('SABOTAGE: ignoring playback in the extracted gate; failures must exit non-zero');

const rows = [];
for (const [path, signal] of [['delay', reply], ['smoothed', smoothedReply]]) {
  for (const g of gains) {
    const runs = delays.map(delay => ({delay, ...simulate(echoPath(signal, g, delay))}));
    const tripped = runs.filter(run => run.trips.length);
    const row = {
      path, g, trips: tripped.length,
      positiveFrames: runs.reduce((sum, run) => sum + run.frames.filter(frame => frame.accepted).length, 0),
      maxHeldMs: Math.max(...runs.map(run => run.maxHeldMs)),
      firstTrip: tripped.length ? Math.min(...tripped.map(run => run.trips[0].timeMs)) : null,
    };
    rows.push(row);
    if (verbose) console.log(`sweep ${JSON.stringify(row)}; delays=${tripped.map(run => run.delay).join(',') || 'none'}`);
    if (g <= BARGE.echoGain) check(`${path} echo g=${g}: zero held trips at all 31 delays (10..40 ms)`, () => {
      assert.equal(tripped.length, 0, `${tripped.length} delays interrupted; first=${JSON.stringify(tripped[0]?.trips[0])}`);
      assert.ok(runs.every(run => run.frames.at(-1).timeMs >= durationMs + BARGE.settleMs), 'cover the entire reply and settle window');
    });
  }
}
const threshold = rows.find(row => row.path === 'delay' && row.trips)?.g;
console.log(`TRIP THRESHOLD: smallest tested echo gain with a held trip = ${threshold ?? 'none in sweep'}`);
console.log(`FRAME THRESHOLD: smallest tested echo gain with a positive predicate = ${rows.find(row => row.path === 'delay' && row.positiveFrames)?.g ?? 'none in sweep'}`);
check('the sweep includes and exposes an AEC failure above BARGE.echoGain', () => {
  assert.ok(gains.includes(BARGE.echoGain), 'sweep must cover the exact configured boundary');
  assert.ok(rows.some(row => row.path === 'delay' && row.g > BARGE.echoGain && row.trips > 0), 'no above-boundary trip was measured');
});

const near = nearSpeech();
let nearMin = Infinity, nearMax = -Infinity;
for (const g of gains.filter(g => g <= BARGE.echoGain)) {
  check(`near-end speech over echo g=${g}: trips only after sustained energy and hold`, () => {
    for (const delay of delays) {
      const run = simulate(mix(echoPath(reply, g, delay), near));
      assert.equal(run.trips.length, 1, `g=${g}, delay=${delay}: missing interruption`);
      const trip = run.trips[0];
      assert.ok(trip.heldMs >= BARGE.holdMs, `page interrupted at only ${trip.heldMs} ms`);
      assert.ok(trip.timeMs - 608 >= BARGE.holdMs, 'interrupted too soon after the near-end waveform onset');
      let sustainedMs = 0;
      for (const frame of run.frames) sustainedMs = rmsAt(near, frame.timeMs) > BARGE.floor ? sustainedMs + tickMs : 0;
      assert.ok(sustainedMs >= BARGE.holdMs, `only ${sustainedMs} ms of sustained near-end energy`);
      assert.ok(trip.timeMs < 608 + 320, 'interruption must happen while the person is speaking');
      nearMin = Math.min(nearMin, trip.timeMs - 608); nearMax = Math.max(nearMax, trip.timeMs - 608);
    }
  });
}
console.log(`near-end onset-to-trip latency: ${nearMin}..${nearMax} ms; hold=${BARGE.holdMs} ms`);
check('a short real-speech burst cannot satisfy the hold', () => {
  const run = simulate(nearSpeech(80));
  assert.ok(run.frames.some(frame => frame.accepted), 'burst must actually reach the predicate');
  assert.equal(run.trips.length, 0);
});

// Stop mid-phoneme at 960 ms, then return 30 decaying room reflections with
// delays 10..300 ms. Tap weights sum to 0.5, so this is residual echo, not new
// near-end speech. The finite 300 ms response plus the 42.67 ms mic window fits
// inside settleMs=350. A 1x -> 16x linear AGC ramp (24 dB stress case) acts ONLY
// after playback stops; it is a chosen stress envelope, not a measured AEC3 AGC.
const stopMs = 960, tailMs = 300;
const stoppedReply = reply.slice(0, sampleAt(stopMs));
const tail = new Float32Array(stoppedReply.length + sampleAt(tailMs));
const taps = Array.from({length: 30}, (_, i) => ({delay: sampleAt((i + 1) * 10), weight: Math.exp(-i * 10 / 400)}));
const tapSum = taps.reduce((sum, tap) => sum + tap.weight, 0);
for (let n = 0; n < tail.length; n++) {
  let value = 0;
  for (const tap of taps) value += 0.5 * tap.weight / tapSum * (stoppedReply[n - tap.delay] ?? 0);
  tail[n] = value;
}
const pumped = Float32Array.from(tail, (value, n) => value *
  (n < stoppedReply.length ? 1 : 1 + 15 * (n - stoppedReply.length) / sampleAt(tailMs)));
assert.ok(pumped.every(value => Math.abs(value) < 1), 'AGC stress must not clip');
const tailOptions = {playbackSignal: stoppedReply, stopMs, startAtMs: stopMs, endMs: stopMs + 500};
const settled = simulate(pumped, tailOptions);
const unsettled = simulate(pumped, {...tailOptions, settleMs: 0});
const unpumped = simulate(tail, {...tailOptions, settleMs: 0});
check('AGC tail: settle window is load-bearing', () => {
  assert.equal(unpumped.trips.length, 0, 'the same tail without pumping must not trip');
  assert.equal(settled.trips.length, 0, 'settle window must reject the pumped tail');
  assert.equal(settled.frames.filter(frame => frame.accepted).length, 0, 'settled tail has no positive frames');
  assert.equal(unsettled.trips.length, 1, 'settleMs=0 must expose the pumped tail');
  const trip = unsettled.trips[0];
  assert.ok(trip.since < BARGE.settleMs && trip.since < tailMs, 'the trip occurs during the audible tail, inside settling');
  assert.ok(trip.mic > BARGE.floor && trip.playback === 0, 'mic tail is audible while playback is silent');
  assert.ok(settled.frames.every(frame => frame.since === frame.timeMs - stopMs), 'sincePlayback resets on stop');
});
console.log(`AGC: settle=${BARGE.settleMs} ms -> ${settled.trips.length} trips; settle=0 -> ${unsettled.trips.length} trips, first at +${unsettled.trips[0]?.since ?? 'none'} ms; no pump -> ${unpumped.trips.length} trips`);
// playReply actually stops the barge watcher on ended; listen() then applies
// its own settle window. Above is a post-stop kernel replay to test the gate's
// stop clock, NOT a claim that the page keeps its barge watcher armed on ended.
check('the page protects listening after playback with the same settle setting', () => {
  const listen = pageFunction('listen');
  // The settle window must be charged from the TIME ELAPSED since playback
  // stopped.  The first version asked only whether playbackEndedAt was non-zero
  // -- a timestamp read as a flag -- so every clip after the first reply paid
  // 350 ms of dead endpointing forever.  listen() cannot be executed outside a
  // browser, so this pins the shape of the arithmetic and forbids the shape of
  // the bug; the window's own behaviour is simulated above.
  assert.match(listen, /const sincePlayback = playbackEndedAt \? performance\.now\(\) - playbackEndedAt : Infinity;/);
  assert.match(listen, /let settleUntil = sincePlayback < BARGE\.settleMs/);
  assert.doesNotMatch(listen, /playbackEndedAt \? BARGE\.settleMs : 0/,
    'a settle window charged to every clip after the first reply');
  assert.match(listen, /if \(now < settleUntil\) \{raf = requestAnimationFrame\(tick\); return;\}/);
});

check('no AEC: source guard refuses before enabling the microphone', () => {
  assert.match(availableSource, /return settings \? settings\.echoCancellation === true : false;/);
  const guard = watchSource.match(/if \(!bargeAvailable\(\)\) \{[\s\S]*?return false;\s*\}/);
  assert.ok(guard, 'startBargeWatch must refuse when AEC is unavailable');
  assert.ok(guard.index < watchSource.indexOf('track.enabled = true'), 'refusal must precede enabling capture');
  const noAecEcho = echoPath(reply, 1, 30);
  const refused = simulate(noAecEcho, {echoCancellation: false});
  assert.equal(refused.armed, false); assert.equal(refused.micEnabled, false);
  assert.equal(refused.frames.length, 0); assert.equal(refused.trips.length, 0);
  assert.match(refused.note, /no echo cancellation/i);
  // Paired control: the identical uncancelled waveform with reported AEC=true
  // DOES interrupt. Thus source/device refusal, not an inaudible fixture, saves it.
  assert.equal(simulate(noAecEcho).trips.length, 1, 'uncancelled waveform must expose a false interruption if armed');
});

console.log(assertionsComplete);
console.log(failures ? `ECHO PATH FAIL: ${failures} assertion groups` : 'ECHO PATH PASS');
// Normal test semantics in BOTH arms. A detected sabotage stays non-zero;
// never turn an assertion failure into success because --sabotage was passed.
process.exitCode = failures ? 1 : 0;
