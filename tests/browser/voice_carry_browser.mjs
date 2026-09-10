import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// The carry-forward test: someone says "I", pauses for breath, then says "want
// to go to the museum".  The endpointer cuts that into two clips, and before the
// fix the page answered the first one on its own -- a confident reply to a
// question nobody asked -- and then answered the second one too.
//
// This drives a REAL microphone (Chromium's fake capture device fed by
// tests/fixtures/capture-two-part.wav: real speech, a 1.8 s pause, real speech)
// through the REAL page, and asserts the two halves reach the model as ONE
// sentence.  Nothing here is stubbed except the network.
//
//   PLAYWRIGHT=/path/to/playwright/index.mjs node tests/browser/voice_carry_browser.mjs
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const sabotage=process.argv.includes('--sabotage');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');

// The hold verdict comes from the REAL tools/turn_control.py.  A suite that
// re-implemented the server's rules in JavaScript would keep passing while the
// server was broken, which is the exact shape of the bug this file exists for.
function verdict(text,voicedMs){
  const out=execFileSync('python3',['-c',
    'import sys,json;sys.path.insert(0,"tools");import turn_control;'+
    'print(json.dumps(turn_control.completeness(sys.argv[1],'+
    'int(sys.argv[2]) if sys.argv[2] else None)))',text,voicedMs??''],
    {cwd:path.resolve('.'),env:{...process.env,CUDA_VISIBLE_DEVICES:''}});
  return JSON.parse(out.toString());
}

// What the ASR would return for each clip.  The voiced durations are asserted
// against the fixture, never fed back in: the page must measure them itself.
const CLIPS=['I.','want to go to the museum.'];
const server=http.createServer((req,res)=>{
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 // The paired sabotage: keep the hold, throw the words away.  This is the
 // regression itself, and this suite must go red for it.
 if(sabotage&&file.endsWith('.js'))
   body=body.replaceAll("heldText = (carried ? carried + ' ' : '') + text;","heldText = '';");
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
 const browser=await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture-two-part.wav')}`]});
 try{
  const context=await browser.newContext();
  await context.grantPermissions(['microphone']);
  const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  page.setDefaultTimeout(25000);
  let heard=[],posts=[];
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',async route=>{
   const voiced=route.request().headers()['x-voiced-ms'];
   const index=heard.length;
   heard.push({voiced,bytes:route.request().postDataBuffer().length});
   const text=CLIPS[Math.min(index,CLIPS.length-1)];
   await route.fulfill({json:{text,raw_text:text,audio_seconds:1.6,chunks:1,turn:verdict(text,voiced)}});
  });
  await page.route('**/chat/completions',async route=>{posts.push(route.request().postDataJSON());
   await route.fulfill({json:{text:'Museums are wonderful.'}}).catch(()=>{});});
  await page.route('**/tts?*',route=>route.fulfill({contentType:'audio/wav',body:wav}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.locator('#start').click();

  // Half a sentence: the page must hold it and say so, not answer it.
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('I heard'),
    null,{timeout:20000});
  const held=await page.locator('#status').textContent();
  assert.equal(heard.length,1,'the fragment was uploaded as its own clip');
  assert.equal(posts.length,0,'a held fragment is never dispatched as a turn');
  assert.ok(held.includes('I'),`the hold names what it caught (${held})`);

  // The rest of the sentence arrives, and the two halves go out together.
  await page.waitForFunction(()=>document.querySelector('#messages .user'),null,{timeout:20000});
  // posts lives in Node, not in the page, so this waits here rather than in a
  // waitForFunction that would look up a Node binding inside the browser.
  const dispatched=Date.now();
  while(!posts.length&&Date.now()-dispatched<20000) await page.waitForTimeout(50);
  assert.ok(posts.length,'the sentence was never sent to the model');
  const said=posts[0].messages.at(-1).content;
  // No full stop after "I": the fragment was judged unfinished, so the period
  // qasr put on it is dropped when the halves are joined.
  assert.equal(said,'I want to go to the museum.',
    'the pause split the recording, not the sentence');
  assert.equal(heard.length,2,'the held fragment cost one extra clip, not a retry loop');
  assert.equal(posts.length,1,'one sentence is one turn');

  // The header is the whole mechanism, so check it is a measurement and not a
  // constant: the second burst is twice as long as the first, and the first is
  // far below the 2500 ms hold line while the clip itself is over 1500 ms long.
  const [first,second]=heard.map(h=>Number(h.voiced));
  assert.ok(Number.isFinite(first)&&Number.isFinite(second),`both clips carried X-Voiced-Ms (${heard})`);
  assert.ok(first>=100&&first<2500,`the fragment measured short (${first} ms)`);
  assert.ok(second>first*1.5,`the second burst measured longer (${first} -> ${second} ms)`);
  assert.ok(heard.every(h=>h.bytes>1000),'both uploads carried real audio');

  await page.locator('#end').click();
  assert.deepEqual(errors,[]);
  if(sabotage){console.error('SABOTAGE PASSED (this is the failure): the carry-forward is load-bearing');
   process.exitCode=1;}
  else{fs.mkdirSync('evidence/browser',{recursive:true});
   await page.screenshot({path:'evidence/browser/carry.png',fullPage:true});
   console.log('PASS chromium: real two-clip utterance held, carried and dispatched as one sentence');}
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
