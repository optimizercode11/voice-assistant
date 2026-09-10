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

# How much more time to grant, by how strongly the transcript says "not yet".
# These are added to the browser's own window, not substituted for it.
EXTRA_HARD_MS = 1200     # ends on a conjunction/preposition: a clause is open
EXTRA_SOFT_MS = 600      # no terminal punctuation and too short to judge
EXTRA_FILLER_MS = 900    # nothing but "um"
MAX_EXTRA_MS = 2000


def completeness(text: str) -> dict:
    """Judge a transcript.  Returns {complete, reason, extra_silence_ms, words}."""
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
    if last in DANGLING:
        return {"complete": False, "reason": f"ends on the open word {last!r}",
                "extra_silence_ms": EXTRA_HARD_MS, "words": len(words)}

    # A trailing comma is the transcript saying the same thing a conjunction does.
    if stripped.endswith(",") or stripped.endswith(":"):
        return {"complete": False, "reason": "ends on a comma", "extra_silence_ms": EXTRA_HARD_MS,
                "words": len(words)}

    if ends_terminal:
        return {"complete": True, "reason": "ends on terminal punctuation",
                "extra_silence_ms": 0, "words": len(words)}

    # ASR punctuation is not guaranteed, so most real endings land here.  A
    # single content word that is not dangling is a complete answer; anything
    # shorter than a phrase is not worth interrupting for.
    if len(content) == 1:
        if last in SELF_CONTAINED:
            return {"complete": True, "reason": "a self-contained one-word answer",
                    "extra_silence_ms": 0, "words": len(words)}
        return {"complete": False, "reason": "a single word is too little to judge",
                "extra_silence_ms": EXTRA_SOFT_MS, "words": len(words)}

    if len(content) <= 2 and not ends_terminal:
        return {"complete": False, "reason": "a two-word fragment with no ending",
                "extra_silence_ms": EXTRA_SOFT_MS, "words": len(words)}

    return {"complete": True, "reason": "a phrase with no open word at the end",
            "extra_silence_ms": min(EXTRA_SOFT_MS // 2, MAX_EXTRA_MS), "words": len(words)}


def main(argv) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Judge whether a transcript is a finished turn.")
    parser.add_argument("text", nargs="?", help="omit to read lines from stdin")
    args = parser.parse_args(argv[1:])
    if args.text is not None:
        print(json.dumps(completeness(args.text)))
        return 0
    for line in sys.stdin:
        if line.strip():
            print(json.dumps({**completeness(line), "text": line.strip()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
