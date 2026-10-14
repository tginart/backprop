"""Check notebook input boundaries and mandatory genuine-kernel comparisons.

Architecture: portable deadline/capability checks -> isolated real kernel pair
when installed -> exact raw outcomes, secret flags and cleanup assertions.
Main callers: focused local verification and dedicated notebook CI. Why this
exists: a dependency skip records a missing kernel; CI's required flag turns it
into a failure rather than making genuine input coverage silently disappear.
"""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from backprop.errors import UsageError
from backprop import login_prompt
from framework_gap_process import run_observation
from framework_init_notebook_cell import execution_reply, probe_cell, stdin_request_counts
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]


class KernelChannelFixture:
    """Expose a real selectable socket pair at the unavailable ZeroMQ boundary.

    Main callers: portable protocol/deadline tests. The input reader, deadlines,
    message validation and signal handling stay real; only transport message
    framing is controlled. Genuine ipykernel tests below replace this fixture.
    """
    def __init__(self):
        self.reader, self.writer = socket.socketpair()
        self.pending = []
        self.replies = []
        self.sent = []
        self.interrupt = False
        self.zmq = ModuleType('zmq')
        self.zmq.POLLIN = self.zmq.NOBLOCK = 1
        self.socket = SimpleNamespace(poll=self.poll, recv_multipart=self.discard)
        self.kernel = SimpleNamespace(_allow_stdin=True, stdin_socket=self.socket,
            session=SimpleNamespace(send=self.send, recv=self.recv),
            _parent_ident={'shell': b'frontend'}, get_parent=lambda channel: {'header': {'msg_id': 'execute'}},
            getpass=lambda message: None, raw_input=lambda message: None)

    def feed(self, *replies):
        self.pending.extend(replies)
        self.writer.sendall(b'x' * len(replies))

    def poll(self, milliseconds, flags):
        if self.interrupt and self.sent:
            raise KeyboardInterrupt()
        return bool(select.select([self.reader], [], [], milliseconds / 1000)[0])

    def discard(self, flags):
        self.reader.recv(65536)
        self.pending.clear()

    def send(self, socket, kind, content, *, parent, ident):
        self.sent.append({'kind': kind, 'content': content, 'parent': parent, 'ident': ident})
        if self.replies:
            self.feed(*self.replies)
        return {'header': {'msg_id': 'request-current'}}

    def recv(self, socket, *, mode):
        self.reader.recv(1)
        return [b'frontend'], self.pending.pop(0)

    def close(self):
        self.reader.close()
        self.writer.close()


class NotebookInputBoundaries(unittest.TestCase):
    def test_probe_cell_keeps_runpy_globals_out_of_actual_ipython_display(self):
        if not importlib.util.find_spec('IPython'):
            self.skipTest('Actual IPython shell is unavailable')
        from IPython.core.interactiveshell import InteractiveShell
        from IPython.utils.capture import capture_output
        from traitlets.config import Config
        config = Config()
        config.HistoryManager.hist_file = ':memory:'
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / 'probe.py'
            probe.write_text("NEW = 'c' * 40\nprint('intentional diagnostic')\n")
            shell = InteractiveShell.instance(config=config, ipython_dir=directory)
            argv = list(sys.argv)
            try:
                with capture_output() as before:
                    baseline = shell.run_cell(f'import runpy\nrunpy.run_path({str(probe)!r})')
                self.assertTrue(baseline.success)
                self.assertIn('c' * 40, before.stdout)
                with capture_output() as after:
                    corrected = shell.run_cell(probe_cell(probe, 'reference', 'success', Path(directory) / 'out.json', directory))
                self.assertTrue(corrected.success)
                self.assertIsNone(corrected.result)
                self.assertEqual(after.stdout, 'intentional diagnostic\n')
                self.assertNotIn('c' * 40, after.stdout + after.stderr)
                self.assertEqual(after.outputs, [])
                self.assertNotIn('_framework_init_probe_globals', shell.user_ns)
            finally:
                sys.argv[:] = argv
                shell.history_manager.end_session()
                InteractiveShell.clear_instance()

    def test_shell_reply_correlation_retains_readiness_and_requires_execution_success(self):
        def message(kind, parent, content):
            return {'msg_type': kind, 'parent_header': {'msg_id': parent}, 'content': content}
        ready = message('kernel_info_reply', 'readiness', {'status': 'ok', 'banner': 'actual readiness'})
        previous = message('execute_reply', 'previous', {'status': 'ok'})
        current = message('execute_reply', 'current', {'status': 'ok', 'execution_count': 1})
        replies = iter([ready, previous, current])
        retained = []
        client = SimpleNamespace(get_shell_msg=lambda timeout: next(replies))
        self.assertIs(execution_reply(client, 'current', retained), current)
        self.assertEqual(retained, [{'type': item['msg_type'], 'parent_id': item['parent_header']['msg_id'],
                                    'content': item['content']} for item in [ready, previous, current]])
        failed = message('execute_reply', 'current', {'status': 'error', 'ename': 'RuntimeError', 'evalue': 'original failure'})
        client.get_shell_msg = lambda timeout: failed
        with self.assertRaisesRegex(AssertionError, 'original failure'):
            execution_reply(client, 'current', [])
        wrong = message('kernel_info_reply', 'current', {'status': 'ok'})
        client.get_shell_msg = lambda timeout: wrong
        with self.assertRaisesRegex(AssertionError, 'Unexpected shell reply type'):
            execution_reply(client, 'current', [])

    def test_shell_reply_missing_matching_execution_obeys_its_deadline(self):
        import queue
        def empty(timeout):
            time.sleep(timeout)
            raise queue.Empty()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'execution reply'):
            execution_reply(SimpleNamespace(get_shell_msg=empty), 'absent', [], timeout=.02)
        self.assertLess(time.monotonic() - started, .2)

    def test_menu_token_text_is_visible_but_actual_credential_prompt_must_be_hidden(self):
        menu = {'prompt': '1) Use a Foundry token  2) Continue offline: ', 'password': False}
        secret = {'prompt': 'Foundry token (hidden): ', 'password': True}
        self.assertEqual(stdin_request_counts([menu, secret], 'native', 'success'), {'tokens': 1, 'menus': 1})
        with self.assertRaisesRegex(AssertionError, 'Credential input'):
            stdin_request_counts([{**secret, 'password': False}], 'native', 'success')
        with self.assertRaisesRegex(AssertionError, 'Menu input'):
            stdin_request_counts([{**menu, 'password': True}], 'native', 'success')
        with self.assertRaisesRegex(AssertionError, 'Unexpected notebook'):
            stdin_request_counts([{'prompt': 'unknown secret prompt', 'password': True}], 'native', 'success')

    def test_only_exact_unauthenticated_otel_routes_are_separate_from_authentication(self):
        checker = FrameworkInitNotebookContracts()
        telemetry = {'path': '/sdk/otel/v1/metrics', 'method': 'POST', 'operation': None,
                     'credential_old': False, 'credential_new': False, 'body': None}
        authentication = {**telemetry, 'path': '/graphql', 'operation': 'Viewer', 'credential_new': True}
        self.assertEqual(checker.authentication_requests({'requests': [telemetry, authentication]}, True), [authentication])
        unknown = {**telemetry, 'path': '/sdk/otel/v1/unknown'}
        self.assertEqual(checker.authentication_requests({'requests': [unknown]}, True), [unknown])
        for changed in [{'method': 'GET'}, {'operation': 'Viewer'}, {'credential_new': True}, {'body': {'unexpected': True}}]:
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                checker.authentication_requests({'requests': [{**telemetry, **changed}]}, True)
        with self.assertRaises(AssertionError):
            checker.authentication_requests({'requests': [telemetry]}, False)

    def test_configured_ipython_shell_without_kernel_is_not_an_input_channel(self):
        if not importlib.util.find_spec('IPython'):
            self.skipTest('Actual IPython shell is unavailable')
        from IPython.core.interactiveshell import InteractiveShell
        from traitlets.config import Config
        config = Config()
        config.HistoryManager.hist_file = ':memory:'
        config.IPKernelApp.connection_file = 'jupyter-not-a-kernel.json'
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        shell = InteractiveShell.instance(config=config, ipython_dir=directory.name)
        try:
            self.assertIsNone(login_prompt._notebook_kernel())
            with patch.object(sys.stdin, 'isatty', return_value=False):
                with self.assertRaisesRegex(UsageError, 'interactive terminal'):
                    login_prompt.prompt_token('https://fixture.invalid')
        finally:
            shell.history_manager.end_session()
            InteractiveShell.clear_instance()

    def test_hidden_input_uses_only_the_kernel_password_bridge(self):
        calls = []
        kernel = SimpleNamespace(raw_input=lambda message: calls.append(('menu', message)) or '1',
                                 getpass=lambda message: calls.append(('password', message)) or 'synthetic-token')
        with patch.object(login_prompt, '_notebook_kernel', return_value=kernel):
            self.assertEqual(login_prompt.prompt_token('https://fixture.invalid'), 'synthetic-token')
        self.assertEqual([kind for kind, _ in calls], ['menu', 'password'])

    def test_kernel_eof_cancellation_is_an_interrupt(self):
        def cancelled(message):
            raise EOFError()
        kernel = SimpleNamespace(raw_input=cancelled, getpass=cancelled)
        with patch.object(login_prompt, '_notebook_kernel', return_value=kernel):
            with self.assertRaises(KeyboardInterrupt):
                login_prompt.prompt_token('https://fixture.invalid', force=True)

    def test_deadline_polls_real_blocked_io_without_a_reader_thread(self):
        channel = KernelChannelFixture()
        try:
            with patch.dict(sys.modules, {'zmq': channel.zmq}):
                with self.assertRaisesRegex(TimeoutError, 'timed out'):
                    login_prompt._notebook_readline(channel.kernel, 'input', hidden=False,
                                                    deadline=time.monotonic() + .05)
            self.assertEqual(channel.sent[0]['content'], {'prompt': 'input', 'password': False})
        finally:
            channel.close()

    @unittest.skipUnless(hasattr(signal, 'setitimer'), 'POSIX interval timer is required')
    def test_deadline_does_not_touch_caller_alarm_even_on_interrupt(self):
        channel = KernelChannelFixture()
        previous = signal.getsignal(signal.SIGALRM)
        try:
            signal.setitimer(signal.ITIMER_REAL, 10)
            with patch.dict(sys.modules, {'zmq': channel.zmq}):
                with self.assertRaises(TimeoutError):
                    login_prompt._notebook_readline(channel.kernel, 'input', hidden=True, deadline=time.monotonic() + .05)
                channel.interrupt = True
                with self.assertRaises(KeyboardInterrupt):
                    login_prompt._notebook_readline(channel.kernel, 'input', hidden=True, deadline=time.monotonic() + 1)
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 9)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            channel.close()

    def test_timed_input_flushes_stale_replies_and_validates_fresh_password_message(self):
        channel = KernelChannelFixture()
        reply = lambda value, parent: {'header': {'msg_type': 'input_reply'},
            'content': {'value': value}, 'parent_header': {'msg_id': parent}}
        try:
            channel.feed(reply('stale-before-request', 'old-request'))
            channel.replies = [reply('stale-delayed-reply', 'old-request'), reply('fresh-secret', 'request-current')]
            with patch.dict(sys.modules, {'zmq': channel.zmq}):
                self.assertEqual(login_prompt._notebook_readline(channel.kernel, 'hidden', hidden=True,
                    deadline=time.monotonic() + 1), 'fresh-secret')
            self.assertEqual(channel.sent, [{'kind': 'input_request',
                'content': {'prompt': 'hidden', 'password': True},
                'parent': {'header': {'msg_id': 'execute'}}, 'ident': b'frontend'}])
            channel.replies = [reply(42, 'request-current')]
            with patch.dict(sys.modules, {'zmq': channel.zmq}):
                with self.assertRaisesRegex(UsageError, 'invalid stdin reply'):
                    login_prompt._notebook_readline(channel.kernel, 'hidden', hidden=True,
                        deadline=time.monotonic() + 1)
        finally:
            channel.close()


class FrameworkInitNotebookContracts(unittest.TestCase):
    def authentication_requests(self, case, reference):
        """Validate exact unavailable OTEL routes while retaining all raw traffic.

        Main callers: genuine scenario assertions. Process-global pinned OTEL
        can export after a run stage ends. These exact fixture POST routes carry
        no auth candidate; every other record remains part of the authentication
        or backend contract and must pass that scenario's assertions.
        """
        authentication = []
        for request in case['requests']:
            if request['path'] in {'/sdk/otel/v1/metrics', '/sdk/otel/v1/logs'}:
                self.assertTrue(reference, 'Native init must not emit pinned OTEL traffic')
                self.assertEqual(request['method'], 'POST')
                self.assertIsNone(request['operation'])
                self.assertFalse(request['credential_old'])
                self.assertFalse(request['credential_new'])
                self.assertIsNone(request['body'])
            else:
                authentication.append(request)
        return authentication

    def observe(self, scenario):
        """Require actual installed kernel runtimes and retain every subprocess.

        Main callers: selected genuine notebook tests. Explicit reference
        selection and BACKPROP_REQUIRE_NOTEBOOK_KERNEL never degrade to skips.
        Source and native kernel processes use their own interpreters and imports.
        """
        missing = [name for name in ['ipykernel', 'jupyter_client'] if not importlib.util.find_spec(name)]
        if missing:
            if os.environ.get('BACKPROP_REQUIRE_NOTEBOOK_KERNEL') == '1':
                self.fail('Required real notebook dependencies are missing: ' + ', '.join(missing))
            self.skipTest('Real notebook dependencies are unavailable: ' + ', '.join(missing))
        reference = reference_runtime()
        pair = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'framework-init-notebook-{scenario}-{mode}.json'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR=str(Path(tempfile.gettempdir()) / 'backprop-reference-data'), TERM='xterm')
            result = run_observation([reference.python if mode == 'reference' else sys.executable, '-B',
                str(ROOT / 'tests/framework_init_notebook_driver.py'), mode, scenario], output,
                cwd=tempfile.gettempdir(), env=env, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            data = json.loads(output.read_text())
            driver = json.loads(output.with_suffix('.driver.json').read_text())
            self.assertEqual(driver['execution_status'], 'ok')
            self.assertEqual(stdin_request_counts(driver['requests'], mode, scenario),
                             {key: driver[key] for key in ['tokens', 'menus']})
            case = data['observation']
            self.assertFalse(case['stdin_tty'])
            self.assertFalse(case['stderr_tty'])
            self.assertTrue(case['alarm_restored'])
            self.assertEqual(case['unknown_requests'], [])
            self.assertTrue(case['global_empty'])
            self.assertEqual(case['native_workers_after_teardown'], [])
            self.assertEqual(case['repeat_log'], {'returned_run': False, 'id': None, 'value': None})
            self.assertEqual(case['previous_finish'], None if scenario == 'login_rejection' else
                             {'returned_run': False, 'id': None, 'value': None})
            self.assertTrue(case['after']['foreign_session_empty'])
            self.authentication_requests(case, mode == 'reference')
            for suffix in ['.json', '.iopub.json', '.stdin.json', '.shell.json', '.stdout.log', '.stderr.log']:
                self.assertNotIn(b'c' * 40, output.with_suffix(suffix).read_bytes())
            pair.append((case, driver))
        return pair

    def test_actual_kernel_secret_input_retry_and_force_reuse(self):
        for scenario in ['success', 'retry', 'force']:
            with self.subTest(scenario=scenario):
                pair = self.observe(scenario)
                for case, driver in pair:
                    self.assertEqual(case['value'], 'Run')
                    self.assertEqual(case['id'], 'previous')
                    self.assertTrue(case['previous_global'])
                    self.assertTrue(case['after']['settings_new'])
                    self.assertTrue(case['after']['session_new'])
                    self.assertEqual(driver['tokens'], 2 if scenario == 'retry' else 1)
                    self.assertEqual(driver['menus'], 0 if scenario == 'force' else 2 if scenario == 'retry' else 1)
                # Pinned non-silent login can look up the selected account for its
                # display message. Preserve those exact server calls separately
                # from native Foundry's deferred verification and run publication.
                self.assertEqual(pair[1][0]['requests'], [])
                for request in self.authentication_requests(pair[0][0], True):
                    self.assertIn(request['operation'], {'Viewer', 'ServerInfo', 'ServerFeaturesQuery'})
                    if request['operation'] == 'Viewer':
                        self.assertTrue(request['credential_new'])

    def test_actual_kernel_unmarked_connection_filename_is_explicit_native_repair(self):
        expected, actual = self.observe('unmarked')
        source, source_driver = expected
        native, native_driver = actual
        self.assertEqual(source_driver['connection_name'], 'kernel-contract.json')
        self.assertEqual(native_driver['connection_name'], 'kernel-contract.json')
        self.assertEqual(source['error'], 'UsageError')
        self.assertEqual(source['message'], 'No API key configured. Use `wandb login` to log in.')
        self.assertEqual(source['after'], source['before'])
        self.assertEqual(source_driver['requests'], [])
        self.assertTrue(source['previous_global'])
        self.assertTrue(source['credential_files_unchanged'])
        self.assertEqual(native['value'], 'Run')
        self.assertEqual(native['id'], 'previous')
        self.assertTrue(native['after']['settings_new'])
        self.assertTrue(native['after']['session_new'])
        self.assertEqual(native_driver['tokens'], 1)
        self.assertTrue(native['previous_global'])
        self.assertEqual(native['requests'], [])

    def test_actual_kernel_interrupt_and_eof_preserve_retained_session(self):
        for scenario in ['interrupt', 'token_interrupt', 'eof']:
            with self.subTest(scenario=scenario):
                pair = self.observe(scenario)
                for case, _ in pair:
                    self.assertEqual(case['error'], 'KeyboardInterrupt')
                    self.assertEqual(case['after'], case['before'])
                    self.assertTrue(case['credential_files_unchanged'])
                    self.assertTrue(case['previous_global'])
                    self.assertEqual(case['previous_log'], {'returned_run': False, 'id': None, 'value': None})
                    self.assertEqual(self.authentication_requests(case, case is pair[0][0]), [])
                self.assertEqual({key: pair[0][0][key] for key in ['error', 'message', 'cause']},
                                 {key: pair[1][0][key] for key in ['error', 'message', 'cause']})

    def test_actual_kernel_rejected_verified_login_then_cancel_preserves_session(self):
        for case, driver in self.observe('login_rejection'):
            self.assertEqual(case['error'], 'KeyboardInterrupt')
            self.assertEqual(case['after'], case['before'])
            self.assertTrue(case['credential_files_unchanged'])
            self.assertEqual(driver['tokens'], 2)
            self.assertTrue(case['requests'])
            rejected = [request for request in case['requests'] if request.get('response_status') == 401]
            self.assertTrue(rejected)
            self.assertTrue(all(request['credential_new'] for request in rejected))

    def test_actual_kernel_selected_auth_precedes_backend_failure(self):
        pair = self.observe('backend_rejection')
        for case, _ in pair:
            self.assertEqual(case['error'], 'CommError')
            self.assertTrue(case['after']['settings_new'])
            self.assertTrue(case['after']['session_new'])
            self.assertFalse(case['previous_global'])
            self.assertEqual(case['previous_log']['error'], 'UsageError')
            self.assertTrue(case['requests'])
            rejected = [request for request in case['requests'] if request.get('response_status') == 403]
            self.assertTrue(rejected)
            self.assertTrue(all(request['credential_new'] for request in rejected))
        self.assertEqual(pair[0][0]['previous_log'], pair[1][0]['previous_log'])

    def test_actual_kernel_timeout_uses_disabled_mode_without_native_input_worker(self):
        pair = self.observe('timeout')
        for case, _ in pair:
            self.assertEqual(case['value'], 'Run')
            self.assertEqual(case['after']['settings_mode'], 'disabled')
            self.assertTrue(case['after']['settings_old'])
            self.assertTrue(case['after']['session_old'])
            self.assertEqual(self.authentication_requests(case, case is pair[0][0]), [])
            self.assertTrue(case['credential_files_unchanged'])
        # Retain the upstream worker and native repair separately, without
        # stripping threads from the raw source/native observations.
        self.assertTrue(any('get_input' in name for name in pair[0][0]['new_threads']))
        self.assertFalse(any('get_input' in name for name in pair[1][0]['new_threads']))
        self.assertEqual(pair[1][0]['active_alarm'], 0.0 if hasattr(signal, 'setitimer') else None)


if __name__ == '__main__':
    unittest.main()
