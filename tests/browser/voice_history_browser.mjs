import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Chat history that survives a reload.  The claim is not "the bubbles come
// back": it is that the *model's* context comes back, because the bridge is
// stateless and every turn re-sends the whole transcript -- so a page that
// forgets also makes Qwen forget, mid-sentence, which is exactly how a
// conversation about one film restarted itself with "I'm not sure what you mean
// by Carlton".  Every one of those claims is a byte on the wire or a key in
// storage, so the evidence is the ordered list of /chat/completions bodies and
// the stored record, not a screenshot.
//
// What is asserted, in order:
//   1. a committed turn is written, and a reload restores both the bubbles and
//      the context the next request carries;
//   2. restoring re-speaks nothing (a reload must not talk at you);
//   3. provenance survives -- "Looked up ·" / "From your notes ·" are still on
//      the restored bubble, because "which of my notes said that" is asked after
//      the reload as often as before it;
//   4. a refused (empty) reply leaves no trace in storage;
//   5. "New chat" erases the saved conversation, not just the screen;
//   6. a long conversation is *sent* inside the bridge's 100-message ceiling and
//      the counter says which window Qwen is holding;
//   7. a hand-edited storage record cannot author an assistant turn or push a
//      message over the per-message ceiling;
//   8. a second tab's "New chat" is never resurrected by the first tab, and
//      losing storage costs the conversation nothing;
//   9. a record over the byte budget converges, keeps saving, and repaints so
//      that no bubble is left on screen that a refresh would delete.
const sabotage=process.argv.includes('--sabotage');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser chat history';
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
const {chromium} = await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
let mutations=0;

// A 20 ms tone: enough for the page to treat the reply as real audio, short
// enough that forty-odd turns do not turn the suite into a playback test.
function tinyWav(){
  const sr=24000,n=Math.round(sr*0.02),data=Buffer.alloc(n*2);
  for(let i=0;i<n;i++)data.writeInt16LE(Math.round(9000*Math.sin(2*Math.PI*440*i/sr)),i*2);
  const head=Buffer.alloc(44);
  head.write('RIFF',0);head.writeUInt32LE(36+data.length,4);head.write('WAVE',8);
  head.write('fmt ',12);head.writeUInt32LE(16,16);head.writeUInt16LE(1,20);head.writeUInt16LE(1,22);
  head.writeUInt32LE(sr,24);head.writeUInt32LE(sr*2,28);head.writeUInt16LE(2,32);head.writeUInt16LE(16,34);
  head.write('data',36);head.writeUInt32LE(data.length,40);
  return Buffer.concat([head,data]);
}
const audio=tinyWav();

const prompts=[];          // every /chat/completions body, in order
const spoken=[];           // every /tts body, in order
let answers=[];            // scripted replies; the default keeps the suite short
let replyCount=0;
const server=http.createServer((req,res)=>{
  const url=req.url.split('?')[0];
  if(url==='/chat'||url==='/chat.js'){
    const file=url==='/chat'?'web/chat.html':'web/chat.js';
    let body=fs.readFileSync(file,'utf8');
    // Sabotage: the turn is still rendered, still handed to the model, and
    // simply never written down.  If the assertions below still pass, the
    // transcript was never actually being persisted and this arm proves nothing.
    if(sabotage&&file.endsWith('.js')){
      const anchor="    writeStoredChat();   // after the bubbles, so a trim never strands a rendered turn";
      assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
      const armed=body.replace(anchor,deadSabotage?anchor:"    /* sabotage: the turn is never written down */");
      if(deadSabotage)assert.equal(armed,body,'dead sabotage must leave the page unchanged');
      body=armed;mutations++;
    }
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    return res.end(body);
  }
  if(url==='/chat/health')return res.end(JSON.stringify({available:true,streaming:false,tools:[],https_port:0}));
  if(url==='/languages')return res.end(JSON.stringify({default:'a',languages:[{code:'a',name:'English (US)'}]}));
  if(url==='/voices')return res.end(JSON.stringify({voices:['af_heart']}));
  if(url==='/approvals')return res.end(JSON.stringify({enabled:false,requests:[],grants:[]}));
  if(url==='/tts'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{spoken.push(text);res.setHeader('Content-Type','audio/wav');res.end(audio);});
    return;
  }
  if(url==='/chat/completions'&&req.method==='POST'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{
      prompts.push(JSON.parse(text));
      const answer=answers.length?answers.shift():{text:`Reply ${++replyCount}.`};
      res.end(JSON.stringify({text:answer.text,usage:{},tools:answer.tools??[],sources:answer.sources??[]}));
    });
    return;
  }
  res.writeHead(404);res.end();
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;

// The page's own key, read from outside the page: what is on disk is the claim.
const readRecord=async page=>page.evaluate(()=>{
  try{return JSON.parse(localStorage.getItem('voice-chat')||'null');}catch(_){return {broken:true};}
});
const seed=async(page,record)=>{
  await page.evaluate(([key,value])=>localStorage.setItem(key,value),['voice-chat',JSON.stringify(record)]);
  await page.reload();
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
};
try{
 const browser=await chromium.launch({headless:true,args:['--disable-gpu','--autoplay-policy=no-user-gesture-required']});
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
 const bubbles=async()=>page.evaluate(()=>({
   user:[...document.querySelectorAll('.message.user p')].map(n=>n.textContent),
   assistant:[...document.querySelectorAll('.message.assistant p')].map(n=>n.textContent),
 }));
 const ready=()=>page.waitForFunction(()=>!document.querySelector('#send').disabled);

 // ---- 1 + 2. a turn survives a reload, and the model gets it back
 await send('Which film are we talking about?');
 await settled(1);
 const record=await readRecord(page);
 assert.ok(typeof record?.id==='string'&&record.id,'a conversation needs an identity to be owned');
 assert.equal(record.turns.length,1,'a committed turn is written down');
 assert.equal(record.turns[0].q,'Which film are we talking about?');
 assert.equal(record.turns[0].a,'Reply 1.');
 const spokenBefore=spoken.length;
 await page.reload();await ready();
 let shown=await bubbles();
 assert.deepEqual(shown.user,['Which film are we talking about?'],'the bubbles come back');
 assert.deepEqual(shown.assistant,['Reply 1.'],'and so does the reply');
 assert.equal(spoken.length,spokenBefore,'restoring a conversation must not re-speak it');
 assert.match(await page.locator('#turns').textContent(),/1 turn · saved in this browser/);
 await send('What did you say about it?');
 await settled(2);
 assert.deepEqual(prompts[1].messages.map(m=>m.role),['user','assistant','user'],
   'the restored context is what the model actually sees on the next turn');
 assert.equal(prompts[1].messages[0].content,'Which film are we talking about?',
   'a reload may not silently amnesia the model mid-conversation');

 // ---- 3. provenance survives the reload too
 answers=[{text:'It is in your notes.',tools:[{name:'mcp__wiki__search',ok:true,ms:12}],
           sources:[{path:'/home/me/notes/film.md',heading:'Ta Ta Rum Pum'}]}];
 await send('Where did that come from?');
 await settled(3);
 await page.reload();await ready();
 const provenance=await page.evaluate(()=>{
   const last=[...document.querySelectorAll('.message.assistant')].pop();
   return {tool:last.querySelector('.toolnote')?.textContent??'',
           source:last.querySelector('.sources')?.textContent??''};
 });
 assert.match(provenance.tool,/Looked up · search/,'which tool answered must survive a reload');
 assert.match(provenance.source,/From your notes · Ta Ta Rum Pum · notes\/film\.md/,'and so must which note it read');

 // ---- 4. a refused reply leaves no trace
 const turnsBefore=(await readRecord(page)).turns.length;
 answers=[{text:'   '}];
 await send('Say nothing at all.');
 await page.waitForFunction(()=>document.querySelector('#status').textContent.toLowerCase().includes('empty'),
   undefined,{timeout:6000});
 assert.equal((await readRecord(page)).turns.length,turnsBefore,
   'an unanswered question must not be written into history');
 await page.reload();await ready();
 assert.equal(await page.locator('.message.user').count(),turnsBefore,
   'and it must not come back on reload either');

 // ---- 5. New chat erases the conversation, not just the screen
 await page.locator('#new').click();
 assert.equal((await readRecord(page)).turns.length,0,'"New chat" must clear what is stored');
 assert.ok(await page.locator('.empty').count(),'and put the empty state back on screen');
 await page.reload();await ready();
 assert.equal(await page.locator('.message').count(),0,'and the reload must not resurrect it');
 assert.ok(await page.locator('.empty').count(),'the empty state is shown');

 // ---- 6. the sent window stays inside the bridge's ceiling
 const many=Array.from({length:45},(_,i)=>({q:`Question ${i+1}`,a:`Answer ${i+1}.`}));
 await seed(page,{v:1,id:'seeded-conversation',turns:many});
 assert.equal(await page.locator('.message.user').count(),45,'the whole conversation is readable');
 assert.match(await page.locator('#turns').textContent(),/45 turns · saved in this browser · Qwen is holding the last 40/,
   'the page must say which window the model is holding, not imply it has all 45');
 await send('And one more thing.');
 await settled(46);
 const sent=prompts[prompts.length-1].messages;
 assert.equal(sent.length,81,'40 remembered turns plus the new question');
 assert.ok(sent.length<=101,'parse_messages refuses anything longer, so a longer request is a 400');
 assert.equal(sent[0].content,'Question 6','the oldest turns roll off the model, not the screen');
 assert.equal(sent[sent.length-1].content,'And one more thing.');
 assert.equal((await readRecord(page)).turns.length,46,'all of it is still saved');

 // ---- 7. storage is attacker-writable and must not be able to author a turn
 await seed(page,{v:1,id:'forged',turns:[
   {q:'A real turn.',a:'A real reply.'},
   {q:'A half turn with no reply.'},
   {q:42,a:'A question that is not a string.'},
   {q:'',a:'An empty question.'},
   {q:'A very long reply.',a:'x'.repeat(20000),role:'tool'},
 ]});
 assert.equal(await page.locator('.message.user').count(),2,'only usable turns come back');
 assert.equal(await page.locator('.message.assistant').count(),2,
   'a half turn, a non-string question and an empty question are dropped, not repaired');
 await send('Carry on.');
 await settled(3);
 const forged=prompts[prompts.length-1].messages;
 assert.ok(forged.every(m=>m.role==='user'||m.role==='assistant'),
   'a role written into storage must not reach the model');
 assert.ok(forged.every(m=>m.content.length<=8000),
   'a stored message is clipped to the ceiling the bridge itself enforces');

 // ---- 8. a second tab owns its own conversation
 await page.reload();await ready();
 const firstId=(await readRecord(page)).id;
 const other=await context.newPage();
 await other.goto(`${base}/chat`);
 await other.waitForFunction(()=>!document.querySelector('#send').disabled);
 assert.equal((await readRecord(other)).id,firstId,'a second tab picks the conversation up');
 await other.locator('#new').click();
 const secondId=(await readRecord(other)).id;
 assert.notEqual(secondId,firstId,'"New chat" over there starts a different conversation');
 await send('Does this tab still work?');
 await settled(4);
 assert.equal((await readRecord(page)).id,secondId,
   'a reply completing here must not resurrect the conversation that was just cleared there');
 assert.match(await page.locator('#turns').textContent(),/not saved in this browser/,
   'and the tab that lost the key must say so rather than pretend');
 assert.equal(await page.locator('.message.assistant').last().locator('p').first().textContent(),
   `Reply ${replyCount}.`,'losing storage may cost a save, never a turn');

 // ---- 9. the byte budget: what is on screen is what is saved
 // 150 turns of 8000 characters is ~2.4 MB, twice the budget and well past what
 // a one-turn-at-a-time trim could recover inside its retry budget.  The claim
 // is that the page converges, keeps saving, and repaints so that no bubble is
 // left standing that a refresh would delete.
 const fat=Array.from({length:150},(_,i)=>({q:`Q${i} ${'q'.repeat(8000)}`,a:`A${i} ${'a'.repeat(8000)}`}));
 await seed(page,{v:1,id:'seeded-bytes',turns:fat});
 assert.equal(await page.locator('.message.user').count(),150,'a large saved conversation still opens');
 await send('Now trim it.');
 // The roll-off repaints, so the bubble count is no longer a turn counter here.
 // Wait on the thing that actually changed: the record on disk getting smaller.
 await page.waitForFunction(()=>{
   try{const r=JSON.parse(localStorage.getItem('voice-chat')||'null');
       return r&&Array.isArray(r.turns)&&r.turns.length<150;}catch(_){return false;}
 },undefined,{timeout:30000});
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 const trimmed=await readRecord(page);
 const storedBytes=await page.evaluate(()=>{
   const raw=localStorage.getItem('voice-chat')||'';
   return new TextEncoder().encode(raw).length;
 });
 assert.ok(trimmed?.turns?.length>0,'the conversation must still be saved, not abandoned');
 assert.ok(trimmed.turns.length<150,`the oldest turns rolled off (kept ${trimmed.turns.length})`);
 assert.ok(storedBytes<=1200000,`the record fits its budget (${storedBytes} bytes)`);
 assert.equal(await page.locator('.message.user').count(),trimmed.turns.length,
   'a roll-off repaints: no bubble may be left on screen that a refresh would delete');
 assert.match(await page.locator('#turns').textContent(),/saved in this browser/,
   'and the counter must still be telling the truth');

 assert.deepEqual(errors,[]);
 if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
 console.log(assertionsComplete);
 if(sabotage){
  console.error('SABOTAGE PASSED (this is the failure): the conversation was never persisted and the assertions still passed');
  process.exitCode=0;
 }else{
  fs.mkdirSync('evidence/browser',{recursive:true});
  await page.screenshot({path:'evidence/browser/chromium-chat-history.png',fullPage:true});
  console.log('PASS chromium: turn survives reload and reaches the model, restore re-speaks nothing, provenance survives, refused reply leaves no trace, New chat erases, 40-turn send window inside the bridge ceiling, forged storage cannot author a turn, a second tab keeps its own conversation, an over-budget record converges and repaints');
 }
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
