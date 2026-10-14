"""Check malformed writer metadata without an optional export dependency.

Main caller: the SDK suite. Announced writers cannot become complete through
duplicate IDs, a gap, conflicting sequence reuse or an unbounded sequence value.
The shared browser fixture independently checks these same public indicators.
"""
import unittest
from backprop.apis.public.history_exports import incomplete_writers


class HistoryCompletenessTests(unittest.TestCase):
    def test_invalid_sequence_conflict_duplicate_and_large_gap(self):
        writers = {'one': {'state': 'finished', 'label': None}}
        def event(sequence, identity='event', kind='writer_finish'):
            return {'payload': {'_writer': {'id': 'one', 'sequence': sequence}}, 'event_id': identity, 'type': kind}
        for sequence in [None, -1, 1.5, '0', True, 2**53]:
            with self.subTest(sequence=sequence):
                result = incomplete_writers(writers, [event(sequence)])
                self.assertTrue(result[0]['invalid_sequence']); self.assertEqual(result[0]['label'], 'one')
        self.assertEqual(incomplete_writers(writers, [event(0), event(0)]), [])
        self.assertTrue(incomplete_writers(writers, [event(0), event(0, 'conflict')])[0]['invalid_sequence'])
        self.assertEqual(incomplete_writers(writers, [event(2**53-1)])[0]['missing_records'], 2**53-1)
        self.assertFalse(incomplete_writers(writers, [event(0, kind='history')])[0]['finish_is_last'])


if __name__ == '__main__': unittest.main()
