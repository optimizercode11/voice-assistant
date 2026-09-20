import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
const source = fs.readFileSync('evidence/review/original-chat.js', 'utf8');
console.log('chat.js SHA256', createHash('sha256').update(source).digest('hex'));
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const controls = new Map();
const $ = id => {if (!controls.has(id)) controls.set(id,{hidden:true}); return controls.get(id);};
const signal = new AbortController();
const handlers = new Map();
let playbackOutcome = 'pending';
const ctx = vm.createContext({$, console, DOMException,
  replySeam:false, resumePlayback:null,
  replaceAudio(){}, stopPlaybackGlow(){},stopBargeWatch(){},setGlow(){}, startPlaybackGlow(){},startBargeWatch(){},setState(){},
  player:{paused:true, addEventListener(name,fn){handlers.set(name,fn);},removeEventListener(name){handlers.delete(name);},pause(){},
    play(){return Promise.reject(Object.assign(new Error('Blocked by policy'),{name:'NotAllowedError'}));}}
});
vm.runInContext(extract('async function playReply(', '\n//'),ctx);
ctx.signal = signal.signal;
vm.runInContext('playReply({}, signal, false, true)',ctx).then(()=>playbackOutcome='resolved',()=>playbackOutcome='rejected');
await new Promise(resolve=>setTimeout(resolve,25));
assert.equal(playbackOutcome,'pending');
assert.equal($('resume').hidden,true);
assert.equal(ctx.resumePlayback,null);
console.log('REPRODUCED: non-final autoplay block leaves pending playback with no resume control');
signal.abort();
let requests = 0;
const json={text:'Side effect completed once'};
const response={ok:true,headers:{get(){return 'application/json';}},async json(){return json;}};
const responseCtx=vm.createContext({fetch:async()=>{requests++;return response;},console});
vm.runInContext(extract('async function responseJSON(', '// Every path'),responseCtx);
await vm.runInContext('responseProgress("/chat/completions", "{}", null)', responseCtx);
assert.equal(requests,2);
console.log('REPRODUCED: plain JSON stream fallback sends two POSTs for one request');
