"""Execute two actual kernel cells around unfinished public teardown.

Architecture: selected real kernel -> unfinished teardown cell -> retained
post/pre cell diagnostics -> fresh-run cell -> checked shutdown -> directory
removal. Main caller: test_framework_init_windows_notebook_contracts. Why this
exists: one cell with an explicit finish hides the stale source event handlers.
All execution replies and IOPub remain raw; callback failures are not removed.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time

from jupyter_client import KernelManager
from framework_init_notebook_cell import execution_reply

MODE, OUTPUT = sys.argv[1], Path(sys.argv[2])
ROOT = Path(__file__).resolve().parents[1]


def execute(client, code, messages, replies):
    """Wait for this real cell's idle event and correlated checked reply.

    Main caller: run_kernel for each lifecycle phase. Event-manager exceptions
    can appear as IOPub errors even when execute_reply status is ok. Preserve
    both channels so a successful transport reply cannot hide callback failure.
    """
    identity = client.execute(code, allow_stdin=False, stop_on_error=True)
    deadline = time.monotonic() + 35
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Unfinished kernel cell did not complete')
        try:
            message = client.get_iopub_msg(timeout=min(.1, remaining))
        except queue.Empty:
            continue
        if message['parent_header'].get('msg_id') != identity:
            continue
        messages.append({'type': message['msg_type'], 'content': message['content'], 'execute_id': identity})
        if message['msg_type'] == 'status' and message['content']['execution_state'] == 'idle':
            break
    return execution_reply(client, identity, replies)['content']['status']


def run_kernel(directory):
    """Keep fixture storage until the real kernel has fully shut down.

    Main caller: entry point. Every phase owns its observations independently;
    fail-fast execution errors retain progress, shell and IOPub before teardown.
    """
    manager = KernelManager(connection_file=str(Path(directory) / 'jupyter-unfinished.json'))
    manager.kernel_spec.argv = [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}']
    client, messages, replies, statuses = None, [], [], []
    try:
        manager.start_kernel(cwd=directory, env=dict(os.environ), stdout=sys.stdout, stderr=sys.stderr)
        client = manager.client()
        client.start_channels()
        client.wait_for_ready(timeout=30)
        probe_directory = Path(directory) / 'probe-data'
        probe_directory.mkdir()
        probe = ROOT / 'tests/framework_init_windows_notebook_probe.py'
        code = ('import runpy,sys\n' + f'sys.argv={json.dumps([str(probe), MODE, str(probe_directory), str(OUTPUT)])}\n'
                + f'_framework_unfinished = runpy.run_path({str(probe)!r}, run_name="__main__")\n'
                + '_framework_unfinished["start"]()')
        statuses.append(execute(client, code, messages, replies))
        statuses.append(execute(client, '_framework_unfinished["repeat"]()', messages, replies))
        return {'mode': MODE, 'execution_statuses': statuses, 'cells': 2}
    finally:
        OUTPUT.with_suffix('.iopub.json').write_text(json.dumps(messages, indent=2) + '\n')
        OUTPUT.with_suffix('.shell.json').write_text(json.dumps(replies, indent=2) + '\n')
        if client is not None:
            client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)


with tempfile.TemporaryDirectory(prefix='framework-init-unfinished-kernel-') as directory:
    observed = run_kernel(directory)
    OUTPUT.with_suffix('.driver.json').write_text(json.dumps(observed, indent=2) + '\n')
