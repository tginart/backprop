"""Build non-displaying kernel cells and correlate actual execution replies.

Architecture: explicit probe argv -> statement-only execution -> bounded shell
reply correlation. Main callers: framework_init_notebook_driver and portable
harness regressions. Why this exists: a final runpy expression exposes the
probe's credential globals through IPython display, while readiness replies
can remain queued ahead of the execution reply on a real Windows kernel.
"""
import json
import queue
import re
import time


def probe_cell(path, mode, scenario, output, directory):
    """Execute the probe without displaying or retaining its returned globals.

    Main caller: the genuine-kernel driver. Assignment and deletion are Python
    statements, so IPython cannot format runpy's result into credential output.
    This leaves the probe's intentional stdout, errors and IOPub intact.
    """
    return ('import runpy,sys\n'
            f'sys.argv = {json.dumps([str(path), mode, scenario, str(directory), str(output)])}\n'
            f'_framework_init_probe_globals = runpy.run_path({str(path)!r}, run_name="__main__")\n'
            'del _framework_init_probe_globals')


def stdin_request_counts(requests, mode, scenario):
    """Require credential requests to be hidden and known menus to be visible.

    Main callers: genuine-kernel assertions and portable prompt-role regression.
    Why this exists: a visible menu can mention a token without requesting one.
    Identify the exact known input prompt first, then validate its password bit;
    unknown prompts fail explicitly instead of being classified by that bit.
    """
    credential = 'wandb: Paste your API key and hit enter: ' if mode == 'reference' else 'Foundry token (hidden): '
    menu = 'wandb: Enter your choice: ' if mode == 'reference' else '1) Use a Foundry token  2) Continue offline: '
    counts = {'tokens': 0, 'menus': 0}
    for request in requests:
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', request['prompt'])
        if text == credential:
            assert request['password'] is True, 'Credential input must have password=True'
            counts['tokens'] += 1
        elif text == menu or (mode == 'reference' and scenario == 'timeout' and text == ''):
            assert request['password'] is False, 'Menu input must have password=False'
            counts['menus'] += 1
        else:
            raise AssertionError('Unexpected notebook stdin prompt')
    return counts


def execution_reply(client, identity, retained, *, timeout=10):
    """Retain unrelated shell replies until this execution finishes or times out.

    Main caller: the genuine-kernel driver after the matching IOPub idle event.
    Why this exists: kernel readiness and execution share the shell channel.
    Match the request identity and execute_reply type without deleting earlier
    observations or accepting an unrelated successful readiness status.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Notebook execution reply did not arrive before its deadline')
        try:
            message = client.get_shell_msg(timeout=min(.2, remaining))
        except queue.Empty:
            continue
        parent = message['parent_header'].get('msg_id')
        retained.append({'type': message['msg_type'], 'parent_id': parent,
                         'content': message['content']})
        if parent != identity:
            continue
        if message['msg_type'] != 'execute_reply':
            raise AssertionError('Unexpected shell reply type for notebook execution: '
                                 + message['msg_type'])
        if message['content']['status'] != 'ok':
            raise AssertionError('Kernel execution failed: ' + json.dumps(message['content']))
        return message
