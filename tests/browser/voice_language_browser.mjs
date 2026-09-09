import fs from 'node:fs';import http from 'node:http';import assert from 'node:assert/strict';
assert.equal(process.env.CUDA_VISIBLE_DEVICES,'');
// Playwright is deliberately NOT a dependency of this repo: point PLAYWRIGHT at any
// install whose chromium/webkit browsers are already downloaded, e.g.
//   PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node tests/browser/<name>.mjs
const {chromium,webkit} = await import(process.env.PLAYWRIGHT
  ?? '/tmp/kokoro-playback-browser/node_modules/playwright/index.mjs');
const sabotage=process.argv.includes('--sabotage'),live=process.argv.includes('--live');
const server=http.createServer((q,r)=>{const name=q.url==='/chat.js'?'chat.js':'chat.html';let body=fs.readFileSync('web/'+name,'utf8');if(sabotage&&name==='chat.js'){const before="autoLanguage=/[\\u0900-\\u097f]/u.test(text)?'h':'a';";assert(body.includes(before));body=body.replace(before,"autoLanguage='a';");}r.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'text/html');r.end(body);});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
try{for(const [name,type] of sabotage?[['chromium',chromium]]:[['chromium',chromium],['webkit',webkit]]){
const browser=await type.launch({args:name==='chromium'?['--disable-gpu']:[],...(name==='webkit'?{env:{...process.env,LIBGL_ALWAYS_SOFTWARE:'1',WEBKIT_DISABLE_COMPOSITING_MODE:'1'}}:{})});
try{
const page=await browser.newPage({ignoreHTTPSErrors:live}),errors=[];page.on('pageerror',e=>errors.push(String(e)));let reply='Hello.',heard='Please check red scarlet.',tts=[],requests=[];
await page.route('**/chat/health',r=>r.fulfill({json:{available:true}}));
await page.route('**/languages',r=>r.fulfill({json:{default:'a',languages:[{code:'a',name:'English',default_voice:'af_heart'},{code:'h',name:'Hindi',default_voice:'hf_alpha'},{code:'f',name:'French',default_voice:'ff_siwis'}]}}));
await page.route('**/voices',r=>r.fulfill({json:{voices:['af_heart','hf_alpha','hf_beta','hm_omega','hm_psi','ff_siwis']}}));
await page.route('**/stt',r=>r.fulfill({json:{text:heard}}));
await page.route('**/chat/completions',r=>{requests.push(r.request().postDataJSON());return r.fulfill({json:{text:reply}});});
await page.route('**/tts?*',r=>{tts.push(Object.fromEntries(new URL(r.request().url()).searchParams));return r.fulfill({contentType:'audio/wav',body:fs.readFileSync('tests/fixtures/microphone.wav')});});
await page.goto(live?'https://192.168.228.113:8092/chat':`http://127.0.0.1:${server.address().port}/chat`);await page.waitForFunction(()=>!document.querySelector('#send').disabled);
const stop=async()=>{await page.waitForFunction(()=>document.querySelector('#player').currentTime>.1&&!document.querySelector('#player').paused);await page.locator('#interrupt').click();};
const send=async(text)=>{await page.locator('#text').fill(text);await page.locator('#send').click();await stop();};
assert.equal(await page.locator('#language').inputValue(),'auto');await send('Hello');assert.equal(tts.at(-1).language,'a');console.log('CONTROL PASS English routes to English',name);
reply='नमस्ते, आप कैसे हैं?';await send('हिंदी में बोलो');assert.equal(tts.at(-1).language,'h','Hindi reply uses Hindi TTS');assert.equal(tts.at(-1).voice,'hf_alpha');assert.equal(tts.at(-1).speed,'1.2');
await page.locator('#voice').selectOption('hm_omega');await send('Again');assert.equal(tts.at(-1).voice,'hm_omega');
reply='English again';await send('English');assert.equal(tts.at(-1).voice,'af_heart');reply='फिर हिंदी';await send('Hindi');assert.equal(tts.at(-1).voice,'hm_omega');
await page.locator('#language').selectOption('h');reply='Namaste';await send('Romanized Hindi');assert.equal(tts.at(-1).language,'h');
await page.reload();await page.waitForFunction(()=>!document.querySelector('#send').disabled);assert.equal(await page.locator('#language').inputValue(),'h');assert.equal(await page.locator('#voice').inputValue(),'hm_omega');
await page.locator('#language').selectOption('f');reply='Bonjour';await send('French');assert.equal(tts.at(-1).voice,'ff_siwis');
// The actual speech pipeline consumes a Blob; inference APIs remain fixtures.
const speech=async text=>{heard=text;await page.evaluate(()=>{primeAudio();void runTurn(new Blob(['fixture'],{type:'audio/wav'}));});await stop();};
await speech('Please check red scarlet.');assert.equal(requests.at(-1).messages.at(-1).content,'Please check Ritz-Carlton.');assert.match(await page.locator('.transcript-note').last().textContent(),/red scarlet/);
await speech('RED SKELETON near me');assert.equal(requests.at(-1).messages.at(-1).content,'Ritz-Carlton near me');
await send('red skeleton');assert.equal(requests.at(-1).messages.at(-1).content,'red skeleton','typed input is never rewritten');
await speech('red skeletons');assert.equal(requests.at(-1).messages.at(-1).content,'red skeletons','whole phrases only');
await page.locator('summary').click();await page.locator('#corrections-enabled').uncheck();await speech('red skeleton');assert.equal(requests.at(-1).messages.at(-1).content,'red skeleton');
await page.locator('#corrections-enabled').check();await page.locator('#corrections').fill('a.b = $hotel\n$hotel = other');await speech('a.b axb');assert.equal(requests.at(-1).messages.at(-1).content,'$hotel axb','literal, single-pass correction');
await page.locator('#corrections').fill('invalid');await speech('red scarlet');assert.equal(requests.at(-1).messages.at(-1).content,'red scarlet');assert.equal(await page.locator('#corrections').getAttribute('aria-invalid'),'true');
// State transition permits configuration while listening, freezes it during a reply.
await page.evaluate(()=>{active=true;busy=false;setState('listening');});assert.equal(await page.locator('#language').isEnabled(),true);await page.evaluate(()=>{busy=true;setState('thinking');});assert.equal(await page.locator('#language').isEnabled(),false);await page.evaluate(()=>stopSession());
assert(requests.every(x=>Object.keys(x).join(',')==='messages'),'Qwen requests remain unchanged');assert.deepEqual(errors,[]);
await page.locator('#language').selectOption('auto');await page.locator('#corrections').fill('red scarlet = Ritz-Carlton\nred skeleton = Ritz-Carlton');
fs.mkdirSync('evidence/browser',{recursive:true});
await page.screenshot({path:`evidence/browser/${name}-${live?'live':'controls'}.png`,fullPage:true});await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
console.log('PASS',name,'Hindi/English auto, manual languages/voices, persistence, speech-only corrections and original text, boundaries/escaping/disable, active controls, unchanged Qwen payload');
}finally{await browser.close();}
}}finally{await new Promise(r=>server.close(r));}
