"""Durable logging-handle summaries use the same source projection as readers.

Architecture: real Run journal -> revision/ordinary/manual operations -> restarted
handle -> public replay. No TensorBoard dependency or remote store is simulated
as evidence of decoder parity; these checks target persistence and reduction.
"""
import copy
import tempfile
import unittest

import backprop
from backprop.apis.public.replay import reduce_events
from backprop.integration.tensorboard.grouping import closed_groups, history_metrics
from backprop.transport import read_journal
from tensorboard_grouping_replay_fixture import member


class TensorBoardGroupRecovery(unittest.TestCase):
    def test_axis_rewind_and_definition_interactions(self):
        from tensorboard_group_interactions import fixtures
        for name, case in fixtures().items():
            for events in (case['events'], list(reversed(case['events'])), case['events']*2):
                with self.subTest(case=name):
                    state = reduce_events(events)
                    visible = [{k:v for k,v in row.items() if not k.startswith('_')} for row in state['history']]
                    self.assertEqual(visible,case['expected_history'])
                    for key, value in case['expected_summary'].items(): self.assertEqual(state['summary'][key], value)

    def test_live_summary_restart_and_new_observation_count_once(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            options = dict(id='group-recovery', entity='e', project='tb', dir=directory,
                _sink=sink, _deployment={}, settings={'silent': True, 'console': 'off',
                    'disable_git': True, 'x_disable_stats': True})
            prefix = list(closed_groups([member(0, 0), member(1, 5, {'a': 1})], final=True))[0]
            full = list(closed_groups([member(0, 0), member(1, 5, {'a': 1}), member(2, 5, {'a': 3})], final=True))[0]
            with backprop.Run(**options) as run:
                run.define_metric('a', summary='min,max,mean,last')
                run.log(history_metrics(prefix)); run.log(history_metrics(full))
                expected = {'min': 3, 'max': 3, 'mean': 3, 'last': 3}
                self.assertEqual(run.summary['a']._as_dict(), expected)
                run.log({'a': 99})
                expected = {'min': 3, 'max': 99, 'mean': 51, 'last': 99}
                self.assertEqual(run.summary['a']._as_dict(), expected)
                self.assertEqual(reduce_events(read_journal(run.sync_dir))['summary']['a'], expected)
            with backprop.Run(**options, resume='must') as resumed:
                self.assertEqual(resumed.summary['a']._as_dict(), expected)
                resumed.log(history_metrics(prefix))
                self.assertEqual(resumed.summary['a']._as_dict(), expected)
                resumed.log({'a': 51})
                self.assertEqual(resumed.summary['a']._as_dict(), {**expected, 'last': 51})
                resumed.summary['a'] = 123
                resumed.log(history_metrics(full))
                self.assertEqual(resumed.summary['a'], 123)
                del resumed.summary['a']
                resumed.log(history_metrics(prefix))
                self.assertNotIn('a', resumed.summary._as_dict())
                resumed.log({'a': 7})
                self.assertEqual(resumed.summary['a']._as_dict(),
                    {'min': 3, 'max': 99, 'mean': 40, 'last': 7})

    def test_invalid_revision_fails_before_history_or_summary_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            with backprop.Run(id='invalid-group', project='tb', mode='offline', dir=directory,
                    settings={'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True}) as run:
                group = list(closed_groups([member(0, 0), member(1, 5, {'a': 1})], final=True))[0]
                run.log(history_metrics(group))
                before = copy.deepcopy(run.summary._as_dict())
                journal = copy.deepcopy(read_journal(run.sync_dir))
                invalid = history_metrics(group)
                invalid['a'] = 2
                with self.assertRaisesRegex(ValueError, 'disagree with member evidence'):
                    run.log(invalid)
                self.assertEqual(read_journal(run.sync_dir), journal)
                self.assertEqual(run.summary._as_dict(), before)
                # Replace the rejected pending value with a valid retry.
                run.log(history_metrics(group))
                self.assertEqual(run.summary['a'], 1)
