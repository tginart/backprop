"""Observe init's real terminal and transport lifecycle in one process.

Architecture: checked SDK import -> test-owned localhost TLS boundary -> real
terminal auth selection -> actual offline/disabled/core or denied online init
-> exact state and mandatory JSON. Main callers: terminal contract tests.
Why this exists: replacing prompt_token cannot establish input deadlines, EOF
or terminal restoration. Hosted endpoints are blocked by the local deny proxy.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import termios
import threading

from framework_init_local_protocol import LocalProtocol

REFERENCE = sys.argv[1] == 'reference'
SCENARIO = sys.argv[2]
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

IMPORT = os.environ.get('FRAMEWORK_INIT_IMPORT', 'top')
if IMPORT == 'sdk':
    from wandb.sdk import init as init_run
elif IMPORT == 'wandb':
    from wandb import init as init_run
else:
    assert IMPORT == 'top'
    init_run = sdk.init

TOKEN = 'c' * 40
SETTINGS = dict(console='off', disable_git=True, disable_code=True,
                x_disable_stats=True, init_timeout=5)


def attempt(call):
    """Retain exact caller-visible errors, including cancellation subclasses."""
    try:
        value = call()
        return {'returned_run': hasattr(value, 'id'), 'id': getattr(value, 'id', None),
                'value': None if value is None else 'Run' if hasattr(value, 'id') else value}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error),
                'cause': type(error.__cause__).__name__ if error.__cause__ else None}


def observe(directory):
    """Measure prompt selection before binding, writes or active-run policy.

    Main caller: isolated process entry point. The server denies an actual new
    online initialization after successful token entry; reused handles need no
    publication. Every case then creates a genuine successful offline session.
    """
    previous = None
    with LocalProtocol(failure='init' if REFERENCE else 'publish') as server:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = server.proxy
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        os.environ.pop('ALL_PROXY', None)
        os.environ.pop('all_proxy', None)
        os.environ['NETRC'] = str(Path(directory) / 'netrc')
        os.environ['WANDB_CONFIG_DIR'] = str(Path(directory) / 'settings')
        os.environ['BACKPROP_HOST'] = server.host
        os.environ['BACKPROP_STREAM_DATASET_RID'] = 'ri.foundry.main.dataset.init-fixture'
        os.environ['BACKPROP_MEDIA_DATASET_RID'] = 'ri.foundry.main.dataset.init-media'
        selected = sdk.Settings(**SETTINGS, base_url=server.host,
            insecure_disable_ssl=True, credentials_file=str(Path(directory) / 'credentials.json'),
            login_timeout=.35 if SCENARIO.startswith(('menu_', 'secret_')) else 5,
            relogin=SCENARIO == 'offline_relogin_previous')
        context = None if SCENARIO == 'offline_implicit_setup' else sdk.setup(selected)
        if SCENARIO == 'offline_relogin_previous':
            sdk.login(key=TOKEN, host=server.host, verify=False)
        if SCENARIO.endswith('_previous'):
            previous = init_run(mode='offline', id='previous', project='terminal-init',
                                entity='e', dir=directory, settings=SETTINGS)
        before = termios.tcgetattr(0) if sys.stdin.isatty() else None
        files_before = sorted(str(path.relative_to(directory)) for path in Path(directory).rglob('*') if path.is_file())
        options = dict(id='prompted', project='terminal-init', entity='e', dir=directory,
                       reinit='return_previous' if previous else 'default',
                       force=SCENARIO.startswith(('force', 'secret')))
        if previous is None and SCENARIO.startswith(('token', 'force', 'secret')):
            # A fresh generated identity requires no archive read. This slice
            # exercises actual prompt selection and rejected publication; the
            # separate transport probe owns explicit-ID read failure evidence.
            options.pop('id')
        if context is None:
            options['settings'] = selected
        # No explicit mode override: the prompt's singleton mode becomes the
        # returned run's mode in pinned init. Native resolves the same choice.
        result = attempt(lambda: init_run(**options))
        if context is None:
            context = sdk.setup()
        returned = sdk.run
        after = termios.tcgetattr(0) if before is not None else None
        result.update(scenario=SCENARIO, restored=before == after,
            global_id=getattr(returned, 'id', None),
            mode=getattr(getattr(returned, 'settings', None), 'mode', None),
            disabled=getattr(returned, 'disabled', None),
            previous_global=previous is not None and returned is previous,
            previous_log=attempt(lambda: previous.log({'terminal_previous': 1})) if previous else None,
            setup_mode=context.settings.mode,
            files_after=sorted(str(path.relative_to(directory)) for path in Path(directory).rglob('*') if path.is_file()),
            files_before=files_before,
            netrc_exists=Path(os.environ['NETRC']).is_file(),
            credential_file_exists=Path(selected.credentials_file).is_file(),
            supplied_mode=selected.mode,
            setup_key_matches=context.settings.api_key == TOKEN,
            supplied_key_matches=selected.api_key == TOKEN,
            auth_headers=[request['authorization'] for request in server.requests if request.get('authorization')],
            native_start_ids=[json.loads(line)['run_id'] for path in Path(directory).rglob('events.jsonl')
                              for line in path.read_text().splitlines() if json.loads(line)['type'] == 'run'],
            threads_after=[thread.name for thread in threading.enumerate()])
        if 'error' not in result and context.settings.mode in {'offline', 'disabled'}:
            implicit = init_run(project='terminal-init', entity='e', dir=directory, reinit='return_previous')
            result['implicit_repeat_mode'] = implicit.settings.mode
        sdk.teardown()
        # Prompt mode changes cannot strand future explicitly offline sessions.
        repeat = init_run(mode='offline', id='repeat', project='terminal-init', entity='e',
                          dir=directory, settings={**SETTINGS, 'silent': True})
        result['repeat_log'] = attempt(lambda: repeat.log({'terminal_repeat': 1}))
        repeat.finish()
        sdk.teardown()
        result.update(repeat_global_empty=sdk.run is None,
                      unknown_requests=server.unknown, requests=server.requests,
                      threads_after_teardown=[thread.name for thread in threading.enumerate()])
        return result


with tempfile.TemporaryDirectory(prefix='framework-init-terminal-') as directory:
    source_root = Path(sdk.__file__).parent
    names = ['sdk/wandb_init.py', 'sdk/wandb_login.py', 'sdk/lib/wbauth/prompt.py',
             'errors/term.py', 'sdk/lib/timed_input.py'] if REFERENCE else [
             '__init__.py', 'login_session.py', 'login_prompt.py', 'init_integrations.py']
    data = {'sdk': sdk.__version__, 'python': sys.version, 'scenario': SCENARIO,
            'init_parameters': {name: str(parameter.default) for name, parameter in inspect.signature(init_run).parameters.items()
                                if not name.startswith('_')},
            'source_hashes': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in names},
            'observation': observe(directory)}
    OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
