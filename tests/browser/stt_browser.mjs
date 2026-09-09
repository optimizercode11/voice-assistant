// Browser UI contract checks. Transcripts and microphone events are fixtures;
// this suite does not claim engine accuracy or physical microphone verification.
import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
// Playwright is deliberately NOT a dependency of this repo: point PLAYWRIGHT at any
// install whose chromium/webkit browsers are already downloaded, e.g.
//   PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node tests/browser/<name>.mjs
const {chromium,webkit} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const html = fs.readFileSync('web/index.html');
let requests = 0, health = true;
const server = http.createServer((req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  res.setHeader('Content-Type', path === '/' ? 'text/html' : 'application/json');
  if (path === '/') return res.end(html);
  if (path === '/voices') return res.end('{"voices":["default"]}');
  if (path === '/languages') return res.end('{"default":"a","languages":[{"code":"a","name":"English","default_voice":"default"}]}');
  if (path === '/stats') return res.end('{}');
  if (path === '/stt/health') return res.end(JSON.stringify({available:health}));
  if (path === '/stt') {
    requests++;
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      const body = Buffer.concat(chunks).toString();
      setTimeout(() => {
        if (body === 'error') {res.writeHead(503); return res.end('{"error":"Transcription is busy. Please try again shortly."}');}
        if (body === 'invalid') return res.end('{"wrong":"schema"}');
        res.end(JSON.stringify({text:body === 'empty' ? '' : 'Bonjour <script>世界</script>.'}));
      }, body === 'slow' ? 1200 : 80);
    });
    return;
  }
  res.writeHead(404); res.end('{}');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
try {
  for (const [name, type] of [['webkit', webkit], ['chromium', chromium]]) {
    const browser = await type.launch({headless:true, ...(name === 'chromium' ? {args:['--disable-gpu']} : {env:{...process.env, LIBGL_ALWAYS_SOFTWARE:'1', WEBKIT_DISABLE_COMPOSITING_MODE:'1'}})});
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(7000);
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      await page.addInitScript(() => {
        window.micState = {stops:0, mode:'ok', recordings:0};
        Object.defineProperty(navigator, 'mediaDevices', {value:{getUserMedia:async () => {
          if (micState.mode === 'denied') throw new DOMException('Denied', 'NotAllowedError');
          if (micState.mode === 'delayed') await new Promise(resolve => setTimeout(resolve, 400));
          return {getTracks:() => [{stop:() => micState.stops++}]};
        }}});
        window.MediaRecorder = class {
          constructor() {this.state = 'inactive'; this.mimeType = 'audio/webm';}
          start() {this.state = 'recording'; micState.recordings++;}
          stop() {this.state = 'inactive'; setTimeout(() => {
            this.ondataavailable({data:new Blob(['recorded'], {type:this.mimeType})}); this.onstop();
          }, 0);}
        };
      });
      await page.goto(base);
      await page.waitForFunction(() => !document.querySelector('#stt-record').disabled);
      assert.equal(await page.locator('#playback').inputValue(), name === 'webkit' ? 'native' : 'stream');
      const original = await page.locator('#text').inputValue();
      async function upload(body) {
        await page.locator('#stt-file').setInputFiles({name:'test.wav', mimeType:'audio/wav', buffer:Buffer.from(body)});
        await page.locator('#stt-upload').click();
      }
      await upload('ok');
      await page.waitForFunction(() => document.querySelector('#stt-status').textContent.startsWith('Transcript ready'));
      assert.equal(await page.locator('#stt-text').inputValue(), 'Bonjour <script>世界</script>.');
      assert.equal(await page.locator('#text').inputValue(), original, 'no automatic TTS overwrite');
      await page.locator('#stt-text').fill('Edited transcript.');
      await page.locator('#stt-use').click();
      assert.equal(await page.locator('#text').inputValue(), 'Edited transcript.');
      await upload('slow');
      await page.locator('#stt-cancel').click();
      await page.waitForTimeout(1400);
      assert.equal(await page.locator('#stt-text').inputValue(), 'Edited transcript.', 'late result cannot replace transcript');
      for (const body of ['error', 'invalid']) {
        await upload(body);
        await page.waitForFunction(() => document.querySelector('#stt-cancel').disabled);
        assert.match(await page.locator('#stt-status').textContent(), body === 'error' ? /busy/ : /no transcript/);
        assert.equal(await page.locator('#stt-text').inputValue(), 'Edited transcript.');
      }
      await upload('empty');
      await page.waitForFunction(() => document.querySelector('#stt-status').textContent.startsWith('No speech'));
      assert(await page.locator('#stt-use').isDisabled());
      await page.locator('#stt-record').click();
      await page.waitForFunction(() => !document.querySelector('#stt-finish').disabled);
      await page.locator('#stt-finish').click();
      await page.waitForFunction(() => document.querySelector('#stt-status').textContent.startsWith('Transcript ready'));
      assert((await page.evaluate(() => micState.stops)) > 0, 'microphone tracks released');
      const before = requests;
      await page.locator('#stt-record').click();
      await page.locator('#stt-cancel').click();
      await page.waitForTimeout(150);
      assert.equal(requests, before, 'cancelled recording is never uploaded');
      await page.evaluate(() => {micState.mode = 'denied';});
      await page.locator('#stt-record').click();
      await page.waitForFunction(() => document.querySelector('#stt-status').textContent.includes('permission denied'));
      await page.evaluate(() => {micState.mode = 'delayed';});
      const stops = await page.evaluate(() => micState.stops);
      await page.locator('#stt-record').click();
      await page.locator('#stt-cancel').click();
      await page.waitForTimeout(550);
      assert.equal(await page.evaluate(() => micState.stops), stops + 1, 'late microphone permission releases tracks');
      assert.equal(requests, before);
      health = false;
      await page.reload();
      await page.waitForFunction(() => document.querySelector('#stt-status').textContent.includes('unavailable'));
      assert(await page.locator('#stt-record').isDisabled());
      assert(await page.locator('#speak').isEnabled(), 'TTS remains available when STT is offline');
      health = true;
      const insecure = await browser.newPage();
      await insecure.addInitScript(() => Object.defineProperty(navigator, 'mediaDevices', {value:undefined}));
      await insecure.goto(base);
      await insecure.waitForFunction(() => document.querySelector('#stt-status').textContent.startsWith('Ready'));
      assert(await insecure.locator('#stt-record').isDisabled());
      assert.match(await insecure.locator('#stt-hint').textContent(), /HTTPS or localhost/);
      await insecure.locator('#stt-file').setInputFiles({name:'test.wav', mimeType:'audio/wav', buffer:Buffer.from('ok')});
      assert(await insecure.locator('#stt-upload').isEnabled(), 'file upload survives unavailable microphone');
      await insecure.close();
      assert.deepEqual(errors, []);
      fs.mkdirSync('evidence/browser', {recursive:true});
      await page.screenshot({path:`evidence/browser/${name}-studio.png`, fullPage:true});
      console.log(`PASS ${name}: upload, editing, cancellation, recording lifecycle, permission failures, unavailable service, Safari default`);
    } finally {await browser.close();}
  }
} finally {server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
