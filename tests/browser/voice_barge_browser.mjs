import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Barge-in: the microphone stays live while the assistant speaks, and the page
// must tell the user's voice apart from its own.
//
// This suite used to assert the REFUSAL path, on the theory that headless
// Chromium never lets the AudioContext reach 'running' and so never builds the
// WebAudio tap that is the echo reference. That theory was wrong, and it was
// hiding a real defect: armGlow() asked `player.createMediaElementSource`, but
// createMediaElementSource lives on the AudioContext, not on the media element.
// The guard failed in every browser, so the tap never existed, the orb never
// glowed while the assistant spoke, and barge-in refused everywhere -- and this
// file graded that as correct behaviour.
//
// So the property under test is now the one the user asked for:
//   * a device that reports echo cancellation gets a live microphone during the
//     reply, and the reply's own level is being measured (the glow proves the
//     same tap that the gate reads is real);
//   * a device that reports NO cancellation is refused outright and keeps the
//     microphone muted, because an uncancelled reply is a person the assistant
//     imagines it can hear.
// The paired sabotage makes bargeAvailable() trust any device: the refusal run
// must catch it.
//   PLAYWRIGHT=... node tests/browser/voice_barge_browser.mjs [--sabotage|--dead-sabotage]
const sabotage = process.argv.includes('--sabotage');
const deadSabotage = process.argv.includes('--dead-sabotage');
const assertionsComplete = 'ASSERTIONS COMPLETE: browser barge';
// Both flags run the child through the real sabotage exit path with a no-op.
if (deadSabotage && !sabotage) {
  const run = spawnSync(process.execPath, [process.argv[1], '--sabotage', '--dead-sabotage'],
    {encoding: 'utf8', timeout: 180000});
  process.stdout.write(run.stdout ?? '');
  process.stderr.write(run.stderr ?? '');
  assert.ifError(run.error);
  assert.equal(run.signal, null, 'dead sabotage must finish normally');
  assert.equal(run.status, 0, 'an uncaught sabotage must exit zero so make rejects it');
  assert.ok(run.stdout.split('\n').includes(assertionsComplete), 'the browser assertions must finish');
  console.log('DEAD SABOTAGE PASS: no-op mutation escaped after the browser assertions ran');
  process.exit(0);
}
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav = fs.readFileSync('tests/fixtures/microphone.wav');
let mutations = 0;
const server = http.createServer((req,res)=>{
  const file = req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
  if(!file){res.writeHead(404);res.end();return;}
  let body = fs.readFileSync(file,'utf8');
  // The paired sabotage: trust whatever the device says nothing about. A
  // Bluetooth pair reports echoCancellation:false and means it; ignoring that
  // answer is how the assistant ends up interrupting itself.
  if(sabotage && file.endsWith('.js')) {
    const anchor = 'return settings ? settings.echoCancellation === true : false;';
    assert.ok(body.includes(anchor), 'the sabotage anchor no longer matches web/chat.js');
    const armed = body.replace(anchor,
      deadSabotage ? anchor : 'return true; // paired sabotage: trust any device');
    if (deadSabotage) assert.equal(armed, body, 'dead sabotage must leave the page unchanged');
    body = armed;
    mutations++;
  }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');
  res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));

// One conversation, observed while the reply is playing. `noAec` rewrites what
// the capture device reports, which is the only way to get a Bluetooth-shaped
// answer out of a fake microphone.
async function observe(browser, {noAec}) {
  const context = await browser.newContext();
  await context.grantPermissions(['microphone']);
  await context.addInitScript((force) => {
    window.observedTracks = [];
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (...args) => {
      const s = await original(...args);
      window.observedTracks.push(...s.getTracks());
      return s;
    };
    if (force) {
      const settings = MediaStreamTrack.prototype.getSettings;
      MediaStreamTrack.prototype.getSettings = function () {
        const reported = settings.call(this);
        if (this.kind === 'audio') { reported.echoCancellation = false; reported.autoGainControl = false; }
        return reported;
      };
    }
    window.glowSeen = 0;
    const sample = () => {
      const orb = document.querySelector('#orb');
      if (orb && document.querySelector('#state').textContent === 'Speaking') {
        window.glowSeen = Math.max(window.glowSeen, Number(orb.style.getPropertyValue('--glow')) || 0);
      }
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, noAec);
  const page = await context.newPage(); const errors = []; page.on('pageerror', e => errors.push(String(e)));
  page.setDefaultTimeout(20000);
  await page.route('**/chat/health', r => r.fulfill({json:{available:true}}));
  await page.route('**/languages', r => r.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices', r => r.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt', r => r.fulfill({json:{text:'What is a fox?',turn:{complete:true,hold:false,words:3}}}));
  await page.route('**/chat/completions', r => r.fulfill({json:{text:'A fox is a small mammal.'}}));
  await page.route('**/tts?*', r => r.fulfill({contentType:'audio/wav', body:wav}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.fill('#silence-timeout','0');   // the recording has a 10.6 s quiet gap; the silence timeout (default 5 s) must not close the microphone under this suite
  await page.locator('#start').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',null,{timeout:20000});
  // Let the reply actually render a few frames before reading the meter.
  await page.waitForTimeout(600);
  const seen = await page.evaluate(()=>({
    note: document.querySelector('#barge-note').textContent,
    micOpen: window.observedTracks.some(t=>t.enabled),
    echoCancellation: window.observedTracks[0]?.getSettings?.()?.echoCancellation ?? null,
    autoGainControl: window.observedTracks[0]?.getSettings?.()?.autoGainControl ?? null,
    glow: window.glowSeen,
  }));
  await page.locator('#end').click();
  await context.close();
  return {...seen, errors};
}

try {
  const browser = await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--autoplay-policy=no-user-gesture-required',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture.wav')}`]});
  try {
    // Barge-in is ON by default -- the user asked for it.
    {
      const page = await browser.newPage();
      await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
      await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
      await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart']}}));
      await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
      await page.waitForFunction(()=>!document.querySelector('#send').disabled);
      assert.equal(await page.locator('#barge-in').isChecked(), true, 'barge-in is on unless switched off');
      await page.close();
    }

    // -- 1. a device that cancels its own echo gets to interrupt -------------
    const cancelled = await observe(browser, {noAec:false});
    assert.deepEqual(cancelled.errors, [], 'the page must not throw while watching');
    assert.equal(cancelled.echoCancellation, true, 'the fake capture reports AEC engaged');
    // AGC is requested off when barge-in is wanted: it pumps the gain the instant
    // a reply stops, which is exactly when residual echo comes back.
    assert.equal(cancelled.autoGainControl, false, 'auto gain control is off during barge-in');
    assert.doesNotMatch(cancelled.note, /Unavailable/i, `it does not refuse a device that can cancel (${cancelled.note})`);
    assert.equal(cancelled.micOpen, true, 'the microphone stays live for the whole reply');
    // The gate and the glow read the same tap. A meter that never moves means
    // there is no reference signal, and a gate with no reference is a loudness
    // trigger wearing a security costume.
    assert.ok(cancelled.glow > 0, `the reply is measured while it plays (glow peaked at ${cancelled.glow})`);

    // -- 2. a device that does NOT cancel is refused, not guessed at ---------
    const raw = await observe(browser, {noAec:true});
    assert.deepEqual(raw.errors, [], 'the refusal path must not throw');
    assert.equal(raw.echoCancellation, false, 'the device says it is not cancelling');
    assert.match(raw.note, /no echo cancellation/i, `it says why it refused (${raw.note})`);
    assert.equal(raw.micOpen, false, 'with no cancellation the microphone stays muted for the whole reply');

    if (sabotage) assert.ok(mutations > 0, 'the page must load the sabotage replacement');
    console.log(assertionsComplete);
    if (sabotage) {
      console.error('SABOTAGE PASSED (this is the failure): barge-in armed on a device that cannot cancel its echo');
      // The assertions passed: exit zero so make sabotage rejects this arm.
      process.exitCode = 0;
    } else {
      fs.mkdirSync('evidence/browser',{recursive:true});
      console.log(`PASS chromium: AEC device keeps the microphone live (glow peaked at ${cancelled.glow.toFixed(4)}), and a device without cancellation is refused`);
    }
  } finally { await browser.close(); }
} finally { await new Promise(resolve=>server.close(resolve)); }
