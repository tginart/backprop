"""Drive real stdin messages from a fresh kernel selected by its interpreter.

Architecture: checked Python -> actual ipykernel process -> stdin-channel replies
or interrupt -> immutable JSON and raw IOPub -> public kernel shutdown.
Main caller: test_framework_init_notebook_contracts. Why this exists: replacing
builtins.input would skip the non-TTY password channel and interrupt lifecycle.
The driver never stores submitted tokens, even in its raw message evidence.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time

from jupyter_client import KernelManager
from framework_init_notebook_cell import execution_reply, probe_cell

MODE, SCENARIO = sys.argv[1:3]
OUTPUT = Path(sys.argv[-1])
ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'c' * 40


def run_kernel(directory):
    """Use actual password flags and preserve every unrelated diagnostic.

    Main caller: process entry point. The selected kernelspec is overridden
    with this exact interpreter; inherited global kernels never choose another
    environment. Interrupts and EOF are sent only after a real stdin request.
    Timeout replies arrive late, retaining the pinned reader's real behavior.
    """
    connection_name = 'kernel-contract.json' if SCENARIO == 'unmarked' else 'jupyter-contract.json'
    manager = KernelManager(connection_file=str(Path(directory) / connection_name))
    manager.kernel_spec.argv = [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}']
    messages, requests, shell_messages = [], [], []
    client = None
    try:
        manager.start_kernel(cwd=directory, env=dict(os.environ), stdout=sys.stdout, stderr=sys.stderr)
        client = manager.client()
        client.start_channels()
        client.wait_for_ready(timeout=30)
        probe_directory = Path(directory) / 'probe-data'
        probe_directory.mkdir()
        code = probe_cell(ROOT / 'tests/framework_init_notebook_probe.py', MODE, SCENARIO, OUTPUT, probe_directory)
        identity = client.execute(code, allow_stdin=True, stop_on_error=True)
        deadline = time.monotonic() + 60
        menu_count = token_count = 0
        delayed_reply = None
        complete = False
        while not complete:
            if time.monotonic() >= deadline:
                raise TimeoutError('Jupyter authentication probe did not complete')
            try:
                request = client.get_stdin_msg(timeout=.02)
            except queue.Empty:
                request = None
            if request is not None:
                if request['parent_header'].get('msg_id') != identity:
                    raise AssertionError('Unexpected stdin request parent')
                content = request['content']
                secret = bool(content['password'])
                requests.append({'prompt': content['prompt'], 'password': secret})
                if secret:
                    token_count += 1
                else:
                    menu_count += 1
                if SCENARIO == 'interrupt' or (SCENARIO == 'token_interrupt' and secret):
                    manager.interrupt_kernel()
                elif SCENARIO == 'eof' and secret:
                    client.input('\x04')
                elif SCENARIO == 'login_rejection' and token_count > 1:
                    client.input('\x04')
                elif SCENARIO == 'timeout':
                    delayed_reply = time.monotonic() + .6
                elif secret:
                    client.input('invalid token' if SCENARIO == 'retry' and token_count == 1 else TOKEN)
                else:
                    client.input('2' if MODE == 'reference' else '1')
            if delayed_reply is not None and time.monotonic() >= delayed_reply:
                client.input('2' if MODE == 'reference' else '1')
                delayed_reply = None
            try:
                message = client.get_iopub_msg(timeout=.02)
            except queue.Empty:
                continue
            if message['parent_header'].get('msg_id') != identity:
                continue
            messages.append({'type': message['msg_type'], 'content': message['content']})
            if message['msg_type'] == 'status' and message['content']['execution_state'] == 'idle':
                complete = True
        reply = execution_reply(client, identity, shell_messages)
        # A late source menu reply releases its surviving reader before public
        # shutdown, but cannot publish a credential after timeout selection.
        if delayed_reply is not None:
            time.sleep(max(0, delayed_reply - time.monotonic()))
            client.input('2' if MODE == 'reference' else '1')
        return {'scenario': SCENARIO, 'mode': MODE, 'connection_name': connection_name, 'requests': requests,
                'menus': menu_count, 'tokens': token_count, 'execution_status': reply['content']['status']}
    finally:
        OUTPUT.with_suffix('.iopub.json').write_text(json.dumps(messages, indent=2) + '\n')
        OUTPUT.with_suffix('.stdin.json').write_text(json.dumps(requests, indent=2) + '\n')
        OUTPUT.with_suffix('.shell.json').write_text(json.dumps(shell_messages, indent=2) + '\n')
        if client is not None:
            client.stop_channels()
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)


with tempfile.TemporaryDirectory(prefix='framework-init-kernel-driver-') as directory:
    observed = run_kernel(directory)
    OUTPUT.with_suffix('.driver.json').write_text(json.dumps(observed, indent=2) + '\n')
    for path in [OUTPUT, OUTPUT.with_suffix('.iopub.json'), OUTPUT.with_suffix('.stdin.json'),
                 OUTPUT.with_suffix('.shell.json')]:
        assert TOKEN not in path.read_text(), 'Credential appeared in retained notebook evidence'
