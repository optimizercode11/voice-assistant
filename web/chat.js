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
let bargeRaf = 0, bargeVoiced = 0, bargeLast = 0, playbackStartedAt = 0, playbackEndedAt = 0;
// A reply is played as several clips (one per sentence).  For the barge-in gate
// the whole reply is ONE playback: the settle window opens once, when the first
// clip starts, and closes once, when the last clip ends.  Bumping the
// timestamps at every sentence seam re-closed the gate for 350 ms per sentence
// and reset the voiced counter each time, which is what made interruption fail
// on multi-sentence replies after the per-sentence pipeline landed (d753f87).
let replySeam = false;   // true between the first clip's start and the last clip's end
const canRecord = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
// ------------------------------------------------------------ barge-in (AEC3)
// The browser's own echo canceller -- libwebrtc AEC3 -- is ALREADY requested:
// getUserMedia below passes echoCancellation:true.  What stopped you interrupting
// was never the echo.  This page mutes the microphone for the whole reply
// (clearCapture sets track.enabled=false), so AEC3 has nothing to cancel and
// nobody is listening.  Opening the mic is the easy half; the hard half is not
// mistaking the reply for your voice.
//
// Two facts make that safe enough to try.  The reply is already tapped for the
// orb glow (playbackAnalyser), so the page knows how loud it is being right now
// -- echo is a scaled copy of that, and a person talking over it is energy ABOVE
// it.  And getSettings().echoCancellation reports whether the canceller is
// actually engaged: if the browser says false -- Bluetooth, some Linux capture
// paths -- barge-in is refused outright rather than left to guess.
//
// The bias is deliberate.  A missed interruption costs one more try; a false one
// makes the assistant talk over you and then act on words nobody said.
// BARGE-GATE-BEGIN: extracted verbatim by tests/barge_gate_test.mjs.
const BARGE = {
  settleMs: 350,   // AEC3 re-converges when playback starts AND when it stops
  floor: 0.020,    // never trust mic energy below this, echo or no echo
  echoGain: 0.5,   // assume ~6 dB of return loss, not AEC3's best case
  holdMs: 220,     // sustained near-end speech before the reply is cut
};
function nearEndSpeech(mic, playback, sincePlayback) {
  // Fail closed on every input.  A playback level we cannot measure is an echo
  // we cannot cancel, and the safe answer to "was that me or them?" is then
  // "probably me -- do not act on it".  The first version of this coerced a
  // missing playback level to zero, which silently degraded the gate to "any
  // loud microphone interrupts", i.e. the assistant interrupting itself.
  if (typeof mic !== 'number' || !Number.isFinite(mic) || mic <= 0) return false;
  if (typeof playback !== 'number' || !Number.isFinite(playback) || playback < 0) return false;
  if (typeof sincePlayback !== 'number' || !Number.isFinite(sincePlayback) || sincePlayback < 0) return false;
  if (sincePlayback < BARGE.settleMs) return false;           // still converging
  // playback === 0 is not junk, it is a gap between words: there is no echo to
  // cancel right now, so the absolute floor alone decides.
  return mic > Math.max(BARGE.floor, playback * BARGE.echoGain);
}
// BARGE-GATE-END
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
// ------------------------------------------------------------- chat history
// The bridge is stateless: every turn re-sends the whole transcript, so what
// Qwen remembers is exactly what this page holds in `history`.  Persisting is
// therefore not decoration, it is the model's memory.
//
// One saved conversation is a scratchpad, not a history, so this browser now
// keeps a *list* of them: an index key holds the metadata for every chat and
// each chat gets its own key for its turns.  Separate keys are not tidiness --
// rewriting every conversation on every turn is write amplification against a
// synchronous, quota-limited store, and one oversized chat would then be able to
// lose all of them.
//
// Four limits are load-bearing rather than defensive:
//   * tools/voice_chat.py parse_messages refuses a request carrying more than
//     101 messages -- measured on the deployed bridge: 101 is a 200, 102 is a
//     400 -- so the transcript that is *sent* is capped at SEND_TURNS while the
//     whole conversation stays readable, and the counter says which is which.
//   * one reply may be 8000 characters (measured: 8000 is a 200, 8001 is a 400),
//     so a single chat is bounded in bytes, oldest turns rolling off first.
//   * localStorage is a few MB per origin, so the *whole list* is bounded too --
//     by count and by bytes -- and eviction never touches the chat on screen.
//   * storage is attacker-writable, so every record is read with the same
//     suspicion as a request body: half turns, forged roles and over-long
//     messages are dropped or clipped, never repaired.
// Storage failing may never cost a turn: the conversation carries on in memory
// and the counter admits out loud that it is not being saved.
const CHAT_INDEX = 'voice-chats';        // metadata for every chat, newest first
const CHAT_PREFIX = 'voice-chat-';       // voice-chat-<id> holds one conversation
const LEGACY_KEY = 'voice-chat';         // the single-conversation build, migrated below
const SEND_TURNS = 40;      // 81 messages at the cap, inside the bridge's 101
const STORE_TURNS = 200;
const STORE_BYTES = 1200000;   // one conversation
const TOTAL_BYTES = 3000000;   // every conversation together
const MAX_CHATS = 60;
const MAX_TITLE = 90;
const MAX_MESSAGE = 8000;   // measured: 8000 chars is a 200, 8001 is a 400
const INDEX_BYTES_LIMIT = 200000;   // the index itself, so one setItem can never fail on it
let transcript = [];        // the chat on screen, oldest turn first
let conversationId = '';
let conversationTitle = '';
let conversationAt = 0;
let saving = true;          // false once storage is unavailable
let chatsOpen = false;
function newConversationId() {
  try { if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
function byteLength(text) {
  // A Hindi reply is three bytes a character, so a character count would let a
  // byte budget be exceeded by a third and turn a save into a quota exception.
  try { return new TextEncoder().encode(text).length; } catch (_) { return text.length * 3; }
}
function usableTurn(value) {
  // Storage is attacker-writable: a hand-edited or half-written record must not
  // be able to author an assistant turn or push a request over the bridge's own
  // per-message ceiling.  Anything unusable is dropped, never repaired.
  if (!value || typeof value !== 'object') return null;
  const q = typeof value.q === 'string' ? value.q.trim() : '';
  const a = typeof value.a === 'string' ? value.a.trim() : '';
  if (!q || !a) return null;                       // a half turn is not a turn
  const turn = {q: q.slice(0, MAX_MESSAGE), a: a.slice(0, MAX_MESSAGE)};
  if (typeof value.original === 'string' && value.original.trim())
    turn.original = value.original.slice(0, MAX_MESSAGE);
  if (Array.isArray(value.tools)) turn.tools = value.tools.slice(0, 8)
    .filter(tool => tool && typeof tool.name === 'string')
    .map(tool => ({name: tool.name.slice(0, 120), ok: tool.ok !== false, ms: Number(tool.ms) || 0}));
  if (Array.isArray(value.sources)) turn.sources = value.sources.slice(0, 8)
    .filter(source => source && typeof source.path === 'string')
    .map(source => ({path: source.path.slice(0, 400),
                     heading: typeof source.heading === 'string' ? source.heading.slice(0, 200) : ''}));
  return turn;
}
function turnFrom(question, answer, original, evidence) {
  return usableTurn({q: question, a: answer, original,
                     tools: evidence?.tools, sources: evidence?.sources});
}
function messagesOf(turn) {
  return [{role: 'user', content: turn.q}, {role: 'assistant', content: turn.a}];
}
function historyFrom(turns) { return turns.slice(-SEND_TURNS).flatMap(messagesOf); }
function chatKey(id) { return CHAT_PREFIX + id; }
function titleFor(turns) {
  // The first thing the person actually said, not a summary: a summary would be
  // a second model call, and would be wrong in a way nobody can argue with.
  const first = turns.find(turn => turn && turn.q);
  if (!first) return 'New conversation';
  const flat = first.q.replace(/\s+/g, ' ').trim();
  return flat.length > MAX_TITLE ? flat.slice(0, MAX_TITLE - 1) + '\u2026' : flat;
}
function whenIs(millis) {
  const stamp = Number(millis);
  if (!Number.isFinite(stamp) || stamp <= 0) return 'unknown time';
  const minutes = Math.floor((Date.now() - stamp) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
  try { return new Date(stamp).toLocaleDateString(); } catch (_) { return 'a while ago'; }
}
function parseRecord(raw, expectId) {
  if (typeof raw !== 'string' || !raw) return null;
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch (_) { return null; }
  if (!parsed || typeof parsed !== 'object' || typeof parsed.id !== 'string') return null;
  if (expectId && parsed.id !== expectId) return null;
  if (!Array.isArray(parsed.turns)) return null;
  const turns = parsed.turns.map(usableTurn).filter(Boolean);
  return {id: parsed.id, turns,
          title: typeof parsed.title === 'string' && parsed.title.trim() ? parsed.title.slice(0, MAX_TITLE) : titleFor(turns),
          at: Number(parsed.at) || 0};
}
function readConversation(id) {
  if (typeof id !== 'string' || !id) return null;
  try { return parseRecord(localStorage.getItem(chatKey(id)), id); } catch (_) { return null; }
}
function readIndex(verify = false) {
  // The index is a cache of what the keys say.  An orphan (key gone: evicted or
  // deleted in another tab) is dropped by checking the key exists; the record
  // itself is only re-parsed when `verify` is set (rendering the list), because
  // parsing every stored chat on every saved turn is the write amplification
  // separate keys exist to avoid.  Opening a chat parses it anyway.
  let parsed = null;
  try { parsed = JSON.parse(localStorage.getItem(CHAT_INDEX) || 'null'); } catch (_) { return []; }
  const list = Array.isArray(parsed) ? parsed : (parsed && Array.isArray(parsed.chats) ? parsed.chats : null);
  if (!list) return [];
  const seen = new Set();
  const entries = [];
  for (const item of list) {
    const id = item && typeof item.id === 'string' ? item.id : '';
    if (!id || seen.has(id) || id === LEGACY_KEY) continue;
    const raw = (() => { try { return localStorage.getItem(chatKey(id)); } catch (_) { return null; } })();
    if (raw === null) continue;                                  // orphan
    seen.add(id);
    if (verify) {
      const record = parseRecord(raw, id);
      if (!record) { seen.delete(id); continue; }                // unreadable
      entries.push({id, title: record.title, at: record.at, turns: record.turns.length,
                    bytes: byteLength(raw)});
      continue;
    }
    const title = typeof item.title === 'string' ? item.title.slice(0, MAX_TITLE) : '';
    entries.push({id, title, at: Number(item.at) || 0,
                  turns: Math.max(0, Number(item.turns) || 0), bytes: raw.length});
  }
  return entries;
}
function writeIndex(entries) {
  let list = entries.slice(0, MAX_CHATS);
  for (let attempt = 0; attempt < 6; attempt++) {
    const payload = JSON.stringify({v: 2, chats: list});
    if (byteLength(payload) > INDEX_BYTES_LIMIT && list.length > 1) { list = list.slice(0, -1); continue; }
    try { localStorage.setItem(CHAT_INDEX, payload); return true; }
    catch (_) { if (list.length > 1) { list = list.slice(0, -1); continue; } return false; }
  }
  return false;
}
function evict(order) {
  // `order` is index entries, newest first, and the chat on screen is always
  // last-resort protected: an eviction that deletes the conversation you are
  // looking at is worse than one that deletes a chat you have not opened in a
  // month.  Evicted turns go with the entry, or the list lies about what it has.
  const keep = [];
  let total = 0;
  for (const entry of order) {
    if (entry.id === conversationId) { keep.unshift(entry); total += entry.bytes || 0; continue; }
    if (keep.length >= MAX_CHATS - 1) { localStorage.removeItem(chatKey(entry.id)); continue; }
    if (total + (entry.bytes || 0) > TOTAL_BYTES && keep.some(item => item.id !== conversationId)) {
      localStorage.removeItem(chatKey(entry.id)); continue;
    }
    total += entry.bytes || 0;
    keep.push(entry);
  }
  return keep;
}
function saveConversation() {
  if (!saving) return false;
  let turns = transcript.slice(-STORE_TURNS);
  let wrote = false;
  // The title is the first thing the person said and stays that even after the
  // oldest turns roll off; `at` is the last activity, which is what "newest
  // first" and "3 min ago" both mean.
  if (!conversationTitle) conversationTitle = titleFor(transcript);
  conversationAt = Date.now();
  for (let attempt = 0; attempt < 8; attempt++) {
    const payload = JSON.stringify({v: 2, id: conversationId, title: conversationTitle,
                                    at: conversationAt, turns});
    const bytes = byteLength(payload);
    if (bytes > STORE_BYTES && turns.length > 1) {
      // Drop a *proportional* slice, not one turn: a record far over budget has
      // to converge inside the retry budget, or the loop exits without writing
      // anything while the counter still claims "saved".
      const keep = Math.ceil(turns.length * (STORE_BYTES / bytes));
      turns = turns.slice(turns.length - Math.max(1, Math.min(turns.length - 1, keep)));
      continue;
    }
    try {
      localStorage.setItem(chatKey(conversationId), payload);
      wrote = true;
      break;
    } catch (error) {
      // QuotaExceeded is the ordinary case: a full origin or a private window.
      if (turns.length > 1) { turns = turns.slice(Math.ceil(turns.length / 2)); continue; }
      saving = false;
      break;
    }
  }
  adoptTranscript(turns);
  if (!wrote) { renderTurnCount(); return false; }
  const entry = {id: conversationId, title: conversationTitle, at: conversationAt,
                 turns: turns.length, bytes: byteLength(localStorage.getItem(chatKey(conversationId)) || '')};
  writeIndex(evict([entry, ...readIndex().filter(item => item.id !== conversationId)]));
  const wanted = `#/chat/${encodeURIComponent(conversationId)}`;
  if (location.hash !== wanted) replaceHash(wanted);   // a saved chat is addressable
  if (chatsOpen) renderChatList();
  return true;
}
function deleteConversation(id) {
  if (typeof id !== 'string' || !id) return false;
  try { localStorage.removeItem(chatKey(id)); } catch (_) { return false; }
  writeIndex(readIndex().filter(item => item.id !== id));
  if (chatsOpen) renderChatList();
  return true;
}
function adoptTranscript(turns) {
  // The single place `transcript` shrinks.  Anything still on screen must be
  // something storage also holds, so a roll-off repaints rather than leaving a
  // bubble that a refresh would silently delete.
  const dropped = transcript.length > turns.length;
  transcript = turns;
  history = historyFrom(transcript);   // what is saved is what Qwen holds
  if (dropped) renderTranscript();
}
function renderTranscript() {
  const host = $('messages');
  host.replaceChildren();
  if (!transcript.length) {
    // The empty state is markup in the document, so a repaint that clears the
    // list has to put it back -- otherwise a fresh chat and a wiped chat both
    // stare at a blank panel instead of asking what is on your mind.
    const empty = document.createElement('div'); empty.className = 'empty';
    const ask = document.createElement('strong'); ask.textContent = 'What\u2019s on your mind?';
    const hint = document.createElement('p'); hint.textContent = 'Start talking, or write a message below.';
    empty.append(ask, hint); host.append(empty);
    return;
  }
  for (const turn of transcript) {
    message('user', turn.q, turn.original ?? null);
    message('assistant', turn.a, null, {tools: turn.tools, sources: turn.sources});
  }
}
function renderTurnCount() {
  const turns = transcript.length;
  const where = !saving ? 'not saved in this browser'
    : turns ? 'saved in this browser' : 'nothing saved yet';
  const remembered = turns > SEND_TURNS ? `Qwen is holding the last ${SEND_TURNS}` : '';
  $('turns').textContent = [turns ? `${turns} ${turns === 1 ? 'turn' : 'turns'}` : 'No conversation yet',
                            where, remembered].filter(Boolean).join(' \u00b7 ');
}
function chatRow(entry) {
  const row = document.createElement('div'); row.className = 'chat-entry'; row.dataset.id = entry.id;
  const open = document.createElement('button');
  open.type = 'button'; open.className = 'chat-open';
  open.append(document.createElement('span'));
  open.firstChild.textContent = entry.title || 'Untitled conversation';
  open.title = entry.title || 'Untitled conversation';
  if (entry.id === conversationId) open.setAttribute('aria-current', 'true');
  open.onclick = () => openChat(entry.id);
  const meta = document.createElement('span'); meta.className = 'chat-meta';
  meta.textContent = `${entry.turns} ${entry.turns === 1 ? 'turn' : 'turns'} \u00b7 ${whenIs(entry.at)}`;
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'chat-remove';
  remove.textContent = '\u00d7';
  remove.setAttribute('aria-label', `Delete this conversation: ${entry.title || 'untitled'}`);
  remove.onclick = () => {
    // Deleting the chat you are looking at has to land somewhere, so it starts a
    // fresh one instead of leaving a panel full of bubbles that no longer exist.
    if (!window.confirm(`Delete this conversation?\n\n${entry.title || 'Untitled'}\n\nIt is gone from this browser and cannot be recovered.`)) return;
    deleteConversation(entry.id);
    if (entry.id === conversationId) startFreshChat(); else setState('idle', 'Conversation deleted.');
  };
  row.append(open, meta, remove);
  return row;
}
function renderChatList() {
  const card = $('chats'), toggle = $('chats-toggle');
  if (!card || !toggle) return;
  const entries = readIndex(true);
  toggle.textContent = `Chats (${entries.length})`;
  card.hidden = !chatsOpen;
  if (!chatsOpen) return;
  card.replaceChildren();
  if (!entries.length) {
    const none = document.createElement('p'); none.className = 'chat-none';
    none.textContent = 'No saved conversations yet.';
    card.append(none);
    return;
  }
  for (const entry of entries) card.append(chatRow(entry));
}
function setChatsOpen(open) {
  chatsOpen = open === undefined ? !chatsOpen : open === true;
  $('chats-toggle')?.setAttribute('aria-expanded', String(chatsOpen));
  renderChatList();
}
function chatIdFromHash() {
  const match = /^#\/chat\/(.+)$/.exec(location.hash || '');
  return match ? decodeURIComponent(match[1]) : '';
}
function openChat(id) {
  const stored = readConversation(id);
  if (!stored) {
    // The index is a cache, so a missing key means it was evicted or deleted in
    // another tab.  Say so and repair the list rather than showing nothing.
    writeIndex(readIndex());
    renderChatList();
    setState('idle', 'That conversation is no longer in this browser.');
    return 0;
  }
  if (busy) interruptReply();   // never swap the transcript out from under a live turn
  heldText = ''; fragmentHolds = 0;   // a half-heard sentence belongs to the chat it was said in
  saving = true;                      // storage may have been full for the last chat, not this one
  conversationId = stored.id;
  conversationTitle = stored.title;
  conversationAt = stored.at;
  transcript = stored.turns.slice(-STORE_TURNS);
  history = historyFrom(transcript);
  renderTranscript();
  renderTurnCount();
  if (chatsOpen) renderChatList();
  if (transcript.length < stored.turns.length) saveConversation();   // store what is shown
  const wanted = `#/chat/${encodeURIComponent(stored.id)}`;
  if (location.hash !== wanted) replaceHash(wanted);
  renderChatList();
  return transcript.length;
}
function replaceHash(hash) {
  // Replacing rather than assigning means browsing back through chats does not
  // stack up an entry for every click.  `history` is this page's message array,
  // so the browser's own object has to be named in full.
  try { window.history.replaceState(null, '', hash || location.pathname + location.search); }
  catch (_) { if (hash) location.hash = hash; }
}
function startFreshChat() {
  // A fresh chat is only an id until its first turn is committed: nothing is
  // written, so pressing "New chat" twice cannot fill the list with blanks, and
  // the old conversation stays in the list untouched.
  if (transcript.length || !conversationId) {
    conversationId = newConversationId();
    conversationTitle = '';
    conversationAt = 0;
    transcript = [];
    history = [];
  }
  saving = true;
  heldText = ''; fragmentHolds = 0;
  renderTranscript();
  renderTurnCount();
  renderChatList();
  replaceHash('');
}
function migrateLegacy() {
  // The previous build kept exactly one conversation under `voice-chat`.  A
  // reader must not lose it to the upgrade, and must not get it twice, so the
  // key is removed in the same breath as the new record is written.
  let raw = null;
  try { raw = localStorage.getItem(LEGACY_KEY); } catch (_) { return false; }
  if (raw === null) return false;
  const record = parseRecord(raw);
  try { localStorage.removeItem(LEGACY_KEY); } catch (_) {}
  if (!record || !record.turns.length) return false;
  const id = newConversationId();
  const turns = record.turns.slice(-STORE_TURNS);
  const at = record.at || Date.now();
  const payload = JSON.stringify({v: 2, id, title: titleFor(turns), at, turns});
  try { localStorage.setItem(chatKey(id), payload); } catch (_) { return false; }
  writeIndex(evict([{id, title: titleFor(turns), at, turns: turns.length, bytes: byteLength(payload)},
                    ...readIndex().filter(item => item.id !== id)]));
  return id;
}
function restoreChat() {
  const migrated = migrateLegacy();
  const wanted = chatIdFromHash();
  if (wanted && readConversation(wanted)) return openChat(wanted);
  if (migrated && readConversation(migrated)) return openChat(migrated);
  const first = readIndex().find(entry => readConversation(entry.id));
  if (first) return openChat(first.id);
  startFreshChat();
  return 0;
}
window.addEventListener('hashchange', () => {
  // Back/forward and pasted links land on the chat they name; an unknown or
  // empty fragment leaves the conversation on screen alone.
  const wanted = chatIdFromHash();
  if (wanted && wanted !== conversationId && readConversation(wanted)) openChat(wanted);
});
try {
  localStorage.setItem('voice-chat-probe', '1');
  localStorage.removeItem('voice-chat-probe');
} catch (_) { saving = false; }
window.addEventListener('storage', event => {
  // Another tab wrote, deleted or evicted something.  Adopting its transcript
  // mid-reply would be its own bug, so this tab refreshes the *list* only and
  // keeps working on the conversation it has on screen.
  if (event.key !== null && event.key !== CHAT_INDEX && !String(event.key).startsWith(CHAT_PREFIX)) return;
  if (chatsOpen) renderChatList();
  if (event.key === null) { saving = false; renderTurnCount(); }   // storage cleared entirely
});

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
  stopBargeWatch();
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
    if (!Audio || !window.AnalyserNode) return;
    if (!context || context.state === 'closed') context = new Audio();
    const ctx = context;
    // createMediaElementSource is a method of the AudioContext, not of the media
    // element, so this capability check is one no browser can pass.  Asking the
    // element made armGlow return before building the graph, which silently
    // killed three things at once: the glow while the assistant speaks, the
    // echo reference barge-in needs, and barge-in itself.
    if (typeof ctx.createMediaElementSource !== 'function') return;
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
  stopBargeWatch(); stopThinkingAloud(); replySeam = false;
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
  // AEC3 is still re-converging right after a reply, and the tail of that reply
  // is the likeliest thing to be mistaken for your voice -- it is the assistant
  // answering itself.  Charge the settle window only while the reply is actually
  // still settling.  playbackEndedAt is a timestamp, not a flag: reading it as a
  // flag made every later clip pay 350 ms of dead endpointing forever after the
  // first reply, which is patience charged to turns that earned none.
  const sincePlayback = playbackEndedAt ? performance.now() - playbackEndedAt : Infinity;
  let settleUntil = sincePlayback < BARGE.settleMs
    ? performance.now() + (BARGE.settleMs - sincePlayback) : 0;
  const samples = new Float32Array(analyser.fftSize);
  function tick() {
    if (recorder !== rec || rec.state !== 'recording') return;
    analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length), now=performance.now();
    $('level').style.width = `${Math.min(100,rms*1000)}%`;
    setGlow(rms * 7);                       // same measurement, one shared meter
    if (now < settleUntil) {raf = requestAnimationFrame(tick); return;}
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
function bargeWanted() {
  const box = $('barge-in');
  return !(box && box.checked === false);              // on unless switched off
}
function bargeAvailable() {
  // Chromium reports whether the canceller is actually engaged.  A Bluetooth
  // output or a raw Linux capture path answers false, and on those devices the
  // assistant really would hear itself -- so refuse instead of guessing.
  const settings = stream?.getAudioTracks?.()[0]?.getSettings?.();
  return settings ? settings.echoCancellation === true : false;
}
function stopBargeWatch() {
  if (bargeRaf) cancelAnimationFrame(bargeRaf);
  bargeRaf = 0; bargeVoiced = 0;
}
function startBargeWatch() {
  // Already watching and still speaking: keep the loop and its voiced counter.
  // Restarting per clip discarded an interruption that spanned a sentence seam.
  if (bargeRaf && phase === 'speaking') return true;
  stopBargeWatch();
  if (!active || !analyser || !bargeWanted()) return false;
  if (!playbackAnalyser) {
    // Without the glow's WebAudio tap there is no reference signal, and a gate
    // with no reference is a loudness trigger wearing a security costume.
    const note = $('barge-note');
    if (note) note.textContent = 'Unavailable: the reply is not routed through WebAudio.';
    return false;
  }
  if (!bargeAvailable()) {
    // Say it out loud rather than failing quietly: "it would not let me
    // interrupt" is a far worse mystery than a device that says it cannot
    // cancel its own echo.
    const note = $('barge-note');
    if (note) note.textContent = 'Unavailable on this audio device: no echo cancellation.';
    return false;
  }
  // Re-entrant on purpose.  A reply is now played as several clips, and the
  // gate has to survive the seam between them; a frame where the element is
  // briefly paused makes the loop bail out, so it must be safe to start again.
  // The whole point: capture stays open while the reply plays.
  stream.getTracks().forEach(track => { track.enabled = true; });
  const samples = new Float32Array(analyser.fftSize);
  const bytes = playbackAnalyser ? new Uint8Array(playbackAnalyser.fftSize) : null;
  bargeLast = performance.now();
  const step = () => {
    if (!active || phase !== 'speaking') { stopBargeWatch(); return; }
    const now = performance.now();
    analyser.getFloatTimeDomainData(samples);
    const mic = Math.sqrt(samples.reduce((sum, x) => sum + x * x, 0) / samples.length);
    let playback = 0;
    if (bytes && !player.paused && !player.ended) {
      playbackAnalyser.getByteTimeDomainData(bytes);
      playback = rmsOf(Array.from(bytes, value => (value - 128) / 128), 1);
    }
    // Whichever happened last: AEC3 re-converges on a stop as well as a start.
    const since = now - Math.max(playbackStartedAt, playbackEndedAt);
    if (nearEndSpeech(mic, playback, since)) bargeVoiced += Math.min(100, now - bargeLast);
    else bargeVoiced = 0;
    bargeLast = now;
    if (bargeVoiced >= BARGE.holdMs) { stopBargeWatch(); interruptReply(); return; }
    bargeRaf = requestAnimationFrame(step);
  };
  bargeRaf = requestAnimationFrame(step);
  return true;
}
// ------------------------------------------------------ thinking aloud
// A tool round costs a second full generation, so the gap between the last
// word of the question and the first word of the answer can be several seconds
// of nothing.  Silence there is indistinguishable from a hang -- the user
// cannot tell "it is reading your notes" from "it died" -- so say which it is,
// in the same voice, and get out of the way the moment the reply is ready.
//
// Two rules keep this honest.  It never claims more than is happening: the
// wording comes from the tool the server actually reported running, and the
// fallback is deliberately generic.  And it never delays the answer: the clip
// is fetched in parallel with the generation, dropped if the answer wins the
// race, and stopped mid-syllable if the answer arrives while it is playing.
const THINK = {afterMs: 900};
const THINK_LINES = {
  search_notes: 'Let me check your notes.',
  fetch_url: 'Let me look that up.',
  request_directory: 'I need permission for that folder first.',
  mcp__stack__health: 'Let me check how the stack is doing.',
};
let thinkTimer = 0, thinkToken = 0, thinkSpoken = false, thinkPlaying = false;

function thinkingAloudWanted() {
  const box = $('think-aloud');
  return !(box && box.checked === false);              // on unless switched off
}
function thinkLine(calls) {
  for (const call of calls || []) {
    if (call === 'now') continue;                      // instant; never worth a word
    if (THINK_LINES[call]) return THINK_LINES[call];
    if (call.startsWith('mcp__files__')) return 'Let me have a look at the files.';
  }
  return 'One moment.';
}
function cancelThinkingAloud() {
  thinkToken++;                                        // any in-flight clip is now stale
  if (thinkTimer) { clearTimeout(thinkTimer); thinkTimer = 0; }
  thinkSpoken = false;
}
function stopThinkingAloud() {
  // Called when the reply exists, and from every path that ends a turn.
  cancelThinkingAloud();
  if (!thinkPlaying) return;
  thinkPlaying = false;
  player.pause();
  stopPlaybackGlow(); setGlow(0);
}
function armThinkingAloud(id, signal) {
  cancelThinkingAloud();
  if (!thinkingAloudWanted()) return;                  // every reply here is spoken, so this one is too
  const token = thinkToken;
  thinkTimer = setTimeout(() => {
    thinkTimer = 0;
    if (id !== epoch || thinkToken !== token || signal.aborted) return;
    mentionThinking('One moment.', id, signal);
  }, THINK.afterMs);
}
function mentionThinking(text, id, signal) {
  if (!thinkingAloudWanted()) return;                  // both throats answer to the same switch
  if (thinkSpoken) return;                             // one per turn, not one per tool round
  thinkSpoken = true;
  let spoken;
  try { spoken = replyVoice(text); } catch (_) { return; }
  const token = thinkToken;
  const query = new URLSearchParams({format:'wav', language:spoken.language,
                                     voice:spoken.voice, speed:String(speechSpeed())});
  const filler = new AbortController();
  signal.addEventListener('abort', () => filler.abort(), {once: true});
  fetch('/tts?' + query, {method:'POST', body:text, signal:filler.signal})
    .then(response => response.ok ? response.blob() : null)
    .then(audio => {
      if (!audio || audio.size <= 44) return;
      // The answer won the race, or the turn moved on: throw the clip away.
      if (id !== epoch || thinkToken !== token || signal.aborted) return;
      // Deliberately NOT phase 'speaking': barge-in stays disarmed for an
      // acknowledgment, so a room can never interrupt its own filler and be
      // credited with interrupting the reply.  Space still stops everything.
      thinkPlaying = true;
      const finish = () => {
        player.removeEventListener('ended', finish);
        player.removeEventListener('error', finish);
        if (thinkToken !== token) return;
        thinkPlaying = false; stopPlaybackGlow(); setGlow(0);
      };
      replaceAudio(audio);
      startPlaybackGlow();
      player.addEventListener('ended', finish);
      player.addEventListener('error', finish);
      player.play().catch(() => { finish(); });
    })
    .catch(() => {});
}
async function playReply(blob, signal, final = true, first = true, onStart = null) {
  // A continuation clip must not reopen the settle window (see replySeam).
  replySeam = !first;
  replaceAudio(blob);
  await new Promise((resolve,reject)=>{
    let settled = false;
    const cleanup = () => {
      settled = true;
      // Between two sentences of the same reply the voice is not finished, so
      // the glow and the interruption gate stay up.  Tearing them down per clip
      // would blink the orb out and disarm barge-in at every comma.
      if (final) { stopPlaybackGlow(); stopBargeWatch(); setGlow(0); }
      if (final || signal.aborted) replySeam = false;
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
      player.play().then(()=>{replySeam = !final;if(!settled && !player.paused){startPlaybackGlow();startBargeWatch();onStart?.();}},error=>{
        if(settled || signal.aborted)return;
        if(error.name!=='NotAllowedError'){failed();return;}
        if(!final){startBargeWatch();return;}   // re-entrant: attempt() is already the retry path
        resumePlayback=attempt;$('resume').hidden=false;
        setState('speaking','Choose Play reply to allow audio in your browser.');
      });
    };
    player.addEventListener('ended',ended);player.addEventListener('error',failed);signal.addEventListener('abort',cancelled,{once:true});
    if(signal.aborted){cancelled();return;}
    attempt();
  });
}

// ---------------------------------------------------------------- speaking a reply
// The engine synthesizes an entire request before it returns a single byte of
// audio.  Measured live against the deployed Kokoro: 32 characters take 0.10 s,
// 108 take 0.29 s, 441 take 0.93 s, 774 take 1.16 s -- about 2 ms per character
// with no fixed cost to amortize.  Asking for a whole answer in one request is
// therefore a promise that the listener waits the full 1.2 s *after* the words
// are already on the screen.  That gap is what people call "slow TTS": the
// engine is not slow, the page asked for too much at once.
//
// The first sentence is its own request so the first word is audible after
// ~0.1 s.  What follows is NOT one request per sentence (2026-09-11): every
// request is a fresh connection, a serialized G2P pass and its own GPU step, and
// every clip boundary is a source swap the ear hears as a 50-150 ms hiccup, so a
// reply cut into sentences cost 3-4x the synthesis and a stutter at every full
// stop.  Instead the remainder is grouped into a few requests that grow
// geometrically: a clip only has to be synthesized while the previous one is
// being spoken, and speech is ~25x slower than synthesis, so each group may be
// `growth` times the one before it.  The engine batches the sentences inside
// one request itself.
// SPEECH-CHUNK-BEGIN
const SPEECH_CHUNK = {minChars: 24, maxChars: 420, prefetch: 2, growth: 8, groupMax: 4000};

function speechChunks(text) {
  // Only words can be spoken.  A number or an object reaching here is a bug
  // somewhere upstream, and saying "NaN" or "object Object" aloud to a person
  // is a worse outcome than saying nothing at all.
  const clean = typeof text === 'string' ? text.replace(/\s+/g, ' ').trim() : '';
  if (!clean) return [];
  // A sentence ends at a full stop, bang, question mark or ellipsis and may
  // carry a closing quote or bracket with it.  Anything after the last mark is
  // a sentence the engine left unpunctuated, and it still has to be spoken.
  const sentences = clean.match(/[^.!?\u2026]+[.!?\u2026]+["'\u201d\u2019)\]]*|[^.!?\u2026]+$/g) ?? [clean];
  const chunks = [];
  for (let piece of sentences.map(value => value.trim())) {
    if (!piece) continue;
    // A run-on clause with no full stop in it is still too long to hold the
    // voice hostage.  Break at the last comma or semicolon that fits, then at
    // the last space: a seam mid-word is audible as a word cut in half, which
    // is a worse defect than the pause we are trying to remove.
    while (piece.length > SPEECH_CHUNK.maxChars) {
      const room = SPEECH_CHUNK.maxChars;
      const clause = Math.max(piece.lastIndexOf(',', room), piece.lastIndexOf(';', room));
      const space = piece.lastIndexOf(' ', room);
      // Cut after punctuation when there is punctuation to cut after, otherwise
      // on a space, and only as a last resort through the middle of a word.
      const at = clause > SPEECH_CHUNK.minChars ? clause + 1
               : space > SPEECH_CHUNK.minChars ? space
               : room;
      chunks.push(piece.slice(0, at).trim());
      piece = piece.slice(at).trim();
    }
    if (!piece) continue;
    // "Yes." is a real turn of speech; a stray "\u2026" is a click.  A fragment too
    // short to be worth its own request joins the sentence before it -- but the
    // first sentence never waits for anyone, because that is the one the
    // listener is waiting for.
    if (piece.length < SPEECH_CHUNK.minChars && chunks.length) chunks[chunks.length - 1] += ' ' + piece;
    else chunks.push(piece);
  }
  const pieces = chunks.filter(Boolean);
  if (pieces.length < 2) return pieces;
  // Group everything after the first bite.  Group k may hold up to `growth`
  // times the characters of group k-1 (never less than one run-on ceiling,
  // never more than groupMax), which keeps its synthesis inside the previous
  // clip's playback and leaves one or two seams instead of one per sentence.
  const groups = [pieces[0]];
  for (const piece of pieces.slice(1)) {
    const k = groups.length - 1;
    const budget = k === 0 ? 0
      : Math.min(SPEECH_CHUNK.groupMax, Math.max(SPEECH_CHUNK.maxChars, groups[k - 1].length * SPEECH_CHUNK.growth));
    if (k === 0 || groups[k].length + 1 + piece.length > budget) groups.push(piece);
    else groups[k] += ' ' + piece;
  }
  return groups;
}
// SPEECH-CHUNK-END

// ------------------------------------------------ streamed speech (2026-09-11)
// The bridge now forwards the model's prose as it is generated ('delta'
// events).  Waiting for the whole answer before speaking cost the entire
// generation -- ~2 s for a typical reply -- in silence after the question.
// Sentences are spoken as they complete: the first one alone, then whatever
// has arrived by the time the current clip is about to end, so generation
// (~45 chars/s) stays ahead of speech (~17 chars/s) and the engine still
// batches the bulk.  The 'answer' event at the end is authoritative: what it
// says beyond what was already spoken is spoken, and nothing is committed to
// the transcript from the stream itself.
function sentenceSource() {
  const src = {queue: [], closed: false, buffer: '', round: 0, taken: '', all: '', waiter: null};
  const wake = () => { const w = src.waiter; src.waiter = null; if (w) w(); };
  src.wait = signal => new Promise((resolve, reject) => {
    if (src.queue.length || src.closed) return resolve();
    src.waiter = resolve;
    signal?.addEventListener('abort', () => { src.waiter = null; reject(new DOMException('Stopped', 'AbortError')); }, {once: true});
  });
  const drain = final => {
    const text = src.buffer.replace(/\s+/g, ' ');
    const done = text.match(/[^.!?\u2026]+[.!?\u2026]+["'\u201d\u2019)\]]*/g) ?? [];
    let used = 0;
    for (const sentence of done) { used += sentence.length; const piece = sentence.trim(); if (piece) src.queue.push(piece); }
    src.buffer = text.slice(used);
    if (final) { const tail = src.buffer.trim(); if (tail) src.queue.push(tail); src.buffer = ''; }
    if (src.queue.length || final) wake();
  };
  src.push = (round, text) => {
    if (round !== src.round) { src.buffer = ''; src.all = ''; src.round = round; }   // a new generation
    src.buffer += text; src.all += text;
    drain(false);
  };
  src.finish = answer => {
    // What the stream said must be what the answer says; if the two differ the
    // unspoken remainder of the answer wins and the stale queue is dropped.
    drain(true);
    const same = (a, b) => a.replace(/\s+/g, '') === b.replace(/\s+/g, '');
    if (!same(src.all, answer)) {
      src.queue.length = 0;
      const rest = remainderAfter(answer, src.taken);
      if (rest === null) console.warn('streamed speech diverged from the answer; the rest is not re-spoken');
      else for (const piece of speechChunks(rest)) src.queue.push(piece);
    }
    src.closed = true; wake();
  };
  src.close = () => { src.closed = true; wake(); };
  return src;
}
function remainderAfter(answer, spoken) {
  // The part of `answer` after `spoken`, ignoring whitespace; null if `spoken`
  // is not a prefix of it.
  let i = 0, j = 0;
  while (j < spoken.length) {
    if (/\s/.test(spoken[j])) { j++; continue; }
    while (i < answer.length && /\s/.test(answer[i])) i++;
    if (i >= answer.length || answer[i] !== spoken[j]) return null;
    i++; j++;
  }
  return answer.slice(i).trim();
}
async function speakStreamed(src, signal, onFirstClip) {
  let query = null;
  const clipFor = text => {
    if (!query) {
      const spoken = replyVoice(text);   // the first sentence decides the voice for the reply
      query = new URLSearchParams({format: 'wav', language: spoken.language, voice: spoken.voice, speed: String(speechSpeed())});
    }
    return fetch('/tts?' + query, {method: 'POST', body: text, signal})
      .then(response => { if (!response.ok) throw new Error('Speech synthesis failed. Your reply is shown above.'); return response.blob(); })
      .then(audio => { if (audio.size <= 44) throw new Error('The reply audio was empty.'); return audio; })
      .catch(error => { if (error.name !== 'AbortError') error.keepSession = true; throw error; });
  };
  let first = true, prevLen = 0;
  const take = () => {
    if (!src.queue.length) return null;
    let group;
    if (first) { first = false; group = src.queue.shift(); }
    else {
      const budget = Math.min(SPEECH_CHUNK.groupMax, Math.max(SPEECH_CHUNK.maxChars, prevLen * SPEECH_CHUNK.growth));
      group = '';
      while (src.queue.length && (!group || group.length + 1 + src.queue[0].length <= budget))
        group = group ? group + ' ' + src.queue.shift() : src.queue.shift();
    }
    prevLen = group.length; src.taken += (src.taken ? ' ' : '') + group;
    return group;
  };
  const next = async () => { for (;;) { const g = take(); if (g) return g; if (src.closed) return null; await src.wait(signal); } };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  let group = await next();
  if (!group) return false;
  let pending = clipFor(group), index = 0;
  try {
    while (group) {
      const audio = await pending;
      if (signal.aborted) throw new DOMException('Stopped', 'AbortError');
      if (!index) onFirstClip?.();
      let startResolve; const started = new Promise(resolve => { startResolve = resolve; });
      const playing = playReply(audio, signal, false, index === 0, startResolve);
      // Take the next group late: what has arrived by ~0.5 s before this clip
      // ends travels together, and its synthesis still lands before the seam.
      const durationMs = Math.max(0, (audio.size - 44) / 2 / 24000 * 1000);
      await Promise.race([started, playing]);
      await Promise.race([sleep(Math.max(0, durationMs - 500)), playing]);
      const following = next().then(g => g ? {g, clip: clipFor(g)} : null);
      await playing;
      const item = await following;
      if (!item) break;
      group = item.g; pending = item.clip; index++;
    }
  } finally {
    // The reply is over (or was cut): close the settle window and the gate now,
    // since every clip was played as a continuation.
    replySeam = false; playbackEndedAt = performance.now();
    stopPlaybackGlow(); stopBargeWatch(); setGlow(0);
  }
  return true;
}

async function speakReply(answer, signal, onFirstClip) {
  const spoken = replyVoice(answer);
  const query = new URLSearchParams({format: 'wav', language: spoken.language,
                                     voice: spoken.voice, speed: String(speechSpeed())});
  const parts = speechChunks(answer);
  if (!parts.length) {
    const empty = new Error('The reply audio was empty.');
    empty.keepSession = true;
    throw empty;
  }
  const clips = new Map();
  const clip = index => {
    if (!clips.has(index)) {
      clips.set(index, fetch('/tts?' + query, {method: 'POST', body: parts[index], signal})
        .then(response => {
          if (!response.ok) throw new Error('Speech synthesis failed. Your reply is shown above.');
          return response.blob();
        })
        .then(audio => {
          if (audio.size <= 44) throw new Error('The reply audio was empty.');
          return audio;
        })
        .catch(error => {
          // The words are already on the screen and the microphone is still
          // good: a synthesis failure is a lost sentence, not a dead session.
          if (error.name !== 'AbortError') error.keepSession = true;
          throw error;
        }));
    }
    return clips.get(index);
  };
  // Fire ahead, but only a little: enough that the voice never waits on the
  // engine, few enough that a long answer does not stampede a GPU that is
  // already carrying the 27B model and the ASR worker.
  const ahead = index => { if (index >= 0 && index < parts.length) clip(index).catch(() => {}); };
  for (let index = 0; index < SPEECH_CHUNK.prefetch; index++) ahead(index);
  for (let index = 0; index < parts.length; index++) {
    const audio = await clip(index);
    ahead(index + SPEECH_CHUNK.prefetch);
    // The acknowledgment gets exactly as much air as the first sentence took to
    // synthesize, and is cut at the last moment before real speech begins.
    if (!index) onFirstClip?.();
    await playReply(audio, signal, index === parts.length - 1, index === 0);
  }
}
async function runTurn(input, forced = false, voicedMs = null) {
  if (!(input instanceof Blob)) { heldText = ''; fragmentHolds = 0; }
  clearCapture(); player.pause(); busy = true;
  const id = ++epoch, controller = new AbortController(); abort = controller;
  const check = () => {if(id !== epoch || controller.signal.aborted) throw new DOMException('Stopped','AbortError');};
  const before = history.slice(), was = transcript.slice(); let committed = false;
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
    armThinkingAloud(id, controller.signal);
    const pending = [...before,{role:'user',content:text}];
    const source = streaming ? sentenceSource() : null;
    let speaking = null, speechError = null;
    const spoken = () => {
      stopThinkingAloud();        // the reply is in hand: it never waits behind the acknowledgment
      setState('speaking',active?'Press Space to interrupt and speak. Listening resumes after the reply.':'Press Space to stop the reply.');
    };
    const progress = event => {
      if (event.type === 'delta' && source) {
        source.push(Number(event.round) || 0, String(event.text ?? ''));
        if (!speaking && source.queue.length) {
          cancelThinkingAloud();
          setState('synthesizing','Your reply is becoming speech\u2026');
          speaking = speakStreamed(source, controller.signal, spoken).catch(error => { speechError = error; return false; });
        }
        return;
      }
      check();
      if (event.type === 'status') {
        setState('thinking', `Looking that up — ${event.calls.join(', ')}…`);
        // A tool round is a second full generation, so this is exactly where the
        // silence would otherwise start: name the tool instead of waiting for it.
        mentionThinking(thinkLine(event.calls), id, controller.signal);
      }
      else if (event.type === 'tool') setState('thinking', event.ok
        ? (event.citations?.length ? `Found ${event.citations.length} passage${event.citations.length > 1 ? 's' : ''} in your notes…` : 'Read it. Thinking…')
        : 'That did not work. Answering from what it has…');
    };
    const reply = streaming
      ? await responseProgress('/chat/completions', JSON.stringify({messages: pending}), controller.signal, progress)
      : await responseJSON('/chat/completions', JSON.stringify({messages: pending}), controller.signal);
    check();
    // No further acknowledgment may start from here, but one already playing
    // keeps playing: the reply's own audio still needs a few hundred ms of
    // synthesis, and cutting the line now would trade one silence for another.
    cancelThinkingAloud();
    // The engine can answer with zero tokens -- measured live: prompt 3646 tok,
    // "gen 0 tok | stop", HTTP 200, empty body.  Committing that as an assistant
    // turn puts an empty turn into every later prompt, and asking the TTS to speak
    // it asks for silence.  That is how a working conversation stopped dead with no
    // error recorded on any server.  Refuse it, keep the history clean, and let the
    // person ask again.
    const answer = String(reply.text ?? '').trim();
    if (!answer) {
      const empty = new Error('That came back empty. Please ask it again.');
      empty.keepSession = true;
      throw empty;
    }
    // One commit point for both memories.  `history` is derived from the
    // transcript rather than maintained beside it, so the model can never be
    // shown a turn the browser would lose on refresh, or forget one it kept.
    const turn = turnFrom(text, answer, original, reply);
    if (turn) transcript = [...transcript, turn].slice(-STORE_TURNS);
    history = historyFrom(transcript);
    committed = true;
    message('assistant',answer,null,{tools:reply.tools,sources:reply.sources});
    // A request_directory call happened during that generation, so the card the
    // user needs to see is one poll overdue.  Fetch it now, not in four seconds.
    refreshApprovals();
    saveConversation();   // after the bubbles, so a trim never strands a rendered turn
    if (speaking) {
      source.finish(answer);
      const spokeAny = await speaking;
      check();
      if (speechError) throw speechError;
      if (!spokeAny) { setState('synthesizing','Your reply is becoming speech\u2026'); await speakReply(answer, controller.signal, spoken); }
    } else {
      if (source) source.close();
      setState('synthesizing','Your reply is becoming speech\u2026');
      await speakReply(answer, controller.signal, spoken);
    }
    check();busy=false;
    if(active)listen();else setState('idle','Send another message, or start a voice conversation.');
  } catch(error) {
    if(id !== epoch) return;
    if(!committed){ history=before; transcript=was; }   // a refused reply leaves no trace
    controller.abort();          // a streamed reply that failed must not keep talking
    stopThinkingAloud();
    // A refusal is not the end of a conversation.  Anything that merely declined
    // to answer keeps the microphone, so the next sentence needs no second click
    // on Start.
    if (error.keepSession && active) {
      busy = false; listen();
      setState('listening', error.message);
    } else {
      stopSession(error.name==='AbortError'?'Reply stopped.':error.message);
      if(error.name!=='AbortError')setState('error',error.message);
    }
  } finally {if(id===epoch)abort=null;}
}
$('start').onclick = async () => {
  if(secureURL){location.assign(secureURL);return;}
  if(active || busy) return;
  active=true;const id=++epoch;primeAudio();setState('listening','Allow microphone access to begin.');$('finish').hidden=true;
  try {
    const acquired=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:!bargeWanted()}});
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
  if (!replySeam) playbackStartedAt = performance.now();
  // Replaying an older reply through the native controls must also mute capture.
  if(active && stream && phase==='listening'){
    clearCapture();busy=true;setState('speaking','Playing your reply. Interrupt to speak again.');
    player.addEventListener('ended',()=>{if(active && phase==='speaking'){busy=false;listen();}},{once:true});
  }
});
player.addEventListener('pause',()=>{if (!replySeam) playbackEndedAt = performance.now();});
player.addEventListener('ended',()=>{if (!replySeam) playbackEndedAt = performance.now();});
$('end').onclick=()=>stopSession();
$('finish').onclick=()=>{if(recorder?.state==='recording'){recorder.sendNow=true;recorder.stop();}};
function interruptReply() {
  if(!busy)return;
  epoch++;abort?.abort();abort=null;busy=false;clearCapture();replySeam=false;player.pause();
  playbackEndedAt = performance.now();
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
$('new').onclick=()=>{stopSession('A fresh conversation. Start talking or type below.');setGlow(0);startFreshChat();renderChatList();player.removeAttribute('src');if(audioURL){URL.revokeObjectURL(audioURL);audioURL=null;}};
if($('chats-toggle'))$('chats-toggle').onclick=()=>setChatsOpen();
$('compose').onsubmit=event=>{event.preventDefault();const text=$('text').value.trim();if(!text||busy||!ready)return;primeAudio();armGlow();$('text').value='';runTurn(text);};
let languageList=[],autoLanguage='a',speechPreferences={voices:{}};
try{const saved=JSON.parse(localStorage.getItem('voice-speech')||'null');if(saved && typeof saved==='object' && !Array.isArray(saved))speechPreferences={...saved,voices:saved.voices&&typeof saved.voices==='object'?saved.voices:{}};}catch(_){}
function saveSpeech(){
  speechPreferences.language=$('language').value;
  speechPreferences.corrections=$('corrections').value;
  speechPreferences.enabled=$('corrections-enabled').checked;
  speechPreferences.barge=$('barge-in').checked;
  speechPreferences.think=$('think-aloud').checked;
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
if(typeof speechPreferences.barge==='boolean')$('barge-in').checked=speechPreferences.barge;
$('barge-in').onchange=saveSpeech;
if(typeof speechPreferences.think==='boolean')$('think-aloud').checked=speechPreferences.think;
$('think-aloud').onchange=saveSpeech;
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
// Before the first fetch, so a reload cannot let a turn be sent against a
// transcript the page has not rebuilt yet.
const restoredTurns = restoreChat();
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
    if(restoredTurns) setState('idle',`Picked up where you left off — ${restoredTurns} ${restoredTurns===1?'turn':'turns'} from this browser. Nothing was re-spoken.`);
  }catch(error){setState('error',error.message);}
})();
