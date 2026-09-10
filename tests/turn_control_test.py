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


if __name__ == '__main__':
    unittest.main(verbosity=2)
