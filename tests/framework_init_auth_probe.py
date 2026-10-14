"""Observe init authentication through genuine independent SDK entry points.

Architecture: real public setup/init and credential selection -> terminal input
or unavailable server boundary fixture -> mandatory JSON and raw diagnostics.
Main caller: test_framework_init_auth_contracts. Why this exists: testing login
alone cannot establish whether init authenticates before reinit changes a live
run. Only terminal input, credential-file isolation and hosted server calls are
supplied; the pinned login and native selection functions execute unchanged.
"""
from contextlib import ExitStack
import importlib
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
    from wandb.errors import AuthenticationError
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk
    from backprop.errors import AuthenticationError

COMMON = {'silent': True, 'console': 'off', 'disable_git': True,
          'x_disable_stats': True, 'init_timeout': 10}
HOST = 'https://framework-init.invalid'
TOKENS = {name: char * 40 for name, char in
          [('environment', 'a'), ('setup', 'b'), ('explicit', 'c'), ('prompt', 'd')]}


def outcome(call):
    """Keep the original public error class/message and success identity."""
    try:
        return {'value': call()}
    except BaseException as error:
        return {'error': 'Error' if type(error) is sdk.Error else type(error).__name__,
                'message': str(error)}


def offline(directory, identifier):
    options = dict(mode='offline', dir=directory, id=identifier,
                   entity='e', project='auth-init', settings=dict(COMMON))
    if not REFERENCE:
        options.update(_sink=sdk.MemorySink(), _deployment={})
    return sdk.init(**options)


def auth_boundaries(stack, prompt):
    """Isolate terminal and remote user settings while retaining real login.

    Main callers: failed-auth and selection scenarios. No shared host session,
    home credential file, network credential verification or hosted backend is
    used. Explicit pinned key validation and selection remain actual SDK code.
    """
    if REFERENCE:
        from wandb.sdk import wandb_setup
        from wandb.sdk.lib import wbauth
        from wandb.sdk.lib.wbauth import authenticate, wbnetrc
        authenticate.unauthenticate_session(update_settings=False)
        stack.enter_context(patch.object(wbnetrc, 'read_netrc_auth_with_source', return_value=None))
        stack.enter_context(patch.object(wbauth.prompt, 'prompt_and_save_api_key', side_effect=prompt))
        stack.enter_context(patch.object(type(wandb_setup.singleton()), '_load_user_settings', return_value=None))
    else:
        stack.enter_context(patch('backprop.login_prompt.prompt_token', side_effect=prompt))
        stack.enter_context(patch('backprop.credential_store.read_token', return_value=None))
        stack.enter_context(patch('backprop.credential_store.write_token'))


def backend_boundary(stack, selected):
    """Observe final auth fields at the unavailable hosted backend boundary.

    Main callers: selection and repeated-session probes. Reference records the
    real resolved fields before its hosted constructor sentinel; native creates
    an actual Run with MemorySink supplied only by the remote store factory.
    """
    if REFERENCE:
        from wandb.sdk.wandb_init import _WandbInit
        def boundary(owner, settings, *args):
            selected.update(token=settings.api_key, mode=settings.mode,
                            host=settings.base_url, run_api_key=settings.api_key)
            raise RuntimeError('declared hosted backend boundary')
        stack.enter_context(patch.object(_WandbInit, 'init', boundary))
    else:
        runtime = importlib.import_module('backprop.run')
        def store(config, *, token=None):
            selected.update(token=token, mode='online', host=config['host'])
            return sdk.MemorySink()
        stack.enter_context(patch.object(runtime, 'native_store', side_effect=store))


def failure_cases(directory):
    """Reject chosen auth before all policies can alter a genuine live run.

    Main caller: auth ordering test. Actual offline previous handles log after
    the failed online request; this verifies public usability and global state
    without inspecting an invented private closed flag.
    """
    result = {}
    for policy, error_type in [(policy, error_type)
        for policy in ['return_previous', 'finish_previous', 'create_new']
        for error_type in [AuthenticationError, KeyboardInterrupt, RuntimeError]]:
        sdk.teardown()
        first = offline(directory, policy + '-' + error_type.__name__)
        calls = []
        def reject(*args, **kwargs):
            calls.append('prompt')
            raise error_type('fixture rejected auth')
        with ExitStack() as stack:
            auth_boundaries(stack, reject)
            options = dict(mode='online', dir=directory, id='rejected', reinit=policy,
                           settings={**COMMON, 'base_url': HOST,
                                     'credentials_file': str(Path(directory) / 'auth.json')})
            if not REFERENCE:
                options['_deployment'] = {'host': HOST}
            result[policy + ':' + error_type.__name__] = {
                'result': outcome(lambda: sdk.init(**options) is first),
                'calls': calls, 'global_previous': sdk.run is first,
                'previous_log': outcome(lambda: first.log({'still_alive': 1})),
                'credential_environment': {name: value for name, value in os.environ.items()
                    if name in ['WANDB_API_KEY', 'BACKPROP_TOKEN', 'FOUNDRY_TOKEN']}}
        sdk.teardown()
    return result


def selection_cases(directory):
    """Trace explicit, reusable and prompted credential choices at init.

    Main caller: precedence/force/relogin test. Reference stops at its final
    backend constructor; native uses a MemorySink only at the store boundary.
    This observes real credential selection, not hosted auth or persistence.
    """
    result = {}
    for source in ['implicit', 'environment', 'setup', 'explicit', 'relogin',
                   'init_relogin', 'explicit_relogin', 'host_scope', 'host_scope_explicit']:
        for setup_force, force in [(False, None), (True, None), (True, False), (False, True)]:
            sdk.teardown()
            calls, selected = [], {}
            environment = {'WANDB_API_KEY' if REFERENCE else 'BACKPROP_TOKEN': TOKENS['environment']}
            if source in ['setup', 'host_scope', 'host_scope_explicit']:
                environment = {}
            with patch.dict(os.environ, environment), ExitStack() as stack:
                def prompted(*args, **kwargs):
                    calls.append({'force': kwargs['no_offline'] if REFERENCE else kwargs['force']})
                    return TOKENS['prompt']
                auth_boundaries(stack, prompted)
                setup = sdk.Settings(**COMMON, base_url=HOST,
                                     relogin=source in ['relogin', 'explicit_relogin'],
                                     mode='online', force=setup_force,
                                     **({} if source == 'implicit' else {'api_key': TOKENS['setup']}))
                sdk.setup(setup)
                backend_boundary(stack, selected)
                options = dict(mode='online', dir=directory, force=force,
                               settings={**COMMON, 'credentials_file': str(Path(directory) / 'auth.json')})
                if source in ['explicit', 'explicit_relogin', 'host_scope_explicit']:
                    options['settings']['api_key'] = TOKENS['explicit']
                if source in ['host_scope', 'host_scope_explicit']:
                    options['settings']['base_url'] = 'https://different-init.invalid'
                if source == 'init_relogin':
                    options['settings']['relogin'] = True
                if not REFERENCE:
                    options['_deployment'] = {'host': HOST}
                def initialize():
                    run = sdk.init(**options)
                    selected['run_api_key'] = run.settings.api_key
                    return run.offline
                observed = outcome(initialize)
                if REFERENCE:
                    selected['run_api_key'] = selected.get('token')
                result[source + ':' + str(setup_force) + ':' + str(force)] = {
                    'prompt_calls': calls, 'selected': selected, 'raw_result': observed,
                    'chosen_source': next((name for name, token in TOKENS.items()
                                           if token == selected.get('token')), None)}
            sdk.teardown()
    return result


def session_cases(directory):
    """Keep a successful process session until public teardown resets it.

    Main caller: repeated init credential test. First selection uses actual
    explicit/system/prompt flows. Later environment changes cannot silently
    replace that session, while actual teardown permits fresh selection.
    """
    result = {}
    environment_key = 'WANDB_API_KEY' if REFERENCE else 'BACKPROP_TOKEN'
    for first_source in ['environment', 'explicit', 'prompt']:
        sdk.teardown()
        selected, calls = {}, []
        environment = {environment_key: TOKENS['environment']} if first_source == 'environment' else {}
        with patch.dict(os.environ, environment), ExitStack() as stack:
            def prompted(*args, **kwargs):
                calls.append('prompt')
                return TOKENS['prompt']
            auth_boundaries(stack, prompted)
            backend_boundary(stack, selected)
            sdk.setup(sdk.Settings(**COMMON, mode='online', base_url=HOST))
            observations = []
            for index in range(3):
                if index == 1:
                    os.environ[environment_key] = TOKENS['setup']
                if index == 2:
                    sdk.teardown()
                    sdk.setup(sdk.Settings(**COMMON, mode='online', base_url=HOST))
                selected.clear()
                options = dict(dir=directory, id=first_source + '-session-' + str(index), project='session-init',
                               settings={**COMMON, 'credentials_file': str(Path(directory) / 'auth.json')})
                if first_source == 'explicit' and index == 0:
                    options['settings']['api_key'] = TOKENS['explicit']
                if not REFERENCE:
                    options['_deployment'] = {'host': HOST}
                def initialize():
                    run = sdk.init(**options)
                    selected['run_api_key'] = run.settings.api_key
                    run.finish()
                    return run.offline
                raw = outcome(initialize)
                observations.append({'selected': dict(selected), 'raw_result': raw,
                    'chosen_source': next((name for name, token in TOKENS.items()
                                           if token == selected.get('token')), None)})
            result[first_source] = {'calls': calls, 'runs': observations}
        sdk.teardown()
    return result


with tempfile.TemporaryDirectory(prefix='framework-init-auth-') as directory:
    with patch.dict(os.environ, {'WANDB_CACHE_DIR': directory, 'WANDB_CONFIG_DIR': directory,
                                'WANDB_DATA_DIR': directory, 'BACKPROP_DIR': directory}):
        try:
            observed = globals()[SCENARIO + '_cases'](directory)
        finally:
            sdk.teardown()
        OUTPUT.write_text(json.dumps({'sdk': sdk.__version__, 'scenario': SCENARIO,
                                     'observations': observed}, sort_keys=True, indent=2) + '\n')
