#!/usr/bin/env python3
"""Build the two-utterance microphone fixture that the carry-forward test needs.

    python3 tests/fixtures/make_two_part_capture.py

WHY THIS EXISTS AS A GENERATOR AND NOT JUST A WAV
    The bug this fixture reproduces is about *timing structure* -- one short
    utterance, a pause longer than the endpointer's window, then the rest of the
    sentence.  A blob of samples cannot be reviewed for that; this file can.
    It also keeps the fixture honest: every sample here is copied out of
    capture.wav, which is itself a real recording (see PROVENANCE.md).

WHY REAL SPEECH AND NOT A SYNTHESISED TONE
    The page opens the microphone with noiseSuppression and autoGainControl on
    (web/chat.js).  Chromium's suppressor is tuned against speech and will eat
    or distort a pure sine, so a tone fixture would be testing the noise gate
    rather than the endpointer.  Reusing the already-certified recording keeps
    the audio characteristics identical to the suite next door.

The two bursts are the two halves of a sentence someone said with a breath in
the middle: "I" ... "want to go to the museum".
"""
import array
import os
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "capture.wav")
TARGET = os.path.join(HERE, "capture-two-part.wav")

# Second boundaries inside capture.wav, chosen from its measured 50 ms RMS
# envelope: 0.42-0.95 s and 1.50-2.62 s are both sustained speech at 4-11 %
# RMS, several times the page's 1.5 % voiced threshold.
BURST_A = (0.42, 0.95)     # ~0.53 s voiced  -> "I"
BURST_B = (1.50, 2.62)     # ~1.12 s voiced  -> "want to go to the museum"

LEAD_IN = 1.20             # room for getUserMedia/setUp latency before burst A
GAP = 1.80                 # > the 1000 ms endpointer window, so the clip closes
TAIL = 8.00                # keeps a looping fake device out of the assertions
FADE_MS = 5                # boundary clicks would register as voiced samples


def fade(samples, sr):
    edge = max(1, int(sr * FADE_MS / 1000))
    for i in range(min(edge, len(samples))):
        ramp = i / edge
        samples[i] = int(samples[i] * ramp)
        samples[len(samples) - 1 - i] = int(samples[len(samples) - 1 - i] * ramp)
    return samples


def main() -> int:
    if not os.path.isfile(SOURCE):
        print(f"missing source recording: {SOURCE}", file=sys.stderr)
        return 2
    with wave.open(SOURCE, "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            print("capture.wav is no longer mono 16-bit", file=sys.stderr)
            return 2
        rate = handle.getframerate()
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))

    def burst(window):
        start, stop = int(window[0] * rate), int(window[1] * rate)
        if stop > len(samples):
            print(f"{window} runs past the end of capture.wav", file=sys.stderr)
            raise SystemExit(2)
        return fade(array.array("h", samples[start:stop]), rate)

    silence = lambda seconds: array.array("h", bytes(int(seconds * rate) * 2))
    out = array.array("h")
    for piece in (silence(LEAD_IN), burst(BURST_A), silence(GAP), burst(BURST_B), silence(TAIL)):
        out.extend(piece)

    with wave.open(TARGET, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(out.tobytes())
    print(f"{TARGET}: {len(out)/rate:.2f} s at {rate} Hz -- "
          f"speech {LEAD_IN:.2f}-{LEAD_IN+BURST_A[1]-BURST_A[0]:.2f} s, "
          f"pause {GAP:.2f} s, speech "
          f"{LEAD_IN+(BURST_A[1]-BURST_A[0])+GAP:.2f}-"
          f"{LEAD_IN+(BURST_A[1]-BURST_A[0])+GAP+(BURST_B[1]-BURST_B[0]):.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
