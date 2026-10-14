"""Observe real training frameworks through isolated pinned/native SDK imports.

Architecture: deterministic CPU data -> actual framework training and public
callbacks -> genuine SDK logging with recorded arguments -> portable JSON.
Main caller: test_optional_framework_gaps. Public logging is wrapped, never
replaced, so validation, checkpoint creation and journal writes still execute.
Missing framework dependencies are handled by the parent test, never stubbed.
"""
from contextlib import contextmanager, redirect_stdout
from functools import partial
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

import numpy as np

REFERENCE = sys.argv[1] == 'reference'
FAMILY = sys.argv[2]
OUTPUT = Path(sys.argv[3])
if REFERENCE:
    import wandb as w
    assert w.__version__ == '0.30.0'
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {
    'xgboost': ['wandb.integration.xgboost', 'wandb.xgboost'],
    'lightgbm': ['wandb.integration.lightgbm', 'wandb.lightgbm'],
    'catboost': ['wandb.integration.catboost', 'wandb.catboost'],
    'fastai': ['wandb.integration.fastai'],
    'sb3': ['wandb.integration.sb3'],
}
PUBLIC = {
    'xgboost': ['WandbCallback', 'WandbCallback.before_training',
                'WandbCallback.after_iteration', 'WandbCallback.after_training', 'wandb_callback'],
    'lightgbm': ['wandb_callback', 'log_summary'],
    'catboost': ['WandbCallback', 'WandbCallback.after_iteration', 'log_summary'],
    'fastai': ['FastaiError', 'WandbCallback', 'WandbCallback.on_train_begin',
               'WandbCallback.on_epoch_end', 'WandbCallback.on_train_end'],
    'sb3': ['WandbCallback', 'WandbCallback.save_model'],
}


from framework_gap_values import portable as _portable
portable = partial(_portable, sdk=w)


def attempt(call):
    try:
        return {'value': portable(call())}
    except Exception as error:
        name = 'Error' if type(error) is w.Error else type(error).__name__
        return {'error': name, 'message': str(error)}


@contextmanager
def session(identity):
    """Start an actual run and record the calls that reach its logging boundary.

    Main callers: all framework cases. Official W&B runs offline; Backprop uses
    its normal MemorySink and durable local journal, with no hosted services.
    """
    with tempfile.TemporaryDirectory() as directory:
        settings = dict(silent=True, console='off', disable_git=True, x_disable_stats=True)
        options = {} if REFERENCE else {'_sink': w.MemorySink(), '_deployment': {}}
        mode = 'offline' if REFERENCE else 'online'
        with patch.dict(os.environ, {'BACKPROP_DIR': directory, 'WANDB_CACHE_DIR': directory + '/cache'}):
            with w.init(id=identity, entity='e', project='framework-gap', dir=directory,
                        mode=mode, settings=settings, **options) as run:
                calls = []
                log, define, artifact = w.log, w.define_metric, w.log_artifact

                def record_log(values, *args, **kwargs):
                    calls.append(['log', portable(values), portable(kwargs)])
                    return log(values, *args, **kwargs)

                def record_define(name, *args, **kwargs):
                    calls.append(['define_metric', name, portable(kwargs)])
                    return define(name, *args, **kwargs)

                def record_artifact(value, *args, **kwargs):
                    entries = value.manifest.entries
                    calls.append(['artifact', value.name, value.type, sorted(entries), portable(kwargs)])
                    return artifact(value, *args, **kwargs)

                with patch.object(w, 'log', record_log), patch.object(w, 'define_metric', record_define), \
                        patch.object(w, 'log_artifact', record_artifact), redirect_stdout(io.StringIO()):
                    yield run, calls, directory


def xgboost_cases(module):
    import xgboost as xgb
    from framework_gap_xgboost_order import ordered_callbacks
    data = np.arange(160, dtype=np.float32).reshape(40, 4)
    labels = np.arange(40) % 2
    result = {}
    for enabled in [False, True]:
        for logger_first in [True, False]:
            with session('xgb') as (run, calls, directory):
                train = xgb.DMatrix(data, label=labels)
                callback = module.WandbCallback(log_model=enabled, log_feature_importance=enabled,
                                                importance_type='weight', define_metric=enabled)
                stop = xgb.callback.EarlyStopping(rounds=2)
                callbacks = [('logger', callback), ('early_stopping', stop)]
                if not logger_first:
                    callbacks.reverse()
                dispatch = []
                composed = ordered_callbacks(xgb, callbacks, dispatch)
                params = {'objective': 'binary:logistic', 'nthread': 1, 'seed': 7, 'eta': 0,
                          'eval_metric': ['logloss', 'auc'], 'tree_method': 'hist'}
                model = xgb.train(params, train, num_boost_round=8, evals=[(train, 'valid')],
                                  callbacks=[composed], verbose_eval=False)
                name = 'early_stop_' + str(enabled) + ('' if logger_first else '_stop_first')
                result[name] = {
                    'calls': calls, 'dispatch': dispatch,
                    'callback_order': [label for label, _ in callbacks],
                    'best_iteration': model.best_iteration, 'best_score': model.best_score,
                    'rounds': model.num_boosted_rounds(), 'prediction': portable(model.predict(train)),
                    'repeat_before': attempt(lambda: callback.before_training(model) is model),
                    'after_identity': callback.after_training(model) is model,
                    'after_returns': callback.after_iteration(model, 12, {'valid': {'logloss': [0.5]}}),
                }
        with session('xgb-before') as (run, calls, directory):
            callback = module.WandbCallback(log_feature_importance=False)
            result['before_' + str(enabled)] = {
                'before_identity': callback.before_training(model) is model,
                'saved_config': portable(run.config['learner']),
                'model_config': json.loads(model.save_config())['learner'],
            }
        with session('xgb-cv') as (run, calls, directory):
            callback = module.WandbCallback(log_feature_importance=False, define_metric=enabled)
            result['cv_' + str(enabled)] = {
                'result': attempt(lambda: xgb.cv(params, xgb.DMatrix(data, label=labels),
                    num_boost_round=8, nfold=2, seed=7, early_stopping_rounds=2,
                    callbacks=[callback], as_pandas=False, verbose_eval=False)), 'calls': calls}
    with session('xgb-fold-metrics') as (run, calls, directory):
        # The before_training CV container is outside the Booster contract.
        # after_iteration separately declares tuple-valued score lists, which
        # can be populated from genuine cv results and sent with a real Booster.
        cv = xgb.cv(params, xgb.DMatrix(data, label=labels), num_boost_round=3,
                    nfold=2, seed=7, as_pandas=False, verbose_eval=False)
        evaluations = {}
        for key, means in cv.items():
            if key.endswith('-mean'):
                base = key[:-5]
                data_name, metric_name = base.split('-', 1)
                evaluations.setdefault(data_name, {})[metric_name] = list(zip(means, cv[base + '-std']))
        callback = module.WandbCallback(log_feature_importance=False, define_metric=False)
        result['fold_metrics'] = {'calls': calls, 'evaluations': portable(evaluations),
                                 'return': callback.after_iteration(model, 0, evaluations)}
    with session('xgb-old') as (run, calls, directory):
        with __import__('warnings').catch_warnings(record=True) as warnings:
            callback = module.wandb_callback()
        from types import SimpleNamespace
        returned = callback(SimpleNamespace(evaluation_result_list=[('valid-error', 0.25)]))
        result['legacy'] = {'return': returned, 'calls': calls,
                            'warnings': [str(item.message) for item in warnings]}
    return result


def lightgbm_cases(module):
    import lightgbm as lgb
    data = np.arange(160, dtype=np.float32).reshape(40, 4)
    labels = np.arange(40) % 2
    params = {'objective': 'binary', 'verbosity': -1, 'num_threads': 1,
              'min_data_in_leaf': 1, 'seed': 7, 'metric': ['binary_logloss', 'auc']}
    result = {}
    for enabled in [False, True]:
        with session('lgb') as (run, calls, directory):
            train = lgb.Dataset(data, label=labels)
            validation = lgb.Dataset(data, label=1-labels, reference=train)
            model = lgb.train(params, train, num_boost_round=10, valid_sets=[validation],
                valid_names=['valid'], callbacks=[module.wandb_callback(enabled, enabled),
                                                lgb.early_stopping(2, verbose=False)])
            returned = module.log_summary(model, feature_importance=enabled, save_model_checkpoint=enabled)
            result['early_stop_' + str(enabled)] = {'calls': calls, 'return': returned,
                'best_iteration': model.best_iteration, 'best_score': portable(model.best_score),
                'summary_best_iteration': run.summary['best_iteration'],
                'summary_best_score': portable(run.summary['best_score']),
                'prediction': portable(model.predict(data)),
                'config_present': 'objective' in run.config,
                'invalid_model': attempt(lambda: module.log_summary(object()))}
        with session('lgb-cv') as (run, calls, directory):
            result['cv_' + str(enabled)] = {'result': attempt(lambda: lgb.cv(params,
                lgb.Dataset(data, label=labels), num_boost_round=10, nfold=2, seed=7,
                eval_train_metric=True, callbacks=[module.wandb_callback(enabled, enabled),
                                                   lgb.early_stopping(2, verbose=False)])), 'calls': calls}
    return result


def catboost_cases(module):
    from catboost import CatBoostClassifier, Pool, cv
    data = np.arange(160, dtype=np.float32).reshape(40, 4)
    labels = np.arange(40) % 2
    result = {}
    for period in [1, 2]:
        with session('cat') as (run, calls, directory):
            model = CatBoostClassifier(iterations=10, thread_count=1, random_seed=7,
                verbose=False, allow_writing_files=False, metric_period=period, use_best_model=period == 1)
            callback = module.WandbCallback(metric_period=period)
            model.fit(data, labels, eval_set=(data, 1-labels), early_stopping_rounds=2,
                      callbacks=[callback])
            skip_checkpoint = module.log_summary(model, log_all_params=False,
                save_model_checkpoint=False, log_feature_importance=False)
            returned = module.log_summary(model, log_all_params=period == 1,
                save_model_checkpoint=True, log_feature_importance=period == 1)
            result['early_stop_' + str(period)] = {'calls': calls, 'return': returned,
                'skip_checkpoint_return': skip_checkpoint,
                'best_iteration': model.get_best_iteration(), 'best_score': portable(model.get_best_score()),
                'summary_best_iteration': run.summary['best_iteration'],
                'summary_best_score': portable(run.summary['best_score']),
                'prediction': portable(model.predict(data)), 'config_present': 'iterations' in run.config,
                'invalid_model': attempt(lambda: module.log_summary(object()))}
        with session('cat-cv') as (run, calls, directory):
            observed = cv(Pool(data, labels), params={'iterations': 4, 'loss_function': 'Logloss',
                'thread_count': 1, 'random_seed': 7, 'verbose': False, 'allow_writing_files': False},
                fold_count=2, partition_random_seed=7, as_pandas=False)
            # CatBoost cv has no callback parameter. Feed its actual fold-mean
            # results through the public callback's documented info contract.
            from types import SimpleNamespace
            callback = module.WandbCallback(period)
            returns = []
            for index in range(len(observed['iterations'])):
                info = SimpleNamespace(iteration=index + 1, metrics={
                    'validation': {'Logloss': observed['test-Logloss-mean'][:index + 1]}})
                returns.append(callback.after_iteration(info))
            result['cv_' + str(period)] = {'calls': calls, 'result': portable(observed), 'returns': returns}
    return result


def sb3_cases(module):
    import torch
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_util import make_vec_env
    result = {}
    for count in [1, 2]:
        with session('sb3') as (run, calls, directory):
            env = make_vec_env('CartPole-v1', n_envs=count, seed=7)
            try:
                model = PPO('MlpPolicy', env, n_steps=4, batch_size=4, n_epochs=1,
                            seed=7, device='cpu', verbose=0)
                callback = module.WandbCallback(model_save_path=directory, model_save_freq=2,
                                                gradient_save_freq=0, log=None)
                returned = model.learn(total_timesteps=8, callback=callback)
                restored = PPO.load(Path(directory) / 'model.zip', env=env, device='cpu')
                observations = np.zeros((count, 4), dtype=np.float32)
                result['environments_' + str(count)] = {
                    'model_identity': returned is model, 'num_timesteps': model.num_timesteps,
                    'callback_calls': callback.n_calls, 'return': callback.save_model(),
                    'config': {key: portable(run.config[key]) for key in ['algo', 'n_steps', 'batch_size']},
                    'prediction': portable(model.predict(observations, deterministic=True)[0]),
                    'restored_prediction': portable(restored.predict(observations, deterministic=True)[0]),
                    'files': sorted(path.name for path in Path(directory).glob('*.zip')),
                }
                blocked = Path(directory) / 'blocked'
                blocked.write_text('this is a file')
                callback.path = str(blocked / 'model.zip')
                result['save_failure_' + str(count)] = attempt(callback.save_model)
                result['save_failure_' + str(count)]['message'] = re.sub(
                    re.escape(directory), '<root>', result['save_failure_' + str(count)].get('message', ''))
            finally:
                env.close()
    return result


def fastai_cases(module):
    from fastai.callback import Callback
    from framework_gap_fastai_fixture import tensor_learner, vision_learner
    result = {}
    for segmentation in [False, True]:
        with session('fastai') as (run, calls, directory):
            learner = vision_learner(directory, segmentation)
            callback = module.WandbCallback(learner, log=None, save_model=True,
                input_type='images', predictions=2, seed=3)
            returned = learner.fit(1, callbacks=[callback])
            result['segmentation' if segmentation else 'classification'] = {'calls': calls,
                'return': returned, 'sample_count': len(callback.validation_data),
                'checkpoint': callback.model_path.is_file()}
            explicit = module.WandbCallback(learner, log=None, save_model=False, monitor='valid_loss',
                mode='min', input_type='images', validation_data=callback.validation_data,
                predictions=1, seed=99)
            result['explicit_' + str(segmentation)] = {
                'same_validation_data': explicit.validation_data is callback.validation_data,
                'sample_count': len(explicit.validation_data),
                'monitor': explicit.monitor, 'mode': explicit.mode,
                'prediction_return': explicit._wandb_log_predictions(),
            }
        with session('fastai-cancel') as (run, calls, directory):
            learner = tensor_learner(directory)
            callback = module.WandbCallback(learner, log=None, save_model=False)

            class Cancel(Callback):
                def on_batch_begin(self, train, **kwargs):
                    if train:
                        raise RuntimeError('cancelled training')

            result['cancellation'] = {'result': attempt(lambda: learner.fit(1,
                callbacks=[callback, Cancel()])), 'calls': calls}
    error = module.FastaiError('prediction failed', {'stage': 'prediction'})
    result['error_fields'] = {'message': error.message, 'context': error.context, 'args': list(error.args)}
    return result


def signatures(module):
    """Compare all named parameters at each public import independently.

    Main caller: the process driver. Annotations point at intentional platform
    substitutions; names, defaults and parameter kinds are compared exactly.
    """
    result = {}
    for name in PUBLIC[FAMILY]:
        value = module
        for part in name.split('.'):
            value = getattr(value, part)
        result[name] = [[p.name, p.kind.name, None if p.default is inspect.Parameter.empty else p.default]
                        for p in inspect.signature(value).parameters.values()]
    return result


cases = globals()[FAMILY + '_cases']
observed = {}
canonical = importlib.import_module(ALIASES[FAMILY][0])
for prefix in ALIASES[FAMILY]:
    # Pinned top-level framework exports are LazyLoader attributes. Access the
    # declared namespace first, then verify its materialized import path too.
    module = getattr(w, FAMILY) if prefix == 'wandb.' + FAMILY else importlib.import_module(prefix)
    identity = all(getattr(module, name.split('.')[0]) is getattr(canonical, name.split('.')[0])
                   for name in PUBLIC[FAMILY])
    import_identity = all(getattr(importlib.import_module(prefix), name.split('.')[0]) is
                          getattr(canonical, name.split('.')[0]) for name in PUBLIC[FAMILY])
    errors = {}
    if FAMILY in ['xgboost', 'catboost', 'sb3']:
        errors['before_init'] = attempt(module.WandbCallback)
        errors['unknown_argument'] = attempt(lambda: module.WandbCallback(unknown=True))
    elif FAMILY == 'fastai':
        errors['before_init'] = attempt(lambda: module.WandbCallback(None))
        errors['unknown_argument'] = attempt(lambda: module.WandbCallback(None, unknown=True))
    else:
        errors['before_init'] = attempt(lambda: module.log_summary(object()))
        errors['unknown_argument'] = attempt(lambda: module.wandb_callback(unknown=True))
    observed[prefix] = {'signatures': signatures(module), 'canonical_identity': identity,
                        'materialized_import_identity': import_identity,
                        'errors': errors, 'cases': cases(module)}
evidence = {}
if FAMILY == 'xgboost':
    import xgboost as xgb
    container = inspect.getsource(xgb.callback.CallbackContainer)
    source = Path(inspect.getsourcefile(xgb.callback.CallbackContainer))
    evidence = {'runtime_version': xgb.__version__, 'callback_source_sha256': hashlib.sha256(
        source.read_bytes()).hexdigest(), 'callback_container_source': container}
OUTPUT.write_text(json.dumps({'family': FAMILY, 'sdk': w.__version__, 'portable': observed,
                             'runtime_source_evidence': evidence}, sort_keys=True) + '\n')
