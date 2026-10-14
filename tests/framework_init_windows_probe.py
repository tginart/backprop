"""Observe public authentication through an actual attached Windows console.

Architecture: real console diagnostics -> fail-closed local TLS service ->
retained host-owned auth/run -> actual menu/token input -> checked cleanup and
repeat offline history. Main caller: test_framework_init_windows_contracts.
Why this exists: terminal input must remain the real SDK/CRT path; only hosted
GraphQL/Foundry endpoints are supplied by the existing loopback protocol fixture.
No secret is serialized: session/header evidence uses equality booleans only.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import getpass
import json
import os
from pathlib import Path
import sys
import threading
import time
import msvcrt

from framework_init_local_protocol import LocalProtocol
from framework_init_runtime import histories
from framework_init_windows_console import module_source_evidence

REFERENCE = sys.argv[1] == 'reference'
SCENARIO, DIRECTORY, OUTPUT = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
assert os.name == 'nt', 'Actual Windows console is required'
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

OLD, NEW = 'b' * 40, 'c' * 40
SETTINGS = dict(console='off', disable_git=True, disable_code=True, x_disable_stats=True, init_timeout=5)


def outcome(call):
    try:
        value = call()
        return {'returned_run': hasattr(value, 'id'), 'id': getattr(value, 'id', None),
                'value': 'Run' if hasattr(value, 'id') else value}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error),
                'cause': type(error.__cause__).__name__ if error.__cause__ else None}


def console_state():
    """Require genuine CRT terminal streams and real console-mode API handles.

    Main caller: before/after the public auth call. Pipes cannot satisfy these
    checks, so ConPTY attachment failure cannot become false Windows evidence.
    """
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetStdHandle.argtypes, api.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
    api.GetFileType.argtypes, api.GetFileType.restype = [wintypes.HANDLE], wintypes.DWORD
    api.GetConsoleMode.argtypes, api.GetConsoleMode.restype = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL
    state = {}
    for name, stream, identifier in [('stdin', sys.stdin, -10), ('stdout', sys.stdout, -11), ('stderr', sys.stderr, -12)]:
        handle, mode = api.GetStdHandle(identifier & 0xffffffff), wintypes.DWORD()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)):
            raise ctypes.WinError(ctypes.get_last_error())
        state[name] = {'tty': stream.isatty(), 'file_type': api.GetFileType(handle), 'console_mode': mode.value}
        assert state[name]['tty'] and state[name]['file_type'] == 2
    return state


def session(context, host):
    """Retain host ownership without outputting credentials or candidate reprs."""
    foreign = host.replace('127.0.0.1', 'localhost')
    if REFERENCE:
        from wandb.sdk.lib.wbauth.authenticate import session_credentials
        candidate, other = session_credentials(host=host), session_credentials(host=foreign)
        token = getattr(candidate, 'api_key', None)
    else:
        from backprop.login_session import current_init_session
        candidate, other = current_init_session(host), current_init_session(foreign)
        token = getattr(candidate, 'token', None)
    return {'session_old': token == OLD, 'session_new': token == NEW,
            'settings_old': context.settings.api_key == OLD,
            'settings_new': context.settings.api_key == NEW,
            'mode': context.settings.mode, 'foreign_session_empty': other is None}


def credential_files():
    return {str(path.relative_to(DIRECTORY)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in DIRECTORY.rglob('*') if path.is_file()
            and (path.name in {'netrc', 'credentials.json'} or 'settings' in path.parts)}


def request_record(request):
    header = request.get('authorization') or ''
    def matches(token):
        return header in {'Bearer ' + token, 'Basic ' + base64.b64encode(('api:' + token).encode()).decode()}
    return {**{key: value for key, value in request.items() if key != 'authorization'},
            'credential_old': matches(OLD), 'credential_new': matches(NEW)}


def global_after_cleanup(selected_global):
    """Retain the actual binding left by public teardown before another init.

    Main caller: observe, after finishing the previous handle and teardown.
    Why this exists: pinned teardown can leave a newly selected disabled Run
    bound even after the earlier active Run finishes. Identity/id/disabled state
    distinguish that source outcome from native clearing every owned handle.
    """
    current = sdk.run
    return {'present': current is not None,
            'is_selected_global': current is not None and current is selected_global,
            'disabled': current.disabled if current is not None else None,
            'id': current.id if current is not None else None}


def observe():
    """Capture selection before cleanup, then finish and prove repeat logging.

    Main caller: process entry point. Source silent=False may perform actual
    account lookup for display; all such requests remain raw, with setup traffic
    kept separate from selected init/login. Explicit finish here does not test
    the unfinished-kernel teardown boundary owned by the separate probe.
    """
    failure = 'auth' if REFERENCE else 'read'
    with LocalProtocol(failure=failure if SCENARIO == 'login_rejection' else None, status=401) as server:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = server.proxy
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        for key in ('ALL_PROXY', 'all_proxy'):
            os.environ.pop(key, None)
        os.environ.update(NETRC=str(DIRECTORY / 'netrc'), WANDB_CONFIG_DIR=str(DIRECTORY / 'settings'),
            BACKPROP_HOST=server.host, BACKPROP_STREAM_DATASET_RID='ri.foundry.main.dataset.init-fixture',
            BACKPROP_MEDIA_DATASET_RID='ri.foundry.main.dataset.init-media')
        timeout = .35 if SCENARIO in {'menu_timeout', 'menu_eof_timed', 'secret_timeout'} else None
        selected = sdk.Settings(**SETTINGS, base_url=server.host, insecure_disable_ssl=True,
            credentials_file=str(DIRECTORY / 'credentials.json'), relogin=True, login_timeout=timeout)
        context = sdk.setup(selected)
        sdk.login(key=OLD, host=server.host, verify=False)
        previous = None if SCENARIO == 'login_rejection' else sdk.init(mode='offline', id='previous',
            project='windows-init', entity='e', dir=str(DIRECTORY), settings=SETTINGS)
        before, files_before, modes_before = session(context, server.host), credential_files(), console_state()
        threads_before = {thread.ident for thread in threading.enumerate()}
        setup_count = len(server.requests)
        start = time.monotonic()
        if SCENARIO == 'login_rejection':
            result = outcome(lambda: sdk.login(host=server.host, relogin=True, force=True, verify=True))
        else:
            result = outcome(lambda: sdk.init(project='windows-init', entity='e', dir=str(DIRECTORY),
                reinit='return_previous', force=SCENARIO == 'secret_timeout', settings=selected))
        result.update(elapsed_seconds=time.monotonic() - start, before=before,
            after=session(context, server.host), console_before=modes_before, console_after=console_state(),
            credential_files_unchanged=credential_files() == files_before,
            previous_global=previous is not None and sdk.run is previous,
            previous_log=outcome(lambda: previous.log({'windows_survived': 1})) if previous else None,
            threads_after_call=[thread.name for thread in threading.enumerate() if thread.ident not in threads_before],
            setup_requests=[request_record(request) for request in server.requests[:setup_count]])
        selected_global = sdk.run
        result['previous_finish'] = outcome(previous.finish) if previous is not None else None
        sdk.teardown()
        result['global_empty_after_teardown'] = sdk.run is None
        result['global_after_teardown'] = global_after_cleanup(selected_global)
        repeat = sdk.init(mode='offline', id='repeat', project='windows-init', entity='e', dir=str(DIRECTORY),
            settings={**SETTINGS, 'silent': True})
        result['repeat_log'] = outcome(lambda: repeat.log({'windows_repeat': 1}))
        repeat.finish()
        sdk.teardown()
        result['repeat_history'] = histories(repeat, REFERENCE,
            reference_output=OUTPUT.with_suffix('.wandb') if REFERENCE else None)
        result.update(global_empty=sdk.run is None,
            native_workers_after_teardown=[thread.name for thread in threading.enumerate()
                if thread.name.startswith(('backprop-flush-', 'backprop-stats-', 'backprop-stop-', 'backprop-model-'))],
            unknown_requests=server.unknown,
            requests=[request_record(request) for request in server.requests[setup_count:]])
        return result


source_root = Path(sdk.__file__).parent
names = ['sdk/wandb_init.py', 'sdk/wandb_login.py', 'sdk/lib/wbauth/prompt.py', 'errors/term.py',
         'sdk/lib/timed_input.py', 'sdk/wandb_setup.py', 'sdk/lib/noop_run.py'] if REFERENCE else [
             'login_prompt.py', 'login_session.py', '__init__.py', 'run.py']
version = sys.getwindowsversion()
data = {'sdk': sdk.__version__, 'python': sys.version,
        'platform': {name: getattr(version, name) for name in ['major', 'minor', 'build', 'platform', 'service_pack']},
        'runtime_source_hashes': {'getpass.py': hashlib.sha256(Path(getpass.__file__).read_bytes()).hexdigest(),
                                  'python_executable': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()},
        'runtime_modules': {'getpass': module_source_evidence(getpass),
                            'msvcrt': module_source_evidence(msvcrt)},
        'scenario': SCENARIO, 'source_hashes': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in names}, 'observation': observe()}
OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
