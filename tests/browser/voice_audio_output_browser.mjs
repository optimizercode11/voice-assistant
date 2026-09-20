import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';

assert.equal(process.env.CUDA_VISIBLE_DEVICES, '', 'browser validation is CPU-only');
const {chromium} = await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
// Unlike completion-only playback tests, use a nonzero waveform and measure the
// application's actual output analyser while a real browser renders it.
const rate = 24000, samples = rate, wav = Buffer.alloc(44 + samples * 2);
wav.write('RIFF'); wav.writeUInt32LE(36 + samples * 2, 4); wav.write('WAVE', 8);
wav.write('fmt ', 12); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20);
wav.writeUInt16LE(1, 22); wav.writeUInt32LE(rate, 24); wav.writeUInt32LE(rate * 2, 28);
wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36);
wav.writeUInt32LE(samples * 2, 40);
for (let i = 0; i < samples; i++) wav.writeInt16LE(Math.round(9000 * Math.sin(i * 2 * Math.PI * 440 / rate)), 44 + i * 2);
let useStreaming = false;
const server = http.createServer((req, res) => {
  const path = req.url.split('?')[0];
  if (path === '/chat' || path === '/chat.js') {
    res.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : 'text/html');
    return res.end(fs.readFileSync(path === '/chat' ? 'web/chat.html' : 'web/chat.js'));
  }
  const json = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
  if (path === '/chat/health') return json({available:true, events:true, streaming:useStreaming, tools:useStreaming ? ['now'] : [], tool_sources:{}});
  if (path === '/languages') return json({default:'a', languages:[{code:'a', name:'English (US)'}]});
  if (path === '/voices') return json({voices:['af_bella']});
  if (path === '/approvals') return json({enabled:false, requests:[], grants:[]});
  if (path === '/tts') { req.resume(); res.setHeader('Content-Type', 'audio/wav'); return res.end(wav); }
  if (path === '/chat/completions') {
    req.resume();
    if (useStreaming) {
      res.setHeader('Content-Type', 'application/x-ndjson');
      res.write(JSON.stringify({type:'delta', round:1, text:'You should hear this reply.'}) + '\n');
      return res.end(JSON.stringify({type:'answer', text:'You should hear this reply.', usage:{}, tools:[], sources:[]}) + '\n');
    }
    return json({text:'You should hear this reply.', usage:{}, tools:[], sources:[]});
  }
  res.writeHead(404); res.end();
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({headless:true, args:['--disable-gpu']});
try {
  for (const mode of ['element', 'gapless']) {
    useStreaming = mode === 'gapless';
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.addInitScript(() => {
      window.__eventConnections = [];
      window.EventSource = class extends EventTarget {
        constructor(url) { super(); this.url = url; this.closed = false; window.__eventConnections.push(this); }
        close() { this.closed = true; }
      };
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
    await page.waitForFunction(() => !document.getElementById('send').disabled);
    assert.deepEqual(await page.evaluate(() => window.__eventConnections.map(connection => connection.closed)), [false]);
    const measure = async label => {
      await page.locator('#text').fill('Say a short reply.');
      await page.locator('#send').click();
      let peakRms = 0;
      for (let i = 0; i < 40; i++) {
        const rms = await page.evaluate(() => {
          if (!playbackAnalyser || context?.state !== 'running') return 0;
          const signal = new Float32Array(playbackAnalyser.fftSize);
          playbackAnalyser.getFloatTimeDomainData(signal);
          return Math.sqrt(signal.reduce((sum, sample) => sum + sample * sample, 0) / signal.length);
        });
        peakRms = Math.max(peakRms, rms);
        if (peakRms > .1) break;
        await page.waitForTimeout(50);
      }
      const state = await page.evaluate(() => ({context:context?.state ?? null, graph:!!playbackAnalyser, elementTime:player.currentTime, playerPaused:player.paused, stats:window.__speechStats, status:document.getElementById('status').textContent}));
      console.log(JSON.stringify({mode, label, peakRms, ...state}));
      assert.ok(peakRms > .1, `${mode} ${label}: rendered output must contain the nonzero test signal`);
      await page.waitForFunction(() => document.getElementById('state').textContent === 'Ready');
    };
    await measure('before cached-page restore');
    await page.evaluate(() => {
      window.__beforeCacheContext = context;
      // Exercise the browser lifecycle deterministically, without requiring
      // Chromium to admit a particular page to its heuristic BFCache policy.
      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
    });
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__eventConnections.map(connection => connection.closed)), [true], 'leaving the page closes its events connection');
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true})));
    assert.deepEqual(await page.evaluate(() => window.__eventConnections.map(connection => connection.closed)), [true, false], 'restoring the page reconnects events');
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true})));
    assert.deepEqual(await page.evaluate(() => window.__eventConnections.map(connection => connection.closed)), [true, false], 'a repeated pageshow does not duplicate event connections');
    await measure('after cached-page restore');
    assert.equal(await page.evaluate(() => context === window.__beforeCacheContext), true, 'a cached page preserves its media element/context pairing');
    assert.deepEqual(errors, []);
    if (mode === 'gapless') assert.ok(await page.evaluate(() => window.__speechStats.gapless >= 2), 'both streamed replies used gapless playback');
    await page.close();
  }
  console.log('BROWSER AUDIO OUTPUT PASS: nonzero element and gapless output survives cached-page restoration');
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
