"""Observe automatic adapter ownership across simultaneous real Run handles.

Architecture: isolated SDK -> offline active handles -> pinned notification or
real TensorBoard writer -> selected owners and immutable histories -> teardown.
Main callers: init adapter contracts. Why this exists: create_new deliberately
leaves the global handle unchanged, so an automatic writer must not duplicate
data into every live handle. Boundary mode replaces only absent dependency
installation and summary decoding, while retaining actual init/run routing.
"""
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from framework_init_runtime import histories

REFERENCE = sys.argv[1] == 'reference'
SCENARIO = sys.argv[2]
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == '0.30.0'
else:
    import backprop as sdk

SETTINGS = dict(console='off', silent=True, disable_git=True, disable_code=True,
                x_disable_stats=True, init_timeout=5)
BOUNDARY = SCENARIO == 'boundary'


def cases(directory):
    """Keep hook installation alive, recording only genuine dispatch owners.

    Main callers: process entry point. Boundary fixtures explicitly replace the
    absent optional installation and decoder, not active-run policy, global
    routing or Run.flush. Writer mode uses installed EventFileWriter/protobufs
    and actual native reader/pinned core to establish imported values.
    """
    result = {}
    tb = importlib.import_module('wandb.integration.tensorboard' if REFERENCE else 'backprop.integration.tensorboard')
    for first_sync in [True, False, None]:
        sdk.teardown()
        tb.unpatch()
        trace = []
        with ExitStack() as stack:
            if BOUNDARY:
                if REFERENCE:
                    initializer = importlib.import_module('wandb.sdk.wandb_init')
                    stack.enter_context(patch.object(initializer, '_monkeypatch_tensorboard'))
                else:
                    stack.enter_context(patch.object(tb, 'patch'))
                    stack.enter_context(patch('backprop.settings_policy.validate_logging_settings', return_value=[]))
                    stack.enter_context(patch.object(tb, 'sync', side_effect=lambda run, *args, **kwargs: trace.append(run.id)))
            first = None
            if first_sync is not None:
                first = sdk.init(mode='offline', id='first', project='adapter-routing', entity='e',
                    dir=directory, sync_tensorboard=first_sync, settings=SETTINGS)
            second = sdk.init(mode='offline', id='second', project='adapter-routing', entity='e',
                dir=directory, sync_tensorboard=True, reinit='create_new', settings=SETTINGS)
            if BOUNDARY:
                path = str(Path(directory) / ('events-' + str(first_sync)))
                Path(path).mkdir()
                if REFERENCE:
                    notify = importlib.import_module('wandb.integration.tensorboard.monkeypatch')._notify_tensorboard_logdir
                    if first is not None:
                        original = first._tensorboard_callback
                        def callback(*args, **kwargs):
                            trace.append(first.id)
                            return original(*args, **kwargs)
                        stack.enter_context(patch.object(first, '_tensorboard_callback', side_effect=callback))
                    notify(path, root_logdir=path)
                else:
                    # Discovery's current-run adoption normally sees a real
                    # installed hook. Expose that state at the unavailable
                    # dependency boundary, retaining real _discover routing.
                    tb._patches.append((type('UnavailableWriter', (), {'writer': None}), 'writer', None))
                    tb._discover(path)
                if first is not None and not REFERENCE:
                    first.flush()
                if not REFERENCE:
                    second.flush()
            else:
                from tensorboard.compat.proto.event_pb2 import Event
                from tensorboard.compat.proto.summary_pb2 import Summary
                writer_module = importlib.import_module('tensorboard.summary.writer.event_file_writer')
                path = Path(directory) / ('writer-events-' + str(first_sync))
                writer = writer_module.EventFileWriter(str(path))
                writer.add_event(Event(wall_time=1000, step=7, summary=Summary(value=[
                    Summary.Value(tag='routing_metric', simple_value=.75)])))
                writer.flush()
                writer.close()
            bound_id = getattr(sdk.run, 'id', None)
            second.finish()
            if first is not None:
                first.finish()
            first_values = []
            second_values = []
            if not BOUNDARY:
                if first is not None:
                    rows = histories(first, REFERENCE, reference_output=OUTPUT.with_name(OUTPUT.stem + f'-{first_sync}-first.wandb') if REFERENCE else None)
                    first_values = [row['routing_metric'] for row in rows if 'routing_metric' in row]
                rows = histories(second, REFERENCE, reference_output=OUTPUT.with_name(OUTPUT.stem + f'-{first_sync}-second.wandb') if REFERENCE else None)
                second_values = [row['routing_metric'] for row in rows if 'routing_metric' in row]
            result[str(first_sync)] = {'global_id': bound_id, 'trace': trace,
                'automatic_owners': sorted(set(trace)), 'first_values': first_values,
                'second_values': second_values, 'global_empty': sdk.run is None}
        sdk.teardown()
        tb.unpatch()
    return result


with tempfile.TemporaryDirectory(prefix='framework-init-adapter-') as directory:
    os.environ['NETRC'] = str(Path(directory) / 'netrc')
    os.environ['WANDB_CONFIG_DIR'] = str(Path(directory) / 'settings')
    names = ['sdk/wandb_init.py', 'integration/tensorboard/monkeypatch.py'] if REFERENCE else [
        '__init__.py', 'init_integrations.py', 'run.py', 'integration/tensorboard/__init__.py']
    root = Path(sdk.__file__).parent
    OUTPUT.write_text(json.dumps({'sdk': sdk.__version__, 'scenario': SCENARIO,
        'source_hashes': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
        'observations': cases(directory)}, indent=2, sort_keys=True) + '\n')
