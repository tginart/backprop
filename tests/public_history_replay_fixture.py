"""Supply SDK-generated history to shared Python/browser replay checks.

Architecture: real journals -> lost acknowledgement -> delayed snapshot ->
resume and two shared writers -> independent public reads. Main callers: the
JavaScript suite and live-release data checks. No server account is involved.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk'))
import backprop as w

SETTINGS = {'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True, 'flush_interval': 600}


def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        def writer(identity='history', **kwargs):
            return w.Run(id=identity, entity='e', project='history-replay', dir=directory, _sink=sink, _deployment={}, **kwargs)
        with writer(settings=SETTINGS) as run:
            run.define_metric('loss', step_metric='epoch')
            run.log({'epoch': 0, 'loss': 4, 'weights': w.Histogram(np_histogram=([2, 3], [0, 1, 2]))})
            run._emit('system', {'step': 0, 'metrics': {'cpu': 12, 'memory': {'rss': 24}}})
            run.flush(); delayed = copy.deepcopy(sink.events)
            run.log({'epoch': 1, 'validation': .5})
            run.log({'epoch': 2, 'loss': 2})
            publish = sink.publish
            def lost_ack(events):
                publish(events); raise ConnectionError('lost acknowledgement')
            with patch.object(sink, 'publish', side_effect=lost_ack):
                try: run.flush()
                except w.Error: pass
                else: raise AssertionError('Lost acknowledgement must be surfaced')
            run.flush()
        with writer(settings=SETTINGS, resume='must') as run:
            run.log({'epoch': 3, 'loss': 1})
            run._emit('system', {'step': 1, 'metrics': {'cpu': 15}})
        with writer('shared', mode='shared', settings={**SETTINGS, 'x_primary': True, 'x_label': 'rank0'}) as primary:
            with writer('shared', mode='shared', settings={**SETTINGS, 'x_primary': False, 'x_label': 'rank1'}) as worker:
                for run, value in [(primary, 10), (worker, 20)]:
                    run.log({'epoch': 0, 'loss': value})
                    run._emit('system', {'step': 0, 'metrics': {'cpu': value}})
        snapshots = []
        for events in [delayed, sink.events]:
            read_store = w.MemorySink(); read_store.events = events
            api = w.Api._from_store(read_store, entity='e', project='history-replay')
            expected = {}
            for run in api.runs():
                expected[run.id] = {'history': run.history(pandas=False), 'system': run.history(stream='system', pandas=False),
                                    'selected': run.history(samples=2, keys=['loss'], x_axis='epoch', pandas=False), 'state': run.state}
            snapshots.append({'events': events, 'expected': expected})
        return snapshots


if __name__ == '__main__': print(json.dumps(fixture()))
