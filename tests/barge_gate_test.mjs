import fs from 'node:fs';
import assert from 'node:assert/strict';
// The barge-in gate is the one piece of this feature that decides whether the
// assistant hears YOU or hears ITSELF, and a headless browser has no acoustic
// echo path at all -- Chromium's fake microphone is not fed by the browser's own
// playback.  So the echo question cannot be answered in a browser suite, and it
// is answered here instead, numerically, against the real source.
//
// The gate is extracted from web/chat.js between its markers rather than
// re-typed: a test with its own copy of the rules keeps passing while the rules
// in the page are broken.
//   CUDA_VISIBLE_DEVICES="" node tests/barge_gate_test.mjs [--sabotage]
const sabotage = process.argv.includes('--sabotage');
const source = fs.readFileSync('web/chat.js', 'utf8');
const begin = source.indexOf('// BARGE-GATE-BEGIN');
const end = source.indexOf('// BARGE-GATE-END');
assert.ok(begin > 0 && end > begin, 'the gate markers moved or vanished');
let block = source.slice(source.indexOf('\n', begin) + 1, end);
// The paired sabotage: trust the microphone and ignore what is being played.
// This is the "it interrupts itself" regression, and this file must go red.
if (sabotage) block = block.replace('return mic > Math.max(BARGE.floor, playback * BARGE.echoGain);',
                                    'return mic > BARGE.floor;');
const gate = new Function(`${block}\nreturn {BARGE, nearEndSpeech};`)();
const {BARGE, nearEndSpeech} = gate;

let fail = 0;
const check = (condition, label) => {
  console.log((condition ? '  ok   ' : '  FAIL ') + label);
  if (!condition) fail = 1;
};
const AFTER = BARGE.settleMs + 1;

// -- the property that used to be guaranteed by muting the microphone ---------
check(!nearEndSpeech(0.000, 0.300, AFTER), 'silence while the reply is loud');
check(!nearEndSpeech(0.050, 0.300, AFTER), 'echo at a sixth of the reply');
check(!nearEndSpeech(0.150, 0.300, AFTER), 'echo at half the reply: the AEC worst case');
check(!nearEndSpeech(0.150, 0.300, AFTER), 'echo exactly at the floor (strict >)');
check(!nearEndSpeech(0.030, 0.060, AFTER), 'a quiet reply echoing at a quiet mic');

// -- a person talking over it -------------------------------------------------
check(nearEndSpeech(0.200, 0.300, AFTER), 'speech above a loud reply');
check(nearEndSpeech(0.040, 0.010, AFTER), 'speech over a near-silent reply');
check(nearEndSpeech(0.050, 0.000, AFTER), 'speech in a quiet room, no playback');
check(nearEndSpeech(0.900, 0.000, AFTER), 'shouting in a quiet room');

// -- AEC3 re-convergence is when false interrupts actually happen -------------
check(!nearEndSpeech(0.900, 0.300, 0), 'no decision while the canceller is still converging');
check(!nearEndSpeech(0.900, 0.300, BARGE.settleMs - 1), 'still settling one ms before the window closes');
check(nearEndSpeech(0.900, 0.000, BARGE.settleMs), 'the window opens exactly when it closes');

// -- the absolute floor stands on its own ------------------------------------
check(!nearEndSpeech(0.019, 0.000, AFTER), 'below the absolute floor, playback or no playback');
check(!nearEndSpeech(0.001, 0.000, AFTER), 'room tone is not a person');
// The direction that actually matters: an unmeasurable echo level must CLOSE the
// gate, not open it.  Coercing these to zero was a real bug, found by this test.
check(!nearEndSpeech(0.900, NaN, AFTER), 'an unmeasurable reply level closes the gate');
check(!nearEndSpeech(0.900, undefined, AFTER), 'a missing reply level closes the gate');
check(!nearEndSpeech(0.900, -1, AFTER), 'a negative reply level closes the gate');
check(nearEndSpeech(0.050, 0, AFTER), 'a silent gap in the reply is not echo');

// -- junk must never interrupt ------------------------------------------------
for (const junk of [[NaN, 0.2, AFTER], [0.2, NaN, AFTER], [undefined, 0.2, AFTER],
                    [0.2, undefined, AFTER], [-1, 0.2, AFTER], [0.2, -5, AFTER],
                    [null, null, null], ['', '', ''], [0, 0, 0]]) {
  let threw = false, value = true;
  try { value = nearEndSpeech(...junk); } catch (_) { threw = true; }
  check(!threw && value === false, `junk ${JSON.stringify(junk)} never trips and never throws`);
}

// -- the hold is what actually bounds impatience ------------------------------
check(BARGE.holdMs >= 150, `a hold long enough to ride out a plosive (${BARGE.holdMs} ms)`);
check(BARGE.settleMs >= 250, `a settle window AEC3 can actually need (${BARGE.settleMs} ms)`);
check(BARGE.echoGain > 0 && BARGE.echoGain <= 1, `echo gain is a fraction (${BARGE.echoGain})`);

if (sabotage) {
  if (fail) console.log('sabotage correctly refused: ignoring playback lets the reply interrupt itself');
  else { console.error('SABOTAGE PASSED (this is the failure): the echo floor is not load-bearing'); process.exitCode = 1; }
} else {
  console.log(fail ? 'BARGE GATE FAIL' : 'BARGE GATE PASS');
  process.exit(fail);
}
