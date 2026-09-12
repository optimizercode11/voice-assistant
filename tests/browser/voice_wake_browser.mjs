import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Wake word only.  With the setting on, the REAL page over the REAL microphone
// loop must:
//   - hear, transcribe and DROP a clip that does not begin with the wake word
//     (nothing is sent to the model, nothing is spoken);
//   - act on "Hey Qwen, what time is it?" with the name stripped from the question;
//   - once awake, take the next clip without the name;
//   - after the quiet period with no turn, go dormant again and drop the next
//     unnamed clip.
// The matcher is also checked as a pure function, extracted verbatim from the
// page, for the near-miss rules: "Hey Gwen" wakes it, bare "when" does not.
// The paired sabotage removes the gate: a clip without the wake word is then
// dispatched, and this suite must go red for it.
//
//   PLAYWRIGHT=/path/to/playwright/index.mjs node tests/browser/voice_wake_browser.mjs
const sabotage=process.argv.includes('--sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser wake';
const {chromium} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
const source=fs.readFileSync('web/chat.js','utf8');
let mutations=0;

// --- the matcher, as a function -------------------------------------------
const begin=source.indexOf('// WAKE-MATCH-BEGIN'),end=source.indexOf('// WAKE-MATCH-END');
assert.ok(begin>0&&end>begin,'the wake matcher markers must exist in web/chat.js');
const helpers=source.slice(source.indexOf('const CALL_WORDS'),begin);
const matcher=new Function(helpers+source.slice(begin,end)+'\nreturn afterWakeWord;')();
assert.equal(matcher('Hey Qwen, what time is it?','Qwen'),'what time is it?');
assert.equal(matcher('Qwen what time is it','Qwen'),'what time is it');
assert.equal(matcher('Okay Qwen.','Qwen'),'','just the name is an empty question, not a miss');
assert.equal(matcher('Qwen','Qwen'),'');
assert.equal(matcher('What time is it?','Qwen'),null);
assert.equal(matcher('When is the meeting?','Qwen'),null,'bare "when" must not wake it');
assert.equal(matcher('Hey Gwen, what time is it?','Qwen'),'what time is it?','a near miss after a call word wakes it');
assert.equal(matcher('Hey when is the meeting?','Qwen'),null,'"when" is two edits from "qwen", so even a call word does not make it the name');
assert.equal(matcher('Hey Jarvis, lights','Hey Jarvis'),'lights','a two-word phrase');
assert.equal(matcher('Jarvis lights','Hey Jarvis'),null);
assert.equal(matcher('','Qwen'),null);
assert.equal(matcher('anything','   '),null,'an empty phrase never matches');
// The ASR's own spellings of the bare name, measured on 2026-09-12 by synthesising
// the phrase with Kokoro and transcribing it with the live qasr: these are what
// a person saying "Qwen" without a call word actually produces.
assert.equal(matcher('Q N.','Qwen'),'','"Q N." is the name spelled out');
assert.equal(matcher('Qn, what time is it?','Qwen'),'what time is it?','"Qn" is the name');
assert.equal(matcher('Q. When. What time is it?','Qwen'),'What time is it?','"Q. When." is the name split in two');
assert.equal(matcher('Okay, Qwen. What is the weather?','Qwen'),'What is the weather?','punctuation after a call word');
assert.equal(matcher('Quen, hello','Qwen'),'hello','same consonants, same first letter');
assert.equal(matcher('Kwen, hello','Qwen'),null,'a different first letter needs a call word');
assert.equal(matcher('Hey Kwen, hello','Qwen'),'hello');
assert.equal(matcher('Question one','Qwen'),null,'"question" shares the q but not the consonants');
assert.equal(matcher('Quick question','Qwen'),null);
assert.equal(matcher('Wendy is here','Qwen'),null);
assert.equal(matcher('Q and A time','Qwen'),null,'two tokens absorbed must still spell the name');
assert.equal(matcher('Jervis lights','Jarvis'),'lights','the spelled rule is not Qwen-specific');
assert.equal(matcher('Travis lights','Jarvis'),null);
// "Bubu" (the person's own choice) is heard as "Boo boo", "Boo bo." and
// "Boo! Boo!": both halves are the name, and the question keeps none of it.
assert.equal(matcher('Boo boo! What time is it?','Bubu'),'What time is it?');
assert.equal(matcher('Boo bo.','Bubu'),'');
assert.equal(matcher('Boo! Boo!','Bubu'),'');
assert.equal(matcher('Hey, Boo Boo. What time is it?','Bubu'),'What time is it?');
assert.equal(matcher('Boo hoo, I am sad','Bubu'),null,'a two-consonant name gets no slip');
assert.equal(matcher('Book a table','Bubu'),null);
assert.equal(matcher('Quen, I want to go','Qwen'),'I want to go','a tie goes to the shorter run: "I" is not absorbed');

// --- the page -----------------------------------------------------------------
const server=http.createServer((req,res)=>{
 const file=req.url==='/chat'?'web/chat.html':req.url==='/chat.js'?'web/chat.js':null;
 if(!file){res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file,'utf8');
 if(sabotage&&file.endsWith('.js')){
   const anchor="        const rest = afterWakeWord(text, wakePhrase());";
   assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
   body=body.replace(anchor,"        const rest = text; /* sabotage: every clip is for the assistant */");mutations++;
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
  const posts=[],uploads=[];
  // What the ASR hears, clip by clip.  Clip 1 is not for the assistant.  Clips
  // 4 and 5 are silence, long enough for the quiet period to elapse.  Clip 6 is
  // again not for the assistant, and must be dropped because the page went
  // dormant in between.
  const CLIPS=['What time is it?','Hey Qwen, what time is it?','And tomorrow?','','','What time is it?'];
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true,streaming:true,tools:['now']}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English (US)'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',async route=>{
   const text=CLIPS[Math.min(uploads.length,CLIPS.length-1)];uploads.push({at:Date.now(),text});
   await route.fulfill({json:{text,raw_text:text,audio_seconds:1.6,chunks:1,turn:{complete:true,hold:false,reason:'test',extra_silence_ms:0}}});
  });
  await page.route('**/chat/completions',async route=>{
   const body=route.request().postDataJSON();posts.push(body);
   await route.fulfill({contentType:'application/x-ndjson',body:ndjson([{type:'answer',text:`You said: ${body.messages.at(-1).content}`,usage:{},tools:[],sources:[],controls:{}}])}).catch(()=>{});
  });
  await page.route('**/tts?*',route=>route.fulfill({contentType:'audio/wav',body:wav}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.uncheck('#barge-in');
  await page.check('#wake-enabled');
  // Longer than one loop of the capture file (13.25 s), so an awake page takes
  // the next clip; short enough that two silent loops put it back to sleep.
  await page.fill('#wake-quiet','20');
  await page.fill('#silence-timeout','0');   // the recording has a 10.6 s quiet gap; the silence timeout (default 5 s) must not close the microphone under this suite
  await page.locator('#start').click();

  // Clip 1: heard and dropped.  The page keeps listening and says what it waits for.
  const deadline=Date.now()+40000;while(uploads.length<1&&Date.now()<deadline)await page.waitForTimeout(200);
  assert.equal(uploads.length,1,'the first clip was transcribed');
  await page.waitForTimeout(1500);
  const droppedFirst=posts.length===0;
  // The decisive assertion, live in both arms: with the gate sabotaged the first
  // clip reaches the model and this line goes red.
  assert.ok(droppedFirst,'a clip without the wake word must not reach the model');
  if(!sabotage){
    assert.match(await page.locator('#status').textContent(),/Waiting for “Qwen”/);
    assert.equal(await page.locator('.message.user').count(),0,'nothing to show for a clip that was not for us');
  }
  // Clip 2: the wake word, then a question.  The question arrives without the name.
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===1,null,{timeout:45000});
  const first=posts.find(post=>/time/.test(post.messages.at(-1).content));
  assert.ok(first,'the named question reached the model');
  assert.equal(first.messages.at(-1).content,'what time is it?','the wake word is stripped, the question kept');
  assert.equal(await page.locator('.message.user p').first().textContent(),'what time is it?','shown as said, without the name');
  // Clip 3: awake now, so an unnamed clip is a turn.
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2,null,{timeout:45000});
  assert.equal(posts.at(-1).messages.at(-1).content,'And tomorrow?');
  // Clips 4-5 are silence: after the quiet period the page goes dormant on its own.
  await page.waitForFunction(()=>/Gone quiet/.test(document.querySelector('#status').textContent),null,{timeout:60000});
  const postsWhenQuiet=posts.length;
  // Clip 6: unnamed again, and dormant again: dropped.
  const uploadsWhenQuiet=uploads.length;
  const later=Date.now()+40000;while(uploads.length<=uploadsWhenQuiet&&Date.now()<later)await page.waitForTimeout(200);
  await page.waitForTimeout(2000);
  const droppedAgain=posts.length===postsWhenQuiet;
  if(!sabotage){
    assert.ok(droppedAgain,'dormant again: the unnamed clip must be dropped');
    assert.equal(await page.locator('.message.assistant').count(),2);
    assert.match(await page.locator('#status').textContent(),/Waiting for “Qwen”|Gone quiet/);
  }
  assert.deepEqual(errors,[]);
  if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
  console.log(assertionsComplete);
  if(sabotage){console.error('SABOTAGE PASSED (this is the failure): a clip without the wake word reached the model');process.exitCode=0;}
  else{fs.mkdirSync('evidence/browser',{recursive:true});
   await page.screenshot({path:'evidence/browser/wake.png',fullPage:true});
   console.log('PASS chromium: wake word gates the real microphone loop, strips the name, and goes dormant after the quiet period');}
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
