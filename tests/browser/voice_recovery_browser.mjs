// Real page and microphone, deterministic STT verdicts: a held turn must finish
// even when there is no next clip, without losing continuations or wake gating.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
const {chromium} = await import(process.env.PLAYWRIGHT);
const sabotage = process.argv.includes('--sabotage');
const server = http.createServer((req,res) => {
  const file = req.url === '/chat' ? 'web/chat.html' : req.url === '/chat.js' ? 'web/chat.js' : null;
  if (!file) { res.writeHead(404); res.end(); return; }
  let body = fs.readFileSync(file,'utf8');
  if (sabotage && file.endsWith('.js')) {
    const anchor = 'heldFlushTimer = setTimeout(checkHeld, 2200);';
    assert.ok(body.includes(anchor), 'sabotage anchor matches');
    body = body.replace(anchor, '/* sabotage: no quiet flush */');
  }
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html'); res.end(body);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser = await chromium.launch({headless:true,args:['--disable-gpu','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${path.resolve('tests/fixtures/silence.wav')}`]});
try {
  const ctx = await browser.newContext(); await ctx.grantPermissions(['microphone']);
  const page = await ctx.newPage(); page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror',e=>errors.push(String(e)));
  const posts = []; let heard = 'Maybe.', hold = true, discard = false, failReply = false;
  await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
  await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English'}]}}));
  await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart']}}));
  await page.route('**/stt',r=>r.fulfill({json:{text:heard,turn:{hold,discard}}}));
  await page.route('**/chat/completions',r=>{posts.push(r.request().postDataJSON()); return failReply ? r.fulfill({status:503,json:{error:'Try this turn again.'}}) : r.fulfill({json:{text:'Understood.'}});});
  await page.route('**/tts?*',r=>r.fulfill({contentType:'audio/wav',body:fs.readFileSync('tests/fixtures/microphone.wav')}));
  await page.goto(`http://127.0.0.1:${server.address().port}/chat`);
  await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  await page.fill('#silence-timeout','0'); await page.uncheck('#barge-in'); await page.uncheck('#think-aloud');
  await page.click('#start'); await page.waitForFunction(()=>phase==='listening'); await page.waitForTimeout(400);
  const clip = async () => page.evaluate(()=>runTurn(new Blob(['audio'],{type:'audio/webm'}),false,220));
  await clip(); assert.equal(posts.length,0,'a fragment initially waits for continuation');
  await page.waitForFunction(()=>document.querySelectorAll('.message.user').length===1,null,{timeout:6000}).catch(async error=>{console.log(await page.evaluate(()=>({heldText,heldFlushTimer,active,busy,epoch,phase,voiced:recorder?.voicedMs,status:document.querySelector('#status').textContent})));throw error;});
  assert.equal(posts[0]?.messages.at(-1).content,'Maybe.','quiet flush sends held text once');
  await page.waitForFunction(()=>phase==='listening'&&!busy);

  // Send now sends what was heard instead of uploading silence indefinitely.
  heard='The blue one.'; await clip(); await page.click('#finish');
  await page.waitForFunction(()=>document.querySelectorAll('.message.user').length===2);
  assert.equal(posts[1].messages.at(-1).content,'The blue one.');
  await page.waitForFunction(()=>phase==='listening'&&!busy);

  // New speech arriving during the hold is combined, without a stale timer turn.
  heard='I.'; await clip(); await page.waitForTimeout(350);
  heard='want the museum.'; hold=false; await clip();
  assert.equal(posts[2].messages.at(-1).content,'I want the museum.');
  await page.waitForFunction(()=>phase==='listening'&&!busy); await page.waitForTimeout(2500);
  assert.equal(posts.length,3,'old hold timer cannot create a duplicate turn');

  heard='Something.'; hold=true; await clip();
  heard='uh'; discard=true; await clip();
  assert.equal(await page.evaluate(()=>heldText),'Something.','filler cannot replace carried words');
  discard=false; await page.click('#end');
  await page.waitForTimeout(2600); assert.equal(posts.length,3,'ending clears pending fragment');
  await page.click('#start'); await page.waitForFunction(()=>phase==='listening'); await page.waitForTimeout(400);
  await page.check('#wake-enabled'); await page.dispatchEvent('#wake-enabled','change');
  await page.evaluate(()=>{dormant=true;}); heard='Not addressed to you.'; await clip();
  await page.waitForTimeout(2800); assert.equal(posts.length,3,'timed flush still respects wake gating');
  await page.uncheck('#wake-enabled'); await page.dispatchEvent('#wake-enabled','change');

  // Disabling automatic sleep while asleep immediately recovers the microphone.
  await page.fill('#silence-timeout','1'); await page.dispatchEvent('#silence-timeout','change');
  await page.waitForFunction(()=>asleep && phase==='paused');
  await page.fill('#silence-timeout','0'); await page.dispatchEvent('#silence-timeout','change');
  await page.waitForFunction(()=>phase==='listening'&&!asleep);
  await page.evaluate(async()=>{paused=true;holdListening();await context.suspend();});
  await page.click('#resume-listening');
  await page.waitForFunction(()=>context.state==='running'&&phase==='listening'&&!paused);
  failReply=true;
  await page.evaluate(()=>{paused=true;holdListening();});
  await page.evaluate(()=>runTurn('Decline this typed turn'));
  assert.equal(await page.locator('#state').textContent(),'Paused','failed typed turn cannot hide a deliberate pause');
  assert.equal(await page.locator('#resume-listening').isVisible(),true);
  failReply=false; await page.click('#resume-listening');
  await page.waitForFunction(()=>phase==='listening');

  // 120–180 ms of real measured energy used to wait for the 20-second ceiling.
  const beforeShort=posts.length;
  heard='Yes.'; hold=false;
  await page.evaluate(()=>{
    const started=performance.now();
    window.originalRAF=window.requestAnimationFrame;
    window.requestAnimationFrame=()=>0; // hidden-tab rendering must not own the endpoint
    window.originalSamples=analyser.getFloatTimeDomainData.bind(analyser);
    analyser.getFloatTimeDomainData=samples=>samples.fill(performance.now()-started < 165 ? 0.04 : 0);
    listen();
  });
  await page.waitForFunction(count=>document.querySelectorAll('.message.user').length>count,await page.locator('.message.user').count(),{timeout:5000});
  assert.equal(posts.length,beforeShort+1,'a brief answer ends after quiet without Send now');
  await page.evaluate(()=>{analyser.getFloatTimeDomainData=window.originalSamples;window.requestAnimationFrame=window.originalRAF;});
  await page.waitForFunction(()=>phase==='listening'&&!busy);

  // Suspended gapless playback must expose a working browser gesture recovery.
  await page.evaluate(async()=>{
    busy=true;
    window.gaplessDone=false;
    const buffer=context.createBuffer(1,24000,24000);
    window.gaplessPromise=gapless.play(buffer,new AbortController().signal).then(()=>{window.gaplessDone=true;});
    await context.suspend();
  });
  await page.waitForFunction(()=>!document.querySelector('#resume').hidden);
  await page.click('#resume');
  await page.waitForFunction(()=>window.gaplessDone && context.state==='running');
  await page.evaluate(()=>{busy=false;listen();});

  // Every non-final element clip denied by autoplay has an explicit retry.
  await page.evaluate(async()=>{
    clearCapture(); busy=true;
    window.originalPlay=player.play.bind(player);
    player.play=()=>Promise.reject(new DOMException('Blocked','NotAllowedError'));
    const audio=await (await fetch('/tts?test=1')).blob();
    window.elementDone=false;
    window.elementPromise=playReply(audio,new AbortController().signal,false,true).then(()=>{window.elementDone=true;});
  });
  await page.waitForFunction(()=>!document.querySelector('#resume').hidden);
  await page.evaluate(()=>{player.play=window.originalPlay;}); await page.click('#resume');
  await page.waitForFunction(()=>window.elementDone);
  await page.evaluate(()=>{busy=false;listen();});

  // JSON fallback consumes the existing response: tools must not execute twice.
  const beforeFallback=posts.length;
  await page.evaluate(()=>responseProgress('/chat/completions',JSON.stringify({messages:[{role:'user',content:'One request only'}]}),new AbortController().signal));
  assert.equal(posts.length,beforeFallback+1,'one user turn is one POST');
  // Stopping Claude only clears its own pending indicator.
  await page.evaluate(()=>{
    for (const name of ['Claude Code','Codex']) {
      const node=document.createElement('div');node.className='message claude pending';
      const label=document.createElement('span');label.className='role';label.textContent=name;node.append(label);document.querySelector('#messages').append(node);
    }
    clearAgentPending('Claude Code');
  });
  assert.deepEqual(await page.locator('.message.claude.pending .role').allTextContents(),['Codex']);
  await page.click('#end');

  // Existing browsers inherit the four-second idle default; custom choices persist.
  await page.evaluate(()=>localStorage.setItem('voice-speech',JSON.stringify({silence:'5'})));
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.inputValue('#silence-timeout'),'4','legacy automatic default migrates to four seconds');
  assert.equal(await page.evaluate(()=>getComputedStyle(document.body).backgroundColor),'rgb(0, 0, 0)');
  await page.fill('#silence-timeout','5'); await page.dispatchEvent('#silence-timeout','change');
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.inputValue('#silence-timeout'),'5','explicit timeout persists');
  await page.evaluate(()=>localStorage.setItem('voice-speech',JSON.stringify({silence:'0',silenceVersion:2})));
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.inputValue('#silence-timeout'),'4','previous continuous-listening default migrates');
  await page.fill('#silence-timeout','0'); await page.dispatchEvent('#silence-timeout','change');
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.inputValue('#silence-timeout'),'0','new explicit continuous listening persists');
  await page.evaluate(()=>localStorage.setItem('voice-speech',JSON.stringify({silence:'3',silenceVersion:2})));
  await page.reload(); await page.waitForFunction(()=>!document.querySelector('#send').disabled);
  assert.equal(await page.inputValue('#silence-timeout'),'3','existing custom timeout persists');
  assert.deepEqual(errors,[]); console.log('PASS: held/brief turns, Send now, carry/filler, cancellation, wake gate, pause/audio recovery, autoplay retry, single POST, agent indicators, preferences and black background');
} finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
