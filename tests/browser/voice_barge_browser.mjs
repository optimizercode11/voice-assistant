import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Barge-in needs an echo reference: the page must know how loud IT is being in
// order to tell a person talking over it from its own reply. That reference is
// the WebAudio tap built for the orb glow (createMediaElementSource), and
// headless Chromium never lets it reach 'running', so the tap is null here.
//
// Which means this suite cannot assert "a person interrupts the reply" -- that
// needs a real browser and a real room. What it CAN assert, and what actually
// protects the user, is the failure path: with no echo reference the page must
// REFUSE barge-in, say why, and keep the microphone muted for the whole reply.
// Arming a loudness trigger and calling it barge-in is the bug; refusing is the
// correct behaviour. The gate's own arithmetic is covered by barge_gate_test.mjs.
//   PLAYWRIGHT=... node tests/browser/voice_barge_browser.mjs [--sabotage]
const sabotage = process.argv.includes('--sabotage');
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav = fs.readFileSync('tests/fixtures/microphone.wav');
const server = http.createServer((req,res)=>{
  const file = req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
  if(!file){res.writeHead(404);res.end();return;}
  let body = fs.readFileSync(file,'utf8');
  // The paired sabotage: arm anyway with no reference signal. The microphone
  // then stays open during the reply with nothing to judge it against.
  if(sabotage && file.endsWith('.js')) {
    // Anchor on the comment, not just the condition: 'if (!playbackAnalyser)'
    // appears three times in this file and String.replace rewrites the FIRST
    // one, which is in armGlow -- sabotaging it leaves the real guard untouched
    // and the suite green for the wrong reason.
    const armed = body.replace('if (!playbackAnalyser) {\n    // Without the glow',
                               'if (false) {\n    // Without the glow');
    if(armed === body) throw new Error('the sabotage anchor no longer matches web/chat.js');
    body = armed;
  }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');
  res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try {
  const browser = await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture.wav')}`]});
  try {
    const context = await browser.newContext();
    await context.grantPermissions(['microphone']);
    await context.addInitScript(()=>{
      window.observedTracks=[];
      if(navigator.mediaDevices?.getUserMedia){
        const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia=async(...args)=>{const s=await original(...args);window.observedTracks.push(...s.getTracks());return s;};
      }
    });
    const page = await context.newPage(); const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
    page.setDefaultTimeout(20000);
    await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
    await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
    await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart']}}));
    await page.route('**/stt',r=>r.fulfill({json:{text:'What is a fox?',turn:{complete:true,hold:false,words:3}}}));
    await page.route('**/chat/completions',r=>r.fulfill({json:{text:'A fox is a small mammal.'}}));
    await page.route('**/tts?*',r=>r.fulfill({contentType:'audio/wav',body:wav}));
    await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
    await page.waitForFunction(()=>!document.querySelector('#send').disabled);

    // Barge-in is ON by default -- the user asked for it. The refusal below is
    // about this device, not about the setting.
    assert.equal(await page.locator('#barge-in').isChecked(), true, 'barge-in is on unless switched off');
    await page.locator('#start').click();
    await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',null,{timeout:20000});

    const state = await page.evaluate(()=>({
      note: document.querySelector('#barge-note').textContent,
      micOpen: window.observedTracks.some(t=>t.enabled),
      echoCancellation: window.observedTracks[0]?.getSettings?.()?.echoCancellation ?? null,
      autoGainControl: window.observedTracks[0]?.getSettings?.()?.autoGainControl ?? null,
    }));
    // The premise the whole feature rests on, measured rather than assumed.
    assert.equal(state.echoCancellation, true,
      'the browser reports AEC3 engaged on this capture device');
    // AGC is turned off when barge-in is wanted: it pumps the gain up the instant
    // a reply stops, which is exactly when residual echo comes back.
    assert.equal(state.autoGainControl, false,
      'auto gain control is off so the reply tail is not amplified into the mic');
    // The safety property: no reference -> no open microphone.
    assert.match(state.note, /Unavailable/i, `it says why it refused (${state.note})`);
    assert.equal(state.micOpen, false,
      'with no echo reference the microphone stays muted for the whole reply');
    assert.deepEqual(errors, []);

    if (sabotage) {
      console.error('SABOTAGE PASSED (this is the failure): barge-in armed with no echo reference');
      process.exitCode = 1;
    } else {
      fs.mkdirSync('evidence/browser',{recursive:true});
      await page.screenshot({path:'evidence/browser/barge.png',fullPage:true});
      console.log('PASS chromium: AEC3 confirmed, AGC off, and barge-in refuses rather than guess');
    }
  } finally { await browser.close(); }
} finally { await new Promise(resolve=>server.close(resolve)); }
