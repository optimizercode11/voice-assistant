import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Playwright is deliberately NOT a dependency of this repo: point PLAYWRIGHT at any
// install whose chromium/webkit browsers are already downloaded, e.g.
//   PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node tests/browser/<name>.mjs
const {chromium,webkit} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const sabotage=process.argv.includes('--sabotage');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
const server=http.createServer((req,res)=>{
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 if(sabotage&&file.endsWith('.js'))body=body.replaceAll('track.enabled = false','track.enabled = true').replaceAll('track.enabled=false','track.enabled=true');
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
for(const [name,type] of (sabotage?[['chromium',chromium]]:[['chromium',chromium],['webkit',webkit]])){
 const browser=await type.launch({headless:true,args:name==='chromium'?['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture.wav')}`]:[],...(name==='webkit'?{env:{...process.env,LIBGL_ALWAYS_SOFTWARE:'1',WEBKIT_DISABLE_COMPOSITING_MODE:'1'}}:{})});
 try{
 const context=await browser.newContext();
 if(name==='chromium')await context.grantPermissions(['microphone']);
 await context.addInitScript(()=>{
   window.observedTracks=[];
   if(navigator.mediaDevices?.getUserMedia){const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);navigator.mediaDevices.getUserMedia=async(...args)=>{const stream=await original(...args);window.observedTracks.push(...stream.getTracks());return stream;};}
 });
 const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(String(error)));
 page.setDefaultTimeout(15000);let posts=[],stt=0,slow=false,release=null;
 await page.route('**/chat/health',route=>route.fulfill({json:{available:true}}));
 await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
 await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
 await page.route('**/stt',route=>{stt++;assert(route.request().postDataBuffer().length>(stt===1?1000:0));return route.fulfill({json:{text:'What is a fox?'}});});
 await page.route('**/chat/completions',async route=>{posts.push(route.request().postDataJSON());if(slow)await new Promise(resolve=>{release=resolve;});await route.fulfill({json:{text:'Hello from Qwen.'}}).catch(()=>{});});
 await page.route('**/tts?*',route=>route.fulfill({contentType:'audio/wav',body:wav}));
 await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 // This suite guards the OLD guarantee: with barge-in switched off the
 // microphone is physically muted for the whole reply, so the assistant
 // cannot hear itself no matter what the room does. voice_barge_browser.mjs
 // covers the opposite mode.
 await page.uncheck('#barge-in');
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};
 await send('Remember <b>orchid</b>.');
 await page.waitForFunction(()=>document.querySelector('#player').currentTime>.15&&!document.querySelector('#player').paused);
 assert.equal(await page.locator('#messages b').count(),0,'chat text is never injected as HTML');
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 await send('What did I say?');
 await page.waitForFunction(()=>document.querySelector('#player').currentTime>.15&&!document.querySelector('#player').paused);
 assert.equal(posts[1].messages.length,3);assert.equal(posts[1].messages[0].content,'Remember <b>orchid</b>.');
 await page.locator('#interrupt').click();await page.locator('#new').click();
 assert.equal(await page.locator('.message').count(),0);
 if(name==='chromium'){
   await page.fill('#silence-timeout','0');   // the recording has a 10.6 s quiet gap; the silence timeout (default 5 s) must not close the microphone under this suite
   await page.locator('#start').click();
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',{}, {timeout:20000});
   assert.equal(stt,1,'silence detection automatically submitted one real MediaRecorder upload');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'microphone is disabled during the spoken reply');
   assert.equal(posts.at(-1).messages.length,1,'new conversation excludes old history');
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening');
   await page.locator('#player').evaluate(a=>a.play());
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'replaying old audio also disables capture');
   await page.locator('#interrupt').click();
   await page.waitForTimeout(400);
   await page.locator('#finish').click();
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking');
   assert.equal(stt,2,'Send now submits even when speech is below the VAD threshold');
   await page.locator('#end').click();
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>t.readyState==='ended')),true);
 }
 slow=true;const before=await page.locator('.assistant').count();await send('A delayed reply');
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Thinking');
 while(!release)await page.waitForTimeout(20);
 await page.locator('#end').click();release();await page.waitForTimeout(200);
 assert.equal(await page.locator('.assistant').count(),before,'stale response never reaches the conversation');
 assert.deepEqual(errors,[]);
 fs.mkdirSync('evidence/browser',{recursive:true});
 await page.screenshot({path:`evidence/browser/${name}-chat.png`,fullPage:true});
 console.log(`PASS ${name}: native playback, history, safe text, new chat, cancellation${name==='chromium'?', real MediaRecorder/VAD, automatic relisten, microphone isolation':''}`);
 }finally{await browser.close();}
}
}finally{await new Promise(resolve=>server.close(resolve));}
