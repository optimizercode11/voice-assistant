"""The dispatch gate.  Every fixture here is a transcript the deployed stack
actually produced, not an invented example -- that is what makes the assertions
worth having."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import turn_control

# Paired sabotage: widen the self-contained allow-list until it swallows the
# fragments it is supposed to hold.  This is the realistic way this rule rots --
# somebody adds a word or two so the assistant stops stalling -- and it must
# break the assertions below rather than pass them.
if '--sabotage' in sys.argv:
    sys.argv = [a for a in sys.argv if a != '--sabotage']
    turn_control.SELF_CONTAINED = turn_control.SELF_CONTAINED | {'i', 'carlton'}


def judge(text):
    return turn_control.completeness(text)


class MeasuredTranscripts(unittest.TestCase):
    def test_punctuated_fragment_is_not_a_finished_turn(self):
        # qasr appends terminal punctuation to fragments.  A 0.63 s clip of one
        # syllable arrived as "I." and the assistant answered it as a question.
        for fragment in ('I.', 'I', 'Carlton.', 'Netflix.'):
            verdict = judge(fragment)
            self.assertFalse(verdict['complete'], f'{fragment!r} was judged finished')

    def test_a_real_one_word_answer_still_dispatches(self):
        # Holding fragments must not turn into holding everything.
        for answer in ('yes', 'Hello.', 'no', 'Thanks'):
            self.assertTrue(judge(answer)['complete'], f'{answer!r} was held')

    def test_open_clause_waits(self):
        verdict = judge('How do I restart the')
        self.assertFalse(verdict['complete'])
        self.assertGreater(verdict['extra_silence_ms'], 0)

    def test_a_real_sentence_dispatches(self):
        self.assertTrue(judge('About Half Moon Bay,s Carlton.')['complete'])
        self.assertTrue(judge('B the Ritz Carlton in Half Moon Bay..')['complete'])
        self.assertTrue(judge('How do I restart the stack?')['complete'])

    def test_breath_is_not_speech(self):
        for noise in ('um', 'um uh', '   ', ''):
            self.assertFalse(judge(noise)['complete'], f'{noise!r} would dispatch')


class ItCanOnlyAddPatience(unittest.TestCase):
    """The module's central safety claim: it never cuts a pause short, so a bug
    here makes the assistant more patient and never more interrupting."""

    CASES = ['', 'I.', 'yes', 'How do I restart the', 'um', 'a', 'a b c d e',
             'Wait.', 'Half Moon Bay, Carlton:', 'x' * 400, '...']

    def test_it_never_subtracts_time(self):
        for text in self.CASES:
            verdict = judge(text)
            self.assertGreaterEqual(verdict['extra_silence_ms'], 0, f'{text!r} removed time')
            self.assertLessEqual(verdict['extra_silence_ms'], turn_control.MAX_EXTRA_MS,
                                 f'{text!r} stalls past the ceiling')
            self.assertIsInstance(verdict['complete'], bool)
            self.assertTrue(verdict['reason'])

    def test_it_never_raises_on_junk(self):
        for text in ('\u0000', '?!?...', '   \t\n  ', '’‘—', '55 555 5555'):
            judge(text)


class HoldTests(unittest.TestCase):
    """`hold` is the field the page actually obeys.

    THE BUG THIS ENCODES.  The page used to gate its own hold on
    `audio_seconds` -- the length of the *recording* -- but the browser is not
    allowed to stop until it has logged a full second of silence, so every clip
    it uploads carries that silence plus whatever lead-in there was.  A one-word
    clip therefore measured ~1.5-1.8 s, the gate never opened, and "I" went to
    the model as a finished turn.  `hold` is decided from the voiced duration
    the browser measured instead, which is the thing the decision was always
    about.
    """

    def test_a_short_utterance_holds_at_any_clip_length(self):
        for speech_ms in (300, 900, 1500, 1600, 2400):
            verdict = turn_control.completeness('I.', speech_ms)
            self.assertTrue(verdict['hold'], f'"I." dispatched after only {speech_ms} ms of speech')

    def test_a_finished_turn_never_holds_however_short(self):
        for text in ('I want to go to the museum.', 'Yes.', 'Hello.'):
            self.assertFalse(turn_control.completeness(text, 300)['hold'], f'{text!r} was held')

    def test_an_open_clause_holds_whatever_the_duration_says(self):
        # Ending on "the" is evidence about the sentence, not about the pause.
        for speech_ms in (600, 2500, 9000):
            verdict = turn_control.completeness('How do I restart the', speech_ms)
            self.assertTrue(verdict['open'])
            self.assertTrue(verdict['hold'], f'open clause dispatched after {speech_ms} ms')

    def test_the_measurement_is_echoed_back_for_the_transcript_log(self):
        self.assertEqual(turn_control.completeness('I.', 430)['speech_ms'], 430)
        self.assertIsNone(turn_control.completeness('I.', None)['speech_ms'])

    def test_without_a_measurement_it_falls_back_to_the_words(self):
        self.assertTrue(turn_control.completeness('I.', None)['hold'])
        self.assertTrue(turn_control.completeness('Want to', None)['hold'])
        # 'I want to go to the' ends on 'the', which is semantic evidence and
        # holds with or without a measurement.  The fallback only governs the
        # weak verdicts, and it must not stall on a long one.
        self.assertFalse(turn_control.completeness('um uh hmm', None)['hold'],
                         'a long run of fillers with no measurement must not stall forever')

    def test_patience_is_bounded_by_a_duration_not_a_word_count(self):
        # Nine seconds of audible speech that transcribed to one word is an ASR
        # failure rather than a half sentence, and holding it would ask the user
        # to repeat themselves.  Dispatching is the lesser evil once the page has
        # already carried the fragment forward.
        self.assertFalse(turn_control.completeness('I.', 9000)['hold'])

    def test_the_verdict_shape_is_stable_for_the_page(self):
        verdict = turn_control.completeness('I.', 300)
        for key in ('complete', 'reason', 'extra_silence_ms', 'words', 'hold', 'open', 'speech_ms'):
            self.assertIn(key, verdict)


if __name__ == '__main__':
    unittest.main(verbosity=2)
