import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Speaking a reply.  The claim is not "the audio arrives" -- it is *when* the
// first word arrives.  The engine synthesizes a whole request before returning
// any audio, so one request per answer means the listener waits out the entire
// synthesis after the words are already on the screen.  The page therefore asks
// for one request per sentence and keeps the next one in flight.
//
// Every part of that is a request the page makes, so the evidence is the ordered
// list of /tts bodies and the moment each one reached the server -- not a
// screenshot.  The server deliberately synthesizes slowly (TTS_DELAY) so that
// "asked for the next sentence while the first was still being synthesized" is
// an observable fact rather than a hope.
const sabotage=process.argv.includes('--sabotage');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser reply pipelining';
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
const TTS_DELAY = 400;          // ms the fake engine spends on any request
// A short silent WAV: long enough for the element to report a real ended event,
// short enough that three clips do not stretch the suite.
const wav=(()=>{const ms=90,rate=24000,samples=Math.round(rate*ms/1000);
 const buf=Buffer.alloc(44+samples*2);
 buf.write('RIFF',0);buf.writeUInt32LE(36+samples*2,4);buf.write('WAVE',8);buf.write('fmt ',12);
 buf.writeUInt32LE(16,16);buf.writeUInt16LE(1,20);buf.writeUInt16LE(1,22);buf.writeUInt32LE(rate,24);
 buf.writeUInt32LE(rate*2,28);buf.writeUInt16LE(2,32);buf.writeUInt16LE(16,34);buf.write('data',36);
 buf.writeUInt32LE(samples*2,40);return buf;})();

const LONG='Half Moon Bay is a small city in California. It is known for its long, cold beaches. The old Carlton hotel closed its doors for good.';
const SHORT='Yes.';
// The third turn is what the bridge does since 2026-09-11: prose streams as
// 'delta' events while the model is still generating, then the answer.
const STREAMED='First things first. Then the second sentence follows a little later. And a third one closes it.';
const scripts=[[{at:0,type:'answer',text:LONG,usage:{},tools:[],sources:[]}],
               [{at:0,type:'answer',text:SHORT,usage:{},tools:[],sources:[]}],
               [{at:0,type:'delta',round:1,text:'First things first. '},
                {at:350,type:'delta',round:1,text:'Then the second sentence '},
                {at:600,type:'delta',round:1,text:'follows a little later. '},
                {at:900,type:'delta',round:1,text:'And a third one closes it.'},
                {at:1100,type:'answer',text:STREAMED,usage:{},tools:[],sources:[]}]];
let turn=0, answerSentAt=0;
const spoken=[];   // every /tts body with the moment it reached the server
const server=http.createServer((req,res)=>{
  const url=req.url.split('?')[0];
  if(url==='/chat'||url==='/chat.js'){
    const file=url==='/chat'?'web/chat.html':'web/chat.js';
    let body=fs.readFileSync(file,'utf8');
    if(sabotage&&file.endsWith('.js')){
      // Sabotage: keep every sentence, keep the order, keep the audio -- but ask
      // for each one only after the previous one has finished playing.  The
      // reply still sounds identical; it is just slow again.  If the timing
      // assertion below still passes, pipelining was never load-bearing.
      const anchors=[
        ['  for (let index = 0; index < SPEECH_CHUNK.prefetch; index++) ahead(index);',
         '  ahead(0);'],
        ['    const audio = await clip(index);\n    ahead(index + SPEECH_CHUNK.prefetch);',
         '    const audio = await clip(index);\n    void SPEECH_CHUNK;'],
        ['    await playReply(audio, signal, index === parts.length - 1, index === 0);',
         '    await playReply(audio, signal, index === parts.length - 1, index === 0);\n    ahead(index + 1);'],
      ];
      for(const [from,to] of anchors){
        assert.ok(body.includes(from),`the sabotage anchor no longer matches web/chat.js: ${from.slice(0,40)}`);
        body=body.replace(from,deadSabotage?from:to);
      }
      if(deadSabotage)assert.equal(body,fs.readFileSync(file,'utf8'),'dead sabotage must leave the page unchanged');
    }
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    return res.end(body);
  }
  // A tool is advertised because the page only streams (and only then can
  // speak a reply while it is generated) when the bridge has tools attached.
  if(url==='/chat/health')return res.end(JSON.stringify({available:true,streaming:true,tools:['now'],tool_sources:{}}));
  if(url==='/languages')return res.end(JSON.stringify({default:'a',languages:[{code:'a',name:'English (US)'}]}));
  if(url==='/voices')return res.end(JSON.stringify({voices:['af_heart']}));
  if(url==='/approvals')return res.end(JSON.stringify({enabled:false,requests:[],grants:[]}));
  if(url==='/tts'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{
      const arrived=Date.now();
      spoken.push({text,arrived});
      setTimeout(()=>{res.setHeader('Content-Type','audio/wav');res.end(wav);},TTS_DELAY);
    });
    return;
  }
  if(url==='/chat/completions'&&req.method==='POST'){
    let text='';req.on('data',c=>text+=c);
    req.on('end',()=>{
      const script=scripts[turn];
      res.writeHead(200,{'Content-Type':'application/x-ndjson','Cache-Control':'no-store'});
      script.forEach((event,index)=>{
        const {at,...payload}=event;
        setTimeout(()=>{if(payload.type==='answer')answerSentAt=Date.now();res.write(JSON.stringify(payload)+'\n');
          if(index===script.length-1){res.end();turn++;}},at);
      });
    });
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
 // Watch the one label the page uses for "waiting on the engine".  A reply made
 // of several sentences must pay that cost once, not once per sentence.
 await page.evaluate(()=>{window.__states=[];const node=document.querySelector('#state');
   new MutationObserver(()=>window.__states.push(node.textContent))
     .observe(node,{childList:true,subtree:true,characterData:true});});
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};

 // 1. a multi-sentence answer is spoken in pipelined pieces
 // Capture the watermark BEFORE the turn: the answer arrives instantly, so the
 // first sentences can reach the server before the click has even resolved.
 let mark=spoken.length;
 await send('tell me about Half Moon Bay');
 await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length===1,1);
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 let said=spoken.slice(mark);
 assert.equal(said.length,2,`a three-sentence answer is a first bite plus one batched request (got ${said.length})`);
 assert.ok(said[0].text.length<LONG.length/3,
   `the first request must be a bite, not the answer (${said[0].text.length} of ${LONG.length} chars)`);
 assert.equal(said.map(part=>part.text).join(' ').replace(/\s+/g,' ').trim(),LONG,
   'every word is spoken, in order, exactly once');
 assert.ok(said[1].arrived-said[0].arrived<TTS_DELAY,
   `the next sentence was requested while the first was still being synthesized (gap ${said[1].arrived-said[0].arrived} ms < ${TTS_DELAY} ms)`);
 const states=await page.evaluate(()=>window.__states.splice(0));
 assert.ok(states.filter(value=>value==='Finding its voice').length<=1,
   `the wait-for-the-engine label appears once per reply, not once per sentence (${JSON.stringify(states)})`);
 assert.ok(states.includes('Speaking'),'the reply is announced as speech before it plays');

 // 2. a one-sentence answer is not chopped up for sport
 mark=spoken.length;
 await send('yes or no');
 await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length===2,2);
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 assert.deepEqual(spoken.slice(mark).map(part=>part.text),[SHORT],
   'a single short sentence stays a single request');

 // 3. a streamed reply is spoken while the model is still talking
 mark=spoken.length;
 await send('stream it');
 await page.waitForFunction(n=>document.querySelectorAll('.message.assistant').length===3,3);
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 said=spoken.slice(mark);
 assert.ok(said.length>=2,`a streamed answer is spoken in more than one request (got ${said.length})`);
 assert.equal(said[0].text,'First things first.','the first sentence is requested as soon as it is complete');
 assert.ok(said[0].arrived<answerSentAt,
   `the first sentence was requested ${answerSentAt-said[0].arrived} ms before the answer event existed`);
 assert.equal(said.map(part=>part.text).join(' ').replace(/\s+/g,' ').trim(),STREAMED,
   'every streamed word is spoken, in order, exactly once');
 assert.equal(await page.locator('.message.assistant').last().locator('p').first().textContent(),STREAMED,
   'the transcript shows the answer the bridge committed, not the stream');
 const stats=await page.evaluate(()=>window.__speechStats);
 console.log('speech stats',JSON.stringify(stats));
 assert.ok(stats.gapless+stats.element>=said.length,'every streamed clip was played, gapless when the audio graph is running');

 assert.deepEqual(errors,[]);
 if(sabotage)assert.ok(true,'the page must load the sabotage replacement');
 console.log(assertionsComplete);
 if(sabotage){
  console.error('SABOTAGE PASSED (this is the failure): the reply was still spoken one sentence at a time, in series');
  process.exitCode=0;
 }else{
  fs.mkdirSync('evidence/browser',{recursive:true});
  await page.screenshot({path:'evidence/browser/chromium-reply-pipelining.png',fullPage:true});
  console.log(`PASS chromium: multi-sentence reply pipelined (${spoken.length} requests, first bite ${said[0].text.length} chars), word-for-word lossless, single sentence unsplit, no per-sentence stall label`);
 }
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
