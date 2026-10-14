"""Observe real init HTTP failures against test-owned loopback TLS replies.

Architecture: independent SDK import -> real local protocol server -> genuine
public init/auth lifecycle -> mandatory JSON plus external raw diagnostics.
Main callers: test_framework_init_transport_contracts. Why this exists: earlier
init probes stopped before transport or replaced the native store with a sink.
No production host, credential cache or unavailable framework is required.
"""
import importlib.metadata
import inspect
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from framework_init_local_protocol import LocalProtocol

REFERENCE = sys.argv[1] == 'reference'
SCENARIO = sys.argv[2]
PARTS = SCENARIO.split(':')
STAGE = PARTS[0]
STATUS = [int(value) for value in PARTS[1].split(',')] if len(PARTS) > 1 else [403]
POLICY = PARTS[2] if len(PARTS) > 2 else None
DIRECT = os.environ.get('FRAMEWORK_INIT_DIRECT_RUN') == '1'
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

IMPORT = os.environ.get('FRAMEWORK_INIT_TRANSPORT_IMPORT', 'top')
if IMPORT == 'sdk':
    import wandb.sdk as supported
    sdk_init, sdk_login = supported.init, supported.login
elif IMPORT == 'wandb':
    import wandb as supported
    sdk_init, sdk_login = supported.init, supported.login
else:
    assert IMPORT == 'top'
    sdk_init, sdk_login = sdk.init, sdk.login

TOKEN = 'c' * 40
SETTINGS = {'console': 'off', 'silent': True, 'disable_git': True,
            'disable_code': True, 'x_disable_stats': True,
            'insecure_disable_ssl': True, 'init_timeout': 5}


def outcome(function):
    """Preserve the original error and explicit cause without normalization."""
    try:
        value = function()
        return {'returned_run': hasattr(value, 'id'), 'id': getattr(value, 'id', None),
                'value': value if value is None or isinstance(value, bool) else 'Run'}
    except BaseException as error:
        causes, current = [], error
        while current is not None:
            causes.append({'error': type(current).__name__, 'message': str(current),
                           'status': getattr(current, 'status_code', None)})
            current = current.__cause__
        return {'error': type(error).__name__, 'message': str(error),
                'cause': {'error': type(error.__cause__).__name__, 'message': str(error.__cause__)}
                if error.__cause__ is not None else None, 'causes': causes}


def observe(directory):
    """Run one real construction, retaining source requests and public state.

    Main caller: process entry point. Read/publish failures are separate native
    stages; reference failures target its actual UpsertBucket HTTP operation.
    """
    if STAGE in {'success', 'auth_success'}:
        failure = None
    elif REFERENCE:
        failure = 'auth' if STAGE == 'auth' else 'blocked_init' if STAGE.startswith('blocked') else 'init'
    else:
        failure = 'read' if STAGE == 'auth' else 'publish' if STAGE == 'publish_echo' else STAGE
    diagnostic = 'fixture echoed synthetic key ' + TOKEN if STAGE == 'publish_echo' else None
    with LocalProtocol(failure=failure, status=STATUS, diagnostic=diagnostic) as server:
        os.environ['NETRC'] = str(Path(directory) / 'netrc')
        os.environ['WANDB_CONFIG_DIR'] = str(Path(directory) / 'config')
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = server.proxy
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        os.environ.pop('ALL_PROXY', None)
        os.environ.pop('all_proxy', None)
        os.environ['BACKPROP_HOST'] = server.host
        os.environ['BACKPROP_STREAM_DATASET_RID'] = 'ri.foundry.main.dataset.init-fixture'
        os.environ['BACKPROP_MEDIA_DATASET_RID'] = 'ri.foundry.main.dataset.init-media'
        settings = {**SETTINGS, 'base_url': server.host, 'api_key': TOKEN,
                    'credentials_file': str(Path(directory) / 'credentials.json')}
        if STAGE.startswith('blocked'):
            settings['init_timeout'] = .25
        previous = None
        if POLICY:
            previous = sdk_init(mode='offline', dir=directory, project='init-fixture', entity='e',
                                id='previous',
                                settings={key:value for key,value in SETTINGS.items() if key != 'insecure_disable_ssl'})
        options = {'mode': 'online', 'project': 'init-fixture', 'entity': 'e',
                   'dir': directory, 'settings': settings}
        if POLICY:
            options['reinit'] = POLICY
        if not REFERENCE:
            options['_deployment'] = {'host': server.host,
                                     'stream_dataset_rid': 'ri.foundry.main.dataset.init-fixture',
                                     'media_dataset_rid': 'ri.foundry.main.dataset.init-media'}
        if STAGE in {'auth', 'auth_success'}:
            supplied = sdk.Settings(**{**settings, 'api_key': 'b' * 40})
            context = sdk.setup(supplied)
            before = {'key': context.settings.api_key, 'host': context.settings.base_url,
                      'environment': {key:os.environ.get(key) for key in ('WANDB_API_KEY','BACKPROP_TOKEN','FOUNDRY_TOKEN')}}
            result = outcome(lambda: sdk_login(key=TOKEN, host=server.host, verify=True))
            result['auth_before'] = before
            result['auth_after'] = {'key': context.settings.api_key, 'host': context.settings.base_url,
                      'environment': {key:os.environ.get(key) for key in ('WANDB_API_KEY','BACKPROP_TOKEN','FOUNDRY_TOKEN')}}
            result['credential_file_exists'] = Path(settings['credentials_file']).exists()
            result['netrc_exists'] = Path(os.environ['NETRC']).exists()
            result['supplied_key_after_login'] = supplied.api_key
            if STAGE == 'auth_success':
                server.failure = 'auth' if REFERENCE else 'read'
                result['second_failed_login'] = outcome(lambda: sdk_login(key='d' * 40, host=server.host, verify=True))
                result['retained_key_after_failed_login'] = context.settings.api_key
                server.failure = None
        else:
            started = time.monotonic()
            result = outcome(lambda: sdk.Run(**options) if DIRECT else sdk_init(**options))
            result['init_elapsed_seconds'] = time.monotonic() - started
        result['global_run'] = getattr(sdk.run, 'id', None)
        result['previous_global'] = previous is not None and sdk.run is previous
        if previous is not None:
            result['previous_log'] = outcome(lambda: previous.log({'still_alive': 1}))
        result['threads_after_failure'] = [thread.name for thread in threading.enumerate()]
        result['open_run_log_handlers'] = [{'logger': name, 'handlers': [getattr(handler, 'baseFilename', '') for handler in logger.handlers]}
            for name,logger in logging.Logger.manager.loggerDict.items()
            if isinstance(logger, logging.Logger) and logger.handlers and
            any(getattr(handler, 'baseFilename', '').startswith(directory) for handler in logger.handlers)]
        if STAGE.startswith('blocked'):
            server.release.set()
            time.sleep(.3)
            result['global_run_after_late_response'] = getattr(sdk.run, 'id', None)
            result['threads_after_late_response'] = [thread.name for thread in threading.enumerate()]
        result['native_journals'] = [{'path':str(path.parent), 'events': [json.loads(line) for line in path.read_text().splitlines()],
            'ack': json.loads(path.with_name('ack.json').read_text()) if path.with_name('ack.json').exists() else None}
            for path in Path(directory).rglob('events.jsonl')] if not REFERENCE else []
        if STAGE == 'success':
            result['initial_log'] = outcome(lambda: sdk.run.log({'accepted': 2}))
            result['initial_finish'] = outcome(lambda: sdk.run.finish())
        if STAGE in {'auth', 'auth_success'}:
            result['retained_key_before_recovery'] = context.settings.api_key
        if not REFERENCE and STAGE not in {'success'} and POLICY != 'return_previous':
            server.failure = None
            recovered = sdk_init(**{**options, 'reinit': 'finish_previous'})
            result['repeat_online_log'] = outcome(lambda: recovered.log({'transport_recovered': 1}))
            recovered.finish()
            result['repeat_online_finished'] = sdk.run is None
            if STAGE in {'auth', 'auth_success'}:
                result['retained_key_after_online_recovery'] = context.settings.api_key
        repeated = sdk_init(mode='offline', project='init-fixture', entity='e', dir=directory,
                            reinit='finish_previous', settings={key:value for key,value in SETTINGS.items() if key != 'insecure_disable_ssl'})
        result['repeat_log'] = outcome(lambda: repeated.log({'recovered': 1}))
        repeated.finish()
        result['repeat_global_finished'] = sdk.run is None
        if STAGE in {'auth', 'auth_success'}:
            result['retained_key_before_teardown'] = context.settings.api_key
        sdk.teardown()
        if STAGE in {'auth', 'auth_success'}:
            result['retained_key_after_teardown'] = context.settings.api_key
            later_context = sdk.setup(sdk.Settings(mode='offline', api_key='e' * 40,
                                   root_dir=directory, **{key:value for key,value in SETTINGS.items() if key != 'insecure_disable_ssl'}))
            result['fresh_setup_key'] = later_context.settings.api_key
            result['retained_key_after_fresh_setup'] = context.settings.api_key
            sdk.teardown()
        result['threads_after_teardown'] = [thread.name for thread in threading.enumerate()]
        result['requests'] = server.requests
        result['unknown_requests'] = server.unknown
        result['records'] = server.records
        return result


with tempfile.TemporaryDirectory(prefix='framework-init-transport-') as directory:
    source_names = ['sdk/wandb_init.py', 'sdk/wandb_login.py', 'sdk/wandb_setup.py'] if REFERENCE else ['__init__.py', 'login_session.py', 'run.py', 'foundry.py']
    source_root = Path(sdk.__file__).parent
    result = {'sdk': 'reference' if REFERENCE else 'native', 'scenario': SCENARIO,
              'import': IMPORT, 'runtime': {'python': sys.version,
                  'sdk_version': sdk.__version__, 'module_file': sdk.__file__,
                  'protobuf': importlib.metadata.version('protobuf'),
                  'source_hashes': {name:hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in source_names}},
              'signatures': {'init': str(inspect.signature(sdk_init)),
                             'login': str(inspect.signature(sdk_login))},
              'observations': observe(directory)}
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(result, indent=2) + '\n')
