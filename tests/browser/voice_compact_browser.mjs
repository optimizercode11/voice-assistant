import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
const {chromium}=await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const prompts=[],compactions=[],spoken=[];
let compactMode='ok',releaseCompact=null;
const wav=Buffer.alloc(44+960);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVE',8);wav.write('fmt ',12);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(24000,24);wav.writeUInt32LE(48000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(960,40);
const server=http.createServer((req,res)=>{
 const url=req.url.split('?')[0];
 if(url==='/chat'||url==='/chat.js'){res.setHeader('Content-Type',url.endsWith('.js')?'text/javascript':'text/html');return res.end(fs.readFileSync(url==='/chat'?'web/chat.html':'web/chat.js'));}
 if(url==='/chat/health')return res.end(JSON.stringify({available:true,streaming:false,tools:[],https_port:0}));
 if(url==='/languages')return res.end(JSON.stringify({default:'a',languages:[{code:'a',name:'English'}]}));
 if(url==='/voices')return res.end(JSON.stringify({voices:['af_heart']}));
 if(url==='/approvals')return res.end(JSON.stringify({enabled:false,requests:[],grants:[]}));
 let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
  if(url==='/tts'){spoken.push(body);res.setHeader('Content-Type','audio/wav');return res.end(wav);}
  if(url==='/chat/completions'){prompts.push(JSON.parse(body));return res.end(JSON.stringify({text:'A fresh reply.',usage:{}}));}
  if(url==='/chat/compact'){
   const payload=JSON.parse(body);compactions.push(payload);
   const finish=()=>res.end(JSON.stringify({summary:`Summary ${compactions.length}: ${payload.messages[0].content}`,usage:{}}));
   if(compactMode==='delay'){releaseCompact=finish;return;}
   if(compactMode==='error'){res.writeHead(503);return res.end(JSON.stringify({error:'model busy'}));}
   if(compactMode==='empty')return res.end(JSON.stringify({summary:' '}));
   if(compactMode==='truncated')return res.end(JSON.stringify({summary:'incomplete',finish_reason:'length'}));
   return finish();
  }
  res.writeHead(404);res.end();
 });
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true,args:['--disable-gpu','--autoplay-policy=no-user-gesture-required']});
const errors=[];
async function ready(page){await page.waitForFunction(()=>!document.querySelector('#send').disabled);}
async function records(page){return page.evaluate(()=>new Promise((resolve,reject)=>{const req=indexedDB.open('voice-conversations',1);req.onsuccess=()=>{const db=req.result,tx=db.transaction('chats'),get=tx.objectStore('chats').getAll();tx.oncomplete=()=>{db.close();resolve(get.result);};tx.onerror=()=>reject(tx.error);};}));}
async function seed(page,items,id){await page.evaluate(({items,id})=>new Promise((resolve,reject)=>{
 const req=indexedDB.open('voice-conversations',1);req.onsuccess=()=>{const db=req.result,tx=db.transaction('chats','readwrite');for(const item of items)tx.objectStore('chats').put(item);tx.oncomplete=()=>{db.close();location.hash='#/chat/'+id;resolve();};tx.onerror=()=>reject(tx.error);};
}),{items,id});await page.reload();await ready(page);}
function chat(id,count,at=Date.now()){return {v:3,id,title:id,at,turns:Array.from({length:count},(_,i)=>({q:`${id} question ${i+1}`,a:`${id} answer ${i+1}`}))};}
async function send(page,text,count){await page.locator('#text').fill(text);await page.locator('#send').click();await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length===n,count);await ready(page);}
async function compact(page){await page.locator('#compact').click();await page.waitForFunction(()=>document.querySelector('#compact-status').textContent.startsWith('Compacted.'));await ready(page);}
try{
 const context=await browser.newContext();const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));page.setDefaultTimeout(15000);
 await page.goto(base+'/chat');await ready(page);
 await seed(page,[chat('long',45)],'long');
 await send(page,'Remember the beginning',46);
 assert.equal(prompts.at(-1).messages.length,91);assert.equal(prompts.at(-1).messages[0].content,'long question 1');
 const spokenBefore=spoken.length;
 await compact(page);
 assert.equal(compactions[0].messages.length,80,'forty older turns summarized, six retained');
 assert.equal(compactions[0].messages.at(-1).role,'assistant');assert.equal(compactions[0].summary,undefined);
 let record=(await records(page)).find(r=>r.id==='long');
 assert.equal(record.turns.length,46);assert.equal(record.summaryThrough,40);assert.equal(record.summary,'Summary 1: long question 1');
 assert.equal(await page.locator('.message.assistant').count(),46,'archive remains visible');
 assert.equal(spoken.length,spokenBefore,'compaction is never spoken');
 assert.match(await page.locator('#turns').textContent(),/40 turns summarized \+ 6 recent turns in context/);
 await page.reload();await ready(page);
 assert.equal(await page.locator('.message.assistant').count(),46);await page.locator('#compact-summary summary').click();
 assert.equal(await page.locator('#compact-summary-text').textContent(),record.summary);
 await send(page,'Continue after reload',47);
 assert.equal(prompts.at(-1).summary,record.summary);assert.equal(prompts.at(-1).messages.length,13);
 assert.equal(prompts.at(-1).messages[0].content,'long question 41');
 await compact(page);
 assert.equal(compactions[1].summary,record.summary);assert.deepEqual(compactions[1].messages,[{role:'user',content:'long question 41'},{role:'assistant',content:'long answer 41'}],'repeat rolls summary forward with only newly covered turns');
 record=(await records(page)).find(r=>r.id==='long');assert.equal(record.summaryThrough,41);assert.equal(record.turns.length,47);
 for(const failure of ['error','empty','truncated']){
  compactMode=failure;await page.locator('#compact').click();await page.waitForFunction(()=>document.querySelector('#compact-status').textContent.startsWith('Compaction failed:'));await ready(page);
  const failed=(await records(page)).find(r=>r.id==='long');assert.equal(failed.summary,record.summary);assert.equal(failed.summaryThrough,41);
 }
 // Cancel a delayed response, then deliver it; no summary is allowed to land.
 compactMode='delay';await page.locator('#compact').click();await page.waitForFunction(()=>document.querySelector('#compact-status').textContent.startsWith('Compacting'));
 while(!releaseCompact)await new Promise(resolve=>setTimeout(resolve,10));
 await page.locator('#interrupt').click();releaseCompact();releaseCompact=null;await ready(page);
 assert.match(await page.locator('#compact-status').textContent(),/cancelled/);
 assert.equal((await records(page)).find(r=>r.id==='long').summary,record.summary);
 // Switching and starting new chats during compaction cannot inherit its result.
 await seed(page,[chat('other',4)],'long');
 for(const destination of ['other','new']){
  await page.evaluate(()=>{location.hash='#/chat/long';});await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===47);
  await page.locator('#compact').click();while(!releaseCompact)await new Promise(resolve=>setTimeout(resolve,10));
  if(destination==='new')await page.locator('#new').click();else {await page.evaluate(()=>{location.hash='#/chat/other';});await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===4);}
  releaseCompact();releaseCompact=null;await ready(page);
  assert.equal(await page.locator('#compact-summary').isVisible(),false);
  assert.equal((await records(page)).find(r=>r.id==='long').summary,record.summary);
 }
 // More than the old sixty-chat limit, each independently restored and continued.
 compactMode='ok';const many=Array.from({length:75},(_,i)=>chat('saved-'+i,3,Date.now()+i));
 await seed(page,many,'saved-0');await send(page,'Keep the oldest saved chat',4);
 let all=await records(page);assert.equal(all.length,77);assert.ok(many.every(c=>all.some(r=>r.id===c.id)));
 await page.reload();await ready(page);await page.locator('#chats-toggle').click();assert.equal(await page.locator('.chat-entry').count(),77);
 await page.locator('.chat-entry[data-id="saved-74"] .chat-open').click();await send(page,'Continue the newest',4);
 assert.equal(prompts.at(-1).messages[0].content,'saved-74 question 1');
 assert.equal((await records(page)).find(r=>r.id==='saved-0').turns.at(-1).q,'Keep the oldest saved chat');
 // A separate tab continues a different conversation without changing this one.
 const other=await context.newPage();await other.goto(base+'/chat#/chat/saved-0');await ready(other);await send(other,'Independent tab',5);
 await send(page,'Stay in this chat',5);assert.equal(prompts.at(-1).messages[0].content,'saved-74 question 1');
 assert.equal((await records(page)).find(r=>r.id==='saved-0').turns.at(-1).q,'Independent tab');await other.close();
 // Simulated quota failure must never delete chats or trim the in-memory archive.
 await page.evaluate(()=>{const original=IDBDatabase.prototype.transaction;IDBDatabase.prototype.transaction=function(...args){if(args[1]==='readwrite')throw new DOMException('Quota exceeded','QuotaExceededError');return original.apply(this,args);};});
 const persisted=(await records(page)).find(r=>r.id==='saved-74');await send(page,'Keep me even when storage is full',6);
 assert.match(await page.locator('#turns').textContent(),/not saved in this browser/);
 assert.equal((await records(page)).length,77);assert.deepEqual((await records(page)).find(r=>r.id==='saved-74'),persisted);
 assert.equal(await page.locator('.message.user').last().locator('p').first().textContent(),'Keep me even when storage is full');
 const downloadPromise=page.waitForEvent('download');await page.locator('#export-chat').click();const download=await downloadPromise;
 const exported=JSON.parse(fs.readFileSync(await download.path(),'utf8'));assert.equal(exported.turns.length,6);assert.equal(exported.turns.at(-1).q,'Keep me even when storage is full');
 await page.locator('.chat-entry[data-id="saved-0"] .chat-open').click();await page.locator('.chat-entry[data-id="saved-74"] .chat-open').click();
 assert.equal(await page.locator('.message.assistant').count(),6,'unsaved archive remains switchable in memory');
 assert.match(await page.locator('#turns').textContent(),/not saved in this browser/);
 // Two tabs continuing the same revision must preserve both branches.
 await page.reload();await ready(page);
 const sibling=await context.newPage();await sibling.goto(base+'/chat#/chat/saved-74');await ready(sibling);
 await send(page,'First tab branch',6);await send(sibling,'Second tab branch',6);
 const siblingId=await sibling.evaluate(()=>decodeURIComponent(location.hash.split('/').at(-1)));
 assert.notEqual(siblingId,'saved-74','a stale tab saves a separate branch instead of overwriting');
 all=await records(page);assert.equal(all.find(r=>r.id==='saved-74').turns.at(-1).q,'First tab branch');
 assert.equal(all.find(r=>r.id===siblingId).turns.at(-1).q,'Second tab branch');
 await sibling.reload();await ready(sibling);assert.equal(await sibling.locator('.message.user').last().locator('p').textContent(),'Second tab branch');await sibling.close();
 // Compaction itself can fork a stale same-chat revision and must release busy.
 const compactSibling=await context.newPage();await compactSibling.goto(base+'/chat#/chat/saved-74');await ready(compactSibling);
 await send(page,'Advance while another tab compacts',7);await compact(compactSibling);
 const compactForkId=await compactSibling.evaluate(()=>decodeURIComponent(location.hash.split('/').at(-1)));
 assert.notEqual(compactForkId,'saved-74');
 assert.equal(await compactSibling.locator('#send').isEnabled(),true,'compaction fork releases the busy state');
 all=await records(page);assert.equal(all.find(r=>r.id===compactForkId).summaryThrough,4);
 assert.equal(all.find(r=>r.id==='saved-74').summaryThrough,0);
 await send(compactSibling,'Continue compacted fork',7);assert.ok(prompts.at(-1).summary);assert.equal(prompts.at(-1).messages.length,5);
 await compactSibling.close();
 // Failed migration keeps legacy storage untouched and the readable transcript.
 const blocked=await browser.newContext();await blocked.addInitScript(()=>{
  localStorage.setItem('voice-chat',JSON.stringify({id:'legacy-safe',turns:[{q:'Legacy question',a:'Legacy answer'}]}));
  Object.defineProperty(window,'indexedDB',{get(){throw new Error('storage disabled');}});
 });const blockedPage=await blocked.newPage();await blockedPage.goto(base+'/chat');await ready(blockedPage);
 assert.ok(await blockedPage.evaluate(()=>localStorage.getItem('voice-chat')));assert.equal(await blockedPage.locator('.message.assistant').count(),1);
 assert.match(await blockedPage.locator('#turns').textContent(),/not saved in this browser/);await blocked.close();
 // A new-build tab writing between migration's list read and insert wins;
 // migration must preserve both the newer database record and the old key.
 const racing=await browser.newContext();await racing.addInitScript(()=>{
  localStorage.setItem('voice-chat',JSON.stringify({id:'migration-race',turns:[{q:'Old legacy',a:'Old answer'}]}));
  const original=IDBObjectStore.prototype.getAll;let injected=false;
  IDBObjectStore.prototype.getAll=function(...args){
   const req=original.apply(this,args),db=this.transaction.db;
   if(!injected){injected=true;req.addEventListener('success',()=>{
    const tx=db.transaction('chats','readwrite');tx.objectStore('chats').put({id:'migration-race',revision:'newer-tab',turns:[{q:'Newer tab',a:'Keep this answer'}]});
   });}return req;
  };
 });const racingPage=await racing.newPage();await racingPage.goto(base+'/chat');await ready(racingPage);
 assert.equal(await racingPage.locator('.message.user p').textContent(),'Newer tab');
 assert.equal((await records(racingPage)).find(r=>r.id==='migration-race').revision,'newer-tab');
 assert.ok(await racingPage.evaluate(()=>localStorage.getItem('voice-chat')),'conflicting legacy copy survives');await racing.close();
 assert.deepEqual(errors,[]);
 console.log('PASS chromium: full context beyond 40 turns; compaction/reload/continue; repeated and failed compaction; cancel/switch/new races; 77 saved chats and independent tabs; concurrent same-chat revisions fork without lost turns; quota failure preserves archive and export; failed migration keeps legacy data');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
