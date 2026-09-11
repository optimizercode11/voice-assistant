import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// The pause_listening control.  Measured on the live bridge (2026-09-11): asked
// "stop listening for a bit", the model said "I'll pause listening" and the page
// kept listening -- the fake microphone here is the same loop, a capture file
// Chromium replays forever, so left alone the page takes a turn every ~13 s.
//
// This drives that REAL microphone through the REAL page and asserts that once
// an answer carries {controls:{pause_listening:true}}:
//   - the page says Paused, the tracks are disabled, and NO further clip is
//     uploaded for longer than one full loop of the capture file;
//   - typing still works, and the page returns to Paused afterwards, not Listening;
//   - only a person (Resume listening, or Space) reopens the microphone, and
//     then the automatic turns come back.
// The paired sabotage removes the hold in listen(): the page must then resume
// listening after the pause reply and upload another clip, and this suite must
// go red for it.
//
//   PLAYWRIGHT=/path/to/playwright/index.mjs node tests/browser/voice_pause_browser.mjs
const sabotage=process.argv.includes('--sabotage');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser pause';
// Both flags run the child through the real sabotage exit path with a no-op.
if(deadSabotage&&!sabotage){
 const run=spawnSync(process.execPath,[process.argv[1],'--sabotage','--dead-sabotage'],
  {encoding:'utf8',timeout:180000});
 process.stdout.write(run.stdout??'');
 process.stderr.write(run.stderr??'');
 assert.ifError(run.error);
 assert.equal(run.signal,null,'dead sabotage must finish normally');
 assert.equal(run.status,0,'an uncaught sabotage must exit zero so make rejects it');
 assert.ok(run.stdout.split('\n').includes(assertionsComplete),'the browser assertions must finish');
 console.log('DEAD SABOTAGE PASS: no-op mutation escaped after the browser assertions ran');
 process.exit(0);
}
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0;
// One loop of the capture file plus the endpointer's silence window: if the
// microphone were open, at least one more clip would have been uploaded by now.
const CAPTURE_LOOP_MS=13250+1000;
const QUIET_MS=CAPTURE_LOOP_MS+2000;
const server=http.createServer((req,res)=>{
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 if(sabotage&&file.endsWith('.js')){
   const anchor="if (paused) { holdListening(); return; }";
   assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
   const armed=body.replace(anchor,deadSabotage?anchor:"/* sabotage: the pause is decorative */");
   if(deadSabotage)assert.equal(armed,body,'dead sabotage must leave the page unchanged');
   body=armed;mutations++;
 }
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const ndjson=events=>events.map(event=>JSON.stringify(event)).join('\n')+'\n';
try{
 const browser=await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/capture.wav')}`]});
 try{
  const context=await browser.newContext();
  await context.grantPermissions(['microphone']);
  await context.addInitScript(()=>{
    window.observedTracks=[];
    if(navigator.mediaDevices?.getUserMedia){const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);navigator.mediaDevices.getUserMedia=async(...args)=>{const s=await original(...args);window.observedTracks.push(...s.getTracks());return s;};}
  });
  const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  page.setDefaultTimeout(30000);
  const uploads=[],posts=[];
  // What the ASR "hears", clip by clip.  The second clip is the request to
  // pause; anything after that is a clip the page should never have recorded.
  const CLIPS=['Hello there.','Please stop listening for a bit, I need to take a call.','Are you still there?'];
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true,streaming:true,tools:['now','pause_listening'],tool_sources:{now:'builtin',pause_listening:'builtin'}}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',async route=>{
   const text=CLIPS[Math.min(uploads.length,CLIPS.length-1)];
   uploads.push({at:Date.now(),text});
   await route.fulfill({json:{text,raw_text:text,audio_seconds:1.6,chunks:1,turn:{complete:true,hold:false,reason:'test',extra_silence_ms:0}}});
  });
  await page.route('**/chat/completions',async route=>{
   const body=route.request().postDataJSON();posts.push(body);
   const asked=body.messages.at(-1).content;
   let events;
   if(/stop listening/i.test(asked)){
     // The bridge ran pause_listening mid-turn and folded its control into the
     // answer.  Nothing else in this stream tells the page to pause.
     events=[{type:'status',phase:'tool',round:1,calls:['pause_listening']},
             {type:'tool',name:'pause_listening',ok:true,ms:0,source:'builtin',control:{pause_listening:true,pause_reason:'taking a call'}},
             {type:'answer',text:'Okay, I have stopped listening. Press Resume when you are back.',usage:{},
              tools:[{name:'pause_listening',ok:true,ms:0}],sources:[],controls:{pause_listening:true,pause_reason:'taking a call'}}];
   }else{
     events=[{type:'answer',text:`You said: ${asked}`,usage:{},tools:[],sources:[],controls:{}}];
   }
   await route.fulfill({contentType:'application/x-ndjson',body:ndjson(events)}).catch(()=>{});
  });
  await page.route('**/tts?*',route=>route.fulfill({contentType:'audio/wav',body:wav}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  // Barge-in keeps the microphone open during a reply by design; that mode is
  // voice_barge_browser.mjs's to test.  Here the question is the pause, so use
  // the mode where track state is the whole story.
  await page.uncheck('#barge-in');
  await page.locator('#start').click();

  // Turn 1: an ordinary answer.  Listening must come back on its own -- the
  // pause is a property of ONE answer, not of the tool being configured.
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===1,null,{timeout:30000});
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:30000});
  assert.equal(uploads.length,1);assert.equal(posts.length,1);
  assert.equal(await page.locator('#resume-listening').isVisible(),false,'nothing to resume yet');

  // Turn 2: the request to pause.  The reply is spoken, then the page holds.
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2,null,{timeout:30000});
  assert.equal(posts.length,2);
  assert.match(posts[1].messages.at(-1).content,/stop listening/);
  await page.waitForFunction(()=>['Paused','Listening'].includes(document.querySelector('#state').textContent),null,{timeout:30000});
  const stateAfterPause=await page.locator('#state').textContent();
  const pausedAt=Date.now();
  if(!sabotage){
    assert.equal(stateAfterPause,'Paused','the answer carried pause_listening, so the page must hold');
    assert.match(await page.locator('#status').textContent(),/taking a call/,'the reason the model gave is shown');
    assert.match(await page.locator('.message.assistant').last().locator('.toolnote').textContent(),/paused listening/);
    assert.equal(await page.locator('#resume-listening').isVisible(),true,'the way back is a button');
    assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>!t.enabled&&t.readyState==='live')),true,
      'the microphone is disabled but not released: resume must not re-ask permission');
    assert.equal(await page.locator('#orb').getAttribute('data-state'),'paused');
  }
  // The decisive measurement: over more than one full loop of the capture
  // file, a paused page uploads nothing.  A page that resumed listening would
  // have recorded and uploaded the third clip by now.
  const uploadsAtPause=uploads.length,postsAtPause=posts.length;
  while(Date.now()-pausedAt<QUIET_MS) await page.waitForTimeout(250);
  assert.equal(uploads.length,uploadsAtPause,`no clip may be uploaded while paused (${uploads.length-uploadsAtPause} were, over ${QUIET_MS} ms)`);
  assert.equal(posts.length,postsAtPause,'no automatic turn may be taken while paused');
  assert.equal(await page.locator('#state').textContent(),'Paused');

  // Typing is not listening: a typed turn works, and afterwards the page is
  // still paused rather than quietly back to Listening.
  await page.locator('#text').fill('are you there?');await page.locator('#send').click();
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===3,null,{timeout:30000});
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Paused',null,{timeout:30000});
  assert.equal(posts.length,postsAtPause+1);
  assert.equal(uploads.length,uploadsAtPause,'typing while paused must not reopen the microphone');

  // Only a person resumes.  Space, outside a text field, is that person.
  await page.locator('h1').click();await page.keyboard.press('Space');
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:10000});
  assert.equal(await page.locator('#resume-listening').isVisible(),false);
  assert.equal(await page.evaluate(()=>window.observedTracks.every(t=>t.enabled&&t.readyState==='live')),true,'Space reopens capture');
  // ...and the automatic turns are back: the next loop of the capture file is
  // uploaded and answered without anyone clicking.
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===4,null,{timeout:CAPTURE_LOOP_MS+15000});
  assert.equal(uploads.length,uploadsAtPause+1,'after resume the microphone loop took a turn on its own');
  assert.match(posts.at(-1).messages.at(-1).content,/still there/);

  // Ending the conversation clears the pause: a new session starts listening.
  await page.locator('#end').click();
  assert.equal(await page.locator('#resume-listening').isVisible(),false);
  assert.deepEqual(errors,[]);
  if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
  console.log(assertionsComplete);
  if(sabotage){console.error('SABOTAGE PASSED (this is the failure): the page ignores pause_listening');
   // The assertions passed: exit zero so make sabotage rejects this arm.
   process.exitCode=0;}
  else{fs.mkdirSync('evidence/browser',{recursive:true});
   await page.screenshot({path:'evidence/browser/pause.png',fullPage:true});
   console.log('PASS chromium: pause_listening holds a real microphone loop, typing still works, only Space/Resume reopens it');}
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
