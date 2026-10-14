"""Produce real shared-writer snapshots for Python/browser completeness checks.

Architecture: two SDK journals -> lost acknowledgement -> partial visibility ->
independent Public Run reads -> expected rows and writer completeness. Main
callers: JavaScript replay and browser QA. No optional package, credentials or
storage resources are required. Parquet byte fidelity is tested separately.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk'))
import backprop as w
from backprop.apis.public.history_exports import incomplete_writers, contains_live_history

SETTINGS = {'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True, 'flush_interval': 600}


def fixture():
    """Retain each visibility boundary and reconstruct it with a new reader."""
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        def writer(primary):
            return w.Run(id='shared-export', entity='e', project='history-export-replay', name='Shared history export',
                tags=['synthetic-demo'], mode='shared', dir=directory, _sink=sink, _deployment={},
                settings={**SETTINGS, 'x_primary': primary, 'x_label': 'rank0' if primary else 'rank1'})
        primary, worker = writer(True), writer(False)
        try:
            primary.log({'epoch': 0, 'loss': 3}); worker.log({'epoch': 0, 'loss': 4})
            primary.flush(); worker.flush()
            snapshots = [('running', copy.deepcopy(sink.events))]
            primary.finish()
            snapshots.append(('primary_finished', copy.deepcopy(sink.events)))
            worker.log({'epoch': 1, 'loss': 2})
            publish = sink.publish
            def lost_ack(events):
                publish(events)
                raise OSError('Lost acknowledgement')
            with patch.object(sink, 'publish', side_effect=lost_ack):
                try: worker.flush()
                except w.Error: pass
                else: raise AssertionError('Failed publication must be visible')
            worker.flush(); worker.finish()
            missing = next(event['event_id'] for event in sink.events if event['type'] == 'history'
                           and event['payload']['_writer']['label'] == 'rank1')
            snapshots.append(('missing_record', [event for event in sink.events if event['event_id'] != missing]))
            snapshots.append(('complete', copy.deepcopy(sink.events)))
        finally:
            if not worker._closed: worker.finish()
            if not primary._closed: primary.finish()
        results = []
        for name, events in snapshots:
            store = w.MemorySink(); store.events = copy.deepcopy(events[::-1] + events)
            run = w.Api._from_store(store, entity='e', project='history-export-replay').run('shared-export')
            state = run._ensure()
            results.append({'name': name, 'events': events, 'expected': {
                'state': run.state, 'history': run.history(pandas=False),
                'incomplete': incomplete_writers(state['writers'], run._events),
                'contains_live_data': contains_live_history(state, run._events)}})
        return results


if __name__ == '__main__': print(json.dumps(fixture()))
