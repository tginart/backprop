"""Observe public init switches through genuine isolated pinned/native SDKs.

Architecture: selected scenario -> actual Settings/init entry point -> real
disabled/offline lifecycle or explicitly marked service-dispatch boundary ->
caller-provided JSON file. Diagnostics never share the JSON transport.
Main caller: test_framework_init_contracts.py. Why this exists: framework
callback parity does not establish the init switches that install callbacks.
Service fixtures observe dispatch only; they never claim hosted run behavior.
"""
from contextlib import ExitStack
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

REFERENCE = sys.argv[1] == 'reference'
SCENARIO = sys.argv[2]
OUTPUT = Path(sys.argv[3])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk
IMPORT_PATH = os.environ.get('FRAMEWORK_INIT_IMPORT', 'default')
if IMPORT_PATH != 'default':
    import wandb as sdk
INIT = importlib.import_module('wandb.sdk').init if IMPORT_PATH == 'wandb.sdk' else sdk.init

SETTINGS = {'silent': True, 'console': 'off', 'disable_git': True,
            'x_disable_stats': True, 'init_timeout': 10}
MODES = ['online', 'offline', 'shared', 'disabled', 'dryrun', 'run']
POLICIES = ['default', 'return_previous', 'finish_previous', 'create_new', True, False]


def attempt(call):
    """Retain exact original error type and message, including failed state.

    Main callers: invalid settings, integration errors and boundary fixtures.
    Root SDK Error names are normalized only for the intentional native alias;
    subclasses, validation errors and all messages remain exact.
    """
    try:
        return {'value': call()}
    except Exception as error:
        name = 'Error' if type(error) is sdk.Error else type(error).__name__
        return {'error': name, 'message': str(error)}


def options(directory, mode):
    result = {'id': 'init-probe', 'entity': 'e', 'project': 'framework-init',
              'dir': directory, 'mode': mode, 'settings': dict(SETTINGS)}
    if not REFERENCE:
        result.update(_sink=sdk.MemorySink(), _deployment={})
    return result


def settings_cases():
    result = {'default': sdk.Settings().mode, 'valid': {}, 'invalid': {},
              'unknown_init_argument': attempt(lambda: sdk.init(unknown_init_option=True)),
              'too_many_init_arguments': attempt(lambda: sdk.init(*([None] * 27))),
              'switch_fields': {'default_sync_tensorboard': sdk.Settings().sync_tensorboard,
                  'default_reinit': sdk.Settings().reinit, 'sync_tensorboard': {}, 'reinit': {}}}
    for field, values in [('sync_tensorboard', [True, False, None]), ('reinit', POLICIES)]:
        for value in values:
            constructed = sdk.Settings(**{field: value})
            assigned = sdk.Settings()
            setattr(assigned, field, value)
            result['switch_fields'][field][repr(value)] = {
                'constructed': getattr(constructed, field), 'assigned': getattr(assigned, field)}
    for mode in MODES:
        value = sdk.Settings(mode=mode)
        other = sdk.Settings()
        other.mode = mode
        result['valid'][mode] = {'constructed': value.mode, 'assigned': other.mode}
    for mode in ['invalid', '', None, True, 7, ['online']]:
        key = repr(mode)
        value = sdk.Settings(mode='offline')
        result['invalid'][key] = {'construct': attempt(lambda: sdk.Settings(mode=mode).mode),
            'assign': attempt(lambda: setattr(value, 'mode', mode)), 'mode_after': value.mode}
    return result


def defaults_cases(directory):
    """Measure absent metadata and disabled constructor-derived defaults.

    Main caller: whole-init default review. Settings supplied by the caller
    remain separate from the actual returned Run settings. A real Git scratch
    repository supplies project inference; no commit or remote is created.
    Raw paths are retained alongside their relative location classification.
    """
    import subprocess
    result = {}
    root = Path(directory) / 'inference-repository'
    root.mkdir()
    (root / 'training').mkdir()
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
    old_directory = os.getcwd()
    profiles = [
        ('disabled_absent', 'disabled', directory, {}),
        ('disabled_uncategorized', 'disabled', directory, {'project': 'uncategorized'}),
        ('disabled_named', 'disabled', directory, {'project': 'named', 'entity': 'owner', 'name': 'display'}),
        ('offline_absent', 'offline', directory, {}),
        ('offline_repo', 'offline', root, {'settings': {'disable_git': False}}),
        ('offline_repo_program', 'offline', root, {'settings': {'disable_git': False, 'program': 'training/train.py'}}),
        ('disabled_repo', 'disabled', root, {'settings': {'disable_git': False}}),
        ('offline_git_privacy', 'offline', root, {}),
        ('disabled_git_privacy', 'disabled', root, {}),
    ]
    try:
        for name, mode, working_directory, overrides in profiles:
            sdk.teardown()
            os.chdir(working_directory)
            settings = sdk.Settings(**{**SETTINGS, **overrides.pop('settings', {})}, root_dir=directory)
            arguments = {'mode': mode, 'id': name, 'settings': settings, **overrides}
            if not REFERENCE:
                arguments.update(_sink=sdk.MemorySink(), _deployment={})
            run = sdk.init(**arguments)
            result[name] = {
                'project': run.project, 'entity': run.entity, 'name': run.name,
                'root_is_requested': Path(run.settings.root_dir) == Path(directory),
                'root_is_system_temp': Path(run.settings.root_dir) == Path(tempfile.gettempdir()),
                'input_project': settings.project, 'input_entity': settings.entity,
                'input_name': settings.run_name, 'input_root_is_requested': Path(settings.root_dir) == Path(directory),
                'global_identity': sdk.run is run,
            }
            # Preserve every original path; comparison uses the explicit root
            # classifications above because independent process scratch roots
            # are intentionally different.
            print(json.dumps({'case': name, 'raw_run_root': run.settings.root_dir,
                              'raw_input_root': settings.root_dir}, sort_keys=True))
            run.finish()
            result[name]['global_empty'] = sdk.run is None
    finally:
        os.chdir(old_directory)
    return result


def disabled_cases(directory):
    result = {'switches': {}, 'reinit': {}}
    for arguments in [{}, {'monitor_gym': True}, {'sync_tensorboard': True},
                      {'tensorboard': True}, {'monitor_gym': True, 'sync_tensorboard': True}]:
        sdk.teardown()
        current = options(directory, 'disabled')
        current.update(arguments)
        def observe():
            run = sdk.init(**current)
            observed = {'disabled': run.disabled, 'mode': run.settings.mode,
                'sync_tensorboard': run.settings.sync_tensorboard, 'global_identity': sdk.run is run,
                'config': dict(run.config), 'log_return': run.log({'metric': 4}), 'step': run.step}
            run.finish()
            observed['global_after_finish'] = sdk.run is None
            return observed
        result['switches'][json.dumps(arguments, sort_keys=True)] = attempt(observe)
    for policy in POLICIES:
        sdk.teardown()
        first = sdk.init(**{**options(directory, 'disabled'), 'id': 'first'})
        second = sdk.init(**{**options(directory, 'disabled'), 'id': 'second', 'reinit': policy})
        observed = {'same': first is second, 'first_id': first.id, 'second_id': second.id,
                    'global_first': sdk.run is first, 'global_second': sdk.run is second}
        first.finish()
        observed['global_second_after_first_finish'] = sdk.run is second
        second.finish()
        observed['global_empty_after_second_finish'] = sdk.run is None
        result['reinit'][repr(policy)] = observed
    return result


class ServiceBoundaryReached(RuntimeError):
    """Stop at the service boundary after real argument and switch processing."""


def dispatch_cases(directory):
    """Observe only the public init branches preceding backend construction.

    Main caller: service-dispatch comparison for online/shared and aliases.
    Why this exists: W&B hosted auth/backend requests are outside this local
    fixture. SDK validation and switch selection execute unchanged; only
    maybe_login and the final backend constructor are intercepted. Gym/TB
    spies record adapter selection, independently of their excluded internals.
    """
    gym = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.gym')
    tb = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.tensorboard')
    result = {}
    arguments = [{}, {'sync_tensorboard': True}, {'tensorboard': True},
                 {'tensorboard': True, 'sync_tensorboard': False},
                 {'tensorboard': False, 'sync_tensorboard': True}, {'monitor_gym': True},
                 {'monitor_gym': True, 'sync_tensorboard': True},
                 {'settings': {**SETTINGS, 'sync_tensorboard': True}, 'sync_tensorboard': False}]
    for mode in MODES:
        for argument in arguments:
            sdk.teardown()
            calls = []
            boundary = {}
            native_constructor = None if REFERENCE else sdk.Run
            def construct(*args, **kwargs):
                selected = args[1] if REFERENCE else kwargs['settings']
                if not REFERENCE and selected.mode == 'disabled':
                    return native_constructor(*args, **kwargs)
                boundary.update(mode=selected.mode, sync_tensorboard=selected.sync_tensorboard)
                calls.append('backend')
                raise ServiceBoundaryReached('service boundary reached')
            with ExitStack() as stack:
                stack.enter_context(patch.object(gym, 'monitor', side_effect=lambda: calls.append('gym')))
                stack.enter_context(patch.object(tb, 'patch', side_effect=lambda *a, **k: calls.append('tensorboard')))
                if REFERENCE:
                    init_module = importlib.import_module('wandb.sdk.wandb_init')
                    stack.enter_context(patch.object(init_module._WandbInit, 'maybe_login', return_value=None))
                    stack.enter_context(patch.object(init_module._WandbInit, 'init', new=construct))
                else:
                    stack.enter_context(patch.object(sdk, 'Run', new=construct))
                observed = attempt(lambda: sdk.init(**{**options(directory, mode), **argument}) is not None)
            # A genuine disabled run returns before the backend fixture. Finish
            # it after capturing selection, just as the public API requires.
            if sdk.run is not None:
                sdk.run.finish()
            result[mode + ':' + json.dumps(argument, sort_keys=True)] = {
                'result': observed, 'calls': calls, 'boundary': boundary, 'global_empty': sdk.run is None}
    return result


def offline_cases(directory):
    """Exercise actual offline Go-core and native Run reinit with no service.

    Main caller: local socket-authorized or ordinary CI focused tests. The
    returned/global identities and finish transitions are public behavior;
    persistence schemas and private finished flags are deliberately omitted.
    """
    result = {'reinit': {}, 'mode_precedence': {}, 'disabled_transition': {}}
    for policy in POLICIES:
        sdk.teardown()
        first = sdk.init(**{**options(directory, 'offline'), 'id': 'first'})
        second = sdk.init(**{**options(directory, 'offline'), 'id': 'second', 'reinit': policy})
        observed = {'same': first is second, 'second_id': second.id,
                    'global_first': sdk.run is first, 'global_second': sdk.run is second}
        first.finish()
        observed['global_second_after_first_finish'] = sdk.run is second
        second.finish()
        observed['global_empty_after_second_finish'] = sdk.run is None
        result['reinit'][repr(policy)] = observed
    for settings_mode, explicit in [('online', 'offline'), ('offline', 'disabled'),
                                    ('shared', 'disabled'), ('dryrun', None)]:
        sdk.teardown()
        selected = sdk.Settings(**{**SETTINGS, 'mode': settings_mode})
        args = {**options(directory, explicit), 'settings': selected}
        run = sdk.init(**args)
        result['mode_precedence'][settings_mode + ':' + repr(explicit)] = {
            'run_mode': run.settings.mode, 'input_settings_mode_after': selected.mode,
            'disabled': run.disabled, 'offline': run.offline}
        run.finish()
    for policy in ['return_previous', 'finish_previous', 'create_new']:
        sdk.teardown()
        first = sdk.init(**{**options(directory, 'offline'), 'id': 'first'})
        second = sdk.init(**{**options(directory, 'disabled'), 'id': 'second', 'reinit': policy})
        result['disabled_transition'][policy] = {'same': first is second,
            'second_disabled': second.disabled, 'global_second': sdk.run is second}
        second.finish()
        first.finish()
    return result


def adapter_failure_cases(directory):
    """Raise at the actual selected adapter boundary and inspect cleanup.

    Main caller: failed-init contract comparison. The intentional failure is a
    caller-visible adapter error, not a fake run or fake framework. Both SDKs
    must propagate it before establishing a global run; native MemorySink
    events expose any premature durable run start independently.
    """
    gym = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.gym')
    tb = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.tensorboard')
    result = {}
    for mode in ['offline', 'disabled', 'online', 'shared']:
        for family, keyword in [('gym', 'monitor_gym'), ('tensorboard', 'sync_tensorboard')]:
            sdk.teardown()
            arguments = options(directory, mode)
            arguments[keyword] = True
            target, method = (gym, 'monitor') if family == 'gym' else (tb, 'patch')
            with ExitStack() as stack:
                stack.enter_context(patch.object(target, method,
                    side_effect=RuntimeError(f'{family} init adapter failure')))
                if REFERENCE:
                    init_module = importlib.import_module('wandb.sdk.wandb_init')
                    stack.enter_context(patch.object(init_module._WandbInit, 'maybe_login', return_value=None))
                observed = attempt(lambda: sdk.init(**arguments) is not None)
            result[mode + ':' + family] = {'result': observed, 'global_empty': sdk.run is None,
                'durable_start_count': 0 if REFERENCE else sum(event['type'] == 'run_start'
                    for event in arguments['_sink'].events)}
            if sdk.run is not None:
                sdk.run.finish()
    return result


def adapter_failure_previous_cases(directory):
    """Verify a failed integration selection preserves an existing live run.

    Main caller: real offline init cleanup comparison. Pinned init installs
    requested integrations before evaluating active-run reinit policy. The
    previous run must therefore still accept ordinary public log calls when a
    selected adapter raises, including with finish_previous requested.
    """
    gym = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.gym')
    tb = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.tensorboard')
    result = {}
    for policy in ['return_previous', 'finish_previous', 'create_new']:
        for family, keyword in [('gym', 'monitor_gym'), ('tensorboard', 'sync_tensorboard')]:
            sdk.teardown()
            first = sdk.init(**{**options(directory, 'offline'), 'id': 'first'})
            target, method = (gym, 'monitor') if family == 'gym' else (tb, 'patch')
            with patch.object(target, method, side_effect=RuntimeError(f'{family} init adapter failure')):
                outcome = attempt(lambda: sdk.init(**{**options(directory, 'offline'),
                    'id': 'second', keyword: True, 'reinit': policy}) is first)
            result[policy + ':' + family] = {'result': outcome, 'global_previous': sdk.run is first,
                'previous_log': attempt(lambda: first.log({'still_alive': 1}))}
            sdk.teardown()
    return result


def allow_val_change_cases(directory):
    """Expose the pinned ignored init argument and the native explicit repair.

    Main caller: the allow_val_change repair contract test. Disabled and offline
    runs retain exact supplied/returned Settings values. Actual numeric/nested
    config edits run offline: pinned retains the original Settings field and
    ignores it during edits as well as ignoring the init argument. Native
    applies the field and explicit argument in both directions. No outcome or
    ConfigError is normalized out of the artifact.
    """
    result = {}
    for mode in ['disabled', 'offline']:
        for initial, argument in [(False, True), (True, False), (False, False), (True, None)]:
            sdk.teardown()
            selected = sdk.Settings(**{**SETTINGS, 'mode': mode, 'allow_val_change': initial})
            arguments = {**options(directory, mode), 'settings': selected,
                         'config': {'value': 1, 'nested': {'depth': 2}}}
            if argument is not None:
                arguments['allow_val_change'] = argument
            run = sdk.init(**arguments)
            observed = {'input_allow_val_change': selected.allow_val_change,
                        'run_allow_val_change': run.settings.allow_val_change}
            if mode == 'offline':
                observed['numeric_edit'] = attempt(lambda: run.config.update({'value': 2}))
                observed['nested_edit'] = attempt(lambda: run.config.update({'nested': {'depth': 3}}))
                observed['config_after'] = dict(run.config)
            run.finish()
            result[f'{mode}:{initial}:{argument}'] = observed
    return result


def tensorboard_runtime_cases(directory):
    """Write genuine scalar events after init through the selected hook.

    Main caller: optional TensorBoard init test. Four keyword combinations
    prove activation and precedence; disabled runs still create a real writer
    file but must never install synchronization or import its scalar history.
    The actual local core/journal files are copied before portable comparison.
    """
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary
    from framework_init_runtime import histories
    tb = importlib.import_module(('wandb' if REFERENCE else 'backprop') + '.integration.tensorboard')
    writer_module = importlib.import_module('tensorboard.summary.writer.event_file_writer')
    result = {}
    combinations = [{'sync_tensorboard': True}, {'tensorboard': True},
                    {'tensorboard': True, 'sync_tensorboard': False},
                    {'tensorboard': False, 'sync_tensorboard': True}]
    for mode in ['disabled', 'offline']:
        for index, arguments in enumerate(combinations):
            sdk.teardown()
            tb.unpatch()
            logdir = Path(directory) / f'events-{mode}-{index}'
            run = sdk.init(**{**options(directory, mode), **arguments,
                              'id': f'tb-{mode}-{index}'})
            writer = writer_module.EventFileWriter(str(logdir))
            writer.add_event(Event(wall_time=1000.0, step=3,
                summary=Summary(value=[Summary.Value(tag='init_loss', simple_value=0.75)])))
            writer.flush()
            writer.close()
            run.finish()
            if mode != 'disabled' and not REFERENCE:
                OUTPUT.with_name(OUTPUT.stem + f'-{index}-events.jsonl').write_bytes(
                    (Path(run.sync_dir) / 'events.jsonl').read_bytes())
            rows = [] if mode == 'disabled' else histories(run, REFERENCE,
                reference_output=OUTPUT.with_name(OUTPUT.stem + f'-{index}.wandb') if REFERENCE else None)
            result[mode + ':' + json.dumps(arguments, sort_keys=True)] = {
                'mode': run.settings.mode, 'sync_tensorboard': run.settings.sync_tensorboard,
                'values': [row['init_loss'] for row in rows if 'init_loss' in row],
                'global_steps': [row['global_step'] for row in rows if 'init_loss' in row],
                'real_event_files': bool(list(logdir.glob('*tfevents*'))), 'global_empty': sdk.run is None}
            tb.unpatch()
    sdk.teardown()
    tb.unpatch()
    run = sdk.init(**{**options(directory, 'offline'), 'sync_tensorboard': False,
                      'id': 'tb-reused'})
    reused = sdk.init(**{**options(directory, 'offline'), 'sync_tensorboard': True,
                         'reinit': 'return_previous', 'id': 'tb-ignored'})
    writer = writer_module.EventFileWriter(str(Path(directory) / 'reused-events'))
    writer.add_event(Event(wall_time=1000.0, step=3,
        summary=Summary(value=[Summary.Value(tag='init_loss', simple_value=0.75)])))
    writer.flush()
    writer.close()
    run.finish()
    if not REFERENCE:
        OUTPUT.with_name(OUTPUT.stem + '-reused-events.jsonl').write_bytes(
            (Path(run.sync_dir) / 'events.jsonl').read_bytes())
    rows = histories(run, REFERENCE,
        reference_output=OUTPUT.with_name(OUTPUT.stem + '-reused.wandb') if REFERENCE else None)
    result['offline:return_previous_enable'] = {'mode': run.settings.mode,
        'sync_tensorboard': run.settings.sync_tensorboard, 'reenabled': True,
        'returned_previous': reused is run,
        'values': [row['init_loss'] for row in rows if 'init_loss' in row],
        'global_steps': [row['global_step'] for row in rows if 'init_loss' in row],
        'real_event_files': bool(list((Path(directory) / 'reused-events').glob('*tfevents*'))),
        'global_empty': sdk.run is None}
    tb.unpatch()
    return result


def history_reader_cases(directory):
    """Exercise transaction capture with ordinary actual offline metrics.

    Main caller: the dependency-free histories regression. Why this exists:
    helper interface and flush-lifecycle regressions must be visible locally
    even when optional TensorBoard writers are unavailable. Both distributions
    log real values; reference decoding and saved evidence share one capture.
    """
    from framework_init_runtime import histories
    run = sdk.init(**options(directory, 'offline'))
    run.log({'history_reader/loss': 1.25, 'count': 3})
    run.log({'history_reader/loss': 0.5, 'count': 4})
    run.finish()
    rows = histories(run, REFERENCE,
        reference_output=OUTPUT.with_suffix('.wandb') if REFERENCE else None)
    return {'metrics': [{key: row[key] for key in ['history_reader/loss', 'count']}
                        for row in rows], 'global_empty': sdk.run is None}


def parameters_cases(directory):
    """Exercise public metadata and config arguments on actual local runs.

    Main caller: the full init parameter matrix. Each case uses normal public
    init and actual Config, including positional binding, files, namespace
    parsing and filters. Physical run directory names stay in raw SDK logs;
    selected caller paths are checked directly before disabled rewrites them.
    """
    from argparse import Namespace
    result = {}
    yaml_path = Path(directory) / 'init-config.yaml'
    yaml_path.write_text('learning_rate:\n  value: 0.25\nepochs:\n  value: 3\n')
    profiles = {
        'metadata': {'name': 'chosen-name', 'notes': 'chosen-notes', 'tags': ['first', 'second'],
                     'group': 'chosen-group', 'job_type': 'chosen-job', 'force': True,
                     'save_code': False, 'anonymous': 'allow'},
        'positional': {}, 'tag_string': {'tags': 'ab'},
        'config_namespace': {'config': Namespace(learning_rate=0.25, epochs=3)},
        'config_path': {'config': str(yaml_path)},
        'config_include': {'config': {'learning_rate': 0.25, 'epochs': 3},
                           'config_include_keys': ['epochs']},
        'config_exclude': {'config': {'learning_rate': 0.25, 'epochs': 3},
                           'config_exclude_keys': ['epochs']}}
    fields = ['project', 'entity', 'run_id', 'run_name', 'run_notes', 'run_tags',
              'run_group', 'run_job_type', 'force', 'save_code']
    for mode in ['disabled', 'offline']:
        for profile, additions in profiles.items():
            sdk.teardown()
            settings = sdk.Settings(**SETTINGS, mode=mode, project='settings-project',
                entity='settings-entity', run_name='settings-name', root_dir=directory)
            arguments = {**options(directory, mode), 'settings': settings,
                         'name': 'chosen-name', **additions}
            if profile == 'positional':
                values = [arguments.pop(key) for key in ['entity', 'project', 'dir', 'id', 'name']]
                values.append('positional-notes')
                run = sdk.init(*values, **arguments)
            else:
                run = sdk.init(**arguments)
            result[mode + ':' + profile] = {'settings': {field: getattr(run.settings, field)
                for field in fields}, 'caller_settings': {field: getattr(settings, field)
                for field in fields}, 'caller_root_matches': settings.root_dir == directory,
                'config': dict(run.config)}
            run.finish()
        sdk.teardown()
        run = sdk.init(**{**options(directory, mode), 'tags': iter(['first', 'second']),
                          'name': 'chosen-name'})
        result[mode + ':tag_iterator'] = {'tags': list(run.tags),
                                         'settings_tags': list(run.settings.run_tags)}
        run.finish()
    return result


def imports_cases(directory):
    """Use one selected supported import in a fresh SDK process.

    Main caller: independent import matrix. The caller starts a separate
    process for each distribution and import path; shared aliases within one
    process cannot conceal an import-order or duplicate-global failure.
    """
    result = {'same_public_callable': INIT is sdk.init, 'cases': {}}
    for mode in ['disabled', 'offline']:
        sdk.teardown()
        run = INIT(**{**options(directory, mode), 'name': 'import-name',
                      'config': {'epochs': 3}})
        result['cases'][mode] = {'mode': run.settings.mode, 'id': run.id,
                                'name': run.name, 'config': dict(run.config),
                                'global_same': sdk.run is run}
        run.finish()
        result['cases'][mode]['global_empty'] = sdk.run is None
    return result


def save_code_cases(directory):
    """Execute automatic code saving through the actual child log_code method.

    Main caller: save_code init contract. The observation wrapper records
    actual artifact manifest names after calling the original method unchanged;
    it never replaces file collection, artifact creation or offline logging.
    Disabled and explicitly disabled-code paths must not invoke that method.
    """
    code = Path(directory) / 'code'
    code.mkdir()
    (code / 'framework-fixture.py').write_text('learning_rate = 0.25\n')
    result = {}
    original = sdk.Run.log_code
    for mode in ['disabled', 'offline']:
        for save, disable in [(True, False), (False, False), (True, True)]:
            sdk.teardown()
            calls = []
            def log_code(owner, root=None, *args, **kwargs):
                artifact = original(owner, root, *args, **kwargs)
                calls.append({'root_matches': Path(root).resolve() == code.resolve(),
                              'manifest_names': sorted(artifact.manifest.entries)})
                return artifact
            arguments = {**options(directory, mode), 'id': f'code-{save}-{disable}',
                         'save_code': save, 'settings': {**SETTINGS, 'code_dir': str(code),
                                                       'disable_code': disable}}
            with patch.object(sdk.Run, 'log_code', log_code):
                run = sdk.init(**arguments)
                run.finish()
            result[f'{mode}:{save}:{disable}'] = {'save_code': run.settings.save_code,
                'disable_code': run.settings.disable_code, 'calls': calls,
                'global_empty': sdk.run is None}
    return result


def interactions_cases(directory):
    """Retain exact resume/branch argument and active-policy interactions.

    Main caller: init branch matrix. Ordinary offline runs use the actual local
    core, while disabled valid branch requests execute the actual no-op init.
    No unavailable server behavior is replaced by an invented resume response.
    """
    result = {'offline_resume': {}, 'disabled_branch': {}, 'active_must': {}}
    for resume in [False, True, 'allow', 'must', 'never', 'auto']:
        sdk.teardown()
        run = sdk.init(**{**options(directory, 'offline'), 'resume': resume})
        result['offline_resume'][repr(resume)] = {'id': run.id, 'resume': run.settings.resume,
                                                'resumed': run.resumed, 'step': run.step}
        run.finish()
    for arguments in [{'fork_from': 'parent?_step=2'}, {'resume_from': 'parent?_step=2', 'id': 'parent'},
                      {'resume_from': 'parent?_step=2', 'id': None},
                      {'resume': False, 'fork_from': 'parent?_step=2'}]:
        sdk.teardown()
        def initialize():
            run = sdk.init(**{**options(directory, 'disabled'), **arguments})
            fields = {name: getattr(run.settings, name) for name in ['resume', 'fork_from', 'resume_from']}
            fields['id'] = run.id
            for name in ['fork_from', 'resume_from']:
                moment = fields[name]
                if moment is not None:
                    fields[name] = {key: getattr(moment, key) for key in ['run', 'metric', 'value']}
            run.finish()
            return fields
        result['disabled_branch'][json.dumps(arguments, sort_keys=True)] = attempt(initialize)
    for policy in ['return_previous', 'default']:
        sdk.teardown()
        first = sdk.init(options(directory, 'offline')['entity'], **{
            key: value for key, value in options(directory, 'offline').items() if key != 'entity'})
        result['active_must'][policy] = {
            'result': attempt(lambda: sdk.init(**{**options(directory, 'offline'),
                                                  'reinit': policy, 'resume': 'must'}) is first),
            'global_previous': sdk.run is first,
            'previous_log': attempt(lambda: first.log({'still_alive': 1}))}
        sdk.teardown()
    return result


def gym_runtime_cases(directory):
    """Use the installed recorder to verify init hook selection and reuse.

    Main caller: optional Gym init test. Only activation of the actual recorder
    close hook is compared. Video encoding and other recorder child methods
    already have separate adapter tests and are not claimed by this probe.
    """
    try:
        library = importlib.import_module('gymnasium')
    except ModuleNotFoundError as error:
        if error.name != 'gymnasium':
            raise
        library = importlib.import_module('gym')
    wrappers = importlib.import_module(library.__name__ + '.wrappers')
    if library.__name__ == 'gymnasium' and hasattr(wrappers.RecordVideo, 'stop_recording'):
        recorder = wrappers.RecordVideo
    else:
        recording = importlib.import_module(library.__name__ + '.wrappers.monitoring.video_recorder')
        # The already documented Gym 0.25 repair changes the completion
        # boundary from upstream's ImageEncoder to the actual VideoRecorder.
        # Inspect each real selected hook; encoding parity remains separate.
        old_gym = library.__name__ == 'gym' and tuple(map(int, library.__version__.split('.')[:2])) < (0, 26)
        recorder = recording.ImageEncoder if REFERENCE and old_gym else recording.VideoRecorder
    original = recorder.close
    result = {'library': library.__name__, 'version': library.__version__, 'cases': {}}
    for mode in ['disabled', 'offline']:
        for enabled in [False, True]:
            sdk.teardown()
            # Pinned Gym patches live at process scope. Restore precisely the
            # original public close hook between independent init observations.
            recorder.close = original
            if REFERENCE:
                sdk.patched['gym'] = []
            run = sdk.init(**{**options(directory, mode), 'monitor_gym': enabled,
                              'id': f'gym-{mode}-{enabled}'})
            first_hook = recorder.close
            repeated = sdk.init(**{**options(directory, mode), 'monitor_gym': enabled,
                                   'id': f'gym-repeat-{mode}-{enabled}', 'reinit': 'return_previous'})
            result['cases'][mode + ':' + str(enabled)] = {
                'mode': run.settings.mode, 'patched': first_hook is not original,
                'same_hook_after_repeated_init': recorder.close is first_hook,
                'same_run_after_repeated_init': repeated is run}
            repeated.finish()
            run.finish()
    sdk.teardown()
    recorder.close = original
    return result


def signatures():
    result = []
    for parameter in inspect.signature(sdk.init).parameters.values():
        if parameter.name.startswith('_'):
            continue
        default = parameter.default
        if parameter.name == 'anonymous':
            default = 'omitted anonymous sentinel'
        result.append([parameter.name, parameter.kind.name, default])
    return result


with tempfile.TemporaryDirectory(prefix='framework-init-') as directory, patch.dict(os.environ, {
        'BACKPROP_DIR': directory, 'WANDB_CACHE_DIR': directory, 'WANDB_CONFIG_DIR': directory,
        'WANDB_DATA_DIR': directory, 'WANDB_MODE': 'offline'}):
    try:
        if SCENARIO == 'settings':
            observed = settings_cases()
        else:
            observed = globals()[SCENARIO + '_cases'](directory)
    finally:
        sdk.teardown()
    OUTPUT.write_text(json.dumps({'scenario': SCENARIO, 'sdk': sdk.__version__,
        'init_signature': signatures(), 'portable': observed}, sort_keys=True, indent=2) + '\n')
