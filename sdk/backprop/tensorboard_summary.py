"""Reconcile grouped history order and summaries without rewriting events.

Architecture: preserve physical writer envelopes -> order each source stream's
occupied history slots -> reduce each logical row once -> overlay affected roots.
Main callers: public replay and the logging Run's durable recovery. Source order
is authoritative within one TensorBoard namespace. Other history and explicit
summary edits keep their event-order slots. A revision changes the original
logical row; it does not create a new statistical sample or a later manual edit.
"""
import copy
from collections import defaultdict

from .metrics import MetricReducer


def order_group_rows(rows, metrics=lambda row: row, scope=lambda row: ()):
    """Sort grouped slots in place while moving the entire writer-owned row.

    Main callers: public history and summary timeline construction. Ordinary
    rows retain their slots, and independent namespaces never reorder each other.
    """
    slots = defaultdict(list)
    for index, row in enumerate(rows):
        position = metrics(row).get('_tensorboard_group_position')
        if position is not None: slots[(*scope(row), position[0])].append(index)
    for indices in slots.values():
        ordered = sorted((rows[index] for index in indices),
                         key=lambda row: tuple(metrics(row)['_tensorboard_group_position'][1:]))
        for index, row in zip(indices, ordered): rows[index] = row


def grouped_summaries(events, imported):
    """Return corrected roots, summaries and reducer state for grouped runs.

    Main callers: SDK replay and Run recovery. Automatic snapshots contain an
    importer's incomplete local view, so cannot overwrite reconciled source
    values. Explicit edits remain real operations, including deletion. Rewinds
    remove logical history beyond their writer-local cutoff and rebuild stats.
    """
    scope = lambda event: (event.get('entity', ''), event['project'], event['run_id'])
    timelines, roots = defaultdict(list), defaultdict(set)
    seen = set()
    for event in events:
        if event['event_id'] in seen: continue
        seen.add(event['event_id'])
        key, payload = scope(event), event['payload']
        if event['type'] == 'history':
            for metrics in imported.rows(event):
                timelines[key].append((event, metrics))
                if '_tensorboard_group_position' in metrics:
                    roots[key].update(k for k in metrics if not k.startswith('_tensorboard'))
        else: timelines[key].append((event, {}))
    results = {}
    for key, owned in roots.items():
        timeline = timelines[key]
        order_group_rows(timeline, metrics=lambda item: item[1])
        # A manual-only key must also survive a stale automatic snapshot.
        for event, _ in timeline:
            p = event['payload']
            if event['type'] == 'summary' and not p.get('automatic'):
                owned.update(p.get('values', {})); owned.update(p.get('deleted', []))
        actions = []
        for event, metrics in timeline:
            if event['type'] == 'history_truncate':
                actions = [(old, values) for old, values in actions
                           if old['type'] not in {'summary', 'history_truncate'}
                           and (old['type'] != 'history' or old['payload'].get('step', 0) <= event['payload']['step'])]
            else: actions.append((event, metrics))
        reducer, summary = MetricReducer(), {}
        for event, metrics in actions:
            kind, p = event['type'], event['payload']
            if kind == 'metric_definition':
                reducer.definitions[p['name']] = copy.deepcopy(p)
                reducer.reconfigure(p['name'])
            elif kind == 'history':
                reducer.apply({k:v for k,v in metrics.items() if not k.startswith('_tensorboard')}, summary)
            elif kind == 'summary' and not p.get('automatic'):
                values, deleted = p.get('values', {}), p.get('deleted', [])
                summary.update(copy.deepcopy(values))
                for name in deleted: summary.pop(name, None)
                reducer.explicit_update(values, deleted)
        results[key] = {'roots': owned, 'summary': summary, 'reducer': reducer}
    return results


def apply_group_summary(summary, correction):
    """Overlay corrected roots, preserving unrelated persisted summary fields."""
    if correction is None: return
    for key in list(summary):
        if key.startswith('_tensorboard'): summary.pop(key)
    for key in correction['roots']:
        if key in correction['summary']: summary[key] = copy.deepcopy(correction['summary'][key])
        else: summary.pop(key, None)


def run_correction(run, events):
    """Rebuild a logging handle's local source view from its durable journal.

    Main callers: Run recovery and grouped history commit preflight. Separate
    writers converge through public replay; this handle sees only its journal.
    The caller may append one prospective event to validate before persisting it.
    """
    from .event_order import ordered_events
    from .tensorboard_replay import TensorBoardHistory
    ordered = ordered_events(events)
    return grouped_summaries(ordered, TensorBoardHistory(ordered)).get((run.entity, run.project, run.id))
