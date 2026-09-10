"""The RAG index: what it finds, and what it admits it cannot find."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import retrieval

assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

CORPUS = {
    'deploy.md': """# Deployment

The voice stack runs on GPU 2 behind a systemd user unit called voice-stack-gpu2.
Restart it with systemctl --user restart voice-stack-gpu2 and never with pkill.

## Ports

The bridge is 8092, Kokoro TTS is 8090, the resident ASR server is 8095, Qwen is 8082.
""",
    'glossary.jsonl': '{"term":"qasr","means":"resident ASR server"}\n'
                      '{"term":"vvasr","means":"per-request native ASR spawn"}\n',
    'budget.csv': 'item,cost,owner\nbridge,0,ambud\ntts,12,ambud\n',
    'ignored.pdf': 'this must never be indexed',
}


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.corpus = cls.root / 'corpus'
        cls.corpus.mkdir()
        for name, text in CORPUS.items():
            (cls.corpus / name).write_text(text)
        cls.index = cls.root / 'notes.sqlite'
        cls.built = retrieval.build([cls.corpus], cls.index)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_only_supported_files_are_ingested(self):
        self.assertEqual(self.built['documents'], 3)
        self.assertNotIn('ignored.pdf', str(self.built['manifest']))

    def test_bm25_finds_the_right_paragraph(self):
        hits = retrieval.search(self.index, 'which port does the bridge listen on', 3)
        self.assertTrue(hits)
        self.assertIn('deploy.md', hits[0].path)
        self.assertIn('8092', hits[0].text)
        self.assertEqual(hits[0].heading, 'Ports', 'the heading must travel with the chunk')

    def test_structured_files_are_searchable_as_text(self):
        hits = retrieval.search(self.index, 'vvasr', 2)
        self.assertTrue(hits and 'per-request native ASR' in hits[0].text)
        hits = retrieval.search(self.index, 'tts cost', 2)
        self.assertTrue(hits and 'budget.csv' in hits[0].path, 'csv rows must be retrievable')

    def test_query_grammar_cannot_throw(self):
        # Every one of these is FTS5 *syntax*; quoting is what makes them words.
        for query in ('NEAR(port bridge)', 'AND OR NOT', 'a:b', '"unbalanced', 'port*', '-', '???'):
            try:
                retrieval.search(self.index, query, 2)
            except Exception as error:
                self.fail(f'{query!r} raised {error!r}')

    def test_a_miss_is_a_miss(self):
        self.assertEqual(retrieval.search(self.index, 'xylophone quantum', 3), [])

    def test_status_reports_staleness_not_silence(self):
        fresh = retrieval.status([self.corpus], self.index)
        self.assertTrue(fresh['ready'], fresh)
        self.assertFalse(fresh['stale'])
        (self.corpus / 'new.md').write_text('added after the index was built')
        stale = retrieval.status([self.corpus], self.index)
        self.assertFalse(stale['ready'])
        self.assertTrue(stale['stale'])
        self.assertIn('new.md', stale['added'][0])
        (self.corpus / 'new.md').unlink()
        self.assertTrue(retrieval.status([self.corpus], self.index)['ready'], 'removal must also settle')

    def test_a_missing_index_is_refused_not_ignored(self):
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.search(self.root / 'nope.sqlite', 'anything')
        self.assertFalse(retrieval.status([self.corpus], self.root / 'nope.sqlite')['exists'])

    def test_a_missing_corpus_path_is_an_error(self):
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.build([self.root / 'nowhere'], self.index)

    def test_chunks_overlap_and_stay_bounded(self):
        long = 'word ' * 3000
        chunks = retrieval.chunk_text(long)
        self.assertGreater(len(chunks), 3)
        for _, body in chunks:
            self.assertLessEqual(len(body), retrieval.CHUNK_CHARS + 200)
        self.assertTrue(any(chunks[i][1][-40:] in chunks[i + 1][1] for i in range(len(chunks) - 1)),
                        'consecutive chunks must overlap or the boundary sentence is lost')

    def test_rebuild_is_idempotent(self):
        again = retrieval.build([self.corpus], self.index)
        self.assertEqual(again['documents'], self.built['documents'])
        self.assertEqual(len(retrieval.search(self.index, 'bridge', 8)),
                         len(retrieval.search(self.index, 'bridge', 8)))

    def test_results_are_bounded_by_character_budget(self):
        hits = retrieval.search(self.index, 'the', 8, max_chars=200)
        self.assertLessEqual(sum(len(hit.text) for hit in hits), 260)


if __name__ == '__main__':
    unittest.main(verbosity=2)
