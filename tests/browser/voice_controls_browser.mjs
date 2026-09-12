import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Playwright is deliberately NOT a dependency of this repo: point PLAYWRIGHT at any
// install whose chromium/webkit browsers are already downloaded, e.g.
//   PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node tests/browser/<name>.mjs
const sabotage=process.argv.includes('--sabotage'),live=process.argv.includes('--live');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser controls';
assert.ok(!(live&&(sabotage||deadSabotage)),'sabotage must use the local page replacement');
// Both flags run the child through the real sabotage exit path with a no-op.
if(deadSabotage&&!sabotage){
 const run=spawnSync(process.execPath,[process.argv[1],'--sabotage','--dead-sabotage'],
  {encoding:'utf8',timeout:120000});
 process.stdout.write(run.stdout??'');
 process.stderr.write(run.stderr??'');
 assert.ifError(run.error);
 assert.equal(run.signal,null,'dead sabotage must finish normally');
 assert.equal(run.status,0,'an uncaught sabotage must exit zero so make rejects it');
 assert.ok(run.stdout.split('\n').includes(assertionsComplete),'the browser assertions must finish');
 console.log('DEAD SABOTAGE PASS: no-op mutation escaped after the browser assertions ran');
 process.exit(0);
}
const {chromium,webkit} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0;
const server=http.createServer((req,res)=>{
 const name=req.url==='/chat.js'?'chat.js':'chat.html';let body=fs.readFileSync('web/'+name,'utf8');
 if(sabotage && name==='chat.js'){
   const check="if(event.code!=='Space' && event.key!==' ')return;";
   assert.ok(body.includes(check),'the sabotage anchor no longer matches web/chat.js');
   const armed=body.replace(check,deadSabotage?check:'return; // paired sabotage disables only the keyboard handler');
   if(deadSabotage)assert.equal(armed,body,'dead sabotage must leave the page unchanged');
   body=armed;mutations++;
 }
 res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'text/html');res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try{
for(const [name,type] of (sabotage?[['chromium',chromium]]:[['chromium',chromium],['webkit',webkit]])){
 const browser=await type.launch({headless:true,args:name==='chromium'?['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture.wav')}`]:[],...(name==='webkit'?{env:{...process.env,LIBGL_ALWAYS_SOFTWARE:'1',WEBKIT_DISABLE_COMPOSITING_MODE:'1'}}:{})});
 try{
 const context=await browser.newContext({ignoreHTTPSErrors:live});if(name==='chromium')await context.grantPermissions(['microphone']);
 await context.addInitScript(()=>{
   window.observedTracks=[];window.denyPlay=false;
   const play=HTMLMediaElement.prototype.play;
   HTMLMediaElement.prototype.play=function(){return this.id==='player'&&window.denyPlay?Promise.reject(new DOMException('Test autoplay denial','NotAllowedError')):play.call(this);};
   if(navigator.mediaDevices?.getUserMedia){const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);navigator.mediaDevices.getUserMedia=async(...args)=>{const s=await original(...args);window.observedTracks.push(...s.getTracks());return s;};}
 });
 const page=await context.newPage(),errors=[],speeds=[];let deny=false,slow=false,release=null;
 page.on('pageerror',e=>errors.push(String(e)));page.setDefaultTimeout(15000);
 // All inference endpoints are fixtures, including when reading the live page.
 await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
 await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English'}]}}));
 await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart']}}));
 await page.route('**/stt',r=>r.fulfill({json:{text:'Hello from the microphone.'}}));
 await page.route('**/chat/completions',async r=>{if(slow)await new Promise(resolve=>{release=resolve;});await r.fulfill({json:{text:'Hello from Qwen.'}}).catch(()=>{});});
 await page.route('**/tts?*',async r=>{speeds.push(new URL(r.request().url()).searchParams.get('speed'));if(deny)await page.evaluate(()=>window.denyPlay=true);await r.fulfill({contentType:'audio/wav',body:wav});});
 await page.goto(live?'https://192.168.228.113:8092/chat':`http://127.0.0.1:${server.address().port}/chat`);
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 // This suite guards the controls, and one of them is "Space interrupts and
 // capture comes back". Barge-in keeps the microphone live during a reply on
 // purpose, which would make the isolation assertions below false for the wrong
 // reason, so switch it off here -- the same click a user makes. It persists
// through the reload below, which is itself the preference being exercised.
// voice_barge_browser.mjs owns the mode where the microphone stays open.
 await page.uncheck('#barge-in');
 assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
 const darkBackground=await page.evaluate(()=>getComputedStyle(document.body).backgroundColor);
 await page.locator('#theme').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
 assert.notEqual(await page.evaluate(()=>getComputedStyle(document.body).backgroundColor),darkBackground);
 await page.reload();await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 assert.equal(await page.locator('html').getAttribute('data-theme'),'light','theme choice persists across reload');
 await page.locator('#theme').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
 await page.locator('#player').evaluate(a=>a.loop=true);
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};
 const playing=()=>page.waitForFunction(()=>document.querySelector('#player').currentTime>.1&&!document.querySelector('#player').paused);
 const space=async()=>{await page.locator('h1').click();await page.keyboard.press('Space');};
 assert.equal(await page.locator('#speed').inputValue(),'1.2');
 assert.equal(await page.locator('#player').isVisible(),false);assert.equal(await page.locator('#player').evaluate(a=>a.controls),false);
 await send('Hello');await playing();assert.equal(speeds[0],'1.2');
 assert.equal(await page.locator('#resume').isVisible(),false);
 console.log('CONTROL PASS hidden native playback and default speed',name);
 // Editing, IME, modifiers and held-key repeats retain their normal behavior.
 await page.locator('#text').focus();await page.keyboard.press('Space');assert.equal(await page.locator('#text').inputValue(),' ');
 assert.equal(await page.locator('#player').evaluate(a=>a.paused),false);
 await page.locator('#speed').focus();await page.keyboard.press('Space');assert.equal(await page.locator('#player').evaluate(a=>a.paused),false);
 await page.locator('h1').click();await page.keyboard.press('Control+Space');await page.keyboard.press('Shift+Space');
 await page.evaluate(()=>{
   document.body.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',key:' ',isComposing:true,bubbles:true}));
   document.body.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',key:' ',repeat:true,bubbles:true}));
 });
 assert.equal(await page.locator('#player').evaluate(a=>a.paused),false);
 // space() clicks the heading first, and Playwright scrolls a clicked element
 // into view, so the position is taken after the click and before the key:
 // what is under test is the key, not the click.
 await page.locator('h1').click();const y=await page.evaluate(()=>scrollY);await page.keyboard.press('Space');
 assert.equal(await page.locator('#player').evaluate(a=>a.paused),true,'Space interrupts the reply');
 assert.equal(await page.locator('#state').textContent(),'Ready');assert.equal(await page.evaluate(()=>scrollY),y);
 await page.locator('#speed').focus();for(let i=0;i<3;i++)await page.keyboard.press('ArrowRight');
 assert.equal(await page.locator('#speed-value').textContent(),'1.5×');
 await send('Faster please');await playing();assert.equal(speeds.at(-1),'1.5');await space();
 // A custom fallback replaces visible WAV controls when autoplay is denied.
 deny=true;await send('Blocked autoplay');await page.locator('#resume').waitFor({state:'visible'});
 assert.equal(await page.locator('#player').isVisible(),false);
 await page.evaluate(()=>window.denyPlay=false);deny=false;await page.locator('#resume').click();await playing();
 assert.equal(await page.locator('#resume').isVisible(),false);await space();
 deny=true;await send('Cancel blocked audio');await page.locator('#resume').waitFor({state:'visible'});await space();
 assert.equal(await page.locator('#resume').isVisible(),false);await page.evaluate(()=>window.denyPlay=false);deny=false;
 // Cancel generation before its delayed response; it must never speak later.
 slow=true;const count=await page.locator('.assistant').count();await send('Delayed model reply');
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Thinking');
 while(!release)await page.waitForTimeout(20);await space();release();slow=false;await page.waitForTimeout(100);
 assert.equal(await page.locator('.assistant').count(),count);assert.equal(await page.locator('#player').evaluate(a=>a.paused),true);
 if(name==='chromium'){
   await page.fill('#silence-timeout','0');   // the recording has a 10.6 s quiet gap; the silence timeout (default 5 s) must not close the microphone under this suite
   await page.locator('#start').click();await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',{}, {timeout:20000});await playing();
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled)),true,'built-in speakers remain isolated');
   await space();assert.equal(await page.locator('#state').textContent(),'Listening');
   assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>t.enabled&&t.readyState==='live')),true,'Space resumes capture');
   await page.locator('#end').click();assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>t.readyState==='ended')),true);
 }
 await page.locator('#new').click();assert.equal(await page.locator('#speed').inputValue(),'1.5','new chat preserves selected speed');
 assert.deepEqual(errors,[]);
 fs.mkdirSync('evidence/browser',{recursive:true});
 await page.screenshot({path:`evidence/browser/${name}-${live?'live':'controls'}.png`,fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile layout has no horizontal overflow');
 await page.screenshot({path:`evidence/browser/${name}-${live?'live':'controls'}-mobile.png`,fullPage:true});
 console.log(`PASS ${name}: dark/light theme persistence and mobile layout, Space interruption/editing/modifiers, hidden playback, autoplay recovery/cancel, speed1.2/1.5${name==='chromium'?', real microphone resumes after Space':''}`);
 if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
 console.log(assertionsComplete);
 if(sabotage)console.error('SABOTAGE PASSED (this is the failure): keyboard handler');
 }finally{await browser.close();}
}
}finally{await new Promise(resolve=>server.close(resolve));}
