import fs from 'node:fs';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
assert.equal(process.env.CUDA_VISIBLE_DEVICES, '');
// The engine synthesizes an entire request before it returns a single byte of
// audio, so the size of the FIRST request is exactly how long the listener sits
// in silence after the words are already on the screen.  Measured live against
// the deployed Kokoro: ~2 ms per character with no fixed cost, so a whole
// 774-character answer is 1.16 s of nothing.  Splitting at sentence boundaries
// is what turns that into ~0.1 s.
//
// The splitter is extracted from web/chat.js between its markers rather than
// re-typed: a test with its own copy of the rules keeps passing while the rules
// in the page are broken.
//   CUDA_VISIBLE_DEVICES="" node tests/speech_chunk_test.mjs [--sabotage|--dead-sabotage]
const sabotage = process.argv.includes('--sabotage');
const deadSabotage = process.argv.includes('--dead-sabotage');
const assertionsComplete = 'ASSERTIONS COMPLETE: speech chunker';
if (deadSabotage && !sabotage) {
  const run = spawnSync(process.execPath, [process.argv[1], '--sabotage', '--dead-sabotage'],
    {encoding: 'utf8', timeout: 30000});
  process.stdout.write(run.stdout ?? '');
  process.stderr.write(run.stderr ?? '');
  assert.ifError(run.error);
  assert.equal(run.signal, null, 'dead sabotage must finish normally');
  assert.equal(run.status, 0, 'an uncaught sabotage must exit zero so make rejects it');
  assert.ok(run.stdout.split('\n').includes(assertionsComplete), 'the chunker assertions must finish');
  console.log('DEAD SABOTAGE PASS: no-op mutation escaped after the chunker assertions ran');
  process.exit(0);
}
const source = fs.readFileSync('web/chat.js', 'utf8');
const begin = source.indexOf('// SPEECH-CHUNK-BEGIN');
const end = source.indexOf('// SPEECH-CHUNK-END');
assert.ok(begin > 0 && end > begin, 'the chunker markers moved or vanished');
let block = source.slice(source.indexOf('\n', begin) + 1, end);
// The paired sabotage: ask for the whole answer in one request.  This is exactly
// the regression the feature exists to prevent -- the reply still sounds
// correct, it just costs over a second of silence before the first word -- so
// every latency assertion below must go red.
if (sabotage) {
  const anchor = 'if (!clean) return [];';
  assert.ok(block.includes(anchor), 'the sabotage anchor no longer matches the chunker in web/chat.js');
  const armed = block.replace(anchor, deadSabotage ? anchor : 'if (!clean) return []; if (clean.length > 12) return [clean];');
  if (deadSabotage) assert.equal(armed, block, 'dead sabotage must leave the chunker unchanged');
  block = armed;
}
const chunker = new Function(`${block}\nreturn {SPEECH_CHUNK, speechChunks};`)();
const {SPEECH_CHUNK, speechChunks} = chunker;

let fail = 0;
const check = (condition, label) => {
  console.log((condition ? '  ok   ' : '  FAIL ') + label);
  if (!condition) fail = 1;
};
const norm = value => String(value).replace(/\s+/g, ' ').trim();
// Nothing may be invented, dropped, or reordered.  Whitespace is excluded
// because a seam deliberately removes one space and the rejoin adds it back.
const lossless = text => {
  const parts = speechChunks(text);
  if (norm(text) === '') return parts.length === 0;
  return parts.join(' ').replace(/\s+/g, '') === norm(text).replace(/\s+/g, '');
};
const sizes = text => speechChunks(text).map(part => part.length);

// -- the claim the feature exists for ----------------------------------------
const longAnswer = 'Yes. ' + 'This is a long explanation that goes on for a while. '.repeat(20);
check(speechChunks(longAnswer)[0].length < 12,
  `the first bite is tiny, so the first word is audible fast (got ${speechChunks(longAnswer)[0].length} chars)`);
check(speechChunks(longAnswer).length > 10, 'a long answer really is split');
check(speechChunks('Half Moon Bay is a small city. It is known for its long beaches. The old Carlton hotel closed.').length === 3,
  'one request per sentence');
check(speechChunks('Half Moon Bay is a small city. It is known for its beaches. The Carlton closed.').length === 2,
  'a trailing fragment joins the sentence before it rather than clicking alone');

// -- a single sentence is left alone -----------------------------------------
check(speechChunks('Half Moon Bay is nice.').length === 1, 'one sentence, one request');
check(speechChunks('Yes.')[0] === 'Yes.' && speechChunks('Yes.').length === 1, 'a one-word reply survives intact');
check(speechChunks('Sure! Here is the answer. It has three parts.').length === 1,
  'short sentences merge rather than click');

// -- nothing is ever lost -----------------------------------------------------
const samples = ['Yes.', '   ', '', 'Half Moon Bay is nice.',
  'It is a classic pangram. The quick brown fox jumps. Over the lazy dog.',
  'Dr. No went to the museum. He came back.',
  'Ta Ra Rum Pum is a 2007 film. It stars Saif Ali Khan. Saif Ali Khan is the son of Mansoor Ali Khan Pataudi.',
  '"Half Moon Bay is nice," she said. "The Carlton is closed."',
  'Wait \u2014 what? Really\u2026 yes. Fine!',
  'x'.repeat(900), 'x '.repeat(450),
  'One, two, three, four. ' + 'and one more clause, with a comma, right here. '.repeat(12),
  longAnswer];
for (const text of samples) check(lossless(text), `lossless: ${JSON.stringify(text.slice(0, 40))}`);

// -- bounds and junk ----------------------------------------------------------
for (const text of samples) {
  const over = speechChunks(text).filter(part => part.length > SPEECH_CHUNK.maxChars).length;
  check(over === 0, `no chunk over the ceiling: ${JSON.stringify(text.slice(0, 28))} -> ${JSON.stringify(sizes(text))}`);
}
for (const junk of [null, undefined, 0, 42, {}, [], NaN, false]) {
  let threw = false, value = null;
  try { value = speechChunks(junk); } catch (_) { threw = true; }
  check(!threw && Array.isArray(value) && value.length === 0,
    `junk ${String(junk)} yields no chunks and never throws`);
}
check(SPEECH_CHUNK.prefetch >= 1, `at least one clip is prefetched (${SPEECH_CHUNK.prefetch})`);
check(SPEECH_CHUNK.minChars > 3, `a stray ellipsis is not worth a request (${SPEECH_CHUNK.minChars})`);
// A seam through the middle of a word is heard as a word cut in half, which is
// worse than the pause the feature set out to remove.
const runOn = 'alpha beta gamma delta '.repeat(60);
check(speechChunks(runOn).every(part => part.length <= SPEECH_CHUNK.maxChars
      && /^[a-z]/.test(part.trim()) && /[a-z]$/.test(part.trim())),
  'a run-on clause breaks between words, never through one');

console.log(assertionsComplete);
if (sabotage) {
  if (fail) console.log('sabotage correctly refused: one request for the whole answer puts the listener back to sleep');
  else console.error('SABOTAGE PASSED (this is the failure): the chunker is not load-bearing');
} else {
  console.log(fail ? 'SPEECH CHUNKER FAIL' : 'SPEECH CHUNKER PASS');
}
// make sabotage treats zero as an escaped mutation and non-zero as refusal.
process.exitCode = fail;
