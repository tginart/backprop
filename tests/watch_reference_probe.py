"""Compare watch registrations in separate native and pinned-source processes.

Architecture: hash-checked W&B watch/TorchHistory bodies -> real PyTorch models
and hooks -> recorded log calls, warnings and errors. Only telemetry, the remote
log sink and unused graph factory are inert. The public-wrapper return and real
graph capture are verified by the separate installed-wheel graph tests.
Main caller: test_optional_watch. No services, credentials or installations.
"""
from __future__ import annotations

import ast
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import backprop
import torch

ROOT = Path(__file__).resolve().parents[1]
HASHES = {
    'sdk/wandb_watch.py': '47b3ad1e6a6fa31fc784daaae60231a97cf8e75e12bd471003c43652218d137e',
    'integration/torch/wandb_torch.py': '54bee05e5655a6d50f83c855e3e57b7697c7d11e4ccaa4f8a3e27f30519d195a',
}


def source_definitions(relative, names, namespace):
    """Execute unchanged definitions after verifying their reference bytes.

    Main caller: recorder factory. Supplying external sinks keeps all numerical
    and registration logic real while avoiding the hosted W&B process service.
    """
    path = ROOT / '.reference/wandb/source/wandb' / relative
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == HASHES[relative]
    nodes = [node for node in ast.parse(data).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), namespace)


def recorder(native):
    """Return equivalent watch/unwatch calls with isolated run-owned outputs."""
    events, messages = [], []
    def log(values, commit=None):
        assert commit is False
        events.append({name: value.to_json() for name, value in values.items()})
    run = SimpleNamespace(_closed=False, log=log)
    if native:
        from backprop.telemetry import ModelWatcher
        observer = ModelWatcher(run)
        return observer.watch, observer.unwatch, events, messages
    api = SimpleNamespace(Histogram=backprop.Histogram, run=SimpleNamespace(_log=log),
                          termwarn=messages.append, util=SimpleNamespace(get_module=lambda *args, **kwargs: torch))
    namespace = {'wandb': api, 'torch': torch, 'LOG_TRACK_COUNT': 0, 'LOG_TRACK_THRESHOLD': 1,
                 'TorchGraph': SimpleNamespace(hook_torch=None)}
    source_definitions('integration/torch/wandb_torch.py',
                       {'TorchHistory', 'log_track_init', 'log_track_update'}, namespace)
    run._torch = namespace['TorchHistory']()
    namespace.update(_global_watch_idx=0, logger=SimpleNamespace(info=lambda *args: None),
                     telemetry=SimpleNamespace(context=lambda: nullcontext(SimpleNamespace(feature=SimpleNamespace()))))
    source_definitions('sdk/wandb_watch.py', {'_watch', '_unwatch'}, namespace)
    return (lambda *args, **kwargs: namespace['_watch'](run, *args, **kwargs),
            lambda *args, **kwargs: namespace['_unwatch'](run, *args, **kwargs), events, messages)


def error_or_value(call):
    try:
        return ['value', call()]
    except Exception as error:
        return ['error', type(error).__name__, str(error)]


def exercise(native, scenario):
    torch.manual_seed(34)
    watch, unwatch, events, messages = recorder(native)
    model = torch.nn.Linear(2, 1)
    other = torch.nn.Linear(2, 1)
    results = []
    with patch.object(backprop, 'termwarn', side_effect=messages.append):
        def train():
            model(torch.ones(2, 2)).sum().backward()
        if scenario == 'repeat':
            results += [watch(model, log='all', log_freq=1), watch(model, log='all', log_freq=2)]
            train(); train(); unwatch(model); train()
        elif scenario == 'repeated_parameter_prefix':
            watch(model, log='parameters', idx=0, log_freq=1)
            watch(model, log='parameters', idx=0, log_freq=2)
            train(); train()
            # Upstream leaks the overwritten parameter hook on cleanup. Compare
            # registration/measurements here; native cleanup has a separate test.
        elif scenario == 'collision':
            watch(model, idx=4, log_freq=1)
            results.append(error_or_value(lambda: watch(model, idx=4)))
            results.append(error_or_value(lambda: watch(other, idx=4)))
            train(); unwatch(); train()
        elif scenario == 'empty_unwatch':
            watch(model, log='all', log_freq=1)
            train(); unwatch([]); train()
            results.append(len(events))
        elif scenario == 'warnings':
            unwatch(model)
            watch(model, log=None); unwatch(model)
            watch(model, idx=0); unwatch(model); unwatch(model)
        elif scenario == 'frozen':
            model.requires_grad_(False)
            watch(model); unwatch(model)
        elif scenario == 'same_model_list':
            results.append(watch([model, model], log='all', log_freq=1))
            train(); unwatch(model); train()
        elif scenario == 'input_errors':
            for value in [object(), [model, object()], (x for x in [model])]:
                results.append(error_or_value(lambda: watch(value)))
            for mode in ['bad', [], 2]:
                results.append(error_or_value(lambda: watch(model, log=mode)))
            results.append(watch([])); results.append(watch(()))
        elif scenario.startswith('frequency:'):
            raw = scenario.split(':')[1]
            frequency = {'none': None, 'str': 'two', 'nan': float('nan'), 'false': False,
                         'negative': -1, 'zero': 0, 'fractional': 1.5}[raw]
            watch(model, log='all', log_freq=frequency)
            for _ in range(3):
                results.append(error_or_value(train))
            unwatch()
        elif scenario == 'registration_runtime_error':
            with patch.object(model, 'register_forward_hook', side_effect=RuntimeError('fixture failure')):
                results.append(watch(model, log='parameters'))
            unwatch(model)
        else:
            raise AssertionError(scenario)
    return dict(results=results, events=events, warnings=messages)


if __name__ == '__main__':
    scenarios = ['repeat', 'repeated_parameter_prefix', 'collision', 'empty_unwatch',
                 'warnings', 'frozen', 'same_model_list', 'input_errors', 'registration_runtime_error']
    scenarios += ['frequency:' + value for value in ['none', 'str', 'nan', 'false', 'negative', 'zero', 'fractional']]
    print(json.dumps({name: exercise(sys.argv[1] == 'native', name) for name in scenarios}))
