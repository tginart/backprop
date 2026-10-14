"""Run public authentication inside an actual isolated Jupyter kernel.

Architecture: real kernel stdin -> pinned/native public init or login -> guarded
loopback TLS transport -> retained session and resource observations -> teardown.
Main caller: framework_init_notebook_driver. Why this exists: an IPython shell
configured to look like Jupyter cannot verify password-marked kernel messages,
cancellation or cleanup. Only unavailable hosted servers are replaced, using the
same actual local protocol fixture as the separately verified transport cases.
"""
import base64
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import signal
import sys
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

OLD = 'b' * 40
NEW = 'c' * 40
SETTINGS = dict(console='off', disable_git=True, disable_code=True,
                x_disable_stats=True, init_timeout=5)


def outcome(call):
    """Retain original return and error contracts without credential values."""
    try:
        result = call()
        return {'returned_run': hasattr(result, 'id'), 'id': getattr(result, 'id', None),
                'value': result if result is None or isinstance(result, bool) else 'Run'}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error),
                'cause': type(error.__cause__).__name__ if error.__cause__ else None}


def session(context, host):
    """Inspect host ownership by equality, never serializing secret material.

    Main caller: observe before and after each selection/cancellation stage.
    Retained setup handles and credential candidates have different lifetimes,
    so both are checked independently rather than inferred from one property.
    """
    foreign = host.replace('127.0.0.1', 'localhost')
    if REFERENCE:
        from wandb.sdk.lib.wbauth.authenticate import session_credentials
        candidate = session_credentials(host=host)
        foreign_candidate = session_credentials(host=foreign)
        token = getattr(candidate, 'api_key', None)
    else:
        from backprop.login_session import current_init_session
        candidate = current_init_session(host)
        foreign_candidate = current_init_session(foreign)
        token = getattr(candidate, 'token', None)
    settings = context.settings
    environment_key = os.environ.get('WANDB_API_KEY' if REFERENCE else 'BACKPROP_TOKEN')
    return {'settings_old': settings.api_key == OLD, 'settings_new': settings.api_key == NEW,
            'settings_mode': settings.mode, 'session_old': token == OLD,
            'session_new': token == NEW, 'foreign_session_empty': foreign_candidate is None,
            'environment_old': environment_key == OLD, 'environment_new': environment_key == NEW}


def files(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(directory).rglob('*') if path.is_file()
            and (path.name in {'netrc', 'credentials.json'} or 'settings' in path.parts)}


def request_record(request):
    """Retain HTTP evidence with credential equality rather than secret headers."""
    header = request.get('authorization', '') or ''
    def matches(token):
        basic = 'Basic ' + base64.b64encode(('api:' + token).encode()).decode()
        return header in {'Bearer ' + token, basic}
    return {**{key: value for key, value in request.items() if key != 'authorization'},
            'credential_old': matches(OLD), 'credential_new': matches(NEW)}


def observe(directory):
    """Exercise genuine stdin, retained handles, fail-fast auth and recovery.

    Main caller: kernel entry point. Init's deferred credential selection is
    distinguished from verified login rejection and subsequent backend failure.
    All unknown routes are denied by the fixture; no hosted request is allowed.
    """
    failure = None
    if SCENARIO == 'login_rejection':
        failure = 'auth' if REFERENCE else 'read'
    elif SCENARIO == 'backend_rejection':
        failure = 'init' if REFERENCE else 'publish'
    with LocalProtocol(failure=failure, status=401 if SCENARIO == 'login_rejection' else 403) as server:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = server.proxy
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        for key in ('ALL_PROXY', 'all_proxy'):
            os.environ.pop(key, None)
        os.environ.update(NETRC=str(Path(directory) / 'netrc'),
            WANDB_CONFIG_DIR=str(Path(directory) / 'settings'), BACKPROP_HOST=server.host,
            BACKPROP_STREAM_DATASET_RID='ri.foundry.main.dataset.init-fixture',
            BACKPROP_MEDIA_DATASET_RID='ri.foundry.main.dataset.init-media')
        supplied = sdk.Settings(**SETTINGS, base_url=server.host, insecure_disable_ssl=True,
            credentials_file=str(Path(directory) / 'credentials.json'),
            login_timeout=.25 if SCENARIO == 'timeout' else None, relogin=True)
        context = sdk.setup(supplied)
        sdk.login(key=OLD, host=server.host, verify=False)
        previous = None
        if SCENARIO != 'login_rejection':
            previous = sdk.init(mode='offline', id='previous', project='kernel-init', entity='e',
                                dir=directory, settings=SETTINGS)
        before = session(context, server.host)
        before_files = files(directory)
        before_alarm = signal.getsignal(signal.SIGALRM) if hasattr(signal, 'SIGALRM') else None
        before_threads = {thread.ident for thread in threading.enumerate()}
        # Non-silent pinned explicit login may resolve its old account identity.
        # Retain that separate stage rather than attributing it to the prompt.
        setup_requests = len(server.requests)
        if SCENARIO == 'login_rejection':
            result = outcome(lambda: sdk.login(host=server.host, relogin=True, force=True, verify=True))
        else:
            selected = sdk.Settings(**SETTINGS, base_url=server.host, insecure_disable_ssl=True,
                credentials_file=supplied.credentials_file, relogin=True,
                login_timeout=.25 if SCENARIO == 'timeout' else None)
            result = outcome(lambda: sdk.init(project='kernel-init', entity='e', dir=directory,
                reinit='finish_previous' if SCENARIO == 'backend_rejection' else 'return_previous',
                force=SCENARIO == 'force', settings=selected))
        result.update(before=before, after=session(context, server.host),
            credential_files_unchanged=files(directory) == before_files,
            previous_global=previous is not None and sdk.run is previous,
            previous_log=outcome(lambda: previous.log({'kernel_survived': 1})) if previous else None,
            alarm_restored=before_alarm == (signal.getsignal(signal.SIGALRM) if hasattr(signal, 'SIGALRM') else None),
            active_alarm=signal.getitimer(signal.ITIMER_REAL)[0] if hasattr(signal, 'setitimer') else None,
            new_threads=[thread.name for thread in threading.enumerate() if thread.ident not in before_threads],
            stdin_tty=sys.stdin.isatty(), stderr_tty=sys.stderr.isatty(),
            setup_requests=[request_record(request) for request in server.requests[:setup_requests]])
        # Survival is recorded above, before explicitly finishing the retained
        # handle. Pinned teardown alone does not finish it or unregister its
        # notebook callback, so a later cell event would address a closed core.
        result['previous_finish'] = outcome(previous.finish) if previous is not None else None
        sdk.teardown()
        repeat = sdk.init(mode='offline', id='repeated', project='kernel-init', entity='e',
                          dir=directory, settings={**SETTINGS, 'silent': True})
        result['repeat_log'] = outcome(lambda: repeat.log({'kernel_repeat': 1}))
        repeat.finish()
        sdk.teardown()
        result.update(global_empty=sdk.run is None,
            native_workers_after_teardown=[thread.name for thread in threading.enumerate()
                if thread.name.startswith(('backprop-flush-', 'backprop-stats-', 'backprop-stop-', 'backprop-model-'))],
            unknown_requests=server.unknown,
            requests=[request_record(request) for request in server.requests[setup_requests:]])
        return result


def write_observation(directory):
    """Leave directory cleanup to the driver after the actual kernel exits.

    Main caller: the generated kernel cell. Why this exists: pinned notebook
    logging can retain Windows file handles even after public run teardown.
    Removing the fixture inside its live process tests file-lock deletion,
    rather than the selected authentication and explicit finish lifecycle.
    """
    from IPython import get_ipython
    from ipykernel.kernelbase import Kernel
    import jupyter_client.client
    shell = get_ipython()
    assert shell.kernel is not None
    assert not sys.stdin.isatty() and not sys.stderr.isatty()
    source_root = Path(sdk.__file__).parent
    names = ['sdk/wandb_init.py', 'sdk/wandb_login.py', 'sdk/lib/wbauth/prompt.py',
             'errors/term.py', 'sdk/lib/timed_input.py'] if REFERENCE else ['login_prompt.py', 'login_session.py']
    data = {'sdk': sdk.__version__, 'python': sys.version, 'scenario': SCENARIO,
            'runtime': {name: importlib.metadata.version(name) for name in ['ipykernel', 'jupyter_client', 'IPython']},
            'kernel_type': type(shell.kernel).__module__ + '.' + type(shell.kernel).__name__,
            'runtime_source_hashes': {
                'ipykernel._input_request': hashlib.sha256(inspect.getsource(Kernel._input_request).encode()).hexdigest(),
                'jupyter_client.client': hashlib.sha256(Path(jupyter_client.client.__file__).read_bytes()).hexdigest()},
            'source_hashes': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in names},
            'observation': observe(directory)}
    OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


write_observation(sys.argv[3])
