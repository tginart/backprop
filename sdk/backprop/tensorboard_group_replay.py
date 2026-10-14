"""Reconcile immutable TensorBoard group revisions into visible source history.

Architecture: validate each revision -> union physical source observations ->
split at exact training-step boundaries -> expose one row per current group.
Main caller: TensorBoardHistory. Physical events remain untouched for writer
completion, file ownership and retry recovery. Missing source positions are
visible incomplete rows, never permission to merge across an unknown barrier.
"""
import copy
from collections import defaultdict

from .integration.tensorboard.grouping import validate_group_origin, history_metrics, source_position
from .integration.tensorboard.origin import record_identity_from_origin, validate_record_origin
from .tensorboard_steps import apply_step_sync


def surviving_history(events):
    """Discard observations explicitly removed by a later run rewind.

    Main callers: grouped projection and importer durable deduplication. A
    removed observation must not own or suppress a later reimport of its source.
    Input is already in canonical event order; all non-history events survive.
    """
    retained = {}
    for event in events:
        scope = (event.get('entity', ''), event['project'], event['run_id'])
        if event['type'] == 'history': retained[event['event_id']] = event
        elif event['type'] == 'history_truncate':
            retained = {key: old for key, old in retained.items()
                if (old.get('entity', ''), old['project'], old['run_id']) != scope
                or old['payload'].get('step', 0) <= event['payload']['step']}
    return [event for event in events if event['type'] != 'history' or event['event_id'] in retained]


def project_groups(events):
    """Return replacement metric rows and old single-record rows they supersede.

    Main caller: TensorBoardHistory construction over a complete read snapshot.
    Rebuilding from the union means a later-discovered file or step barrier can
    split a previously observed group without overwriting any stored evidence.
    """
    from .tensorboard_replay import semantic_value, _same_value, _MARKERS
    streams = defaultdict(dict)
    source_ids = {}
    group_events = set()
    legacy = []
    surviving = surviving_history(events)
    retained_ids = {event['event_id'] for event in surviving}
    removed = {event['event_id'] for event in events if event['event_id'] not in retained_ids
               and '_tensorboard_origin' in event['payload'].get('metrics', {})}
    for order, event in enumerate(surviving):
        if event['type'] != 'history': continue
        metrics = event['payload'].get('metrics', {})
        group = metrics.get('_tensorboard_group')
        if '_tensorboard_group' not in metrics:
            if '_tensorboard_origin' in metrics: legacy.append(event)
            continue
        validate_group_origin(metrics.get('_tensorboard_record'), metrics.get('_tensorboard_origin'), group)
        expected = history_metrics(group)
        if '_tensorboard_step_sync' in metrics:
            annotations = metrics['_tensorboard_step_sync']
            expected = apply_step_sync(expected, annotations)
            expected['_tensorboard_step_sync'] = annotations
        actual = {k:v for k,v in metrics.items() if k not in {'_runtime','_step'}}
        if not _same_value(semantic_value(actual), semantic_value(expected)):
            raise ValueError('TensorBoard group metrics disagree with member evidence')
        group_events.add(event['event_id'])
        scope = (event.get('entity',''), event['project'], event['run_id'])
        stream = (*scope, metrics['_tensorboard_origin']['namespace'])
        observations = [*group['prefix'], *group['members']]
        if group['next'] is not None: observations.append(group['next'])
        for member in observations:
            position = source_position(member)
            identity = record_identity_from_origin(member['origin'])
            source = (*scope, identity)
            if source in source_ids and source_ids[source] != (stream, position):
                raise ValueError('TensorBoard source identity has conflicting physical positions')
            source_ids[source] = stream, position
            previous = streams[stream].get(position)
            if previous:
                before = previous['member']
                if any(before[key] != member[key] for key in ('origin','index','step','wall_time')):
                    raise ValueError('Conflicting TensorBoard source position')
                if 'metrics' in before and 'metrics' in member:
                    if not _same_value(semantic_value(before['metrics']), semantic_value(member['metrics'])):
                        raise ValueError('Conflicting TensorBoard member metrics')
                    continue
                if 'metrics' in before: continue
            streams[stream][position] = dict(member=copy.deepcopy(member), event=event, order=order)

    consumed = group_events | removed
    for event in legacy:
        metrics = event['payload']['metrics']
        identity = validate_record_origin(metrics.get('_tensorboard_record'), metrics['_tensorboard_origin'])
        source = (event.get('entity',''), event['project'], event['run_id'], identity)
        if source not in source_ids: continue
        stream, position = source_ids[source]
        member = streams[stream][position]['member']
        if 'metrics' not in member: continue
        expected = {**member['metrics'], 'global_step':int(member['step']), '_timestamp':member['wall_time']}
        actual = {k:v for k,v in metrics.items() if k not in _MARKERS}
        if not _same_value(semantic_value(actual), semantic_value(expected)):
            raise ValueError('Legacy TensorBoard row conflicts with grouped source evidence')
        consumed.add(event['event_id'])

    replacements = defaultdict(list)
    for stream, positions in streams.items():
        pending, incomplete, previous = [], False, None
        def emit():
            if not pending or not any(item['member'].get('metrics') for item in pending): return
            first, last = pending[0]['member'], pending[-1]['member']
            owner = min(pending, key=lambda item:item['order'])['event']
            values = {}
            for item in pending: values.update(item['member']['metrics'])
            values.update(global_step=int(last['step']), _timestamp=last['wall_time'],
                _tensorboard_record=record_identity_from_origin(first['origin']),
                _tensorboard_origin=copy.deepcopy(first['origin']), _tensorboard_source=first['origin']['filename'],
                _tensorboard_group_position=[first['origin']['namespace'], first['origin']['filename'], first['index']],
                _tensorboard_group_members=[record_identity_from_origin(item['member']['origin']) for item in pending])
            values = apply_step_sync(values, owner['payload']['metrics'].get('_tensorboard_step_sync', []), converged=True)
            if incomplete: values['_tensorboard_incomplete'] = True
            replacements[owner['event_id']].append(values)
        for position, item in sorted(positions.items()):
            member = item['member']
            gap = (position[1] != 0 if previous is None or position[0] != previous[0] else position[1] != previous[1] + 1)
            if pending and (gap or 'metrics' not in member or member['step'] != pending[-1]['member']['step']):
                if gap: incomplete = True
                emit(); pending = []; incomplete = False
            if 'metrics' not in member:
                previous = position
                incomplete = True
                continue
            incomplete = incomplete or gap
            pending.append(item)
            previous = position
        emit()
    return dict(replacements), consumed
