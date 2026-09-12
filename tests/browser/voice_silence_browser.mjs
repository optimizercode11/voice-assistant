// The silence timeout (2026-09-12): nobody speaks for N seconds and the
// microphone closes -- the page says Paused, the tracks are disabled, no clip is
// uploaded -- but being asleep is not being told to be quiet: an agent's
// finished update is still spoken, and listening returns after it.  Resume or
// Space also wake it; 0 keeps listening.
//
// Sabotage: the line that closes the microphone is removed; the page must then
// stay Listening forever and the first assertion goes red.
//
//   PLAYWRIGHT=/path/to/playwright/index.mjs node tests/browser/voice_silence_browser.mjs [--sabotage]
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
const sabotage=process.argv.includes('--sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser silence';
const {chromium} = await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0,pushed=0;const listeners=[];
function push(event){pushed++;for(const res of listeners)res.write(`id: ${pushed}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);}
const server=http.createServer((req,res)=>{
 if(req.url==='/events'){res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});res.write('retry: 2000\n\n');listeners.push(res);req.on('close',()=>{const at=listeners.indexOf(res);if(at>=0)listeners.splice(at,1);});return;}
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 if(sabotage&&file.endsWith('.js')){
   const anchor="{ asleep = true; rec.stop(); return; }";
   assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
   body=body.replace(anchor,"{ /* sabotage: silence is ignored */ }");mutations++;
 }
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
 const browser=await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/silence.wav')}`]});
 try{
  const context=await browser.newContext();
  await context.grantPermissions(['microphone']);
  await context.addInitScript(()=>{
    window.observedTracks=[];
    if(navigator.mediaDevices?.getUserMedia){const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);navigator.mediaDevices.getUserMedia=async(...args)=>{const s=await original(...args);window.observedTracks.push(...s.getTracks());return s;};}
  });
  const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  page.setDefaultTimeout(30000);
  let uploads=0;const tts=[];
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true,streaming:true,events:true,tools:['now','mcp__claude__send','mcp__claude__updates']}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',route=>{uploads++;return route.fulfill({json:{text:'',raw_text:'',audio_seconds:1,chunks:1,turn:{complete:false,hold:false,reason:'test',extra_silence_ms:0}}});});
  await page.route('**/chat/completions',route=>route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'answer',text:'Typed reply.',usage:{},tools:[],sources:[],controls:{}})+'\n'}));
  await page.route('**/tts?*',route=>{tts.push(route.request().postData()||'');return route.fulfill({contentType:'audio/wav',body:wav});});
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  const deadline=Date.now()+10000;while(!listeners.length&&Date.now()<deadline)await page.waitForTimeout(100);
  assert.equal(await page.locator('#silence-timeout').inputValue(),'5','the shipped default is five seconds');
  await page.fill('#silence-timeout','2');await page.dispatchEvent('#silence-timeout','change');
  await page.uncheck('#wake-enabled');
  await page.locator('#start').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening');
  // 1. Silence: within the timeout plus a margin the microphone closes.
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Paused',null,{timeout:7000})
    .catch(()=>assert.fail('two seconds of silence must close the microphone'));
  assert.match(await page.locator('#status').textContent(),/Quiet for 2 s, so I stopped listening/);
  assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'the tracks are disabled while asleep');
  assert.equal(await page.locator('#resume-listening').isVisible(),true,'the way back is a button');
  assert.equal(uploads,0,'silence is never uploaded');
  // 2. An agent finishes: the update is spoken even though the page is asleep,
  //    and listening returns after it.
  push({type:'working',server:'claude',instruction:'run the tests'});
  push({type:'update',server:'claude',spoken:'The tests pass and nothing else changed.',instruction:'run the tests',is_error:false,seconds:9,activity:{commands:1,files_edited:0,files_read:0,other_tools:0}});
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:30000})
    .catch(()=>assert.fail('after the update is spoken, listening must return'));
  assert.equal(tts.filter(t=>t.includes('The tests pass')).length,1,'the update was spoken once while asleep');
  // 3. ... and silence closes it again.
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Paused',null,{timeout:7000});
  // 4. Space wakes it, like a pause.
  await page.locator('h1').click();await page.keyboard.press('Space');
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening');
  // 5. A typed message while asleep: the reply is spoken and listening returns.
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Paused',null,{timeout:7000});
  await page.fill('#text','hello');await page.locator('#send').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:30000});
  assert.ok(tts.some(t=>t.includes('Typed reply')),'the typed reply was spoken');
  // 6. Zero keeps listening.
  await page.fill('#silence-timeout','0');await page.dispatchEvent('#silence-timeout','change');
  await page.waitForTimeout(4000);
  assert.equal(await page.locator('#state').textContent(),'Listening','0 means the microphone stays open');
  assert.deepEqual(errors,[]);
  if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
  console.log(assertionsComplete);
  if(sabotage){console.error('SABOTAGE PASSED (this is the failure): silence never closed the microphone');process.exitCode=0;}
  else{fs.mkdirSync('evidence/browser',{recursive:true});await page.screenshot({path:'evidence/browser/silence.png',fullPage:true});
   console.log('PASS chromium: silence closes the microphone, an update or a reply reopens it, 0 keeps it open');}
 }finally{await browser.close();}
}finally{for(const res of listeners)res.end();await new Promise(resolve=>server.close(resolve));}
