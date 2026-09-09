// Run the REAL web/index.html script against the REAL server, with the browser
// APIs stubbed.  This is the closest thing to "open it in a browser" that is
// possible without a display: it exercises the page's own fetch-stream loop,
// its odd-chunk byte carry, its Int16->Float32 conversion and its WebAudio
// scheduling, and then asserts on what the fake AudioContext actually heard.
//   node tests/browser/page_harness.mjs [http://127.0.0.1:8090]
//
// Needs node >= 18 (global fetch).  Run it against a live kserver -- locally,
// or through the ssh tunnel:  ssh -N -L 8090:127.0.0.1:8090 vllm
import vm from "node:vm";

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8090";
const html = await (await fetch(BASE + "/")).text();
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const T0 = Date.now();
const now = () => Date.now() - T0;

// ---- fake WebAudio: records every sample the page schedules for playback ---
const heard = { samples: 0, buffers: 0, firstAt: 0, peak: 0, rms2: 0, starts: [] };
class FakeBufferSource {
  constructor() { this.buffer = null; }
  connect() {}
  start(t) { heard.starts.push(t); }
  stop() {}
}
class FakeBuffer {
  constructor(ch, len, rate) { this.numberOfChannels = ch; this.length = len; this.sampleRate = rate; }
  get duration() { return this.length / this.sampleRate; }   // real browsers have this
  copyToChannel(f, _ch) {
    if (!heard.buffers) heard.firstAt = now();
    heard.buffers++;
    heard.samples += this.length;
    for (let i = 0; i < f.length; i++) {
      const a = Math.abs(f[i]);
      if (a > heard.peak) heard.peak = a;
      heard.rms2 += f[i] * f[i];
    }
  }
}
class FakeAudioContext {
  constructor(opts) { this.sampleRate = (opts && opts.sampleRate) || 48000; this.state = "running"; this.destination = {}; }
  get currentTime() { return now() / 1000; }
  resume() { return Promise.resolve(); }
  close() { this.state="closed"; return Promise.resolve(); }
  createBuffer(c, l, r) { return new FakeBuffer(c, l, r); }
  createBufferSource() { return new FakeBufferSource(); }
  createAnalyser() { return { fftSize: 0, frequencyBinCount: 512, connect() {}, getByteTimeDomainData(a) { a.fill(128); } }; }
}

// ---- fake DOM --------------------------------------------------------------
const mk = (id) => ({
  id, value: "", textContent: "", innerHTML: "", className: "", title: "", disabled: false,
  children: [], lastChild: null, style: {}, target: null,
  getContext: () => ({ clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }),
  prepend(n) { this.children.unshift(n); }, appendChild(n) { this.children.push(n); },
  remove() {}, addEventListener() {}, click() {}, pause() {},
  set onclick(f) { this._onclick = f; }, get onclick() { return this._onclick; },
  set oninput(f) { this._oninput = f; }, get oninput() { return this._oninput; },
});
const els = {};
const $ = (id) => (els[id] = els[id] || mk(id));
$("speed").value = "1";
$("voice").value = "default";

const sandbox = {
  AudioContext: FakeAudioContext, window: { AudioContext: FakeAudioContext }, performance: { now },
  document: { getElementById: $, createElement: (t) => mk(t), addEventListener() {} },
  fetch: (u, o) => fetch(u.startsWith("http") ? u : BASE + u, o),
  URLSearchParams, URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} }, Blob,
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  setInterval: () => 0, clearInterval: () => {}, setTimeout, clearTimeout, console,
  AbortController, Int16Array, Float32Array, Uint8Array, Math, Date, Error, JSON,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(script, sandbox, { filename: "index.html<script>" });

// ---- drive it --------------------------------------------------------------
const TEXT = "Hello world, this is a continuous batching test. The quick brown fox jumps over the lazy dog.";
let fail = 0;
const chk = (c, m) => { console.log((c ? "  ok   " : "  FAIL ") + m); if (!c) fail = 1; };

async function run(label, voice, speed) {
  for (const k of Object.keys(heard)) delete heard[k];
  heard.samples = 0; heard.buffers = 0; heard.firstAt = 0; heard.peak = 0; heard.rms2 = 0; heard.starts = [];
  $("text").value = TEXT; $("voice").value = voice; $("speed").value = String(speed);
  await sandbox.speak();
  const dur = heard.samples / 24000;
  const rms = Math.sqrt(heard.rms2 / Math.max(1, heard.samples));
  console.log(`  [${label}] ${heard.buffers} buffers, ${heard.samples} samples = ${dur.toFixed(3)} s, ` +
              `first at ${heard.firstAt} ms, peak=${heard.peak.toFixed(4)} rms=${rms.toFixed(4)}`);
  return { dur, rms, samples: heard.samples, peak: heard.peak };
}

const a = await run("default voice, 1.0x", "default", 1);
const logline = els["log"].children[0].textContent;
console.log(`  page's own summary line: ${logline}`);
chk(a.samples > 24000 * 2, `page heard more than 2 s of audio (${a.dur.toFixed(2)} s)`);
chk(a.peak > 0.05 && a.peak <= 1.0, `scheduled audio has real peaks (${a.peak.toFixed(3)})`);
chk(a.rms > 0.005, `scheduled audio is not silence (rms ${a.rms.toFixed(4)})`);
chk(a.firstAt === undefined && a.samples > 0, "stream completed without hanging");
chk(!/NaN/.test(logline), "the page's summary line has no NaN");
chk(/× real time/.test(logline), "the page computed a real-time factor");
chk(els["m-ttfa"].textContent !== "—", `first-audio readout filled in (${els["m-ttfa"].textContent})`);
chk(/\d/.test(els["m-rt"].textContent) && !/NaN/.test(els["m-rt"].textContent),
    `real-time readout is a number (${els["m-rt"].textContent})`);

// A voice picker that does not change the waveform is a no-op dropdown.
const b = await run("af_bella, 1.0x", "af_bella", 1);
chk(Math.abs(b.rms - a.rms) > 1e-4 || b.samples !== a.samples,
    `switching voice changed the audio (rms ${a.rms.toFixed(4)} -> ${b.rms.toFixed(4)})`);
// speed=2 must roughly halve the duration -- the knob has to reach the engine.
const c = await run("af_bella, 2.0x", "af_bella", 2);
chk(c.dur < a.dur * 0.75, `speed=2 shortened the audio (${a.dur.toFixed(2)} s -> ${c.dur.toFixed(2)} s)`);
console.log(fail ? "PAGE HARNESS FAIL" : "PAGE HARNESS PASS");
process.exit(fail);
