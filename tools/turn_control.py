#!/usr/bin/env python3
"""Is this utterance actually finished?  The semantic half of turn detection.

WHY PAUSE DETECTION NEEDS A SECOND OPINION
    The browser's endpointer (web/endpointer.js) decides "the microphone has
    gone quiet".  That is an acoustic fact and it is the wrong question: people
    stop for breath, for commas, and while they search for a word, all of which
    look identical to being done.  A fixed silence window therefore has to be
    set as a trade between two bad outcomes -- too short and the assistant
    interrupts mid-sentence, too long and every reply arrives a beat late.

    The words answer it.  "How do I restart the" and "How do I restart the
    stack" are acoustically indistinguishable at their pause and semantically
    nothing alike.  So the browser asks, during its grace window, and this
    module answers from the transcript it already has.

    It is deliberately a small, boring, deterministic rule set rather than a
    model call: it runs on the path where the user is still speaking, so it has
    to cost about nothing, and a wrong answer only costs a little more silence,
    never a wrong reply.

IT NEVER DISPATCHES
    This module only ever *adds* time or agrees with a decision the browser was
    already prepared to make.  It cannot cut a pause short, so a bug here makes
    the assistant more patient, not more interrupting.
"""
from __future__ import annotations

import json
import re
import sys

# Words that cannot end an English sentence.  A trailing one is the strongest
# cheap signal that the speaker is between clauses, not at the end of a turn.
DANGLING = frozenset("""
and but or nor so yet because although though while whereas if when whenever
since unless until that which who whom whose as than
of to for with in on at by from into onto upon about above below under over
within without toward towards against through during per minus plus
a an the this these those those my your his her its our their
is are was were be been being am do does did doing have has had having
will would shall should can could may might must ought
""".split())

# Non-words.  A turn that is only these is either a breath or a throat-clear;
# it is not worth a generation, and dispatching on one is how the assistant
# starts answering a cough.
FILLERS = frozenset("""
um uh umm uhmm hmm hm mm ah eh er oh huh ha yeah
""".split())

# A one-word answer really can be a whole turn ("Yes." "Blue.").  These are the
# shapes that are complete on their own, so the short-utterance rule does not
# stall forever on a person answering a question.
SELF_CONTAINED = frozenset("""
yes no maybe perhaps thanks thank ok okay stop quiet never always sometimes
here there hello hi goodbye morning evening who what why how when where
""".split())

WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
TERMINAL = (".", "!", "?", "…")

# Under this much *actually audible* speech, a pause is much more likely a comma,
# a breath or someone reaching for the next word than it is the end of a turn.
# It is deliberately compared against the voiced duration and never against the
# length of the recording -- see _should_hold.
HOLD_SPEECH_MS = 2500

# How much more time to grant, by how strongly the transcript says "not yet".
# These are added to the browser's own window, not substituted for it.
EXTRA_HARD_MS = 1200     # ends on a conjunction/preposition: a clause is open
EXTRA_SOFT_MS = 600      # no terminal punctuation and too short to judge
EXTRA_FILLER_MS = 900    # nothing but "um"
MAX_EXTRA_MS = 2000


def _judge(text: str) -> dict:
    """The words-only verdict.  Use completeness(); this is the half that needs no audio."""
    stripped = (text or "").strip()
    if not stripped:
        return {"complete": False, "reason": "empty", "extra_silence_ms": EXTRA_FILLER_MS, "words": 0}

    words = WORD.findall(stripped)
    lowered = [word.lower() for word in words]
    content = [word for word in lowered if word not in FILLERS]
    ends_terminal = stripped.endswith(TERMINAL)

    # Nothing but "um".  Give them room; do not answer a breath.
    if not content:
        return {"complete": False, "reason": "filler-only", "extra_silence_ms": EXTRA_FILLER_MS,
                "words": len(words)}

    last = content[-1]
    # A question mark outranks the word list (2026-09-11): "What are you
    # doing?", "What are you up to?" and "Where are you from?" all end on a word
    # that opens a clause in a statement, but a transcript that ends on "?" is a
    # question the person finished asking.  Every one of those was held three
    # times until the user pressed "Send now".  A full stop does NOT rescue the
    # word list: the transcript puts one after fragments too (see below).
    if last in DANGLING and not stripped.endswith("?"):
        return {"complete": False, "reason": f"ends on the open word {last!r}", "open": True,
                "extra_silence_ms": EXTRA_HARD_MS, "words": len(words)}

    # A trailing comma is the transcript saying the same thing a conjunction does.
    if stripped.endswith(",") or stripped.endswith(":"):
        return {"complete": False, "reason": "ends on a comma", "open": True,
                "extra_silence_ms": EXTRA_HARD_MS, "words": len(words)}

    # A single content word is judged on what it *is*, never on whether the ASR
    # happened to put a stop after it.  qasr punctuates fragments: measured on
    # the deployed stack, a 0.63 s clip of one syllable comes back as "I." and a
    # lone "Carlton" comes back as "Carlton."  Trusting that period is exactly
    # what let a one-word clip reach the model and be answered as a question.
    if len(content) == 1:
        if last in SELF_CONTAINED:
            return {"complete": True, "reason": "a self-contained one-word answer",
                    "extra_silence_ms": 0, "words": len(words)}
        return {"complete": False, "reason": "a single word is too little to judge",
                "extra_silence_ms": EXTRA_SOFT_MS, "words": len(words)}

    # Two or more content words: now a period is evidence of an ending.
    if ends_terminal:
        return {"complete": True, "reason": "ends on terminal punctuation",
                "extra_silence_ms": 0, "words": len(words)}

    if len(content) <= 2 and not ends_terminal:
        return {"complete": False, "reason": "a two-word fragment with no ending",
                "extra_silence_ms": EXTRA_SOFT_MS, "words": len(words)}

    return {"complete": True, "reason": "a phrase with no open word at the end",
            "extra_silence_ms": min(EXTRA_SOFT_MS // 2, MAX_EXTRA_MS), "words": len(words)}


def _should_hold(verdict: dict, speech_ms) -> bool:
    """Wait for more speech instead of answering what is almost certainly half a sentence.

    WHY THIS MEASURES SPEECH AND NOT THE RECORDING
        The tempting version compares `audio_seconds` to a threshold, and on the
        deployed stack that is precisely the bug that made the assistant answer
        "I" as though it were a question.  The browser is only allowed to stop
        after a full second of silence, so *every* clip it uploads carries
        >=1000 ms of trailing silence plus whatever lead-in there was.  A clip of
        one syllable therefore reports an `audio_seconds` of roughly 1.5-1.8 s --
        over the threshold -- and the hold never fired for the one case it was
        written for.  The clip length is not a measurement of how much a person
        said; it is a measurement of how long the endpointer waits.

    So this takes the browser's voiced-ms figure.  When that is missing it falls
    back to counting words, which is weaker but never worse than what was here
    before.  A wrong answer here only ever costs a little more patience.
    """
    if verdict.get("complete"):
        return False
    if verdict.get("open"):
        # Nobody ends a turn on "the" or "because".  That is evidence about the
        # sentence, not about the pause, so it outranks the duration rule: a
        # speaker who has been audible for four seconds and stopped on "the" is
        # still not finished.
        return True
    if isinstance(speech_ms, (int, float)) and speech_ms > 0:
        return speech_ms < HOLD_SPEECH_MS
    return int(verdict.get("words") or 0) <= 2


def completeness(text: str, speech_ms=None) -> dict:
    """Judge a transcript.  Returns {complete, reason, extra_silence_ms, words, hold, speech_ms}.

    `speech_ms` is optional so the words-only callers (the CLI, the tests, any
    future transcript review) keep working unchanged.
    """
    verdict = _judge(text)
    verdict["speech_ms"] = int(speech_ms) if isinstance(speech_ms, (int, float)) else None
    verdict["open"] = bool(verdict.get("open"))
    verdict["hold"] = _should_hold(verdict, speech_ms)
    return verdict


def main(argv) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Judge whether a transcript is a finished turn.")
    parser.add_argument("--speech-ms", type=int, default=None,
                        help="how long the speaker was actually audible, from the browser")
    parser.add_argument("text", nargs="?", help="omit to read lines from stdin")
    args = parser.parse_args(argv[1:])
    if args.text is not None:
        print(json.dumps(completeness(args.text, args.speech_ms)))
        return 0
    for line in sys.stdin:
        if line.strip():
            print(json.dumps({**completeness(line, args.speech_ms), "text": line.strip()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
