"""Shared boundary fixtures for TensorBoard source-record replay.

Architecture: versioned source + encoded rich values -> distinct writer events
-> Python acceptance and browser acceptance. This fixture checks the persisted
protocol; optional writer tests separately produce genuine protobuf imports.
Main callers: Python/Node contract tests and release evidence.
"""
import copy
import hashlib
import json

from backprop.integration.tensorboard.origin import event_origin, record_identity_from_origin
from backprop.tensorboard_replay import TensorBoardHistory


def history(identifier='first', *, occurrence=0, filename='events.out.tfevents.1.machine.0',
            project='project', placement='first', step=0):
    origin = event_origin('validation', filename, b'fixture-summary-record', occurrence)
    metrics = {'_tensorboard_origin': origin, '_tensorboard_record': record_identity_from_origin(origin),
        '_tensorboard_source': filename, '_timestamp': 100.0, '_runtime': step,
        'global_step': 8, 'loss': .25,
        'image': {'_type': 'image', 'sha256': hashlib.sha256(b'image').hexdigest(), 'size': 5,
            'path': placement+'/image.png', 'name': placement+'.png', 'dataset_rid': placement,
            'width': 2, 'height': 3, 'caption': 'sample', 'mime_type': 'image/png'},
        'curve': {'_type': 'plot', 'kind': 'line', 'title': 'PR', 'table': {
            '_type': 'table', 'table_id': placement, 'artifact_path': placement,
            'columns': ['recall', 'precision'], 'data': [[.25, .75]], 'log_mode': 'IMMUTABLE',
            'row_offset': 0, 'column_types': [{'wb_type': 'number'}, {'wb_type': 'number'}]}}}
    return {'schema_version': 1, 'event_id': identifier, 'entity': 'entity', 'project': project,
        'run_id': 'run', 'timestamp': '2026-01-01T00:00:00+00:00', 'type': 'history',
        'payload': {'step': step, 'metrics': metrics,
            '_writer': {'id': identifier, 'label': identifier, 'primary': False, 'sequence': 1}}}


def cases():
    first = history()
    duplicate = history('duplicate', placement='another', step=19)
    changed = copy.deepcopy(duplicate); changed['payload']['metrics']['loss'] = .75
    wrong_digest = copy.deepcopy(duplicate); wrong_digest['payload']['metrics']['image']['sha256'] = 'f'*64
    wrong_caption = copy.deepcopy(duplicate); wrong_caption['payload']['metrics']['image']['caption'] = 'changed'
    wrong_table = copy.deepcopy(duplicate); wrong_table['payload']['metrics']['curve']['table']['data'][0][0] = 1
    wrong_identity = copy.deepcopy(duplicate); wrong_identity['payload']['metrics']['_tensorboard_record'] = '0'*64
    invalid_origin = copy.deepcopy(duplicate); invalid_origin['payload']['metrics']['_tensorboard_origin']['occurrence'] = True
    boolean = copy.deepcopy(first); boolean['payload']['metrics']['flag'] = True
    number = copy.deepcopy(duplicate); number['payload']['metrics']['flag'] = 1
    legacy = copy.deepcopy(duplicate); del legacy['payload']['metrics']['_tensorboard_origin']
    ordinary = copy.deepcopy(legacy); del ordinary['payload']['metrics']['_tensorboard_record']
    fixtures = {'duplicate': [first, duplicate], 'independent': [first, history('other', filename='events.out.tfevents.1.other.0')],
        'occurrence': [first, history('repeat', occurrence=1)], 'scope': [first, history('scope', project='other')],
        'legacy': [first, legacy], 'ordinary': [first, ordinary], 'conflict-scalar': [first, changed],
        'conflict-image': [first, wrong_digest], 'conflict-caption': [first, wrong_caption],
        'conflict-table': [first, wrong_table], 'conflict-boolean': [boolean, number],
        'invalid-record': [wrong_identity], 'invalid-origin': [invalid_origin]}
    for field in ('namespace', 'filename'):
        valid = copy.deepcopy(first)
        origin = valid['payload']['metrics']['_tensorboard_origin']
        origin[field] = 'source-\ufffd'
        valid['payload']['metrics']['_tensorboard_record'] = record_identity_from_origin(origin)
        for surrogate in ('\ud800', '\udfff'):
            invalid = copy.deepcopy(valid)
            invalid['payload']['metrics']['_tensorboard_origin'][field] = 'source-' + surrogate
            fixtures[f'invalid-unicode-{field}-{ord(surrogate)}'] = [valid, invalid]
    return fixtures


def outcome(events):
    replay = TensorBoardHistory()
    try:
        return {'accepted': [event['event_id'] for event in events if replay.accept(event)]}
    except ValueError:
        return {'error': True}


if __name__ == '__main__':
    fixtures = cases()
    print(json.dumps({'cases': fixtures, 'expected': {key: outcome(events) for key, events in fixtures.items()}}))
