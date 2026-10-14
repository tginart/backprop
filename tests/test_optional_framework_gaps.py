"""Close independent CPU framework gaps without hiding unavailable runtimes.

Architecture: static pinned signatures -> isolated real SDK/framework probes ->
native checkpoint recovery and hook cleanup -> evidence written for CI review.
Main callers: the existing test_optional_*.py matrix and focused local checks.
GPU training is independent of these CPU cases and is never used to skip them.
"""
import ast
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from framework_gap_process import run_observation

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {'xgboost': 'xgboost', 'lightgbm': 'lightgbm', 'catboost': 'catboost',
                'fastai': 'fastai', 'sb3': 'stable_baselines3'}
SETTINGS = dict(silent=True, console='off', disable_git=True, x_disable_stats=True)
os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')


def hooks(model):
    """Count installed observer hooks on real torch modules and parameters.

    Main callers: repeated-training and save-failure tests. Counts inspect
    PyTorch's actual registrations, rather than trusting a callback flag.
    """
    return sum(len(layer._forward_hooks) + len(layer._backward_hooks) for layer in model.modules()) + sum(
        len(parameter._backward_hooks or {}) for parameter in model.parameters())


def compare_family(family):
    def test(self):
        try:
            reference = importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('Pinned W&B 0.30.0 wheel is unavailable')
        self.assertEqual(reference.version, '0.30.0')
        observations = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-gap-{family}-{mode}.json'
            result = run_observation([sys.executable, '-B', str(ROOT / 'tests/framework_gap_probe.py'),
                mode, family], output, cwd=tempfile.gettempdir(), timeout=240,
                env={**os.environ, 'PYTHONHASHSEED': '0', 'OMP_NUM_THREADS': '1',
                     'OPENBLAS_NUM_THREADS': '1', 'WANDB_DATA_DIR': '/tmp/backprop-reference-data',
                     'PYTHONPATH': str(reference.locate_file('')) if mode == 'reference' else str(ROOT / 'sdk')})
            diagnostic = (result.stderr + b'\n' + result.stdout).decode('utf-8', errors='replace')
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertTrue(output.is_file(), diagnostic)
            observation = json.loads(output.read_text())
            observations.append(observation)
        expected = copy.deepcopy(observations[0]['portable'])
        actual = copy.deepcopy(observations[1]['portable'])
        if family == 'lightgbm':
            for prefix in expected:
                for name in ['cv_False', 'cv_True']:
                    before = expected[prefix]['cases'][name]
                    after = actual[prefix]['cases'][name]
                    if 'error' in before['result']:
                        self.assertEqual(before['result'], {'error': 'ValueError',
                            'message': 'not enough values to unpack (expected 2, got 1)'})
                        self.assertIn('value', after['result'])
                        expected[prefix]['cases'].pop(name)
                        actual[prefix]['cases'].pop(name)
        self.assertEqual(expected, actual)
        for prefix, group in observations[1]['portable'].items():
            with self.subTest(prefix=prefix):
                self.assertTrue(group['canonical_identity'])
                self.assertTrue(group['materialized_import_identity'])
                self.assertIn('error', group['errors']['before_init'])
                self.assertEqual(group['errors']['unknown_argument']['error'], 'TypeError')
                cases = group['cases']
                if family in ['xgboost', 'lightgbm', 'catboost']:
                    for name, case in cases.items():
                        if name.startswith('early_stop_'):
                            self.assertTrue(case['calls'])
                            self.assertTrue(any(call[0] == 'log' for call in case['calls']))
                            self.assertLess(case['best_iteration'], 7)
                            if family in ['catboost', 'lightgbm']:
                                self.assertEqual(case['summary_best_iteration'], case['best_iteration'])
                                self.assertEqual(case['summary_best_score'], case['best_score'])
                    if family == 'lightgbm':
                        for name in ['cv_False', 'cv_True']:
                            cv = cases[name]
                            self.assertIn('value', cv['result'])
                            for key, values in cv['result']['value'].items():
                                data_name, metric_name = key.split(' ', 1)
                                logged = [call[1][data_name + '_' + metric_name]
                                    for call in cv['calls'] if call[0] == 'log'
                                    and data_name + '_' + metric_name in call[1]]
                                self.assertEqual(logged[:len(values)], values)
                    if family == 'catboost':
                        self.assertTrue(all(cases['cv_1']['returns']))
                        for period, alias in [(1, 'best'), (2, 'last')]:
                            artifacts = [call for call in cases['early_stop_' + str(period)]['calls']
                                         if call[0] == 'artifact']
                            self.assertEqual(len(artifacts), 1)
                            self.assertEqual(artifacts[0][-1]['aliases'], [alias])
                            self.assertIsNone(cases['early_stop_' + str(period)]['skip_checkpoint_return'])
                    elif family == 'xgboost':
                        for name in ['before_False', 'before_True']:
                            self.assertTrue(cases[name]['before_identity'])
                            self.assertEqual(cases[name]['saved_config'], cases[name]['model_config'])
                        for name, case in cases.items():
                            if not name.startswith('early_stop_'):
                                continue
                            self.assertEqual(case['repeat_before']['error'], 'ConfigError')
                            self.assertEqual(case['rounds'], 3)
                            self.assertEqual(case['best_iteration'], 0)
                            self.assertTrue(case['after_identity'])
                            self.assertFalse(case['after_returns'])
                            logger_first = case['callback_order'][0] == 'logger'
                            expected_epochs = [0, 1, 2] if logger_first else [0, 1]
                            logged_epochs = [call[1]['epoch'] for call in case['calls']
                                             if call[0] == 'log' and 'epoch' in call[1]]
                            self.assertEqual(logged_epochs, [*expected_epochs, 12])
                            dispatched_epochs = [call[2] for call in case['dispatch']
                                if call[0] == 'after_iteration' and call[1] == 'logger']
                            self.assertEqual(dispatched_epochs, expected_epochs)
                            self.assertEqual([call for call in case['dispatch']
                                if call[0] == 'after_iteration' and call[-1]],
                                [['after_iteration', 'early_stopping', 2, True]])
                            lifecycle = [call for call in case['dispatch']
                                         if call[0] in ['before_training', 'after_training']]
                            self.assertEqual(lifecycle, [[stage, callback, True]
                                for stage in ['before_training', 'after_training']
                                for callback in case['callback_order']])
                        for name in ['cv_False', 'cv_True']:
                            self.assertEqual(cases[name]['result'], {'error': 'AttributeError',
                                'message': "'_PackedBooster' object has no attribute 'save_config'"})
                            self.assertEqual(cases[name]['calls'], [])
                        folds = cases['fold_metrics']
                        self.assertFalse(folds['return'])
                        for dataset, metrics in folds['evaluations'].items():
                            for metric, values in metrics.items():
                                logged = [call[1][dataset + '-' + metric] for call in folds['calls']
                                    if call[0] == 'log' and dataset + '-' + metric in call[1]]
                                self.assertEqual(logged, [values[-1]])
                if family == 'sb3':
                    self.assertEqual(cases['environments_1']['callback_calls'], 8)
                    self.assertEqual(cases['environments_2']['callback_calls'], 4)
                    for count in [1, 2]:
                        case = cases['environments_' + str(count)]
                        self.assertTrue(case['model_identity'])
                        self.assertEqual(case['prediction'], case['restored_prediction'])
                        self.assertIn('error', cases['save_failure_' + str(count)])
                if family == 'fastai':
                    for name in ['classification', 'segmentation']:
                        case = cases[name]
                        self.assertTrue(case['checkpoint'])
                        self.assertEqual(case['sample_count'], 2)
                        predictions = [call[1]['Prediction Samples'] for call in case['calls']
                                       if call[0] == 'log' and 'Prediction Samples' in call[1]]
                        self.assertEqual(len(predictions[-1]), 6 if name == 'segmentation' else 2)
                        self.assertTrue(all(image['pixels'] for image in predictions[-1]))
                    for name in ['explicit_False', 'explicit_True']:
                        self.assertTrue(cases[name]['same_validation_data'])
                        self.assertEqual(cases[name]['sample_count'], 2)
                    self.assertEqual(cases['cancellation']['result'],
                                     {'error': 'RuntimeError', 'message': 'cancelled training'})
    return test


class PinnedFrameworkComparisons(unittest.TestCase):
    pass


for _family, _dependency in DEPENDENCIES.items():
    setattr(PinnedFrameworkComparisons, 'test_' + _family,
        unittest.skipUnless(importlib.util.find_spec(_dependency), f'optional {_dependency} is unavailable')(
            compare_family(_family)))


class PinnedFrameworkSources(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'optional numpy is unavailable')
    def test_chart_observation_records_complete_real_pinned_spec_and_values(self):
        try:
            reference = importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('Pinned W&B 0.30.0 wheel is unavailable')
        self.assertEqual(reference.version, '0.30.0')
        script = '''
import json,os,sys
if sys.argv[1]=='reference':
 import wandb as w
 assert w.__version__=='0.30.0'
else:
 import backprop as w
sys.path.insert(0,sys.argv[2])
from framework_gap_values import portable
table=w.Table(columns=['Feature','Importance'],data=[['x',3.5],['y',1]])
chart=w.plot.bar(table,'Feature','Importance',title='Feature Importance')
os.write(1,b'framework training output before observation\\n')
os.write(2,b'framework diagnostic output\\n')
with open(sys.argv[3],'w') as output:
 output.write(json.dumps(portable(chart,w),sort_keys=True)+'\\n')
os.write(1,b'framework training output after observation\\n')
'''
        snapshots = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-gap-chart-{mode}.json'
            result = run_observation([sys.executable, '-B', '-c', script, mode, str(ROOT / 'tests')],
                output, cwd=tempfile.gettempdir(), timeout=30,
                env={**os.environ, 'PYTHONPATH': str(reference.locate_file(''))
                    if mode == 'reference' else str(ROOT / 'sdk')})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.with_suffix('.stdout.log').read_bytes(),
                b'framework training output before observation\nframework training output after observation\n')
            self.assertIn(b'framework diagnostic output\n', output.with_suffix('.stderr.log').read_bytes())
            snapshots.append(json.loads(output.read_text()))
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0]['spec'], {'spec_name': 'wandb/bar/v0',
            'fields': {'label': 'Feature', 'value': 'Importance'},
            'string_fields': {'title': 'Feature Importance'}, 'key': '',
            'panel_type': 'Vega2', 'split_table': False})
        self.assertEqual(snapshots[0]['table'], {'columns': ['Feature', 'Importance'],
                                              'data': [['x', 3.5], ['y', 1]]})

    def test_selected_parameter_names_defaults_kinds_match_pinned_source(self):
        """Keep source declaration evidence separate from runtime behavior.

        Main caller: local checks where optional frameworks are unavailable.
        Why this exists: dependency skips must not erase a detectable signature
        regression, but passing this check does not mark any behavior complete.
        """
        files = {'xgboost/xgboost.py', 'lightgbm/__init__.py', 'catboost/catboost.py',
                 'fastai/__init__.py', 'sb3/sb3.py'}
        reference_root = ROOT / '.reference/wandb/source/wandb/integration'
        if not reference_root.is_dir():
            self.skipTest('Pinned source checkout is unavailable')

        def definitions(path):
            tree = ast.parse(path.read_text())
            output = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
                    output[node.name] = node.args
                if isinstance(node, ast.ClassDef) and not node.name.startswith('_'):
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef) and (child.name == '__init__' or not child.name.startswith('_')):
                            output[node.name + '.' + child.name] = child.args
            for args in output.values():
                for arg in args.posonlyargs + args.args + args.kwonlyargs:
                    arg.annotation = None
                if args.vararg:
                    args.vararg.annotation = None
                if args.kwarg:
                    args.kwarg.annotation = None
            return {name: ast.dump(args, include_attributes=False) for name, args in output.items()}

        for filename in sorted(files):
            with self.subTest(filename=filename):
                self.assertEqual(definitions(reference_root / filename),
                                 definitions(ROOT / 'sdk/backprop/integration' / filename))


@unittest.skipUnless(importlib.util.find_spec('stable_baselines3'), 'optional stable_baselines3 is unavailable')
class SB3Cleanup(unittest.TestCase):
    def test_multiple_environments_repeated_learning_and_real_checkpoint_failures(self):
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_util import make_vec_env
        from backprop.integration.sb3 import WandbCallback
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR': directory}):
            with w.init(id='sb3-cleanup', entity='e', project='p', dir=directory, _sink=w.MemorySink(),
                        _deployment={}, settings=SETTINGS) as run:
                env = make_vec_env('CartPole-v1', n_envs=2, seed=7)
                try:
                    model = PPO('MlpPolicy', env, n_steps=4, batch_size=4, n_epochs=1,
                                seed=7, device='cpu', verbose=0)
                    callback = WandbCallback(model_save_path=directory, model_save_freq=2,
                                             gradient_save_freq=1, log='all')
                    for _ in range(2):
                        self.assertEqual(hooks(model.policy), 0)
                        model.learn(total_timesteps=8, callback=callback)
                        self.assertEqual(hooks(model.policy), 0)
                        self.assertFalse(callback._backprop_watched)
                    valid_path = callback.path
                    blocker = Path(directory) / 'blocked'
                    blocker.write_text('file')
                    callback.path = str(blocker / 'model.zip')
                    with self.assertRaises(OSError):
                        model.learn(total_timesteps=8, callback=callback)
                    self.assertEqual(hooks(model.policy), 0)
                    callback.path = valid_path
                    callback.init_callback(model)
                    self.assertGreater(hooks(model.policy), 0)
                    with patch.object(w, 'save', side_effect=w.Error('checkpoint publication failed')):
                        with self.assertRaisesRegex(w.Error, 'checkpoint publication failed'):
                            callback.save_model()
                    self.assertEqual(hooks(model.policy), 0)
                    model.learn(total_timesteps=8, callback=callback)
                    self.assertEqual(hooks(model.policy), 0)
                    run.flush()
                    saved = w.Api._from_store(run._sink, entity='e', project='p').run(run.id).files()
                    self.assertTrue(any(file.name.endswith('.zip') for file in saved))
                finally:
                    env.close()


@unittest.skipUnless(importlib.util.find_spec('fastai'), 'optional fastai 1 is unavailable')
class FastaiCleanup(unittest.TestCase):
    def test_failed_segmentation_panel_closes_figure_warns_and_keeps_loss_logging(self):
        import matplotlib.pyplot as plt
        from framework_gap_fastai_fixture import vision_learner
        from backprop.integration.fastai import WandbCallback
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR': directory}):
            with w.init(id='fastai-panels', entity='e', project='p', dir=directory, _sink=w.MemorySink(),
                        _deployment={}, settings=SETTINGS) as run:
                learner = vision_learner(directory, segmentation=True)
                callback = WandbCallback(learner, log=None, save_model=False, input_type='images', predictions=1)
                learner.fit(1, callbacks=[callback])
                self.assertIsNotNone(callback.validation_data)
                x, _ = callback.validation_data[0]
                before = plt.get_fignums()
                original_show = type(x).show

                def fail_after_show(image, *args, **kwargs):
                    original_show(image, *args, **kwargs)
                    raise RuntimeError('prediction panel failed')

                with patch.object(type(x), 'show', fail_after_show), patch.object(w, 'termwarn') as warning:
                    self.assertIsNone(callback.on_epoch_end(epoch=1, smooth_loss=.5, last_metrics=[.5]))
                self.assertIsNone(callback.validation_data)
                self.assertEqual(plt.get_fignums(), before)
                self.assertIn('prediction panel failed', warning.call_args.args[0])
                run.flush()
                history = w.Api._from_store(run._sink, entity='e', project='p').run(run.id).history(pandas=False)
                self.assertTrue(any(row.get('epoch') == 1 and row.get('train_loss') == .5 for row in history))

    def test_exceptional_cancellation_releases_real_torch_hooks_and_can_repeat(self):
        from framework_gap_fastai_fixture import tensor_learner
        from fastai.callback import Callback
        from backprop.integration.fastai import WandbCallback
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR': directory}):
            with w.init(id='fastai-cleanup', entity='e', project='p', dir=directory, _sink=w.MemorySink(),
                        _deployment={}, settings=SETTINGS):
                learner = tensor_learner(directory)
                callback = WandbCallback(learner, log='all', save_model=False)

                class Cancel(Callback):
                    def on_batch_begin(self, train, **kwargs):
                        if train:
                            raise RuntimeError('cancelled training')

                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'cancelled training'):
                        learner.fit(1, callbacks=[callback, Cancel()])
                    self.assertFalse(callback._backprop_watched)
                    self.assertEqual(hooks(learner.model), 0)
                learner.fit(1, callbacks=[callback])
                self.assertEqual(hooks(learner.model), 0)

    def test_failed_checkpoint_write_preserves_best_model_and_original_error(self):
        from framework_gap_fastai_fixture import tensor_learner
        from backprop.integration.fastai import WandbCallback
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR': directory}):
            with w.init(id='fastai-save', entity='e', project='p', dir=directory, _sink=w.MemorySink(),
                        _deployment={}, settings=SETTINGS):
                learner = tensor_learner(directory)
                callback = WandbCallback(learner, log='all', save_model=True)
                learner.fit(1, callbacks=[callback])
                before = callback.model_path.read_bytes()
                original_save = learner.save

                def failing_save(file, *args, **kwargs):
                    original_save(file, *args, **kwargs)
                    raise OSError('checkpoint disk failure')

                with patch.object(callback, 'get_monitor_value', return_value=-1), \
                        patch.object(learner, 'save', failing_save):
                    with self.assertRaisesRegex(OSError, 'checkpoint disk failure'):
                        learner.fit(1, callbacks=[callback])
                self.assertEqual(callback.model_path.read_bytes(), before)
                self.assertEqual(list(callback.model_path.parent.glob('bestmodel-*.pth')), [])
                self.assertEqual(hooks(learner.model), 0)


if __name__ == '__main__':
    unittest.main()
