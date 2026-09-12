import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// The bridge speaks first.  A finished Claude Code turn arrives on /events
// (server-sent events) with nobody having asked, and the REAL page must:
//   - speak it through the same TTS path as a reply, and show it as a third
//     voice ("Claude Code"), when the page is idle;
//   - NOT talk over a reply that is playing: the update waits, and is spoken
//     at the seam after the reply, before the microphone reopens;
//   - carry what was said into the next request to the model, so "tell it to
//     also do X" has a referent;
//   - NEVER speak while the microphone is paused (paused means "be quiet"),
//     and say it once a person presses Resume / Space.
// The paired sabotage removes the pause hold in drainUpdates(): the page must
// then read the update aloud while Paused, and this suite must go red for it.
//
//   PLAYWRIGHT=/path/to/playwright/index.mjs node tests/browser/voice_push_browser.mjs
const sabotage=process.argv.includes('--sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser push';
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0;
const listeners=[];      // open /events responses
let pushed=0;
function push(update){
  pushed++;
  const frame=`id: ${pushed}\nevent: ${update.type}\ndata: ${JSON.stringify(update)}\n\n`;
  for(const res of listeners) res.write(frame);
}
const server=http.createServer((req,res)=>{
 if(req.url==='/events'){
   res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
   res.write('retry: 2000\n\n');
   listeners.push(res);
   req.on('close',()=>{const at=listeners.indexOf(res);if(at>=0)listeners.splice(at,1);});
   return;
 }
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 if(sabotage&&file.endsWith('.js')){
   const anchor="  if (paused) return;\n  const talking";
   assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
   body=body.replace(anchor,"  /* sabotage: paused is decorative for updates */\n  const talking");mutations++;
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
  const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  page.setDefaultTimeout(30000);
  const posts=[],tts=[];let uploads=0;
  const CLIPS=['Hello there.','Please stop listening for a bit, I need to take a call.','Are you still there?'];
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true,streaming:true,events:true,tools:['now','pause_listening','mcp__claude__send','mcp__claude__updates']}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',async route=>{
   const text=CLIPS[Math.min(uploads,CLIPS.length-1)];uploads++;
   await route.fulfill({json:{text,raw_text:text,audio_seconds:1.6,chunks:1,turn:{complete:true,hold:false,reason:'test',extra_silence_ms:0}}});
  });
  await page.route('**/chat/completions',async route=>{
   const body=route.request().postDataJSON();posts.push(body);
   const asked=body.messages.at(-1).content;
   let events;
   if(/stop listening/i.test(asked)){
     events=[{type:'tool',name:'pause_listening',ok:true,ms:0,source:'builtin',control:{pause_listening:true,pause_reason:'taking a call'}},
             {type:'answer',text:'Okay, I have stopped listening. Press Resume when you are back.',usage:{},
              tools:[{name:'pause_listening',ok:true,ms:0}],sources:[],controls:{pause_listening:true,pause_reason:'taking a call'}}];
   }else{
     events=[{type:'answer',text:`You said: ${asked}`,usage:{},tools:[],sources:[],controls:{}}];
   }
   await route.fulfill({contentType:'application/x-ndjson',body:ndjson(events)}).catch(()=>{});
  });
  await page.route('**/tts?*',route=>{tts.push(route.request().postData()||'');return route.fulfill({contentType:'audio/wav',body:wav});});
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  // The page connects to /events on its own, because health said it could.
  const deadline=Date.now()+10000;while(!listeners.length&&Date.now()<deadline)await page.waitForTimeout(100);
  assert.equal(listeners.length,1,'the page must hold /events open without being asked');

  // 0. The job lands: a pending "working" bubble, shown and not spoken.
  push({type:'working',server:'claude',instruction:'run the tests'});
  await page.waitForFunction(()=>document.querySelectorAll('.message.claude.pending').length===1);
  assert.match(await page.locator('.message.claude.pending p').textContent(),/Working on it: run the tests/);
  assert.equal(tts.length,0,'a working notice is never spoken');
  // 1. Idle page, nobody talking: the update replaces the pending bubble, is shown as Claude Code and spoken;
  //    the raw report is there to read, collapsed, and never reaches the speaker.
  const REPORT='## Result\n\n```sh\nmake test  # 14 passed\n```\n- `tools/x.py` changed';
  push({type:'update',server:'claude',spoken:'The tests pass and two files changed.',instruction:'run the tests',is_error:false,seconds:42.5,activity:{commands:3,files_edited:2,files_read:1,other_tools:0},detail:REPORT});
  await page.waitForFunction(()=>document.querySelectorAll('.message.claude').length===1);
  assert.equal(await page.locator('.message.claude.pending').count(),0,'the pending bubble is replaced by the result');
  assert.equal(await page.locator('.message.claude details.report summary').textContent(),'Full report');
  assert.equal(await page.locator('.message.claude details.report pre').textContent(),REPORT,'the report is shown verbatim as text');
  assert.equal(await page.locator('.message.claude details.report pre code').count(),0,'never rendered as HTML');
  assert.equal(await page.locator('.message.claude .role').textContent(),'Claude Code');
  assert.equal(await page.locator('.message.claude p').first().textContent(),'The tests pass and two files changed.');
  assert.match(await page.locator('.message.claude .toolnote').textContent(),/Update from Claude Code · 43 s · 3 commands · 2 files edited/);
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
  assert.equal(tts.filter(text=>text.includes('The tests pass')).length,1,'the update went through the TTS path once');
  assert.ok(tts.every(text=>!text.includes('make test')),'the report never reaches the speaker');
  assert.equal(posts.length,0,'speaking an update is not a turn: nothing was sent to the model');
  // 1b. The same route carries Codex (2026-09-12): the bubble is labelled by the
  //     server that sent it, and the note and status say Codex, never Claude Code.
  push({type:'working',server:'codex',instruction:'build it'});
  await page.waitForFunction(()=>document.querySelectorAll('.message.claude.pending').length===1);
  assert.equal(await page.locator('.message.claude.pending .role').textContent(),'Codex');
  push({type:'update',server:'codex',spoken:'The build is green on the local model.',instruction:'build it',is_error:false,seconds:5,activity:{commands:2,files_edited:0,files_read:0,other_tools:0}});
  await page.waitForFunction(()=>document.querySelectorAll('.message.claude').length===2&&!document.querySelector('.message.claude.pending'));
  assert.equal(await page.locator('.message.claude .role').last().textContent(),'Codex');
  assert.match(await page.locator('.message.claude .toolnote').last().textContent(),/Update from Codex · 5 s · 2 commands/);
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
  assert.equal(tts.filter(text=>text.includes('build is green')).length,1,'the Codex update went through the TTS path once');

  // 2. A live conversation.  Push while the reply is PLAYING: the update must
  //    wait for the reply to end, then be spoken before listening resumes.
  await page.uncheck('#barge-in');
  await page.locator('#start').click();
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Speaking',null,{timeout:40000});
  const ttsBeforePush=tts.length;
  push({type:'update',server:'claude',spoken:'I finished the second job while you were talking.',instruction:'second job',is_error:false,seconds:7,activity:{commands:1,files_edited:0,files_read:0,other_tools:0}});
  assert.equal(await page.locator('.message.claude').count(),2,'an update must not interrupt a reply');
  await page.waitForFunction(()=>document.querySelectorAll('.message.claude').length===3,null,{timeout:40000});
  const replyIndex=tts.findIndex((text,index)=>index>=ttsBeforePush-1&&text.includes('You said: Hello there'));
  const updateIndex=tts.findIndex(text=>text.includes('second job'));
  assert.ok(replyIndex>=0&&updateIndex>replyIndex,`the update's speech (${updateIndex}) must come after the reply's (${replyIndex})`);
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:30000});
  // 3. What Claude said travels into the next request as a note on the last turn.
  const postsBefore=posts.length;
  await page.waitForFunction(count=>document.querySelectorAll('.message.assistant').length>count,await page.locator('.message.assistant').count(),{timeout:40000});
  const next=posts[postsBefore];
  assert.ok(next,'the microphone loop took another turn');
  const carried=next.messages.filter(m=>m.role==='assistant').some(m=>m.content.includes('[Claude Code reported: I finished the second job while you were talking.]'));
  assert.ok(carried,'the model must be told what Claude Code said: '+JSON.stringify(next.messages.slice(-3)));

  // 4. Paused.  The second clip asks to stop listening; the answer carries the
  //    control.  An update arriving now must NOT be spoken.
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Paused',null,{timeout:60000});
  const claudeBefore=await page.locator('.message.claude').count(),ttsBeforePaused=tts.length;
  push({type:'update',server:'claude',spoken:'This one arrived while you were on the phone.',instruction:'third',is_error:false,seconds:3,activity:{commands:0,files_edited:0,files_read:0,other_tools:0}});
  await page.waitForTimeout(3500);
  const spokenWhilePaused=tts.slice(ttsBeforePaused).some(text=>text.includes('on the phone'));
  const shownWhilePaused=(await page.locator('.message.claude').count())>claudeBefore;
  // The decisive assertion, live in both arms: with the hold sabotaged the update
  // is read aloud while Paused and this line goes red.
  assert.equal(spokenWhilePaused,false,'paused means quiet: the update must wait');
  if(!sabotage){
    assert.equal(shownWhilePaused,false);
    assert.equal(await page.locator('#state').textContent(),'Paused');
  }
  // 5. A person resumes; the waiting update is said now, then listening returns.
  await page.locator('h1').click();await page.keyboard.press('Space');
  await page.waitForFunction(count=>document.querySelectorAll('.message.claude').length===count+1,claudeBefore,{timeout:30000});
  assert.ok(tts.some(text=>text.includes('on the phone')),'the held update is spoken after Resume');
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='Listening',null,{timeout:30000});
  assert.deepEqual(errors,[]);
  if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
  console.log(assertionsComplete);
  if(sabotage){console.error('SABOTAGE PASSED (this is the failure): an update was spoken while the microphone was paused');process.exitCode=0;}
  else{fs.mkdirSync('evidence/browser',{recursive:true});
   await page.screenshot({path:'evidence/browser/push.png',fullPage:true});
   console.log('PASS chromium: pushed updates are spoken when idle, wait for a reply, ride into history, and hold while paused');}
 }finally{await browser.close();}
}finally{for(const res of listeners)res.end();await new Promise(resolve=>server.close(resolve));}
