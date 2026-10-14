"""Observe the public Api constructor using actual auth and local TLS clients.

Architecture: isolated credential directory -> real login/setup and Api import
-> loopback provider responses -> public/session/cache observations -> teardown.
Main caller: test_public_api_auth_contracts. Hosted services alone are replaced;
the real SDK authentication, credential files and network clients stay intact.
"""
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from framework_init_local_protocol import LocalProtocol

REFERENCE, SCENARIO, ALIAS = sys.argv[1] == 'reference', sys.argv[2], sys.argv[3]
OUTPUT = Path(sys.argv[-1])
sdk = importlib.import_module('wandb' if REFERENCE else 'backprop')
if REFERENCE:
    assert sdk.__version__ == '0.30.0'
OLD, NEW, EXPLICIT = ('a' * 40, 'b' * 40, 'c' * 40)


def label(value):
    return {OLD: 'old', NEW: 'new', EXPLICIT: 'explicit', None: None}.get(value, 'other')


def state(host):
    """Retain host ownership and selected defaults without secret values.

    Main caller: each constructor stage. Reading both public settings and the
    session distinguishes a successful local handle from global publication.
    """
    if REFERENCE:
        from wandb.sdk.lib.wbauth import session_credentials
        selected = session_credentials(host=host)
        token = getattr(selected, 'api_key', None)
    else:
        from backprop.login_session import current_init_session
        selected = current_init_session(host)
        token = getattr(selected, 'token', None)
    settings = sdk.setup().settings
    return {'session': label(token), 'settings': label(settings.api_key), 'mode': settings.mode}


def cache_hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(directory).rglob('*') if p.is_file()
            and p.name in {'netrc', 'credentials.json'}}


def attempt(constructor, **kwargs):
    try:
        api = constructor(**kwargs)
        return {'value': 'Api', 'token': label(api.api_key), 'project': api.settings['project']}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error),
                'cause': type(error.__cause__).__name__ if error.__cause__ else None}


def request_record(request):
    """Keep actual HTTP evidence while replacing only the credential header.

    Main caller: final observation. Synthetic key equality proves selection;
    retaining opaque headers would make redaction failures harder to detect.
    """
    header = request.get('authorization') or ''
    selected = None
    for token in [OLD, NEW, EXPLICIT]:
        if header in {'Bearer ' + token, 'Basic ' + base64.b64encode(('api:' + token).encode()).decode()}:
            selected = label(token)
    return {**{k: v for k, v in request.items() if k != 'authorization'}, 'credential': selected}


def observe(directory, server):
    """Select one credential lifecycle, preserving both sides of verification.

    Main caller: the isolated process entry point. Public login creates real
    cache/session state; teardown tests cache recovery independently. A failed
    explicit reader must leave that earlier session usable on the next call.
    """
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = server.proxy
    os.environ.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost',
                      NETRC=str(directory / 'netrc'), WANDB_CONFIG_DIR=str(directory / 'settings'),
                      BACKPROP_HOST=server.host,
                      BACKPROP_STREAM_DATASET_RID='ri.foundry.main.dataset.init-fixture')
    for name in ('ALL_PROXY', 'all_proxy'):
        os.environ.pop(name, None)
    settings = dict(base_url=server.host, insecure_disable_ssl=True, console='off',
                    disable_git=True, disable_code=True, x_disable_stats=True,
                    credentials_file=str(directory / 'credentials.json'),
                    login_timeout=.3 if SCENARIO == 'prompt_timeout' else None,
                    project='api-auth')
    sdk.setup(sdk.Settings(**settings))
    constructor = (sdk.Api if ALIAS == 'root' else
                   importlib.import_module(sdk.__name__ + '.apis.public').Api if ALIAS == 'public' else
                   importlib.import_module(sdk.__name__ + '.apis.public.api').Api)
    if SCENARIO in {'cache', 'cache_rejected', 'session_relogin', 'empty_key', 'explicit_session', 'failure_recovery'}:
        assert sdk.login(key=OLD, host=server.host, verify=False) is True
        for name in ('BACKPROP_TOKEN', 'FOUNDRY_TOKEN', 'WANDB_API_KEY'):
            os.environ.pop(name, None)
        if SCENARIO in {'cache', 'cache_rejected'}:
            sdk.teardown()
            sdk.setup(sdk.Settings(**settings))
    if SCENARIO in {'environment', 'environment_relogin', 'session_relogin', 'environment_rejected'} or SCENARIO.startswith('timeout_'):
        os.environ.update(WANDB_API_KEY=NEW, BACKPROP_TOKEN=NEW)
    if SCENARIO in {'environment_relogin', 'session_relogin'}:
        sdk.setup(sdk.Settings(**{**settings, 'relogin': True}))
    if SCENARIO == 'settings':
        sdk.setup(sdk.Settings(**{**settings, 'api_key': OLD}))
    kwargs = {'timeout': 2}
    if SCENARIO.startswith('timeout_'):
        kwargs['timeout'] = {'timeout_zero': 0, 'timeout_negative': -1, 'timeout_true': True}[SCENARIO]
    if SCENARIO == 'empty_key': kwargs['api_key'] = ''
    if SCENARIO in {'explicit_session', 'failure_recovery'}: kwargs['api_key'] = EXPLICIT
    before, files = state(server.host), cache_hashes(directory)
    result = attempt(constructor, **kwargs)
    after = state(server.host)
    first_requests = [request_record(r) for r in server.requests]
    recovery = None
    if SCENARIO == 'failure_recovery':
        server.failure = None
        recovery = attempt(constructor, timeout=2)
    finished = {'before': before, 'result': result, 'after': after,
                'cache_unchanged': files == cache_hashes(directory),
                'cache_exists': bool(cache_hashes(directory)),
                'requests': first_requests, 'recovery': recovery,
                'final_state': state(server.host), 'unknown': server.unknown,
                'stdin_tty': sys.stdin.isatty(), 'stderr_tty': sys.stderr.isatty()}
    sdk.teardown()
    finished['native_workers_after_teardown'] = [thread.name for thread in threading.enumerate()
        if thread.name.startswith(('backprop-flush-', 'backprop-stats-', 'backprop-stop-', 'backprop-model-'))]
    return finished


failure = ('auth' if REFERENCE else 'read') if SCENARIO.endswith('_rejected') or SCENARIO == 'failure_recovery' else None
with tempfile.TemporaryDirectory(prefix='public-api-auth-') as directory:
    with LocalProtocol(failure=failure, status=401) as server:
        result = observe(Path(directory), server)
    source = Path(sdk.__file__).parent
    names = ['apis/public/api.py', 'sdk/lib/wbauth/authenticate.py'] if REFERENCE else ['apis/public/api.py', 'login_session.py']
    OUTPUT.write_text(json.dumps({'sdk': sdk.__version__, 'scenario': SCENARIO, 'alias': ALIAS,
        'source_hashes': {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names},
        'observation': result}, indent=2) + '\n')
