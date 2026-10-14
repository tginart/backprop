"""Real logging journals for source grouping with axes, rewinds and definitions.

Main callers: shared Python/browser regression checks. Expected user values are
authored independently of either replay; local Run summaries are checked before
the events are exported. All runs use temporary journals and MemorySink only.
"""
import tempfile

import backprop
from backprop.integration.tensorboard.grouping import closed_groups, history_metrics
from tensorboard_grouping_replay_fixture import member


def fixtures():
    cases = {}
    with tempfile.TemporaryDirectory() as directory:
        settings = {'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True}
        def options(name, sink, **extra):
            return dict(id=name, entity='e', project='tb', dir=directory, _sink=sink, _deployment={},
                        settings={**settings, **extra})
        def metrics(values):
            return history_metrics(list(closed_groups([member(0, 0), member(1, 5, values)], final=True))[0])
        for nested in (False, True):
            name = 'nested_step_sync' if nested else 'step_sync'
            sink = backprop.MemorySink()
            with backprop.Run(**options(name, sink)) as run:
                run.define_metric('train.loss' if nested else 'loss', step_metric='train.batch' if nested else 'batch')
                run.log({'train': {'batch': 10}} if nested else {'batch': 10})
                run.log(metrics({'train': {'loss': .5}} if nested else {'loss': .5}))
                assert run.summary._as_dict()['train' if nested else 'loss'] == ({'batch':10,'loss':.5} if nested else .5)
            prefix = 'train.' if nested else ''
            cases[name] = {'events': sink.events,
                'expected_history': [{prefix+'batch':10},{prefix+'batch':10,prefix+'loss':.5,'global_step':5}],
                'expected_summary': {'train':{'batch':10,'loss':.5}} if nested else {'batch':10,'loss':.5}}
        sink = backprop.MemorySink(); name = 'rewind_reimport'; group = metrics({'loss': .5})
        with backprop.Run(**options(name, sink)) as run:
            run.log({'ordinary':1}); run.log(group)
        with backprop.Run(**options(name, sink, resume_from=name+'?_step=0')) as run:
            from backprop.integration.tensorboard.reader import _durable_seen
            assert group['_tensorboard_group']['revision'] not in _durable_seen(run)[1]
            run.log(group)
            assert run.summary['loss'] == .5
            assert group['_tensorboard_group']['revision'] in _durable_seen(run)[1]
        cases[name] = {'events': sink.events,
            'expected_history':[{'ordinary':1},{'loss':.5,'global_step':5}], 'expected_summary':{'loss':.5}}
        sink = backprop.MemorySink(); name = 'rewind_manual'; group = metrics({'loss': .5})
        with backprop.Run(**options(name, sink)) as run:
            run.log(group); run.summary['loss'] = 123
        with backprop.Run(**options(name, sink, resume_from=name+'?_step=0')) as run:
            assert run.summary['loss'] == .5
            run.log({'ordinary':7})
            assert run.summary['loss'] == .5
        cases[name] = {'events':sink.events,
            'expected_history':[{'loss':.5,'global_step':5},{'ordinary':7}], 'expected_summary':{'loss':.5}}
        sink = backprop.MemorySink(); name = 'unrelated_definition'
        with backprop.Run(**options(name, sink)) as run:
            run.define_metric('b',summary='mean'); run.log({'b':1}); run.log({'b':3})
            run.log(metrics({'a':1})); run.define_metric('b',summary='last',overwrite=True)
            assert run.summary['b']._as_dict() == {'mean':2}
        cases[name] = {'events':sink.events,
            'expected_history':[{'b':1},{'b':3},{'a':1,'global_step':5}], 'expected_summary':{'a':1,'b':{'mean':2}}}
    return cases
