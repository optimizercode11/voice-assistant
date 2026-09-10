import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Thinking aloud.  The claim under test is not "a line appears": it is that the
// assistant *says* what it is doing while a reply is slow, that it says it from
// the tool the server actually reported rather than from a guess, that a fast
// reply is never padded with a fake "one moment", that one turn gets one
// acknowledgment however many tool rounds it takes, and that the acknowledgment
// never delays the answer.  Every one of those is a request the page makes, so
// the evidence is the ordered list of /tts bodies, not a screenshot.
const sabotage=process.argv.includes('--sabotage');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser think-aloud';
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
const {chromium} = await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0;

// Each script is one turn.  `at` is milliseconds after the previous event, so a
// slow turn really is slow and the page's own deadline is what decides.
const slowTool=[
 {at:0,type:'status',phase:'tool',round:1,calls:['search_notes']},
 {at:120,type:'tool',name:'search_notes',ok:true,ms:12,source:'retrieval',citations:[]},
 {at:1400,type:'answer',text:'The bridge is on 8092.',usage:{},tools:[],sources:[]}];
const fast=[
 {at:0,type:'answer',text:'Yes.',usage:{},tools:[],sources:[]}];
const slowNoTools=[
 {at:1500,type:'answer',text:'A quiet, unhurried answer.',usage:{},tools:[],sources:[]}];
const twoRounds=[
 {at:0,type:'status',phase:'tool',round:1,calls:['search_notes']},
 {at:80,type:'tool',name:'search_notes',ok:true,ms:9,source:'retrieval',citations:[]},
 {at:120,type:'status',phase:'tool',round:2,calls:['fetch_url']},
 {at:80,type:'tool',name:'fetch_url',ok:true,ms:40,source:'builtin',citations:[]},
 {at:900,type:'answer',text:'Both looked up.',usage:{},tools:[],sources:[]}];
const empty=[
 {at:0,type:'answer',text:'   ',usage:{},tools:[],sources:[]}];
const recovered=[
 {at:0,type:'answer',text:'Second time is fine.',usage:{},tools:[],sources:[]}];
const scripts=[slowTool,fast,slowNoTools,twoRounds,empty,recovered,slowTool];
let turn=0;
const spoken=[];          // every /tts body, in the order the page asked for it
const prompts=[];         // every /chat/completions body, to prove history stayed clean
const stream=res=>{
  res.writeHead(200,{'Content-Type':'application/x-ndjson','Cache-Control':'no-store'});
  const script=scripts[turn];
  let delay=0;
  script.forEach((event,index)=>{
    delay+=event.at;
    const {at,_ignore, ...payload}=event;
    setTimeout(()=>{
      res.write(JSON.stringify(payload)+'\n');
      if(index===script.length-1){res.end();turn++;}
    },delay);
  });
};
const server=http.createServer((req,res)=>{
  const url=req.url.split('?')[0];
  if(url==='/chat'||url==='/chat.js'){
    const file=url==='/chat'?'web/chat.html':'web/chat.js';
    let body=fs.readFileSync(file,'utf8');
    // Sabotage: the page still receives the progress stream and still renders
    // it, but never says anything.  If the assertions below still pass, the
    // acknowledgment was never load-bearing and this arm proves nothing.
    if(sabotage&&file.endsWith('.js')){
      const anchor="        mentionThinking(thinkLine(event.calls), id, controller.signal);";
      assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
      const armed=body.replace(anchor,deadSabotage?anchor:"        void thinkLine(event.calls);");
      if(deadSabotage)assert.equal(armed,body,'dead sabotage must leave the page unchanged');
      body=armed;mutations++;
    }
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    return res.end(body);
  }
  if(url==='/chat/health')return res.end(JSON.stringify({available:true,streaming:true,tools:['search_notes'],tool_sources:{search_notes:'retrieval'}}));
  if(url==='/languages')return res.end(JSON.stringify({default:'a',languages:[{code:'a',name:'English (US)'}]}));
  if(url==='/voices')return res.end(JSON.stringify({voices:['af_heart']}));
  if(url==='/approvals')return res.end(JSON.stringify({enabled:false,requests:[],grants:[]}));
  if(url==='/tts'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{spoken.push(text);res.setHeader('Content-Type','audio/wav');res.end(wav);});
    return;
  }
  if(url==='/chat/completions'&&req.method==='POST'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{prompts.push(text);return stream(res);});
    return;
  }
  res.writeHead(404);res.end();
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
try{
 const browser=await chromium.launch({headless:true,args:['--disable-gpu']});
 try{
 const context=await browser.newContext();
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 page.setDefaultTimeout(20000);
 await page.goto(`${base}/chat`);
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};
 const settled=async count=>{
   await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length===n,count);
   await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 };
 assert.equal(await page.locator('#think-aloud').isChecked(),true,'thinking aloud is on unless the person switches it off');

 // 1. a tool round is a second full generation: name it, out loud, before the answer
 let mark=spoken.length;
 await send('what port is the bridge on?');
 await settled(1);
 let said=spoken.slice(mark);
 assert.ok(said.length>=2,'a slow tool turn must ask for the acknowledgment and the reply');
 assert.equal(said[0],'Let me check your notes.',
   'the spoken line must come from the tool the server reported, not from a guess');
 assert.equal(said[said.length-1],'The bridge is on 8092.','the reply is still spoken');
 assert.ok(said.length===2,'one acknowledgment, not a stream of them');

 // 2. a fast reply is never padded
 mark=spoken.length;
 await send('yes or no?');
 await settled(2);
 assert.deepEqual(spoken.slice(mark),['Yes.'],
   'a reply that was never slow must not be given a fake "one moment"');

 // 3. slow with no tools at all: the deadline, not a tool event, does the talking
 mark=spoken.length;
 await send('think about it.');
 await settled(3);
 said=spoken.slice(mark);
 assert.equal(said[0],'One moment.','a slow turn with no tool still says something');
 assert.ok(said[said.length-1].includes('unhurried answer'),'and the answer still arrives');

 // 4. two tool rounds, one acknowledgment
 mark=spoken.length;
 await send('check both.');
 await settled(4);
 said=spoken.slice(mark);
 assert.equal(said.filter(line=>line.startsWith('Let me')).length,1,
   'one acknowledgment a turn, not one a tool round');

 // 5. an empty reply must not end the conversation, and must not enter history
 mark=spoken.length;
 await send('say nothing at all.');
 await page.waitForFunction(()=>document.querySelector('#status').textContent.toLowerCase().includes('empty'),
   undefined,{timeout:6000});
 assert.equal(await page.locator('.message.assistant').count(),4,
   'an empty reply is not a turn: it must not be committed to the transcript');
 assert.equal(spoken.length,mark,'nothing is sent to the TTS for an empty reply');

 await send('again please.');
 await settled(5);
 assert.equal((await page.locator('.message.assistant').last().locator('p').first().textContent()).trim(),
   'Second time is fine.','the conversation must still work after a refusal');
 const third=JSON.parse(prompts[5]);
 assert.ok(!third.messages.some(m=>m.role==='assistant'&&!String(m.content).trim()),
   'an empty assistant turn must never be replayed into later prompts');

 // 6. switching it off is honoured
 await page.locator('#think-aloud').uncheck();
 mark=spoken.length;
 await send('quietly now.');
 await settled(6);
 assert.deepEqual(spoken.slice(mark),['The bridge is on 8092.'],
   'the checkbox must actually silence the acknowledgment');

 assert.deepEqual(errors,[]);
 if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
 console.log(assertionsComplete);
 if(sabotage){
  console.error('SABOTAGE PASSED (this is the failure): the page stayed silent and the assertions still passed');
  process.exitCode=0;
 }else{
  fs.mkdirSync('evidence/browser',{recursive:true});
  await page.screenshot({path:'evidence/browser/chromium-think-aloud.png',fullPage:true});
  console.log('PASS chromium: slow tool turn named aloud, fast reply unpadded, deadline covers tool-less waits, one line per turn, empty reply survives, switch honoured');
 }
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
