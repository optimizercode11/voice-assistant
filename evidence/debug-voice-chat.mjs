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
 console.log('completed line 20');
try{
for(const [name,type] of [['webkit',webkit]]){
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
 console.log('completed line 33');
 await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
 console.log('completed line 34');
 await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
 console.log('completed line 35');
 await page.route('**/stt',route=>{stt++;assert(route.request().postDataBuffer().length>(stt===1?1000:0));return route.fulfill({json:{text:'What is a fox?'}});});
 console.log('completed line 36');
 await page.route('**/chat/completions',async route=>{posts.push(route.request().postDataJSON());if(slow)await new Promise(resolve=>{release=resolve;});await route.fulfill({json:{text:'Hello from Qwen.'}}).catch(()=>{});});
 console.log('completed line 37');
 await page.route('**/tts?*',route=>route.fulfill({contentType:'audio/wav',body:wav}));
 console.log('completed line 38');
 await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
 console.log('completed line 39');
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 console.log('completed line 40');
 // This suite guards the OLD guarantee: with barge-in switched off the
 // microphone is physically muted for the whole reply, so the assistant
 // cannot hear itself no matter what the room does. voice_barge_browser.mjs
 // covers the opposite mode.
 await page.uncheck('#barge-in');
 console.log('completed line 45');
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};
 await send('Remember <b>orchid</b>.');
 console.log('completed line 47');
 await page.waitForFunction(()=>document.querySelector('#player').currentTime>.15&&!document.querySelector('#player').paused);
 console.log('completed line 48');
 assert.equal(await page.locator('#messages b').count(),0,'chat text is never injected as HTML');
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 console.log('completed line 50');
 await send('What did I say?');
 console.log('completed line 51');
 await page.waitForFunction(()=>document.querySelector('#player').currentTime>.15&&!document.querySelector('#player').paused);
 console.log('completed line 52');
 assert.equal(posts[1].messages.length,3);assert.equal(posts[1].messages[0].content,'Remember <b>orchid</b>.');
 await page.locator('#interrupt').click();await page.locator('#new').click();
 console.log('completed line 54');
 assert.equal(await page.locator('.message').count(),0);
 if(name==='chromium'){
   await page.fill('#silence-timeout','0');   // the recording has a 10.6 s quiet gap; the silence timeout (default 5 s) must not close the microphone under this suite
   await page.locator('#start').click();
 console.log('completed line 58');
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',{}, {timeout:20000});
 console.log('completed line 59');
   assert.equal(stt,1,'silence detection automatically submitted one real MediaRecorder upload');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'microphone is disabled during the spoken reply');
   assert.equal(posts.at(-1).messages.length,1,'new conversation excludes old history');
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening');
 console.log('completed line 63');
   await page.locator('#player').evaluate(a=>a.play());
 console.log('completed line 64');
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking');
 console.log('completed line 65');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'replaying old audio also disables capture');
   await page.locator('#interrupt').click();
 console.log('completed line 67');
   await page.waitForTimeout(400);
 console.log('completed line 68');
   await page.locator('#finish').click();
 console.log('completed line 69');
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking');
 console.log('completed line 70');
   assert.equal(stt,2,'Send now submits even when speech is below the VAD threshold');
   await page.locator('#end').click();
 console.log('completed line 72');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>t.readyState==='ended')),true);
 }
 slow=true;const before=await page.locator('.assistant').count();await send('A delayed reply');
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Thinking');
 console.log('completed line 76');
 for(let i=0;i<500&&!release;i++)await page.waitForTimeout(20); console.log('RELEASE STATE',!!release, await page.evaluate(()=>({state:document.querySelector('#state').textContent,detail:document.querySelector('#detail')?.textContent,busy,epoch,history,transcript}))); assert.ok(release,'delayed request reached upstream');
 await page.locator('#end').click();release();await page.waitForTimeout(200);
 console.log('completed line 78');
 assert.equal(await page.locator('.assistant').count(),before,'stale response never reaches the conversation');
 assert.deepEqual(errors,[]);
 fs.mkdirSync('evidence/browser',{recursive:true});
 await page.screenshot({path:`evidence/browser/${name}-chat.png`,fullPage:true});
 console.log('completed line 82');
 console.log(`PASS ${name}: native playback, history, safe text, new chat, cancellation${name==='chromium'?', real MediaRecorder/VAD, automatic relisten, microphone isolation':''}`);
 }finally{await browser.close();}
}
}finally{await new Promise(resolve=>server.close(resolve));}
