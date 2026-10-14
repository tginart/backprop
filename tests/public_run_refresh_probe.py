"""Observe public refreshes through the actual pinned or native Run methods.

Architecture: a fixed portable experiment -> declared backend response boundary
-> real public Run -> repeated reads, failures and recovery. Main caller:
test_public_run_refresh_contracts. Native input is a real logging journal;
reference input uses the public fragment's documented backend fields. No public
Run implementation is replaced, and no credentials or remote services are used.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


PORTABLE_FIELDS = ('name', 'displayName', 'state', 'tags', 'group', 'jobType',
                   'notes', 'description', 'readOnly', 'config', 'summaryMetrics',
                   'historyLineCount')


def fields(value):
    """Select portable values while retaining the full result separately.

    Main caller: observe. Foundry timestamps, storage IDs, history-index metadata
    and account records have separate documented contracts; comparing fabricated
    W&B values for those fields would not verify their implementation.
    """
    return {key: copy.deepcopy(value.get(key)) for key in PORTABLE_FIELDS}


def reference_payload(version):
    """Supply a W&B fragment response independently of native replay output."""
    return {'id': 'reference-storage-id', 'name': 'run', 'displayName': 'named',
            'state': 'running', 'tags': ['refresh'], 'group': 'group',
            'jobType': 'train', 'notes': 'note', 'description': None,
            'readOnly': False, 'projectId': '123',
            'user': {'username': 'fixture-user', 'name': 'Fixture User'},
            'createdAt': '2026-01-01T00:00:00Z', 'heartbeatAt': '2026-01-01T00:00:01Z',
            'sweepName': None, 'commit': None,
            'config': json.dumps({'rate': {'value': version, 'desc': None}}),
            'summaryMetrics': json.dumps({'loss': 3}), 'systemMetrics': '{}',
            'historyKeys': {'keys': {'loss': {'previousValue': 3}}, 'lastStep': 0},
            'historyLineCount': 1}


def observe(reference, scenario, output):
    """Exercise caching and failure paths, retaining every returned payload.

    Main caller: this file's isolated-process entry point. The service/store
    supplies only data or a transport error. All decoding, caching, methods,
    property access and return values remain the installed implementation's.
    """
    sdk = importlib.import_module('wandb' if reference else 'backprop')
    if reference:
        assert sdk.__version__ == '0.30.0'
    public = importlib.import_module(('wandb' if reference else 'backprop') + '.apis.public.runs')
    lazy = scenario.endswith('-lazy')
    base = scenario.removesuffix('-lazy').removesuffix('-full')
    calls, trace, payloads = [], [], []
    state = {'version': 1, 'failure': None}
    writer = None
    with tempfile.TemporaryDirectory() as directory:
        os.environ['WANDB_DIR'] = directory
        if reference:
            class Service:
                def execute_graphql(self, query, variables):
                    calls.append({'scope': variables, 'full': 'fragment RunFragment' in query})
                    if state['failure'] == 'transport':
                        raise ConnectionError('fixture read failure')
                    if state['failure'] == 'missing-response': return None
                    if state['failure'] == 'missing-project': return {'project': None}
                    if state['failure'] == 'missing-run': return {'project': {'run': None}}
                    raw = reference_payload(state['version'])
                    if base == 'lightweight' and not calls[-1]['full']:
                        for field in ('config', 'summaryMetrics', 'systemMetrics', 'historyKeys'):
                            raw.pop(field)
                    if state['failure'] == 'malformed': raw['config'] = '{'
                    return {'project': {'run': raw}}
            run = public.Run(Service(), 'team', 'refresh', 'run', lazy=lazy)
        else:
            sink = sdk.MemorySink()
            writer = sdk.Run(project='refresh', entity='team', id='run', name='named',
                             tags=['refresh'], group='group', job_type='train', notes='note',
                             dir=directory, config={'rate': 1}, _sink=sink,
                             settings={'silent': True, 'console': 'off', 'disable_git': True,
                                       'disable_code': True, 'x_disable_stats': True})
            writer.log({'loss': 3}); writer.flush()
            api = sdk.Api._from_store(sink, entity='team', project='refresh')
            run = public.Run._from_events(api, 'team', 'refresh', 'run', sink.read_events(), lazy=lazy)
            original = sink.read_events
            def read(**scope):
                calls.append({'scope': {'entity': scope['entity'], 'project': scope['project'], 'name': scope['run_id']}, 'full': True})
                if state['failure'] == 'transport': raise ConnectionError('fixture read failure')
                if state['failure'] and state['failure'].startswith('missing-'): return []
                events = original(**scope)
                if state['failure'] == 'malformed':
                    events = copy.deepcopy(events)
                    start = next(event for event in events if event['type'] == 'run_start')
                    start['payload']['config'] = ['invalid configuration mapping']
                return events
            sink.read_events = read
        calls.clear()

        def invoke(method, force=False):
            before = len(calls)
            try:
                result = getattr(run, method)(force=force)
                payloads.append(copy.deepcopy(result))
                trace.append({'method': method, 'force': force, 'reads': len(calls)-before,
                              'value': fields(result), 'error': None})
                return result
            except Exception as error:
                trace.append({'method': method, 'force': force, 'reads': len(calls)-before,
                              'error': type(error).__name__, 'message': str(error)})
                return None

        def advance():
            state['version'] = 2
            if writer is not None:
                writer.config.update({'rate': 2}, allow_val_change=True); writer.flush()

        try:
            if base == 'cache':
                invoke('load')
                advance()
                invoke('load')
                invoke('load_full_data')
                invoke('load_full_data')
                invoke('load', force=True)
                invoke('load_full_data', force=True)
            elif base.startswith('missing-') or base == 'transport':
                invoke('load')
                state['failure'] = base
                invoke('load', force=True)
                # Cached reads after a failed refresh must retain the old data.
                invoke('load')
                invoke('load_full_data', force=True)
                state['failure'] = None
                advance()
                invoke('load_full_data', force=True)
            elif base == 'returned-edit':
                value = invoke('load')
                value['config']['rate'] = 99
                invoke('load')
                invoke('load', force=True)
            elif base == 'unsaved-edit':
                run.config['rate'] = 99
                invoke('load')
                invoke('load', force=True)
            elif base == 'lightweight':
                invoke('load')
                advance()
                invoke('load_full_data')
                invoke('load')
            else:
                raise AssertionError(scenario)
            observation = {'trace': trace, 'backend_reads': calls, 'payloads': payloads,
                           'final_config': copy.deepcopy(run.config),
                           # Pinned0.30.0 stores this fragment field privately;
                           # its documented public spelling is not implemented.
                           'project_internal_id': run._project_internal_id if reference else run.project_internal_id}
        finally:
            if writer is not None: writer.finish()
    output.write_text(json.dumps({'sdk': 'reference' if reference else 'native',
                                 'scenario': scenario, 'observations': observation}, indent=2))


if __name__ == '__main__':
    observe(sys.argv[1] == 'reference', sys.argv[2], Path(sys.argv[3]))
