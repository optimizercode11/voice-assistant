import fs from 'node:fs';
import http from 'node:http';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// The tool-loop UI.  /chat/completions here is a real chunked NDJSON stream with
// deliberate gaps, not a mocked body: the whole point is that progress arrives
// *before* the answer, and a fulfill() that hands over one buffer cannot show that.
const sabotage=process.argv.includes('--sabotage');
const deadSabotage=process.argv.includes('--dead-sabotage');
const assertionsComplete='ASSERTIONS COMPLETE: browser tools';
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
const {chromium} = await import(process.env.PLAYWRIGHT ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const wav=fs.readFileSync('tests/fixtures/microphone.wav');
let mutations=0;
const events=[
 {type:'status',phase:'tool',round:1,calls:['search_notes']},
 {type:'tool',name:'search_notes',ok:true,ms:12,source:'retrieval',citations:[{path:'/home/me/notes/deploy.md',chunk:0,heading:'Ports'}]},
 {type:'answer',text:'The bridge is on 8092.',usage:{total_tokens:9},tools:[{name:'search_notes',ok:true,ms:12}],
  sources:[{path:'/home/me/notes/deploy.md',chunk:0,heading:'Ports'}]}];
const failing=[
 {type:'status',phase:'tool',round:1,calls:['mcp__desk__get_time']},
 {type:'tool',name:'mcp__desk__get_time',ok:false,ms:4001,error:'did not finish',source:'mcp:desk'},
 {type:'answer',text:'I could not reach the clock.',usage:{},tools:[{name:'mcp__desk__get_time',ok:false,ms:4001}],sources:[]}];
const injection=[
 {type:'answer',text:'Careful.',usage:{},tools:[],
  sources:[{path:'/x/<img src=nothidden onerror=alert(1)>.md',chunk:0,heading:'<b>bold</b>'}]}];
const scripts=[events,failing,injection];
let turn=0, accepts=[], plain=false;
const stream=res=>{
  // No Content-Length and no manual framing: Node chunks the response itself, so
  // each write is one flush and the page really does see progress arriving.
  res.writeHead(200,{'Content-Type':'application/x-ndjson','Cache-Control':'no-store'});
  const script=scripts[turn];
  script.forEach((event,index)=>setTimeout(()=>{
    res.write(JSON.stringify(event)+'\n');
    if(index===script.length-1){res.end();turn++;}
  },140*index));
};
const server=http.createServer((req,res)=>{
  const url=req.url.split('?')[0];
  if(url==='/chat'||url==='/chat.js'){
    const file=url==='/chat'?'web/chat.html':'web/chat.js';
    let body=fs.readFileSync(file,'utf8');
    // Sabotage: make the page ignore the progress stream. Live status and
    // citations must then stop appearing, not merely look different.
    if(sabotage&&file.endsWith('.js')){
      const anchor="streaming\n      ? await responseProgress";
      assert.ok(body.includes(anchor),'the sabotage anchor no longer matches web/chat.js');
      const armed=body.replace(anchor,deadSabotage?anchor:"false\n      ? await responseProgress");
      if(deadSabotage)assert.equal(armed,body,'dead sabotage must leave the page unchanged');
      body=armed;mutations++;
    }
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript; charset=utf-8':'text/html; charset=utf-8');
    return res.end(body);
  }
  if(url==='/chat/health')return res.end(JSON.stringify({available:true,streaming:true,tools:['search_notes'],tool_sources:{search_notes:'retrieval'}}));
  if(url==='/languages')return res.end(JSON.stringify({default:'a',languages:[{code:'a',name:'English (US)'}]}));
  if(url==='/voices')return res.end(JSON.stringify({voices:['af_heart']}));
  if(url==='/tts'){res.setHeader('Content-Type','audio/wav');return res.end(wav);}
  if(url==='/chat/completions'&&req.method==='POST'){
    accepts.push(req.headers.accept);
    if(plain){turn++;res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({text:'Plain as day.'}));}
    return stream(res);
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
 page.setDefaultTimeout(15000);
 await page.goto(`${base}/chat`);
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 const send=async text=>{await page.locator('#text').fill(text);await page.locator('#send').click();};

 await send('what port is the bridge on?');
 assert.ok(accepts[0].includes('application/x-ndjson'),'a tool-capable bridge is asked for a progress stream');
 await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('Looking that up'),{},{timeout:4000});
 await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('1 passage'),{},{timeout:4000});
 await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===1);
 await page.waitForFunction(()=>document.querySelector('#state').textContent==='Ready');
 assert.equal((await page.locator('.message.assistant p').first().textContent()).trim(),'The bridge is on 8092.');
 assert.equal(await page.locator('.message.assistant .sources').count(),1,'a RAG answer must show where it came from');
 assert.match(await page.locator('.message.assistant .sources').textContent(),/Ports · notes\/deploy\.md/);
 assert.match(await page.locator('.message.assistant .toolnote').textContent(),/search_notes 12ms/);

 await send('what time is it?');
 await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2);
 assert.match(await page.locator('.message.assistant').last().locator('.toolnote').textContent(),/get_time \(did not work\)/,
   'a failed tool must stay visible in the transcript, not be smoothed over');
 assert.equal(await page.locator('.message.assistant').last().locator('.sources').count(),0,'no citations, no source line');

 await send('is this safe?');
 await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===3);
 assert.equal(await page.locator('#messages img, #messages b').count(),0,'citations are never injected as HTML');
 assert.match(await page.locator('.message.assistant').last().locator('.sources').textContent(),/onerror=alert/);

 plain=true;
 await send('and without streaming?');
 await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===4);
 assert.equal((await page.locator('.message.assistant').last().locator('p').first().textContent()).trim(),'Plain as day.');
 assert.deepEqual(errors,[]);
 if(sabotage)assert.ok(mutations>0,'the page must load the sabotage replacement');
 console.log(assertionsComplete);
 if(sabotage){
  console.error('SABOTAGE PASSED (this is the failure): the page still rendered tool progress');
  // The assertions passed: exit zero so make sabotage rejects this arm.
  process.exitCode=0;
 }else{
  fs.mkdirSync('evidence/browser',{recursive:true});
  await page.screenshot({path:'evidence/browser/chromium-tools.png',fullPage:true});
  console.log('PASS chromium: live tool progress, citations kept, failed tool disclosed, citation markup inert, plain-JSON fallback');
 }
 }finally{await browser.close();}
}finally{await new Promise(resolve=>server.close(resolve));}
