"""Conflict, duplicate and writer-accounting checks for imported observations."""
import copy
import tempfile
import unittest

import backprop
from backprop.apis.public.replay import reduce_events
from backprop.integration.tensorboard.origin import event_origin, record_identity_from_origin
from backprop.tensorboard_replay import TensorBoardHistory
from tensorboard_replay_fixture import cases, history, outcome


class TensorBoardReplayContracts(unittest.TestCase):
    def test_distinct_sources_scopes_and_occurrences_never_collapse(self):
        fixtures = cases()
        self.assertEqual(outcome(fixtures['duplicate']), {'accepted': ['first']})
        for name in ('independent', 'occurrence', 'scope', 'legacy', 'ordinary'):
            with self.subTest(name=name):
                self.assertEqual(len(outcome(fixtures[name])['accepted']), 2)
        for name in fixtures.keys() - {'duplicate', 'independent', 'occurrence', 'scope', 'legacy', 'ordinary'}:
            with self.subTest(name=name):
                self.assertEqual(outcome(fixtures[name]), {'error': True})

    def test_real_shared_handles_keep_both_physical_writers_and_one_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            metrics = history()['payload']['metrics']
            for label in ('primary', 'secondary'):
                run = backprop.Run(id='imported', entity='entity', project='project', mode='shared',
                    dir=directory, _sink=sink, _deployment={}, settings={'silent': True, 'console': 'off',
                    'disable_git': True, 'x_disable_stats': True, 'x_label': label, 'x_primary': label == 'primary'})
                run.log(metrics); run.finish()
            self.assertEqual(len([e for e in sink.events if e['type'] == 'history']), 2)
            original = copy.deepcopy(sink.events)
            for events in (sink.events, list(reversed(sink.events)), sink.events*2):
                state = reduce_events(events)
                self.assertEqual(len(state['history']), 1)
                self.assertEqual(len(state['writers']), 2)
                self.assertEqual({w['state'] for w in state['writers'].values()}, {'finished'})
            self.assertEqual(sink.events, original)

    def test_sidecar_types_and_digest_are_verified_even_for_first_observation(self):
        for field, value in [('version', True), ('version', 2), ('occurrence', -1),
                ('occurrence', 2**53), ('filename', '../source'), ('namespace', 'bad\0label'),
                ('event_sha256', 'UPPERCASE')]:
            event = history()
            event['payload']['metrics']['_tensorboard_origin'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                TensorBoardHistory().accept(event)

    def test_importer_owned_table_and_implicit_artifact_ids_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink(); manifests = []
            for index in range(2):
                with backprop.Run(id='table-source', project='tables', entity='e', mode='shared',
                        dir=directory, _sink=sink, _deployment={}, settings={'silent': True, 'console': 'off',
                        'disable_git': True, 'x_disable_stats': True, 'x_label': str(index), 'x_primary': False}) as run:
                    table = backprop.Table(columns=['recall'], data=[[.25]])
                    table._id = '1'*32; table._tensorboard_record_identity = '1'*64
                    run.log({'table': table})
                    run.flush()
                    manifests.append(next(e['payload']['artifacts'][0] for e in reversed(sink.events)
                        if e['type'] == 'history'))
            self.assertEqual(manifests[0]['artifact_id'], manifests[1]['artifact_id'])
            self.assertEqual(manifests[0]['version'], manifests[1]['version'])
