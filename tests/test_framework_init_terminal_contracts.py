"""Compare real POSIX terminal init branches against the pinned SDK.

Architecture: isolated interpreter and PTY -> actual menu/token input -> exact
raw transcript and dedicated JSON -> branch-specific behavior assertions.
Main callers: focused init contracts CI. Why this exists: terminal lifecycle
must be checked without replacing the prompt or contacting hosted services.
The reference token deadline omission is retained as an explicit native repair.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]
TOKEN = b'c' * 40


@unittest.skipUnless(os.name == 'posix', 'Real POSIX PTY input is required')
class FrameworkInitTerminalContracts(unittest.TestCase):
    def observe(self, scenario, *, import_path='top', modes=('reference', 'native')):
        """Preserve terminal bytes and original errors for every selected case.

        Main callers: branch tests below. Drive only prompts actually printed
        by the SDK, with separate valid choices for the provider-specific menu.
        A delayed valid token measures the pinned hidden-input deadline gap
        without stranding its input reader. Child failure always retains bytes.
        """
        if 'reference' in modes and not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 reference interpreter is unavailable')
        reference = reference_runtime() if 'reference' in modes else None
        pair = []
        import pty
        for mode in modes:
            output = ROOT / 'test-results' / f'framework-init-terminal-{scenario}-{import_path}-{mode}.json'
            output.parent.mkdir(exist_ok=True)
            output.unlink(missing_ok=True)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR='/tmp/backprop-reference-data', FRAMEWORK_INIT_IMPORT=import_path,
                       TERM='xterm')
            if scenario.startswith('headless'):
                result = subprocess.run([reference.python if mode == 'reference' else sys.executable, '-B',
                    str(ROOT / 'tests/framework_init_terminal_probe.py'), mode, scenario, str(output)],
                    cwd=tempfile.gettempdir(), env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=45)
                output.with_suffix('.stdout.log').write_bytes(result.stdout)
                output.with_suffix('.stderr.log').write_bytes(result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                pair.append(json.loads(output.read_text())['observation'])
                continue
            master, slave = pty.openpty()
            transcript = bytearray()
            stdout = stderr = b''
            child = subprocess.Popen([reference.python if mode == 'reference' else sys.executable, '-B',
                str(ROOT / 'tests/framework_init_terminal_probe.py'), mode, scenario, str(output)],
                cwd=tempfile.gettempdir(), env=env, stdin=slave, stderr=slave, stdout=subprocess.PIPE)
            os.close(slave)
            menu_count = token_count = 0
            delayed_at = None
            cancelled = False
            try:
                deadline = time.monotonic() + 45
                while child.poll() is None:
                    self.assertLess(time.monotonic(), deadline, 'Terminal probe did not finish: ' + scenario)
                    if select.select([master], [], [], .02)[0]:
                        try:
                            chunk = os.read(master, 65536)
                            if not chunk:
                                break
                            transcript.extend(chunk)
                        except OSError:
                            break
                    menu = b'Enter your choice:' if mode == 'reference' else b'Continue offline:'
                    menus = transcript.count(menu)
                    if menus > menu_count:
                        menu_count = menus
                        if scenario.startswith('menu_timeout'):
                            pass
                        elif scenario.startswith('menu_eof'):
                            os.write(master, b'\x04')
                        elif scenario.startswith('interrupt'):
                            if not cancelled:
                                os.kill(child.pid, signal.SIGINT)
                                cancelled = True
                        else:
                            choice = b'3\n' if mode == 'reference' else b'2\n'
                            if not scenario.startswith('offline'):
                                choice = b'2\n' if mode == 'reference' else b'1\n'
                            if scenario == 'offline_invalid_choice' and menu_count == 1:
                                choice = b'7\n'
                            os.write(master, choice)
                    token = b'Paste your API key and hit enter:' if mode == 'reference' else b'token (hidden):'
                    tokens = transcript.count(token)
                    if tokens > token_count:
                        token_count = tokens
                        if scenario.startswith('secret_timeout'):
                            if mode == 'reference':
                                delayed_at = time.monotonic() + .7
                        elif scenario.startswith('token_eof'):
                            os.write(master, b'\x04')
                        else:
                            os.write(master, b'invalid token\n' if scenario == 'token_retry' and token_count == 1 else TOKEN + b'\n')
                    if delayed_at is not None and time.monotonic() >= delayed_at:
                        os.write(master, TOKEN + b'\n')
                        delayed_at = None
                stdout, stderr = child.communicate(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    stdout, stderr = child.communicate()
                # Drain diagnostics still buffered after process completion.
                while select.select([master], [], [], 0)[0]:
                    try:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        transcript.extend(chunk)
                    except OSError:
                        break
                os.close(master)
                output.with_suffix('.stdout.log').write_bytes(stdout)
                output.with_suffix('.stderr.log').write_bytes(bytes(transcript) + (stderr or b''))
                output.with_suffix('.driver.json').write_text(json.dumps({
                    'scenario': scenario, 'menus': menu_count, 'tokens': token_count,
                    'cancelled': cancelled, 'returncode': child.returncode}, indent=2) + '\n')
            self.assertEqual(child.returncode, 0, transcript.decode(errors='replace'))
            self.assertTrue(output.is_file(), transcript.decode(errors='replace'))
            observed = json.loads(output.read_text())
            self.assertEqual(observed['scenario'], scenario)
            self.assertNotIn(TOKEN, transcript, 'Secret input was echoed')
            pair.append(observed['observation'])
        return pair

    def assert_cleanup(self, case):
        self.assertTrue(case['restored'])
        self.assertEqual(case['unknown_requests'], [])
        self.assertEqual(case['repeat_log'], {'returned_run': False, 'id': None, 'value': None})
        self.assertTrue(case['repeat_global_empty'])
        self.assertFalse(any(name.startswith(('backprop-flush-', 'backprop-stats-', 'backprop-stop-', 'backprop-model-'))
                             for name in case['threads_after_teardown']))

    def test_real_menu_offline_and_timeout_select_local_modes(self):
        for scenario, mode in [('offline', 'offline'), ('offline_invalid_choice', 'offline'),
                               ('offline_implicit_setup', 'offline'), ('menu_timeout', 'disabled')]:
            for case in self.observe(scenario):
                self.assert_cleanup(case)
                self.assertTrue(case['returned_run'])
                self.assertEqual(case['mode'], mode)
                self.assertEqual(case['disabled'], mode == 'disabled')
                self.assertEqual(case['setup_mode'], mode)
                self.assertEqual(case['implicit_repeat_mode'], mode)
                self.assertEqual(case['supplied_mode'], 'online')
                self.assertEqual(case['requests'], [])
                self.assertFalse(case['credential_file_exists'])
                self.assertFalse(case['netrc_exists'])

    def test_real_menu_eof_with_timeout_selects_disabled(self):
        for case in self.observe('menu_eof'):
            self.assert_cleanup(case)
            self.assertTrue(case['returned_run'])
            self.assertEqual(case['mode'], 'disabled')
            self.assertEqual(case['setup_mode'], 'disabled')
            self.assertEqual(case['implicit_repeat_mode'], 'disabled')
            self.assertEqual(case['requests'], [])

    def test_real_prompt_token_and_force_reach_actual_transport_failure(self):
        for scenario in ['token', 'token_retry', 'force']:
            for case in self.observe(scenario):
                self.assert_cleanup(case)
                self.assertEqual(case['error'], 'CommError')
                self.assertIsNone(case['global_id'])
                self.assertTrue(case['netrc_exists'] or case['credential_file_exists'])
                self.assertTrue(case['auth_headers'])
                self.assertTrue(case['setup_key_matches'])
                self.assertFalse(case['supplied_key_matches'])

    def test_real_prompt_error_preserves_existing_handle_and_repeats(self):
        for scenario in ['interrupt_previous', 'token_eof_previous', 'menu_eof_previous',
                         'offline_previous', 'offline_relogin_previous', 'token_previous']:
            expected, actual = self.observe(scenario)
            for case in (expected, actual):
                self.assert_cleanup(case)
                self.assertEqual(case['previous_log']['value'], None)
            self.assertEqual(expected['previous_global'], actual['previous_global'])
            self.assertEqual(expected.get('error'), actual.get('error'))
            self.assertEqual(expected['disabled'], actual['disabled'])
            if scenario == 'offline_relogin_previous':
                self.assertFalse(expected['setup_key_matches'])
                self.assertFalse(actual['setup_key_matches'])

    def test_headless_failure_preserves_old_handle_before_writes(self):
        for case in self.observe('headless_previous'):
            self.assert_cleanup(case)
            self.assertEqual(case['error'], 'UsageError')
            self.assertTrue(case['previous_global'])
            self.assertEqual(case['files_before'], case['files_after'])
            self.assertEqual(case['requests'], [])
            self.assertFalse(case['netrc_exists'])
            self.assertFalse(case['credential_file_exists'])

    def test_hidden_input_deadline_is_explicit_native_repair(self):
        expected, actual = self.observe('secret_timeout')
        self.assert_cleanup(expected)
        self.assert_cleanup(actual)
        # Pinned force skips the timed menu and awaits a late valid token,
        # then reaches the actual rejected HTTP initialization. Native honors
        # the same argument at hidden token entry and never submits that token.
        self.assertEqual(expected['error'], 'CommError')
        self.assertTrue(expected['auth_headers'])
        self.assertTrue(actual['returned_run'])
        self.assertEqual(actual['mode'], 'disabled')
        self.assertEqual(actual['setup_mode'], 'disabled')
        self.assertEqual(actual['implicit_repeat_mode'], 'disabled')
        self.assertEqual(actual['requests'], [])
        self.assertFalse(actual['credential_file_exists'])

    def test_prompt_fallback_at_independent_supported_imports(self):
        for alias in ['wandb', 'sdk']:
            for case in self.observe('offline', import_path=alias):
                self.assert_cleanup(case)
                self.assertEqual(case['mode'], 'offline')


if __name__ == '__main__':
    unittest.main()
