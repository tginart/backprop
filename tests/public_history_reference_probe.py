"""Compare public history contracts in isolated genuine/native SDK processes.

Architecture: real public wrapper and helpers -> explicit decoded-service-row
fixture -> actual pandas conversion, or injected import/conversion failures.
Main callers: optional CI tests and release evidence. The reference transport
supplies rows, not a guessed W&B sampling algorithm. Requests below the wrapper
are recorded separately; deterministic native sampling has its own real-journal
tests. No W&B account, hosted service, or local environment install is used.
"""
import contextlib
import copy
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import patch
import warnings

REFERENCE = sys.argv[1] == 'reference'
SHA = '2d2db69df0175276a3b620094364a848ee2c2857ce123a8656b11e14bddba8da'
if REFERENCE:
    import wandb as w
    assert w.__version__ == '0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()

HISTORY = [
    {'_step': 0, 'epoch': 0, 'loss': 4., 'label': 'start'},
    {'_step': 1, 'epoch': 2, 'accuracy': .25},
    {'_step': 2, 'epoch': 4, 'loss': 2., 'nullable': None},
    {'_step': 3, 'loss': 1., 'nullable': 7},
    {'_step': 4, 'epoch': 8, 'loss': 0., 'label': 'end'},
]
SYSTEM = [{'_step': 0, 'cpu': 10., 'memory': 30.}, {'_step': 1, 'cpu': 20.}]


def owner(cls, requests, *, empty=False, failure=None):
    """Bind actual methods, supplying only storage and returned-row boundaries.

    Main caller: observe. Genuine _exec serializes real GraphQL variables;
    native reads decode the same stored rows. Samples in the paired comparison
    exceed the fixture size, so no service sample selection is fabricated.
    """
    history, system = ([], []) if empty else (copy.deepcopy(HISTORY), copy.deepcopy(SYSTEM))
    obj = SimpleNamespace(entity='team', project='history', id='run')
    if REFERENCE:
        def execute(query, variables):
            requests.append(copy.deepcopy(variables))
            if failure:
                raise failure
            if 'specs' in variables:
                spec = json.loads(variables['specs'][0])
                keys = spec['keys']
                rows = [{key: row[key] for key in keys} for row in history if all(key in row for key in keys)]
                return {'project': {'run': {'sampledHistory': [rows]}}}
            return {'project': {'run': {'history': [json.dumps(row) for row in history], 'events': [json.dumps(row) for row in system]}}}
        obj._service_api = SimpleNamespace(execute_graphql=execute)
        obj._exec = MethodType(cls._exec, obj)
    else:
        def load(force=False):
            requests.append({'force': force})
            if failure:
                raise failure
        obj.load = load
        obj._state = {'history': history, 'system': system}
        obj._store, obj._blobs = w.MemorySink(), {}
        obj._read_history = MethodType(cls._read_history, obj)
    obj._sampled_history = MethodType(cls._sampled_history, obj)
    obj._full_history = MethodType(cls._full_history, obj)
    return obj


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return 'NaN' if math.isnan(value) else str(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(item) for item in value]
    return value


def observe(cls, args=(), kwargs=None, *, empty=False, failure=None, provider='real', debug=False):
    """Record values, warning channels, reads and normalized exception causes."""
    requests = []
    obj = owner(cls, requests, empty=empty, failure=failure)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'WANDB_DEBUG': 'true' if debug else 'false'}))
        error = stack.enter_context(patch.object(w, 'termerror'))
        warn = stack.enter_context(patch.object(w, 'termwarn'))
        records = stack.enter_context(warnings.catch_warnings(record=True))
        warnings.simplefilter('always')
        if provider != 'real':
            class BrokenProvider:
                def __getattr__(self, name):
                    raise ModuleNotFoundError('provider dependency missing', name='provider_dependency')
            def failed_conversion(rows):
                raise ImportError('conversion failed')
            replacement = (None if provider == 'absent' else BrokenProvider() if provider == 'broken'
                           else SimpleNamespace(DataFrame=SimpleNamespace(from_records=failed_conversion)))
            if REFERENCE:
                from wandb.apis.public import runs
                stack.enter_context(patch.object(runs.util, 'get_module', return_value=replacement))
            else:
                from backprop.apis.public import history
                if provider == 'absent':
                    stack.enter_context(patch.object(history.importlib, 'import_module', side_effect=ModuleNotFoundError("No module named 'pandas'", name='pandas')))
                else:
                    stack.enter_context(patch.object(history.importlib, 'import_module', return_value=replacement))
        try:
            value = cls.history(obj, *args, **(kwargs or {}))
            if isinstance(value, list):
                result = {'type': 'list', 'value': value}
            else:
                result = {'type': 'DataFrame', 'value': value.to_dict(orient='split'), 'dtypes': [str(dtype) for dtype in value.dtypes]}
        except Exception as caught:
            result = {'error': type(caught).__name__, 'cause': type(getattr(caught, 'exc', None)).__name__}
            # Python arity messages name the implementation; compare their
            # error class while signatures independently prove the parameters.
            if not isinstance(getattr(caught, 'exc', None), TypeError):
                result['message'] = str(caught)
        calls = lambda mock: [{'args': list(call.args), 'kwargs': call.kwargs} for call in mock.call_args_list]
        return clean({'result': result, 'termerror': calls(error), 'termwarn': calls(warn),
                      'python_warnings': [[type(item.message).__name__, str(item.message)] for item in records],
                      'reads': len(requests)}), requests


def observe_deprecated(cls, args=(), kwargs=None, failure=False):
    """Record the unchanged warning and keyword forwarding on the old name.

    Main caller: main's two-import matrix. The scan is a sentinel because
    beta_scan_history delegates iteration and errors without adapting either.
    Real journal scans are separately exercised in the native suite.
    """
    result = object()
    def scan(*args, **kwargs):
        if failure: raise ValueError('scan failed')
        return result
    with patch.object(w, 'termwarn') as warn, warnings.catch_warnings(record=True) as records:
        warnings.simplefilter('always')
        from unittest.mock import Mock
        scan = Mock(side_effect=scan)
        obj = SimpleNamespace(scan_history=scan)
        try:
            value = cls.beta_scan_history(obj, *args, **(kwargs or {}))
            observed = {'same_result': value is result}
        except Exception as error:
            observed = {'error': type(error).__name__}
        calls = lambda mock: [{'args': list(call.args), 'kwargs': call.kwargs} for call in mock.call_args_list]
        return {'result': observed, 'scan': calls(scan), 'termwarn': calls(warn),
                'python_warnings': [[type(item.message).__name__, str(item.message)] for item in records]}


def main():
    output = {'portable': {}, 'deprecated': {}, 'reference_requests': {}, 'native_validation': {}}
    for module in ['wandb.apis.public', 'wandb.apis.public.runs']:
        cls = importlib.import_module(module).Run
        if REFERENCE:
            assert hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest() == SHA
        signatures = [(p.name, p.kind.name, 'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.history).parameters.values()]
        cases = {}
        matrix = [
            ('default', (), {}, {}),
            ('rows', (), {'pandas': False}, {}),
            ('all_positional', (500, ['loss'], 'epoch', False, 'default'), {}, {}),
            ('all_keywords', (), {'samples': 20, 'keys': ['loss'], 'x_axis': 'epoch', 'pandas': True, 'stream': 'default'}, {}),
            ('axis_already_selected', (), {'keys': ['epoch', 'loss', 'loss'], 'x_axis': 'epoch', 'pandas': False}, {}),
            ('default_axis', (), {'keys': ['loss'], 'pandas': False}, {}),
            ('absent_key', (), {'keys': ['missing']}, {}),
            ('absent_axis', (), {'keys': ['loss'], 'x_axis': 'missing', 'pandas': False}, {}),
            ('nullable', (), {'keys': ['nullable'], 'pandas': False}, {}),
            ('empty_keys', (), {'keys': []}, {}),
            ('empty_keys_axis_unused', (), {'keys': [], 'x_axis': None, 'pandas': False}, {}),
            ('no_keys_axis_unused', (), {'x_axis': ['unused'], 'pandas': False}, {}),
            ('system', (), {'stream': 'system'}, {}),
            ('system_rows', (), {'stream': 'system', 'pandas': False}, {}),
            ('system_empty_keys', (), {'stream': 'system', 'keys': [], 'pandas': False}, {}),
            ('other_stream_reference_routing', (), {'stream': 'events', 'pandas': False}, {}),
            ('falsey_pandas', (), {'pandas': None}, {}),
            ('truthy_pandas', (), {'pandas': 1}, {}),
            ('empty', (), {}, {'empty': True}),
            ('empty_rows', (), {'pandas': False}, {'empty': True}),
            ('keys_string', (), {'keys': 'loss'}, {}),
            ('keys_tuple', (), {'keys': ('loss',)}, {}),
            ('keys_mapping', (), {'keys': {'loss': 1}}, {}),
            ('keys_first_number', (), {'keys': [1]}, {}),
            ('keys_first_none', (), {'keys': [None]}, {}),
            ('keys_system', (), {'keys': ['loss'], 'stream': 'system'}, {}),
            ('key_validation_priority', (), {'keys': [1], 'stream': 'system', 'samples': 0}, {}),
            ('extra_keyword', (), {'extra': True}, {}),
            ('extra_positional', (1, [], '_step', False, 'default', True), {}, {}),
            ('duplicate_argument', (500,), {'samples': 500}, {}),
            ('missing_pandas', (), {}, {'provider': 'absent'}),
            ('no_pandas_needed', (), {'pandas': False}, {'provider': 'absent'}),
            ('invalid_no_pandas_needed', (), {'keys': 1}, {'provider': 'conversion'}),
            ('conversion_error', (), {}, {'provider': 'conversion'}),
            ('conversion_error_debug', (), {}, {'provider': 'conversion', 'debug': True}),
            ('broken_dependency', (), {}, {'provider': 'broken'}),
            ('broken_dependency_debug', (), {}, {'provider': 'broken', 'debug': True}),
            ('read_error', (), {'pandas': False}, {'failure': OSError('storage read failed')}),
            ('read_error_debug', (), {'pandas': False}, {'failure': OSError('storage read failed'), 'debug': True}),
        ]
        for name, args, kwargs, options in matrix:
            cases[name], requests = observe(cls, args, kwargs, **options)
            if REFERENCE:
                output['reference_requests'][module + '.' + name] = requests
        output['portable'][module] = {'signature': signatures, 'cases': cases}
        output['deprecated'][module] = {
            'signature': [(p.name, p.kind.name, 'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(cls.beta_scan_history).parameters.values()],
            'cases': [observe_deprecated(cls, args, kwargs, failure) for args, kwargs, failure in [
                ((), {}, False), ((['loss'], 2, 1, 4, False), {}, False),
                ((), {'keys': [], 'page_size': 10, 'min_step': 0, 'max_step': None, 'use_cache': True}, False),
                ((), {'keys': 'invalid', 'page_size': 0}, False),
                ((), {}, True), ((), {'extra': True}, False), ((None, 1, 0, None, True, 1), {}, False),
            ]],
        }
        if not REFERENCE:
            for name, kwargs in [('zero', {'samples': 0}), ('negative', {'samples': -1}), ('boolean', {'samples': True}),
                                 ('fraction', {'samples': 1.5}), ('string', {'samples': '5'}), ('none', {'samples': None}),
                                 ('later_key', {'keys': ['loss', 1]}), ('axis_type', {'keys': ['loss'], 'x_axis': None})]:
                output['native_validation'][module + '.' + name] = observe(cls, kwargs={'pandas': False, **kwargs})[0]
    print(json.dumps(output, sort_keys=True))


if __name__ == '__main__':
    main()
