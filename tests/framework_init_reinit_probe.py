"""Measure default and explicit reinit with real IPython and offline SDKs.

Architecture: real selected shell -> public init policy -> genuine local runs
and histories -> public teardown -> raw JSON. Main callers: reinit contracts.
Why this exists: mocking in_notebook cannot establish actual shell detection,
global routing or whether failed setup leaves the old handle usable.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile

from framework_init_runtime import histories

REFERENCE = sys.argv[1] == 'reference'
SHELL = sys.argv[2]
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

SETTINGS = dict(console='off', silent=True, disable_git=True, disable_code=True,
                save_code=False, x_disable_stats=True, init_timeout=5)


def attempt(call):
    """Keep original failure classes/messages rather than normalizing them."""
    try:
        value = call()
        return {'value': None if value is None else getattr(value, 'id', value)}
    except BaseException as error:
        return {'error': type(error).__name__, 'message': str(error)}


def run_cases(directory):
    """Exercise active policy and routing before capturing both histories.

    Main caller: process entry point. Both direct handle.log and the global
    sdk.log run through genuine journal/core implementations. A post-failure
    repeat session verifies that teardown leaves no stale active-run policy.
    """
    result = {}
    policies = [None, 'default', 'return_previous', 'finish_previous', 'create_new', True, False]
    for index, policy in enumerate(policies):
        sdk.teardown()
        first = sdk.init(mode='offline', project='reinit-shell', entity='e', dir=directory,
                         id=f'first-{index}', settings=SETTINGS)
        first.log({'first_metric': 1})
        arguments = {} if policy is None else {'reinit': policy}
        second = sdk.init(mode='offline', project='reinit-shell', entity='e', dir=directory,
                          id=f'second-{index}', settings=SETTINGS, **arguments)
        second.log({'second_metric': 2})
        global_before = getattr(sdk.run, 'id', None)
        global_log = attempt(lambda: sdk.log({'global_metric': 3}))
        first_log = attempt(lambda: first.log({'first_after': 4}))
        first.finish()
        global_after_first = getattr(sdk.run, 'id', None)
        second_log = attempt(lambda: second.log({'second_after_first': 5}))
        second.finish()
        # Capturing the first reference run tears down its completed service;
        # both runs are already finished, so each capture is immutable.
        first_rows = histories(first, REFERENCE, reference_output=OUTPUT.with_name(OUTPUT.stem + f'-{index}-first.wandb') if REFERENCE else None)
        second_rows = first_rows if second is first else histories(second, REFERENCE,
            reference_output=OUTPUT.with_name(OUTPUT.stem + f'-{index}-second.wandb') if REFERENCE else None)
        keys = ['first_metric', 'second_metric', 'global_metric', 'first_after', 'second_after_first']
        selected = lambda rows: [{key: row[key] for key in keys if key in row} for row in rows
                                 if any(key in row for key in keys)]
        result[repr(policy)] = {'same_handle': second is first, 'first_id': first.id, 'second_id': second.id,
            'global_before_first_finish': global_before, 'global_after_first_finish': global_after_first,
            'global_log': global_log, 'first_log': first_log, 'second_log': second_log,
            'first_history': selected(first_rows), 'second_history': selected(second_rows),
            'global_empty': sdk.run is None}
    for field, value in [('mode', 'not-a-mode'), ('reinit', 'not-a-policy'), ('config', object())]:
        sdk.teardown()
        first = sdk.init(mode='offline', project='reinit-shell', entity='e', dir=directory,
                         id='validation-first-' + field, settings=SETTINGS)
        before = set(Path(directory).rglob('events.jsonl')) if not REFERENCE else set(Path(directory).rglob('*.wandb'))
        invalid = dict(mode='offline', id='validation-rejected-' + field, project='reinit-shell',
                       entity='e', dir=directory, settings=SETTINGS, reinit='finish_previous')
        invalid[field] = value
        observed = attempt(lambda: sdk.init(**invalid))
        after = set(Path(directory).rglob('events.jsonl')) if not REFERENCE else set(Path(directory).rglob('*.wandb'))
        observed.update(previous_global=sdk.run is first,
                        previous_log=attempt(lambda: first.log({'validation_survived': 1})),
                        new_transactions=len(after - before))
        first.finish()
        sdk.teardown()
        result['invalid_' + field] = observed
    repeat = sdk.init(mode='offline', project='reinit-shell', entity='e', dir=directory,
                      id='repeated', settings=SETTINGS)
    result['repeat'] = attempt(lambda: repeat.log({'repeated': 1}))
    repeat.finish()
    sdk.teardown()
    result['repeat']['global_empty'] = sdk.run is None
    return result


with tempfile.TemporaryDirectory(prefix='framework-init-reinit-') as directory:
    os.environ['NETRC'] = str(Path(directory) / 'netrc')
    os.environ['WANDB_CONFIG_DIR'] = str(Path(directory) / 'settings')
    shell = None
    if SHELL != 'python':
        from traitlets.config import Config
        config = Config()
        config.HistoryManager.hist_file = ':memory:'
        if SHELL == 'terminal':
            from IPython.terminal.interactiveshell import TerminalInteractiveShell as ShellType
        else:
            assert SHELL == 'jupyter_detection'
            from IPython.core.interactiveshell import InteractiveShell as ShellType
            config.IPKernelApp.connection_file = 'jupyter-contract-connection.json'
        shell = ShellType.instance(config=config, ipython_dir=directory)
    try:
        if REFERENCE:
            from wandb.sdk.lib.ipython import in_notebook, in_jupyter
        else:
            from backprop.settings import in_notebook, in_jupyter
        source_root = Path(sdk.__file__).parent
        names = ['sdk/wandb_init.py', 'sdk/lib/ipython.py'] if REFERENCE else ['__init__.py', 'settings.py', 'init_integrations.py']
        data = {'sdk': sdk.__version__, 'python': sys.version, 'shell': SHELL,
                'ipython': importlib.metadata.version('ipython') if shell else None,
                'detection': {'in_notebook': in_notebook(), 'in_jupyter': in_jupyter()},
                'source_hashes': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in names},
                'observations': run_cases(directory)}
        OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    finally:
        sdk.teardown()
        if shell is not None:
            shell.history_manager.end_session()
            ShellType.clear_instance()
