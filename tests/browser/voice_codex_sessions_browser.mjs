// Real browser UI against a deterministic session manager and SSE stream.
import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
const {chromium} = await import(process.env.PLAYWRIGHT);
const listeners = new Set(), posts = [], selections = [], reads = [], tts = [];
const sessions = [
  {session_id:'backend',name:'Backend',cwd:'/projects/api',state:'running',turn_id:'b1',working_on:'Fix login'},
  {session_id:'tests',name:'Tests',cwd:'/projects/tests',state:'running',turn_id:'t1',working_on:'Check suite'},
  {session_id:'unsafe',name:'<img src=x onerror="window.badName=true">',cwd:'/projects/safe',state:'idle'},
];
const selected = new Map(); let enabled = true, eventNumber = 0;
function snapshot(context) { return {enabled,sessions,selected_session_id:selected.get(context) || null}; }
function push(type, data) {
  const event = {type,event_id:`e${++eventNumber}`,server:'codex',...data};
  for (const res of listeners) res.write(`event: ${type}\ndata: ${JSON.stringify(event)}\n\n`);
  return event;
}
function replay(type, event) { for (const res of listeners) res.write(`event: ${type}\ndata: ${JSON.stringify(event)}\n\n`); }
const server = http.createServer((req,res) => {
  if (req.url === '/events') {
    res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'}); res.write(': connected\n\n');
    listeners.add(res); req.on('close',()=>listeners.delete(res)); return;
  }
  const file = req.url === '/chat' ? 'web/chat.html' : req.url === '/chat.js' ? 'web/chat.js' : null;
  if (!file) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html'); res.end(fs.readFileSync(file));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const wav = Buffer.alloc(44 + 4800);
wav.write('RIFF',0);wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);
wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(24000,24);wav.writeUInt32LE(48000,28);
wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(4800,40);
for (let i=0;i<2400;i++) wav.writeInt16LE(Math.round(Math.sin(i/10)*1500),44+i*2);
const browser = await chromium.launch({headless:true,args:['--disable-gpu','--autoplay-policy=no-user-gesture-required']});
try {
  const page = await browser.newPage(), errors = []; page.on('pageerror',error=>errors.push(String(error)));
  page.setDefaultTimeout(15000);
  await page.route('**/chat/health',route=>route.fulfill({json:{available:true,events:true}}));
  await page.route('**/languages',route=>route.fulfill({json:{default:'a',languages:[{code:'a',name:'English'}]}}));
  await page.route('**/voices',route=>route.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/codex/sessions?*',route=>{
    const context = new URL(route.request().url()).searchParams.get('context_id'); reads.push(context);
    return route.fulfill({json:snapshot(context)});
  });
  await page.route('**/codex/sessions/select',route=>{
    const body=route.request().postDataJSON(); selections.push(body); selected.set(body.context_id,body.session);
    return route.fulfill({json:{enabled:true,context_id:body.context_id,
      session:sessions.find(session=>session.session_id===body.session),selected_session_id:body.session}});
  });
  await page.route('**/chat/completions',route=>{posts.push(route.request().postDataJSON());return route.fulfill({json:{text:'Understood.'}});});
  await page.route('**/tts?*',route=>{tts.push(route.request().postData());return route.fulfill({contentType:'audio/wav',body:wav});});
  const url=`http://127.0.0.1:${server.address().port}/chat`;
  await page.goto(url); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.waitForFunction(()=>document.querySelectorAll('.codex-session').length===3);
  await page.waitForFunction(()=>updateSource?.readyState===1);
  assert.equal(await page.locator('#codex-session-list img').count(),0,'session names are rendered as text, never HTML');
  assert.match(await page.locator('#codex-session-list').textContent(),/Fix login/);
  await page.locator('[data-session-id="backend"]').click();
  await page.waitForFunction(()=>document.querySelector('#codex-selected').textContent==='Selected: Backend');
  assert.equal(await page.locator('#codex-sessions').isVisible(),true,'a singular select response preserves session cards');
  assert.equal(await page.locator('.codex-session').count(),3);
  const context=selections[0].context_id;
  assert.ok(context && reads.includes(context),'selection uses the conversation context from session discovery');
  await page.fill('#text','Tell Backend to preserve the API'); await page.click('#send');
  await page.waitForFunction(()=>!busy);
  assert.equal(posts[0].context_id,context,'chat routing carries the same selected context');
  assert.equal(selected.get(posts[0].context_id),'backend');

  // Two sessions share a provider. Completing one must leave the other pending.
  push('working',{context_id:context,session_id:'backend',session_name:'Backend',turn_id:'b1',instruction:'Fix login'});
  push('working',{context_id:context,session_id:'tests',session_name:'Tests',turn_id:'t1',instruction:'Check suite'});
  await page.waitForFunction(()=>document.querySelectorAll('.pending').length===2);
  const done=push('update',{context_id:context,session_id:'tests',session_name:'Tests',turn_id:'t1',spoken:'The suite passed.'});
  replay('update',done);
  await page.waitForFunction(()=>document.querySelectorAll('.pending').length===1&&!busy);
  assert.equal(await page.locator('.pending').getAttribute('data-session-id'),'backend');
  assert.equal(await page.locator('#codex-selected').textContent(),'Selected: Backend','completion never changes focus');
  assert.equal(tts.filter(text=>text.includes('The suite passed.')).length,1,'replayed event speaks once');
  assert.equal(tts.filter(text=>text.includes('The suite passed.'))[0],'Codex · Tests: The suite passed.','speech identifies the session');

  // A late completion of the previous turn cannot erase a newer pending turn.
  push('working',{context_id:context,session_id:'backend',session_name:'Backend',turn_id:'b2',instruction:'Newer task'});
  push('update',{context_id:context,session_id:'backend',session_name:'Backend',turn_id:'b1',spoken:'Earlier turn finished.'});
  await page.waitForFunction(()=>!busy&&document.querySelector('.pending')?.dataset.turnId==='b2'&&document.querySelector('#messages').textContent.includes('Earlier turn finished.'));
  assert.equal(await page.locator('.pending').getAttribute('data-turn-id'),'b2');
  push('trace',{context_id:context,session_id:'tests',session_name:'Tests',kind:'command',line:'make test'});
  await page.waitForFunction(()=>document.querySelector('#console-log').textContent.includes('Codex · Tests command: make test'));
  sessions[1].state='completed'; sessions[1].working_on='';
  const readCount=reads.length; push('session',{context_id:context,session:sessions[1]});
  await page.waitForFunction(()=>document.querySelector('.codex-session[data-session-id="tests"] .codex-state').textContent==='completed');
  assert.ok(reads.length>readCount,'session event refreshes authoritative state');
  assert.equal(await page.locator('#codex-selected').textContent(),'Selected: Backend');

  push('working',{context_id:'other-chat',session_id:'foreign',session_name:'Foreign',turn_id:'f1',instruction:'Unrelated'});
  push('update',{context_id:'other-chat',session_id:'foreign',session_name:'Foreign',turn_id:'f1',spoken:'Unrelated result.'});
  await page.waitForTimeout(250);
  assert.equal(await page.locator('.pending[data-session-id="foreign"]').count(),0);
  assert.ok(!tts.some(text=>text.includes('Unrelated result')),'another conversation cannot speak into this one');
  await page.reload(); await page.waitForFunction(()=>document.querySelector('#codex-selected').textContent==='Selected: Backend');
  assert.equal(reads.at(-1),context,'saved conversation keeps its routing context after reload');
  await page.waitForFunction(()=>updateSource?.readyState===1);
  await page.evaluate(()=>{paused=true;});
  push('update',{context_id:context,session_id:'backend',session_name:'Backend',turn_id:'b3',spoken:'Old conversation queued result.'});
  await page.waitForFunction(()=>pendingUpdates.length===1);
  await page.click('#new'); await page.waitForFunction(()=>document.querySelector('#codex-selected').textContent==='No session selected');
  assert.notEqual(reads.at(-1),context,'a new conversation has independent focus');
  assert.equal(await page.evaluate(()=>pendingUpdates.length),0,'switching conversation discards its queued announcements');
  assert.ok(!tts.some(text=>text.includes('Old conversation queued result')));
  enabled=false; push('session',{});
  await page.waitForFunction(()=>document.querySelector('#codex-sessions').hidden);
  await page.unroute('**/codex/sessions?*');
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.locator('#codex-sessions').isHidden(),true,'missing optional endpoint keeps legacy voice usable');
  assert.deepEqual(errors,[]);
  console.log('PASS browser: named cards, context routing, independent pending turns, attributed and deduplicated speech, authoritative state, safe names, legacy endpoint fallback');
} finally { await browser.close(); server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
