"""Compare export result contracts through the genuine pinned public method.

Architecture: installed Run wrapper -> unchanged asynchronous result watcher ->
explicit service responses; native Run -> real journal -> actual Parquet file.
Main callers: release comparisons and CI. The fixture supplies W&B service
responses, never claims to run its storage backend. File paths and Parquet
layout are recorded separately as Foundry substitutions.
"""
import asyncio
import contextlib
import ctypes
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

REFERENCE = sys.argv[1] == 'reference'
if REFERENCE:
    import wandb as w
    from wandb.apis.public import runs
    from wandb.apis.public.runhistory import downloads
    from wandb.proto import wandb_api_pb2 as apb
    from wandb.sdk.lib.service.service_connection import WandbApiFailedError
    assert w.__version__ == '0.30.0'
    for obj, expected in [(runs.Run, '2d2db69df0175276a3b620094364a848ee2c2857ce123a8656b11e14bddba8da'),
                          (downloads.DownloadHistoryResult, '3bbfc746cf1ce927085060a943c2278d212dcfb232122e09c6509fe40856eed4')]:
        assert hashlib.sha256(Path(inspect.getfile(obj)).read_bytes()).hexdigest() == expected
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


class Service:
    """Supply recorded response shapes at the W&B service boundary only."""
    def __init__(self, live, failure):
        self.live, self.failure, self.requests = live, failure, []

    def send_api_request(self, request):
        initial = request.read_run_history_request.download_run_history_init
        self.requests.append({'directory': initial.download_dir, 'complete': initial.require_complete_history})
        if initial.require_complete_history and self.live:
            raise WandbApiFailedError('incomplete', response=apb.ApiErrorResponse(error_type=apb.ErrorType.INCOMPLETE_RUN_HISTORY_ERROR))
        self.path = Path(initial.download_dir) / 'team_exports_run_0.runhistory.parquet'
        return apb.ApiResponse(read_run_history_response=apb.ReadRunHistoryResponse(
            download_run_history_init=apb.DownloadRunHistoryInitResponse(request_id=7, contains_live_data=self.live)))

    async def send_api_request_async(self, request):
        async def wait_async(timeout=None):
            message = request.read_run_history_request
            if message.HasField('download_run_history'):
                output = apb.DownloadRunHistoryResponse(downloaded_files=[] if self.failure else [str(self.path)],
                    errors={str(self.path): 'disk full'} if self.failure else {})
                return apb.ApiResponse(read_run_history_response=apb.ReadRunHistoryResponse(download_run_history=output))
            return apb.ApiResponse(read_run_history_response=apb.ReadRunHistoryResponse(
                download_run_history_status=apb.DownloadRunHistoryStatusResponse()))
        return SimpleNamespace(wait_async=wait_async)


def signature(call):
    return [(p.name, p.kind.name, 'required' if p.default is p.empty else repr(p.default)) for p in inspect.signature(call).parameters.values()]


def observe(cls, directory, complete=True, *, live=False, failure=False, positional=False, extra=False):
    """Execute the whole public call; normalize only resource-specific filenames."""
    with contextlib.ExitStack() as stack:
        if REFERENCE:
            service = Service(live, failure)
            owner = SimpleNamespace(entity='team', project='exports', id='run', _service_api=service)
            runner = SimpleNamespace(run=lambda task: asyncio.run(task()))
            stack.enter_context(patch.object(runs.wandb_setup, 'singleton', return_value=SimpleNamespace(asyncer=runner, settings_if_loaded=None)))
        else:
            sink = w.MemorySink()
            scratch = stack.enter_context(tempfile.TemporaryDirectory())
            writer = w.Run(entity='team', project='exports', id='run', dir=scratch, _sink=sink, _deployment={},
                           settings={'silent': True, 'console': 'off', 'x_disable_stats': True, 'disable_git': True})
            stack.callback(writer.finish)
            writer.log({'loss': 2}); writer.flush()
            if not live: writer.finish()
            owner = w.Api._from_store(sink, entity='team', project='exports').run('run')
            if failure:
                import pyarrow.parquet
                stack.enter_context(patch.object(pyarrow.parquet, 'write_table', side_effect=OSError('disk full')))
        try:
            if positional:
                result = cls.download_history_exports(owner, directory, complete)
            else:
                result = cls.download_history_exports(owner, download_dir=directory, require_complete_history=complete, **({'extra': 1} if extra else {}))
            def path_info(path):
                return {'type': type(path).__name__, 'parent': str(path.parent), 'absolute': path.is_absolute()}
            return {'type': type(result).__name__, 'public_class_identity': type(result) is importlib.import_module('wandb.apis.public').DownloadHistoryResult, 'paths': [path_info(path) for path in result.paths],
                    'live': result.contains_live_data, 'errors': None if result.errors is None else [{'path': path_info(path), 'message': message} for path, message in result.errors.items()]}
        except Exception as error:
            return {'error': type(error).__name__, 'message': str(error)}


def value_contracts(cls, error_cls):
    """Exercise the selected returned dataclass and builtin exception behavior."""
    paths, errors = [Path('file.parquet')], {Path('bad.parquet'): 'failure'}
    value = cls(paths, False, errors)
    output = {'signature': signature(cls), 'paths_identity': value.paths is paths, 'errors_identity': value.errors is errors,
              'defaults': cls([], True).errors, 'equality': value == cls(paths, False, errors), 'repr': repr(value), 'errors': {}}
    for name, call in [('missing', lambda: cls()), ('extra', lambda: cls([], False, None, True)),
                       ('set_field', lambda: setattr(value, 'paths', [])), ('hash', lambda: hash(value))]:
        try: output['errors'][name] = {'value': call()}
        except Exception as error: output['errors'][name] = {'error': type(error).__name__}
    exception = error_cls('message'); exception.add_note('note')
    output['exception'] = {'args': exception.args, 'notes': exception.__notes__, 'same_traceback': exception.with_traceback(None) is exception}
    for name, call in [('bad_note', lambda: exception.add_note(3)), ('missing_note', lambda: exception.add_note()),
                       ('bad_traceback', lambda: exception.with_traceback(3)), ('missing_traceback', lambda: exception.with_traceback())]:
        try: output['errors'][name] = {'value': call()}
        except Exception as error: output['errors'][name] = {'error': type(error).__name__, 'message': str(error)}
    try: raise error_cls('raised')
    except error_cls as raised:
        traceback = raised.__traceback__
        output['exception']['real_traceback_identity'] = raised.with_traceback(traceback).__traceback__ is traceback
    output['alias_identity'] = cls is importlib.import_module('wandb.apis.public').DownloadHistoryResult and error_cls is importlib.import_module('wandb.apis.public').IncompleteRunHistoryError
    return output


def main():
    output = {'exports': {}, 'returned_objects': {}}
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as scratch:
        os.chdir(scratch)
        try:
            for module in ['wandb.apis.public', 'wandb.apis.public.runs']:
                cls = importlib.import_module(module).Run
                cases = {}
                for name, directory in [('string', 'export'), ('path', Path('export')), ('absolute', Path(scratch) / 'absolute'),
                                        ('none_stringified', None), ('integer_stringified', 42)]:
                    # Keep the temporary directory neutral in comparable outputs.
                    value = observe(cls, directory)
                    cases[name] = json.loads(json.dumps(value).replace(scratch, '<root>'))
                for name, complete, live, failure, positional, extra in [
                    ('positional', True, False, False, True, False), ('live_default', True, True, False, False, False),
                    ('live_allowed', False, True, False, False, False), ('file_failure', False, False, True, False, False),
                    ('flag_none', None, True, False, False, False), ('flag_zero', 0, True, False, False, False),
                    ('flag_two', 2, False, False, False, False), ('flag_float', 1.0, False, False, False, False),
                    ('flag_string', 'yes', False, False, False, False), ('flag_list', [], False, False, False, False),
                    ('flag_largest', (1 << (ctypes.sizeof(ctypes.c_long)*8-1))-1, False, False, False, False),
                    ('flag_smallest', -(1 << (ctypes.sizeof(ctypes.c_long)*8-1)), False, False, False, False),
                    ('flag_positive_overflow', 1 << (ctypes.sizeof(ctypes.c_long)*8-1), False, False, False, False),
                    ('flag_negative_overflow', -(1 << (ctypes.sizeof(ctypes.c_long)*8-1))-1, False, False, False, False),
                    ('extra_keyword', True, False, False, False, True),
                ]:
                    value = observe(cls, 'export', complete, live=live, failure=failure, positional=positional, extra=extra)
                    cases[name] = json.loads(json.dumps(value).replace(scratch, '<root>'))
                output['exports'][module] = {'signature': signature(cls.download_history_exports), 'cases': cases}
            for module in ['wandb.apis.public', 'wandb.apis.public.runhistory', 'wandb.apis.public.runhistory.downloads']:
                try:
                    imported = importlib.import_module(module)
                    output['returned_objects'][module] = value_contracts(imported.DownloadHistoryResult, imported.IncompleteRunHistoryError)
                except ModuleNotFoundError:
                    output['returned_objects'][module] = {'error': 'ModuleNotFoundError'}
        finally: os.chdir(old)
    print(json.dumps(output, sort_keys=True))


if __name__ == '__main__': main()
