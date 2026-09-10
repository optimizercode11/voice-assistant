'use strict';
const $ = id => document.getElementById(id);
const player = $('player');
let resumePlayback = null;
function setTheme(theme){
  document.documentElement.dataset.theme=theme;
  const next=theme==='dark'?'light':'dark';
  $('theme').textContent=next==='light'?'Light mode':'Dark mode';
  $('theme').setAttribute('aria-label','Switch to '+next+' mode');
  try{localStorage.setItem('voice-theme',theme);}catch(_){}
}
$('theme').onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');
setTheme(document.documentElement.dataset.theme==='light'?'light':'dark');
let active = false, busy = false, ready = false, epoch = 0, abort = null;
let stream = null, context = null, source = null, analyser = null, recorder = null, raf = 0;
let history = [], voiceList = [], audioURL = null, secureURL = null, phase = 'idle', streaming = false;
let fragmentHolds = 0, heldText = '';
const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
function setState(value, message) {
  phase = value; $('orb').dataset.state = value;
  $('state').textContent = ({idle:'Ready',listening:'Listening',transcribing:'Hearing you',thinking:'Thinking',synthesizing:'Finding its voice',speaking:'Speaking',paused:'Paused',error:'Something went wrong'})[value] || value;
  if (message !== undefined) $('status').textContent = message;
  $('start').hidden = active; $('start').disabled = !ready || busy || (!canRecord && !secureURL);
  $('end').hidden = !active && !busy;
  $('finish').hidden = value !== 'listening';
  $('interrupt').hidden = !busy;
  $('send').disabled = !ready || busy;
  $('language').disabled = busy; $('voice').disabled = busy;
  $('corrections').disabled=busy;$('corrections-enabled').disabled=busy;
}
function shortPath(path) { const parts = String(path).split('/').filter(Boolean); return parts.slice(-2).join('/'); }
function message(role, text, original = null, evidence = null) {
  $('messages').querySelector('.empty')?.remove();
  const item = document.createElement('div'); item.className = `message ${role}`;
  const label = document.createElement('span'); label.className = 'role'; label.textContent = role === 'user' ? 'You' : 'Qwen';
  const body = document.createElement('p'); body.textContent = text; item.append(label, body); $('messages').append(item);
  if(original!==null){const note=document.createElement('p');note.className='transcript-note';note.textContent='Speech correction · STT heard: '+original;item.append(note);}
  // Provenance stays visible after the reply: "which of my notes said that" is
  // the question a person asks next, and it must not require asking again.
  if (evidence?.tools?.length) {
    const used = document.createElement('p'); used.className = 'toolnote';
    used.textContent = 'Looked up · ' + evidence.tools.map(tool =>
      `${tool.name.replace(/^mcp__[^_]+__/, '')}${tool.ok === false ? ' (did not work)' : ''} ${tool.ms}ms`).join(' · ');
    item.append(used);
  }
  if (evidence?.sources?.length) {
    const from = document.createElement('p'); from.className = 'sources';
    from.textContent = 'From your notes · ' + evidence.sources.map(source =>
      source.heading ? `${source.heading} · ${shortPath(source.path)}` : shortPath(source.path)).join(' · ');
    item.append(from);
  }
  $('messages').scrollTop = $('messages').scrollHeight;
}
function clearCapture() {
  cancelAnimationFrame(raf); raf = 0;
  if (recorder) { recorder.onstop = null; recorder.ondataavailable = null; if (recorder.state !== 'inactive') recorder.stop(); recorder = null; }
  if (stream) stream.getTracks().forEach(track => {track.enabled = false;});
  $('level').style.width = '0%';
  setGlow(0);
}
// ---------------------------------------------------------------- the orb glow
// --glow is the orb's single source of truth for "I am hearing/speaking right
// now".  It is written once per animation frame from measured audio energy, so
// a silent room leaves it at zero instead of breathing on a timer.  Attack is
// fast (a syllable should light up immediately) and release is slow (a word's
// consonant gaps should not strobe).
let glowRaf = 0, glowSmooth = 0, glowArmed = false, mediaSource = null, playbackAnalyser = null;
function setGlow(target) {
  const wanted = Math.max(0, Math.min(1, Number(target) || 0));
  glowSmooth = wanted > glowSmooth ? glowSmooth + (wanted - glowSmooth) * .55
                                   : glowSmooth * .86 + wanted * .14;
  if (glowSmooth < .004) glowSmooth = 0;
  $('orb').style.setProperty('--glow', glowSmooth.toFixed(3));
}
function rmsOf(samples, scale) {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) { const x = samples[i] * scale; sum += x * x; }
  return Math.sqrt(sum / samples.length);
}
function startPlaybackGlow() {
  if (!playbackAnalyser) return;              // no graph, no glow: audio still plays
  stopPlaybackGlow();
  const bytes = new Uint8Array(playbackAnalyser.fftSize);
  const step = () => {
    if (player.paused || player.ended) { glowRaf = 0; setGlow(0); return; }
    playbackAnalyser.getByteTimeDomainData(bytes);
    setGlow(rmsOf(bytes.map ? Array.from(bytes, v => (v - 128) / 128) : bytes, 1) * 4.5);
    glowRaf = requestAnimationFrame(step);
  };
  glowRaf = requestAnimationFrame(step);
}
function stopPlaybackGlow() { if (glowRaf) cancelAnimationFrame(glowRaf); glowRaf = 0; }
async function armGlow() {
  // Routing the reply through WebAudio is the only way to see its waveform, and
  // it is also the only way to *lose* it: a MediaElementSource attached to a
  // suspended context feeds a graph that never renders, so the reply goes
  // silent.  So build it only from a user gesture, only once the context says
  // it is running, and only ever once per media element.  Anything less
  // ambitious leaves the element on the native output -- no glow beats no audio.
  if (glowArmed) return;
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio || !window.AnalyserNode || !player.createMediaElementSource) return;
    if (!context || context.state === 'closed') context = new Audio();
    const ctx = context;
    if (ctx.state === 'suspended') await ctx.resume();
    if (ctx.state !== 'running') return;
    glowArmed = true;
    mediaSource = ctx.createMediaElementSource(player);
    playbackAnalyser = ctx.createAnalyser(); playbackAnalyser.fftSize = 1024;
    mediaSource.connect(playbackAnalyser); playbackAnalyser.connect(ctx.destination);
  } catch (_) {
    // Already armed by an earlier gesture, or the browser refused: either way
    // the reply keeps playing through the element's own output.
    if (!playbackAnalyser) { glowArmed = false; mediaSource = null; }
  }
}
function releaseAudioContext() {
  stopPlaybackGlow(); setGlow(0);
  source?.disconnect(); source = null; analyser = null;
  mediaSource = null; playbackAnalyser = null; glowArmed = false;
  if (context) {context.close().catch(()=>{}); context = null;}
}
function replaceAudio(blob) {
  if (audioURL) URL.revokeObjectURL(audioURL);
  audioURL = URL.createObjectURL(blob); player.src = audioURL;
}
function primeAudio() {
  // Safari authorizes this same native media element in the initiating gesture.
  const wav = new ArrayBuffer(4844), d = new DataView(wav);
  const str = (at, value) => [...value].forEach((c,i) => d.setUint8(at+i,c.charCodeAt(0)));
  str(0,'RIFF');d.setUint32(4,4836,true);str(8,'WAVE');str(12,'fmt ');d.setUint32(16,16,true);
  d.setUint16(20,1,true);d.setUint16(22,1,true);d.setUint32(24,24000,true);d.setUint32(28,48000,true);d.setUint16(32,2,true);d.setUint16(34,16,true);str(36,'data');d.setUint32(40,4800,true);
  replaceAudio(new Blob([wav],{type:'audio/wav'})); player.play().catch(()=>{});
}
function stopSession(note = 'Conversation ended. Start again whenever you like.') {
  heldText = ''; fragmentHolds = 0;
  active = false; epoch++; abort?.abort(); abort = null; busy = false;
  clearCapture(); player.pause(); stopPlaybackGlow(); setGlow(0);
  if (stream) {stream.getTracks().forEach(track=>track.stop()); stream = null;}
  source?.disconnect(); source = null; analyser = null;
  // The AudioContext deliberately survives: the reply's glow is wired with
  // createMediaElementSource, which may only be called once per element ever,
  // so closing the context here would silently give up on it for good.
  setState('idle', note);
}
async function responseJSON(url, body, signal, headers) {
  const response = await fetch(url,{method:'POST',body,signal,
    headers:{...(typeof body==='string'?{'Content-Type':'application/json'}:{}),...(headers||{})}});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed. Please try again.');
  return result;
}
async function responseProgress(url, body, signal, onEvent) {
  // The bridge owns the tool loop, so one request can take several generations.
  // Progress arrives as newline-delimited JSON so the page can say so out loud;
  // a server without tools keeps replying with one plain JSON object.
  const response = await fetch(url, {method: 'POST', body, signal,
    headers: {'Content-Type': 'application/json', 'Accept': 'application/x-ndjson'}});
  const type = response.headers.get('Content-Type') || '';
  if (!type.includes('x-ndjson')) return responseJSON(url, body, signal);
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', answer = null;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    let at;
    while ((at = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, at).trim(); buffer = buffer.slice(at + 1);
      if (!line) continue;
      let event; try { event = JSON.parse(line); } catch (_) { continue; }
      if (event.type === 'answer') answer = event;
      else if (event.type === 'error') throw new Error(event.error || 'The reply failed. Please try again.');
      else onEvent?.(event);
    }
  }
  if (!answer) throw new Error('The reply ended before it finished. Please try again.');
  return answer;
}
function listen() {
  if (!active || !stream) return;
  clearCapture(); player.pause(); stream.getTracks().forEach(track=>{track.enabled=true;});
  const mime = ['audio/webm;codecs=opus','audio/mp4','audio/webm','audio/ogg;codecs=opus'].find(value=>MediaRecorder.isTypeSupported(value));
  const rec = new MediaRecorder(stream,mime?{mimeType:mime}:{}); recorder = rec;
  const chunks = []; let bytes = 0, voiced = 0, lastVoice = 0, lastTick = performance.now(), started = lastTick;
  const thisEpoch = epoch;
  rec.ondataavailable = event => { if (event.data.size) {chunks.push(event.data);bytes+=event.data.size;} if (bytes>20*1024*1024) stopSession('Recording is too large. Please try a shorter message.'); };
  rec.onerror = () => stopSession('Microphone recording failed. Try again or type your message.');
  rec.onstop = () => {
    if (!active || thisEpoch !== epoch) return;
    recorder = null; cancelAnimationFrame(raf); stream.getTracks().forEach(track=>{track.enabled=false;});
    if (!chunks.length || (voiced < 120 && !rec.sendNow)) {listen(); return;}
    runTurn(new Blob(chunks,{type:rec.mimeType || 'audio/webm'}), rec.sendNow === true, voiced);
  };
  rec.start(250); busy = false; setState('listening','I’m listening. A short pause sends your message.');
  const samples = new Float32Array(analyser.fftSize);
  function tick() {
    if (recorder !== rec || rec.state !== 'recording') return;
    analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length), now=performance.now();
    $('level').style.width = `${Math.min(100,rms*1000)}%`;
    setGlow(rms * 7);                       // same measurement, one shared meter
    if (rms > .015) {voiced += Math.min(100,now-lastTick);lastVoice=now;}
    lastTick=now;
    // 20 s, not the engine's 30 s ceiling: measured against known ground truth,
// a 19.5 s upload keeps 96% of its words and a 29.3 s upload keeps 19% -- the
// engine returns its first sentence and then degenerates.  Stopping earlier
// loses the end of a long sentence; stopping here loses almost all of it.
    // The window stays a flat 1000 ms deliberately. Lengthening it for short
    // utterances looks like the fix for "I" -- one word followed by a pause is
    // more often a comma than a period -- but it charges that delay to every
    // short reply, including a real "Yes.", and it is not needed: the cut is no
    // longer the bug. What was broken is what happened *after* the cut, so see
    // the carry-below and turn_control._should_hold.
    if ((voiced >= 180 && now-lastVoice > 1000) || now-started > 20000) {rec.stop();return;}
    raf = requestAnimationFrame(tick);
  }
  raf = requestAnimationFrame(tick);
}
async function playReply(blob, signal) {
  replaceAudio(blob);
  await new Promise((resolve,reject)=>{
    let settled = false;
    const cleanup = () => {
      settled = true;
      stopPlaybackGlow(); setGlow(0);
      player.removeEventListener('ended',ended);player.removeEventListener('error',failed);signal.removeEventListener('abort',cancelled);
      if(resumePlayback===attempt)resumePlayback=null;
      $('resume').hidden=true;
    };
    const ended = () => {cleanup();resolve();};
    const failed = () => {cleanup();reject(new Error('Audio playback failed. The reply is shown in the conversation.'));};
    const cancelled = () => {cleanup();player.pause();reject(new DOMException('Stopped','AbortError'));};
    const attempt = () => {
      if(settled || signal.aborted)return;
      $('resume').hidden=true;
      player.play().then(()=>{if(!settled && !player.paused)startPlaybackGlow();},error=>{
        if(settled || signal.aborted)return;
        if(error.name!=='NotAllowedError'){failed();return;}
        resumePlayback=attempt;$('resume').hidden=false;
        setState('speaking','Choose Play reply to allow audio in your browser.');
      });
    };
    player.addEventListener('ended',ended);player.addEventListener('error',failed);signal.addEventListener('abort',cancelled,{once:true});
    if(signal.aborted){cancelled();return;}
    attempt();
  });
}
async function runTurn(input, forced = false, voicedMs = null) {
  if (!(input instanceof Blob)) { heldText = ''; fragmentHolds = 0; }
  clearCapture(); player.pause(); busy = true;
  const id = ++epoch, controller = new AbortController(); abort = controller;
  const check = () => {if(id !== epoch || controller.signal.aborted) throw new DOMException('Stopped','AbortError');};
  const before = history.slice(); let committed = false;
  try {
    let text = input, original = null;
    if (input instanceof Blob) {
      setState('transcribing','Turning your speech into text…');
      // Tell the bridge how long we actually heard a voice.  It cannot recover
      // that from the clip: the clip also carries the silence the endpointer
      // waits for before it is allowed to stop.
      const heard = await responseJSON('/stt', input, controller.signal,
        Number.isFinite(voicedMs) ? {'X-Voiced-Ms': String(Math.round(voicedMs))} : undefined);
      text = String(heard.text || '').trim(); check();
      // A breath between two halves of a sentence must not throw away the first half, so heldText deliberately survives this path.
      if (!text) {busy=false;if(active)listen();else setState('idle','No speech was detected. Please try again.');return;}
      const corrected=correctSpeech(text);if(corrected!==text){original=text;text=corrected;}
      // A one-word clip is not a question.  qasr punctuates fragments -- a 0.6 s
      // clip of one syllable comes back as "I." -- so the transcript cannot be
      // trusted to say when a turn is finished, and answering "I." produced a
      // confident reply to a question nobody asked.  Hold it, stay open, and
      // bound the holds: this may add patience, it may never wedge a turn.
      // Half a sentence is not a question. The server decides this -- it has the
      // words and the voiced duration this page measured -- and the page only
      // obeys, bounds the patience, and critically KEEPS the words. Dropping a
      // held fragment was its own bug: "I" then "want to go to the museum" used to
      // dispatch the second clip on its own, so the reply was about wanting.
      const judged = heard.turn, carried = heldText;
      if (!forced && judged?.hold && fragmentHolds < 3) {
        fragmentHolds++;
        heldText = (carried ? carried + ' ' : '') + text;
        busy = false;
        if (active) { listen(); setState('listening', `I heard “${heldText}”. Keep talking, or press Send now.`); }
        else setState('idle', `Only “${heldText}” so far. Say more, or press Send.`);
        return;
      }
      // The fragment was judged unfinished, so the full stop qasr put on it is not
      // real.  "I. want to go to the museum." reads as two sentences and makes the
      // voice stop in the middle of a clause, so only the carried half loses its
      // punctuation; whatever closed the last clip is the transcript's own.
      if (carried) text = `${carried.replace(/[.!?\u2026]+\s*$/, '')} ${text}`.trim();
      fragmentHolds = 0; heldText = '';
    }
    message('user',text,original); setState('thinking','Qwen is preparing a reply…');
    const pending = [...before,{role:'user',content:text}];
    const progress = event => {
      check();
      if (event.type === 'status') setState('thinking', `Looking that up — ${event.calls.join(', ')}…`);
      else if (event.type === 'tool') setState('thinking', event.ok
        ? (event.citations?.length ? `Found ${event.citations.length} passage${event.citations.length > 1 ? 's' : ''} in your notes…` : 'Read it. Thinking…')
        : 'That did not work. Answering from what it has…');
    };
    const reply = streaming
      ? await responseProgress('/chat/completions', JSON.stringify({messages: pending}), controller.signal, progress)
      : await responseJSON('/chat/completions', JSON.stringify({messages: pending}), controller.signal);
    check();
    history = [...pending,{role:'assistant',content:reply.text}];committed=true;
    message('assistant',reply.text,null,{tools:reply.tools,sources:reply.sources});
    // A request_directory call happened during that generation, so the card the
    // user needs to see is one poll overdue.  Fetch it now, not in four seconds.
    refreshApprovals();
    $('turns').textContent=`${history.length/2} ${history.length===2?'turn':'turns'} · Just this tab`;
    setState('synthesizing','Your reply is becoming speech…');
    const spoken=replyVoice(reply.text);
    const query = new URLSearchParams({format:'wav',language:spoken.language,voice:spoken.voice,speed:String(speechSpeed())});
    const response = await fetch('/tts?'+query,{method:'POST',body:reply.text,signal:controller.signal});
    if(!response.ok) throw new Error('Speech synthesis failed. Your reply is shown above.');
    const audio = await response.blob();check();if(audio.size<=44)throw new Error('The reply audio was empty.');
    setState('speaking',active?'Press Space to interrupt and speak. Listening resumes after the reply.':'Press Space to stop the reply.');
    await playReply(audio,controller.signal);check();busy=false;
    if(active)listen();else setState('idle','Send another message, or start a voice conversation.');
  } catch(error) {
    if(id !== epoch) return;
    if(!committed) history=before;
    stopSession(error.name==='AbortError'?'Reply stopped.':error.message);
    if(error.name!=='AbortError')setState('error',error.message);
  } finally {if(id===epoch)abort=null;}
}
$('start').onclick = async () => {
  if(secureURL){location.assign(secureURL);return;}
  if(active || busy) return;
  active=true;const id=++epoch;primeAudio();setState('listening','Allow microphone access to begin.');$('finish').hidden=true;
  try {
    const acquired=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    if(id!==epoch){acquired.getTracks().forEach(track=>track.stop());return;}
    stream=acquired;
    armGlow();
    const Audio = window.AudioContext || window.webkitAudioContext;
    if(!context || context.state==='closed')context=new Audio();
    await context.resume();
    if(id!==epoch)return;
    analyser=context.createAnalyser();analyser.fftSize=2048;source=context.createMediaStreamSource(stream);source.connect(analyser);
    listen();
  } catch(error){if(id===epoch)stopSession(error.name==='NotAllowedError'?'Microphone permission was denied. Allow access in your browser, or type below.':error.message);}
};
player.addEventListener('play',()=>{
  // Replaying an older reply through the native controls must also mute capture.
  if(active && stream && phase==='listening'){
    clearCapture();busy=true;setState('speaking','Playing your reply. Interrupt to speak again.');
    player.addEventListener('ended',()=>{if(active && phase==='speaking'){busy=false;listen();}},{once:true});
  }
});
$('end').onclick=()=>stopSession();
$('finish').onclick=()=>{if(recorder?.state==='recording'){recorder.sendNow=true;recorder.stop();}};
function interruptReply() {
  if(!busy)return;
  epoch++;abort?.abort();abort=null;busy=false;clearCapture();player.pause();
  if(active)listen();else setState('idle','Reply interrupted. Send another message when ready.');
}
$('interrupt').onclick=interruptReply;
$('resume').onclick=()=>resumePlayback?.();
let spaceHeld=false;
window.addEventListener('keydown',event=>{
  if(event.code!=='Space' && event.key!==' ')return;
  const target=event.target;
  if(event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
     target?.isContentEditable || target?.closest?.('input,textarea,select,button,a,summary,[role="button"],[role="textbox"]'))return;
  if(spaceHeld){event.preventDefault();return;}
  if(event.repeat || !busy)return;
  event.preventDefault();spaceHeld=true;interruptReply();
});
window.addEventListener('keyup',event=>{if(spaceHeld && (event.code==='Space'||event.key===' ')){event.preventDefault();spaceHeld=false;}});
window.addEventListener('blur',()=>{spaceHeld=false;});
function speechSpeed(){const value=Number($('speed').value);return Number.isFinite(value)?Math.min(2,Math.max(.5,value)):1.2;}
$('speed').oninput=()=>{const value=speechSpeed().toFixed(1);$('speed-value').value=value+'×';$('speed').setAttribute('aria-valuetext',value+' times');};
$('new').onclick=()=>{stopSession('A fresh conversation. Start talking or type below.');setGlow(0);history=[];$('messages').replaceChildren();$('turns').textContent='Just this tab';player.removeAttribute('src');if(audioURL){URL.revokeObjectURL(audioURL);audioURL=null;}};
$('compose').onsubmit=event=>{event.preventDefault();const text=$('text').value.trim();if(!text||busy||!ready)return;primeAudio();armGlow();$('text').value='';runTurn(text);};
let languageList=[],autoLanguage='a',speechPreferences={voices:{}};
try{const saved=JSON.parse(localStorage.getItem('voice-speech')||'null');if(saved && typeof saved==='object' && !Array.isArray(saved))speechPreferences={...saved,voices:saved.voices&&typeof saved.voices==='object'?saved.voices:{}};}catch(_){}
function saveSpeech(){
  speechPreferences.language=$('language').value;
  speechPreferences.corrections=$('corrections').value;
  speechPreferences.enabled=$('corrections-enabled').checked;
  try{localStorage.setItem('voice-speech',JSON.stringify(speechPreferences));}catch(_){}
}
function effectiveLanguage(){return $('language').value==='auto'?autoLanguage:$('language').value;}
function voices(){
  const language=effectiveLanguage(), selected=speechPreferences.voices[language];
  $('voice').replaceChildren();
  for(const voice of voiceList.filter(v=>v.startsWith(language))){const option=document.createElement('option');option.value=voice;option.textContent=voice.replace(/^[a-z]+_/,'').replaceAll('_',' ');$('voice').append(option);}
  const preferred=selected||languageList.find(x=>x.code===language)?.default_voice;
  if([...$('voice').options].some(x=>x.value===preferred))$('voice').value=preferred;
  $('voice-note').textContent=(languageList.find(x=>x.code===language)?.name||language)+' voice';
}
function replyVoice(text){
  if($('language').value==='auto'){
    autoLanguage=/[\u0900-\u097f]/u.test(text)?'h':'a';
    if(!languageList.some(x=>x.code===autoLanguage))throw new Error('This reply needs a language that the TTS service does not offer.');
    voices();
  }
  if(!$('voice').value)throw new Error('No voice is available for the selected TTS language.');
  return {language:effectiveLanguage(),voice:$('voice').value};
}
function correctionRules(){
  const lines=$('corrections').value.split('\n').map(s=>s.trim()).filter(Boolean),rules=[];
  let error=lines.length>20?'Use at most 20 corrections.':'';
  for(const line of lines){const at=line.indexOf('=');const from=line.slice(0,at).trim(),to=line.slice(at+1).trim();
    if(at<1||!from||!to||from.length>120||to.length>120){error='Use heard phrase = intended phrase, up to 120 characters each.';break;}
    rules.push([from,to]);
  }
  $('corrections').setAttribute('aria-invalid',String(!!error));
  $('corrections-note').textContent=error?'Corrections paused. '+error:'Applied after STT, before Qwen. Original text stays visible. Saved in this browser.';
  return error?[]:rules;
}
function correctSpeech(text){
  const rules=correctionRules();if(!$('corrections-enabled').checked||!rules.length)return text;
  const replacements=new Map(rules.map(([from,to])=>[from.toLocaleLowerCase(),to]));
  const escaped=[...replacements.keys()].sort((a,b)=>b.length-a.length).map(s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  const expression=new RegExp('(^|[^\\p{L}\\p{N}_])('+escaped.join('|')+')(?=$|[^\\p{L}\\p{N}_])','giu');
  return text.replace(expression,(_,prefix,phrase)=>prefix+replacements.get(phrase.toLocaleLowerCase()));
}
$('language').onchange=()=>{voices();saveSpeech();};
$('voice').onchange=()=>{speechPreferences.voices[effectiveLanguage()]=$('voice').value;saveSpeech();};
$('corrections').oninput=()=>{correctionRules();saveSpeech();};
$('corrections-enabled').onchange=saveSpeech;
if(typeof speechPreferences.corrections==='string')$('corrections').value=speechPreferences.corrections.slice(0,2400);
if(typeof speechPreferences.enabled==='boolean')$('corrections-enabled').checked=speechPreferences.enabled;
correctionRules();
// ------------------------------------------------------------- directory access
// Qwen can ask for a folder; only this card can hand one over.  The flow is
// deliberately not a held-open request: request_directory files the ask and
// returns at once, the turn ends, and on the next turn the folder is simply
// readable.  That costs one round trip of patience and buys the property that
// no generation is ever blocked on a click that may never come.
let approvalsSeen = '', deciding = false;
function decide(decision, payload, button) {
  const card = $('approvals');
  deciding = true;
  for (const node of card.querySelectorAll('button')) node.disabled = true;
  if (button) button.textContent = '…';
  fetch('/approvals',{method:'POST',headers:{'Content-Type':'application/json'},
                     body:JSON.stringify({decision, ...payload})})
    .then(async response => {
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error
        : 'That did not work. The folder list may have changed; reload the page.');
      approvalsSeen = '';
      renderApprovals(result);
      if (decision === 'approve') $('status').textContent =
        'Approved. Ask again and Qwen can read it now.';
      if (decision === 'revoke') $('status').textContent = 'Access removed. It stops on the next read.';
    })
    .catch(error => { $('status').textContent = error.message; refreshApprovals(); })
    .finally(() => { deciding = false; });
}
function approvalRow(entry) {
  const row = document.createElement('div'); row.className = 'approval';
  const path = document.createElement('code'); path.textContent = entry.realpath; row.append(path);
  if (entry.reason) { const why = document.createElement('div'); why.className = 'why';
    why.textContent = `Qwen asked for it because: ${entry.reason}`; row.append(why); }
  const scope = document.createElement('div'); scope.className = 'scope';
  scope.textContent = entry.error
    ? `Its contents could not be checked (${entry.error}).`
    : `Approving lets Qwen read everything under this folder — about ${entry.files}${entry.truncated ? '+' : ''} files.`;
  row.append(scope);
  if (Array.isArray(entry.hints) && entry.hints.length) {
    const extra = entry.hints.length > 3 ? ` and ${entry.hints.length - 3} more` : '';
    const risk = document.createElement('div'); risk.className = 'risk';
    risk.textContent = `Heads up, it also contains ${entry.hints.slice(0, 3).join(', ')}${extra}. `
      + 'Read those names before you approve.';
    row.append(risk);
  }
  const actions = document.createElement('div'); actions.className = 'row';
  const yes = document.createElement('button'); yes.type = 'button'; yes.className = 'approve';
  yes.textContent = 'Approve'; yes.onclick = () => decide('approve', {id: entry.id}, yes);
  const no = document.createElement('button'); no.type = 'button'; no.textContent = 'Not now';
  no.onclick = () => decide('decline', {id: entry.id}, no);
  actions.append(yes, no); row.append(actions);
  return row;
}
function renderApprovals(state) {
  if (deciding) return;
  const card = $('approvals');
  const pending = Array.isArray(state?.pending) ? state.pending : [];
  const granted = Array.isArray(state?.granted) ? state.granted : [];
  const shown = JSON.stringify([state?.enabled ?? false, pending, granted]);
  // Re-rendering on every poll would rebuild the buttons and eat a click that
  // landed in between.  Nothing changed, nothing moves.
  if (shown === approvalsSeen) return;
  approvalsSeen = shown;
  card.replaceChildren();
  if (!state?.enabled || (!pending.length && !granted.length)) { card.hidden = true; return; }
  card.hidden = false;
  const head = document.createElement('h3');
  head.textContent = pending.length ? 'Qwen asked to read a folder' : 'Folders Qwen can read';
  card.append(head);
  for (const entry of pending) card.append(approvalRow(entry));
  if (granted.length) {
    const wrap = document.createElement('div'); wrap.className = 'granted';
    const label = document.createElement('span');
    label.textContent = `Approved: ${granted.length} folder${granted.length > 1 ? 's' : ''}`;
    wrap.append(label);
    for (const entry of granted) {
      const chip = document.createElement('button'); chip.type = 'button'; chip.title = entry.realpath;
      const leaf = String(entry.realpath).split('/').filter(Boolean).pop() || entry.realpath;
      chip.textContent = `stop reading ${leaf}`;
      chip.onclick = () => decide('revoke', {path: entry.realpath}, chip);
      wrap.append(chip);
    }
    card.append(wrap);
  }
}
function refreshApprovals() {
  // A convenience, never a dependency: if the queue cannot be read the
  // conversation carries on exactly as it did before this feature existed.
  fetch('/approvals').then(response => response.ok ? response.json() : {enabled: false})
    .then(renderApprovals).catch(() => {});
}
// No polling timer.  A new request can only have been filed by a generation, and
// the turn above already refreshes when one finishes; coming back to a hidden tab
// is the only other moment worth catching up on.  A 4 s interval was tried first
// and it kept a background request in flight forever, which is both a timer on a
// page whose whole job is the microphone and enough to wedge the browser suite.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshApprovals(); });
window.addEventListener('pagehide',()=>{stopSession();releaseAudioContext();});
(async()=>{
  try{
    const responses=await Promise.all(['/chat/health','/languages','/voices'].map(url=>fetch(url)));
    if(responses.some(r=>!r.ok))throw new Error('The voice service is unavailable. Please reload shortly.');
    const [health,languages,catalogue]=await Promise.all(responses.map(r=>r.json()));
    if(!health.available)throw new Error('The conversation model is not connected yet.');
    streaming = health.streaming === true && Array.isArray(health.tools) && health.tools.length > 0;
    languageList=languages.languages;autoLanguage=languages.default;
    $('language').replaceChildren();const automatic=document.createElement('option');automatic.value='auto';automatic.textContent='Auto: English / Hindi';$('language').append(automatic);for(const lang of languageList){const option=document.createElement('option');option.value=lang.code;option.textContent=lang.name;$('language').append(option);}$('language').value=[...$('language').options].some(x=>x.value===speechPreferences.language)?speechPreferences.language:'auto';
    voiceList=catalogue.voices;voices();ready=true;refreshApprovals();
    if(!canRecord && location.protocol==='http:' && Number.isInteger(health.https_port)){
      const url=new URL(location.href);url.protocol='https:';url.port=health.https_port;secureURL=url.href;$('start').textContent='Open secure conversation';
    }
    setState('idle',secureURL?'Open the secure page, accept this server’s certificate, then allow the microphone.':canRecord?'Your voice stays on your server. Start whenever you’re ready.':'This browser cannot record audio here. You can still type and hear replies.');
  }catch(error){setState('error',error.message);}
})();
