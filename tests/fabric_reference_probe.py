"""Compare Fabric adapter contracts in independent native/reference processes.

Architecture: hash-pinned W&B class body -> real Lightning decorators/utilities ->
recording fixtures at the experiment-service boundary -> JSON observations.
Main caller: test_optional_fabric. Only network-facing run/artifact operations
are fixtures; constructor logic, branches, forwarding and validation execute.
"""
from __future__ import annotations

from contextlib import nullcontext

import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.reference/wandb/source/wandb/integration/lightning/fabric/logger.py'
SOURCE_HASH = '7d8f86bf6f31356492ab80239fffcc90dbed59d56cb0e77d9ffb8ce60c2ddc57'
MEMBERS = ['__init__', 'after_save_checkpoint', 'download_artifact', 'experiment',
           'finalize', 'group_separator', 'log_dir', 'log_video', 'name', 'root_dir',
           'save', 'save_dir', 'use_artifact', 'version']


class ServiceRun:
    """Record calls at the hosted-service boundary without starting a W&B run."""
    def __init__(self):
        self.id = 'service-run'; self.name = 'display'; self.calls = []
    def _label(self, **kwargs):
        # W&B internal repository telemetry is outside the portable contract.
        pass
    def define_metric(self, *args, **kwargs): self.calls.append(['define_metric', args, kwargs])
    def log(self, *args, **kwargs): self.calls.append(['log', args, kwargs])
    def use_artifact(self, *args, **kwargs):
        self.calls.append(['use_artifact', args, kwargs]); return ServiceArtifact(self.calls)


class ServiceArtifact:
    def __init__(self, calls): self.calls = calls
    def download(self, **kwargs):
        self.calls.append(['download', kwargs]); return kwargs['root'] or 'default-cache'


def reference_class(namespace):
    """Execute the unchanged pinned class with explicit dependency boundaries.

    Main callers: compare; a hash change requires a deliberate reference review.
    """
    source = SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_HASH: raise AssertionError('Pinned Fabric source changed')
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'WandbLogger')
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    scope = dict(namespace)
    # Internal W&B service telemetry has no portable logging side effect.
    scope['telemetry'] = SimpleNamespace(context=lambda **kwargs:
        nullcontext(SimpleNamespace(feature=SimpleNamespace())))
    exec(compile(ast.fix_missing_locations(tree), str(SOURCE), 'exec'), scope)
    return scope['WandbLogger']


def normalize(value):
    if isinstance(value, os.PathLike): return os.fspath(value)
    if isinstance(value, dict): return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [normalize(v) for v in value]
    return value


def result(function):
    try: return {'value': normalize(function())}
    except Exception as error: return {'error': type(error).__name__, 'message': str(error)}


def compare(kind):
    import backprop.integration.lightning.fabric.logger as module
    from lightning.fabric.utilities.rank_zero import rank_zero_only
    service_calls = []
    def init(**kwargs):
        service_calls.append(['init', kwargs]); return ServiceRun()
    def video(value, **kwargs):
        return {'video': value, 'kwargs': kwargs}
    def api():
        def artifact(*args, **kwargs):
            service_calls.append(['api_artifact', args, kwargs]); return ServiceArtifact(service_calls)
        return SimpleNamespace(artifact=artifact)
    facade = SimpleNamespace(run=None, Run=ServiceRun, init=init, Video=video, Api=api)
    output = {'constructors': [], 'routes': [], 'artifacts': [], 'videos': [], 'root_dirs': []}
    with patch.object(module, 'wandb', facade), patch.object(rank_zero_only, 'rank', 0), patch.dict(os.environ, {}, clear=True):
        cls = reference_class(vars(module)) if kind == 'reference' else module.WandbLogger
        output['signatures'] = {}
        for name in MEMBERS:
            member = getattr(cls, name)
            output['signatures'][name] = str(inspect.signature(member.fget if isinstance(member, property) else member))
        output['constant'] = cls.LOGGER_JOIN_CHAR
        cases = [{}, {'save_dir': None}, {'save_dir': Path('runs/one')},
                 {'save_dir': None, 'dir': Path('alternate')}, {'save_dir': '', 'dir': 'alternate'},
                 {'version': 'preferred', 'id': 'ignored', 'name': 'display', 'project': 'project'},
                 {'id': 'fallback', 'anonymous': True, 'prefix': 'train'},
                 {'offline': True, 'log_model': True}, {'offline': True, 'log_model': 'all'},
                 {'save_dir': ['invalid']},
                 {'name': 'name', 'save_dir': 'save', 'version': 'version', 'offline': False,
                  'dir': 'dir', 'id': 'id', 'anonymous': False, 'project': 'project', 'log_model': 'all',
                  'prefix': 'prefix', 'checkpoint_name': 'weights', 'log_checkpoint_on': 'all',
                  'entity': 'team', 'group': 'group', 'tags': ['one'], 'notes': 'note', 'resume': 'must'}]
        for options in cases:
            def create():
                logger = cls(**options)
                names = ['name', 'save_dir', 'log_dir', 'version', 'group_separator']
                values = {name: getattr(logger, name) for name in names}
                values.update(init=logger._wandb_init, experiment=logger._experiment,
                    log_model=logger._log_model, offline=logger._offline, prefix=logger._prefix,
                    checkpoint_name=logger._checkpoint_name, log_checkpoint_on=logger._log_checkpoint_on)
                return values
            output['constructors'].append(result(create))
        with patch.dict(os.environ, {'WANDB_PROJECT': 'environment-project'}):
            output['environment_project'] = cls().name
        for path in [None, '', '.', 'runs/one', Path('/tmp/experiments/run')]:
            logger = cls(save_dir=path)
            output['root_dirs'].append(result(lambda: logger.root_dir))
        original = ServiceRun(); logger = cls(experiment=original, version='constructor-id', project='project')
        output['identity'] = [logger.name, logger.version, logger.experiment is original]
        original.id = 'changed-id'; output['identity'].append(logger.version)
        for mode in [False, True, 'all']:
            for keep in [-1, 0, 2]:
                for when in ['success', 'all']:
                    for status in ['success', 'failed', 'interrupted']:
                        logger = cls(experiment=ServiceRun(), log_model=mode, log_checkpoint_on=when)
                        callback = SimpleNamespace(save_top_k=keep); scans = []
                        logger._scan_and_log_pytorch_checkpoints = lambda cb: scans.append(cb is callback)
                        first = logger.after_save_checkpoint(callback)
                        pending = logger._checkpoint_callback is callback
                        final = logger.finalize(status)
                        output['routes'].append([mode, keep, when, status, first, pending, final, scans])
        logger = cls(); logger._checkpoint_callback = object()
        output['finalize_without_experiment'] = logger.finalize('success')
        logger = cls(experiment=ServiceRun()); output['save_return'] = logger.save()
        output['save_calls'] = logger.experiment.calls
        for active in [False, True]:
            for used in [None, False, True]:
                for directory in [None, 'output', Path('path-output')]:
                    for artifact_type in [None, 'model']:
                        service_calls.clear(); facade.run = ServiceRun() if active else None
                        value = cls.download_artifact('team/project/weights:best', directory, artifact_type, used)
                        output['artifacts'].append([active, used, normalize(directory), artifact_type, value,
                            normalize(service_calls), normalize(facade.run.calls) if active else []])
        facade.run = None
        logger = cls(experiment=ServiceRun())
        for artifact_type in [None, 'model']:
            value = logger.use_artifact('weights:best', artifact_type)
            assert isinstance(value, ServiceArtifact)
        output['use_artifact_calls'] = logger.experiment.calls
        for step in [None, 0, 7]:
            for values, kwargs in [([], {}), (['one.gif'], {}), (['one.gif', 'two.mp4'],
                    {'caption': ['one', 'two'], 'fps': [2, 4], 'format': ['gif', 'mp4']}),
                    ('not-a-list', {}), (['one'], {'caption': []}), (['one'], {'fps': None})]:
                logger = cls(experiment=ServiceRun(), prefix='validation')
                outcome = result(lambda: logger.log_video('clips', values, step, **kwargs))
                output['videos'].append([outcome, logger.experiment.calls])
        logger = cls()
        with patch.object(rank_zero_only, 'rank', 1):
            output['nonzero_rank'] = [logger.save(), logger.finalize('success'),
                logger.log_video('ignored', 'even-invalid'), cls.download_artifact('ignored')]
        output['nonzero_rank_initialized'] = logger._experiment is not None
    return normalize(output)


if __name__ == '__main__': print(json.dumps(compare(sys.argv[1]), sort_keys=True))
