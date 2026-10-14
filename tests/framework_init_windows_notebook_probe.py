"""Keep an unfinished Run across public teardown and a genuine kernel cell.

Architecture: fail-closed loopback fixture -> actual unfinished offline Run ->
public teardown and immutable history -> real IPython post/pre cell events ->
fresh successful run -> retained callback/resource observations. Main callers:
framework_init_windows_notebook_driver. Why this exists: explicit run.finish
removes source hooks before teardown and cannot establish unfinished cleanup.
The driver owns directories until kernel shutdown; no live-file deletion is
used as a proxy for SDK resource correctness.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading

from framework_init_local_protocol import LocalProtocol
from framework_init_runtime import histories

REFERENCE = sys.argv[1] == 'reference'
DIRECTORY, OUTPUT = Path(sys.argv[2]), Path(sys.argv[3])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk
SETTINGS = dict(console='off', silent=True, disable_git=True, disable_code=True, x_disable_stats=True)
result = {}


def outcome(call):
    try:
        value = call()
        return {'returned_run': hasattr(value, 'id'), 'id': getattr(value, 'id', None),
                'value': 'Run' if hasattr(value, 'id') else value}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error),
                'cause': type(error.__cause__).__name__ if error.__cause__ else None}


def callbacks():
    """Inspect actual owned source event registrations without firing stubs.

    Main callers: before/after each public lifecycle stage. This private
    diagnostic explains actual cell failures; it does not promote child methods
    or treat equal global callback registries as the public API contract.
    """
    from IPython import get_ipython
    shell = get_ipython()
    assert shell.kernel is not None
    return {event: [callback.__qualname__ for callback in shell.events.callbacks[event]
        if getattr(callback, '__qualname__', '').startswith('_WandbInit.')]
        for event in ['pre_run_cell', 'post_run_cell']}


def workers():
    return [thread.name for thread in threading.enumerate()
            if thread.name.startswith(('backprop-flush-', 'backprop-stats-', 'backprop-stop-', 'backprop-model-'))]


def start():
    """Teardown the unfinished run inside cell one and leave hooks untouched.

    Main caller: the driver's first execute request. Immutable progress is saved
    before IPython invokes post_run_cell, so an actual callback error cannot
    erase the earlier teardown/old-handle outcome.
    """
    global server, previous
    server = LocalProtocol()
    server.__enter__()
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = server.proxy
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    for key in ('ALL_PROXY', 'all_proxy'):
        os.environ.pop(key, None)
    os.environ.update(NETRC=str(DIRECTORY / 'netrc'), WANDB_CONFIG_DIR=str(DIRECTORY / 'settings'),
        BACKPROP_HOST=server.host, BACKPROP_STREAM_DATASET_RID='ri.foundry.main.dataset.init-fixture',
        BACKPROP_MEDIA_DATASET_RID='ri.foundry.main.dataset.init-media')
    selected = sdk.Settings(**SETTINGS, base_url=server.host, insecure_disable_ssl=True,
        credentials_file=str(DIRECTORY / 'credentials.json'))
    previous = sdk.init(mode='offline', id='unfinished', project='unfinished-kernel', entity='e',
                        dir=str(DIRECTORY), settings=selected)
    result['first_log'] = outcome(lambda: previous.log({'unfinished_before': 1}))
    result['callbacks_before_teardown'] = callbacks()
    result['teardown'] = outcome(sdk.teardown)
    result.update(previous_global_after_teardown=sdk.run is previous,
        previous_closed_after_teardown=previous._is_finished if REFERENCE else previous._closed,
        callbacks_after_teardown=callbacks(), workers_after_teardown=workers(),
        old_log_after_teardown=outcome(lambda: previous.log({'unfinished_after': 2})),
        old_history_after_teardown=histories(previous, REFERENCE,
            reference_output=OUTPUT.with_suffix('.unfinished.wandb') if REFERENCE else None))
    OUTPUT.with_suffix('.first-stage.json').write_text(json.dumps(result, indent=2) + '\n')


def repeat():
    """Start cell two after real pre_run_cell and prove a fresh core session.

    Main caller: the driver's second execute request. Pinned leftover hooks may
    address its closed old core before/after this cell; those actual diagnostics
    stay in the driver IOPub. Successful new logging does not imply old hooks or
    the old Python handle were cleaned up by public teardown.
    """
    try:
        result['callbacks_at_repeat_start'] = callbacks()
        current = sdk.init(mode='offline', id='repeat', project='unfinished-kernel', entity='e',
            dir=str(DIRECTORY), reinit='finish_previous', settings={**SETTINGS,
                'base_url': server.host, 'insecure_disable_ssl': True})
        result['repeat_log'] = outcome(lambda: current.log({'unfinished_repeat': 3}))
        result['repeat_finish'] = outcome(current.finish)
        result['repeat_teardown'] = outcome(sdk.teardown)
        result['repeat_history'] = histories(current, REFERENCE,
            reference_output=OUTPUT.with_suffix('.repeat.wandb') if REFERENCE else None)
        result.update(global_empty_after_repeat=sdk.run is None,
            callbacks_after_repeat=callbacks(), workers_after_repeat=workers(), unknown_requests=server.unknown,
            requests=[{key: value for key, value in request.items() if key != 'authorization'} for request in server.requests])
        source_root = Path(sdk.__file__).parent
        names = ['sdk/wandb_init.py', 'sdk/wandb_setup.py', 'sdk/wandb_run.py'] if REFERENCE else ['__init__.py', 'run.py']
        data = {'sdk': sdk.__version__, 'python': sys.version,
            'runtime': {name: importlib.metadata.version(name) for name in ['ipykernel', 'jupyter_client', 'IPython']},
            'source_hashes': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in names},
            'observation': result}
        OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    finally:
        server.__exit__(None, None, None)
