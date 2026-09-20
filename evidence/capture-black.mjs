// Real page and microphone, deterministic STT verdicts: a held turn must finish
// even when there is no next clip, without losing continuations or wake gating.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
const {chromium} = await import(process.env.PLAYWRIGHT);
const sabotage = process.argv.includes('--sabotage');
const server = http.createServer((req,res) => {
  const file = req.url === '/chat' ? 'web/chat.html' : req.url === '/chat.js' ? 'web/chat.js' : null;
  if (!file) { res.writeHead(404); res.end(); return; }
  let body = fs.readFileSync(file,'utf8');
  if (sabotage && file.endsWith('.js')) {
    const anchor = 'heldFlushTimer = setTimeout(checkHeld, 2200);';
    assert.ok(body.includes(anchor), 'sabotage anchor matches');
    body = body.replace(anchor, '/* sabotage: no quiet flush */');
  }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html'); res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser = await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/silence.wav')}`]});
try {
  const ctx = await browser.newContext(); await ctx.grantPermissions(['microphone']);
  const page = await ctx.newPage(); page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror',e=>errors.push(String(e)));
  const posts = []; let heard = 'Maybe.', hold = true, discard = false, failReply = false;
  await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
  await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English'}]}}));
  await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',r=>r.fulfill({json:{text:heard,turn:{hold,discard}}}));
  await page.route('**/chat/completions',r=>{posts.push(r.request().postDataJSON()); return failReply ? r.fulfill({status:503,json:{error:'Try this turn again.'}}) : r.fulfill({json:{text:'Understood.'}});});
  await page.route('**/tts?*',r=>r.fulfill({contentType:'audio/wav',body:fs.readFileSync('tests/fixtures/microphone.wav')}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.screenshot({path:'evidence/black-theme.png',fullPage:true});
  console.log('Black theme screenshot saved');
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
