"""Require actual Windows console input at the selected public init boundaries.

Architecture: checked reference/native interpreter -> real Windows ConPTY ->
prompt-directed keyboard bytes -> immutable output/observations -> exact
per-branch assertions. Main caller: dedicated Windows CI with
BACKPROP_REQUIRE_WINDOWS_CONSOLE=1. No simulated Windows path counts as evidence.
The existing public hidden-input deadline repair and any real Ctrl+Z differences
are retained separately from the exact interruption/session contracts.
"""
import json
import getpass
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest

from framework_init_windows_console import ConsoleProcess, module_source_evidence
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]
NEW = b'c' * 40


def terminal_text(data):
    """Strip presentation sequences only for finding printed input prompts.

    Main caller: the real console driver. Original bytes remain unchanged in
    the retained transcript and secret assertions; this is not normalization of
    SDK errors, source outcomes or console-mode evidence.
    """
    text = data.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)


class WindowsConsoleContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'nt':
            if os.environ.get('BACKPROP_REQUIRE_WINDOWS_CONSOLE') == '1':
                raise AssertionError('Mandatory actual Windows console execution is unavailable')
            raise unittest.SkipTest('Actual Windows console is unavailable locally')
        cls.reference = reference_runtime()
        import backprop
        cls.native_version = backprop.__version__

    def test_child_startup_uses_real_console_and_retains_traceback(self):
        """Expose startup routing failures before trusting hidden-input captures.

        Main caller: mandatory Windows CI. An intentional ordinary child error
        must appear in ConPTY output, including when CI's parent stdio is
        redirected. This does not replace any SDK authentication scenario.
        """
        output = ROOT / 'test-results/framework-init-windows-console-startup'
        output.parent.mkdir(exist_ok=True)
        script = ("import json,sys; print(json.dumps({'actual_tty': "
                  "[sys.stdin.isatty(),sys.stdout.isatty(),sys.stderr.isatty()]}),flush=True); "
                  "raise RuntimeError('intentional-conpty-startup-traceback')")
        child = ConsoleProcess([sys.executable, '-B', '-c', script],
            environment=os.environ.copy(), cwd=tempfile.gettempdir())
        failure = cleanup_error = None
        driver = {}
        try:
            deadline = time.monotonic() + 20
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Windows console startup child did not complete')
                time.sleep(.01)
            driver['returncode'] = child.poll()
        except BaseException as error:
            failure = error
        finally:
            try:
                child.close()
            except BaseException as error:
                cleanup_error = error
            finally:
                driver.update(forced_termination=child.forced_termination,
                    reader_errors=child.reader_errors,
                    output_reader_alive=child.reader.is_alive(),
                    remaining_handles=len(child.handles),
                    pseudoconsole_closed=not child.console.value,
                    attribute_list_deleted=not child.attributes_initialized,
                    execution_error=None if failure is None else
                        {'error': type(failure).__name__, 'message': str(failure)},
                    cleanup_error=None if cleanup_error is None else
                        {'error': type(cleanup_error).__name__, 'message': str(cleanup_error)})
                output.with_suffix('.console.log').write_bytes(child.output)
                output.with_suffix('.driver.json').write_text(json.dumps(driver, indent=2) + '\n')
        if cleanup_error is not None:
            raise cleanup_error from failure
        if failure is not None:
            raise failure
        self.assertEqual(driver['returncode'], 1)
        self.assertIn('"actual_tty": [true, true, true]', terminal_text(child.output))
        self.assertIn('RuntimeError: intentional-conpty-startup-traceback', terminal_text(child.output))
        self.assertFalse(driver['forced_termination'])
        self.assertFalse(driver['output_reader_alive'])
        self.assertEqual(driver['reader_errors'], [])
        self.assertEqual(driver['remaining_handles'], 0)
        self.assertTrue(driver['pseudoconsole_closed'])
        self.assertTrue(driver['attribute_list_deleted'])

    def observe(self, scenario, mode):
        """Send real keyboard bytes only after the corresponding actual prompt.

        Main callers: paired scenario assertions. The output thread is owned by
        the parent driver. The child owns input itself, with no patched streams,
        msvcrt, builtins.input or credential-selection code. Every failure saves
        the original console bytes and checked driver cleanup before assertions.
        """
        output = ROOT / 'test-results' / f'framework-init-windows-{scenario}-{mode}.json'
        output.parent.mkdir(exist_ok=True)
        output.unlink(missing_ok=True)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
        paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
        environment.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
            WANDB_DATA_DIR=str(Path(tempfile.gettempdir()) / 'backprop-reference-data'), TERM='xterm')
        driver = {'mode': mode, 'scenario': scenario, 'menus': 0, 'tokens': 0,
                  'ctrl_z_sent': False, 'ctrl_c_sent': False, 'delayed_token_sent': False}
        failure = cleanup_error = None
        with tempfile.TemporaryDirectory(prefix='framework-init-windows-') as directory:
            child = ConsoleProcess([self.reference.python if mode == 'reference' else sys.executable,
                '-B', str(ROOT / 'tests/framework_init_windows_probe.py'), mode, scenario, directory, str(output)],
                environment=environment, cwd=tempfile.gettempdir())
            try:
                deadline, delayed = time.monotonic() + 60, None
                while child.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Actual Windows console probe did not complete')
                    text = terminal_text(child.output)
                    menu = 'Enter your choice:' if mode == 'reference' else 'Continue offline:'
                    token = 'Paste your API key and hit enter:' if mode == 'reference' else 'token (hidden):'
                    menus, tokens = text.count(menu), text.count(token)
                    if menus > driver['menus']:
                        driver['menus'] = menus
                        if scenario == 'menu_timeout':
                            pass
                        elif scenario in {'menu_eof', 'menu_eof_timed'} and menus == 1:
                            child.send(b'\x1a\r')
                            driver['ctrl_z_sent'] = True
                        elif scenario == 'menu_eof_timed':
                            # A source retry gets its actual menu deadline.
                            pass
                        elif scenario == 'interrupt_menu' or (scenario in {'menu_eof', 'token_eof'} and menus > 1):
                            child.send(b'\x03')
                            driver['ctrl_c_sent'] = True
                        else:
                            child.send(b'2\r' if mode == 'reference' else b'1\r')
                    if tokens > driver['tokens']:
                        driver['tokens'] = tokens
                        if scenario == 'secret_timeout':
                            if mode == 'reference':
                                delayed = time.monotonic() + .7
                        elif scenario == 'token_eof':
                            child.send(b'\x1a\r')
                            driver['ctrl_z_sent'] = True
                        elif scenario == 'interrupt_token' or (scenario == 'login_rejection' and tokens > 1):
                            child.send(b'\x03')
                            driver['ctrl_c_sent'] = True
                        else:
                            child.send(b'invalid token\r' if scenario == 'token_retry' and tokens == 1 else NEW + b'\r')
                    if delayed is not None and time.monotonic() >= delayed:
                        child.send(NEW + b'\r')
                        driver['delayed_token_sent'] = True
                        delayed = None
                    time.sleep(.01)
                driver['returncode'] = child.poll()
            except BaseException as error:
                failure = error
            finally:
                try:
                    child.close()
                except BaseException as error:
                    cleanup_error = error
                finally:
                    # Evidence must survive a cleanup failure as well as a
                    # child execution failure; neither becomes a success.
                    driver.update(forced_termination=child.forced_termination,
                        reader_errors=child.reader_errors, output_reader_alive=child.reader.is_alive(),
                        remaining_handles=len(child.handles), pseudoconsole_closed=not child.console.value,
                        attribute_list_deleted=not child.attributes_initialized,
                        execution_error=None if failure is None else
                            {'error': type(failure).__name__, 'message': str(failure)},
                        cleanup_error=None if cleanup_error is None else
                            {'error': type(cleanup_error).__name__, 'message': str(cleanup_error)})
                    output.with_suffix('.console.log').write_bytes(child.output)
                    output.with_suffix('.driver.json').write_text(json.dumps(driver, indent=2) + '\n')
            if cleanup_error is not None:
                if failure is not None:
                    raise cleanup_error from failure
                raise cleanup_error
        if failure is not None:
            raise failure
        self.assertEqual(driver['returncode'], 0, child.output.decode(errors='replace'))
        self.assertFalse(driver['forced_termination'])
        self.assertFalse(driver['output_reader_alive'])
        self.assertEqual(driver['reader_errors'], [])
        self.assertEqual(driver['remaining_handles'], 0)
        self.assertTrue(driver['pseudoconsole_closed'])
        self.assertTrue(driver['attribute_list_deleted'])
        self.assertIsNone(driver['cleanup_error'])
        self.assertIsNone(driver['execution_error'])
        self.assertTrue(output.is_file(), child.output.decode(errors='replace'))
        for secret in ['b' * 40, 'c' * 40]:
            self.assertNotIn(secret, terminal_text(child.output),
                             'A credential appeared in terminal presentation after VT decoding')
        for path in [output, output.with_suffix('.console.log'), output.with_suffix('.driver.json')]:
            self.assertNotIn(NEW, path.read_bytes(), 'A credential appeared in retained console evidence')
            self.assertNotIn(b'b' * 40, path.read_bytes(), 'An old credential appeared in retained console evidence')
        data = json.loads(output.read_text())
        self.assertEqual(data['sdk'], '0.30.0' if mode == 'reference' else self.native_version)
        self.assertEqual(data['runtime_modules']['msvcrt'], {'origin': 'built-in', 'sha256': None})
        self.assertEqual(data['runtime_modules']['getpass']['sha256'], data['runtime_source_hashes']['getpass.py'])
        self.assertRegex(data['runtime_source_hashes']['python_executable'], r'^[0-9a-f]{64}$')
        case = data['observation']
        self.assertEqual(case['console_before'], case['console_after'])
        self.assertEqual(case['unknown_requests'], [])
        self.assertTrue(case['after']['foreign_session_empty'])
        for request in case['requests']:
            if request['path'] in {'/sdk/otel/v1/metrics', '/sdk/otel/v1/logs'}:
                self.assertEqual(mode, 'reference')
                self.assertEqual(request['method'], 'POST')
                self.assertIsNone(request['operation'])
                self.assertIsNone(request['body'])
                self.assertFalse(request['credential_old'])
                self.assertFalse(request['credential_new'])
        source_retains_disabled = mode == 'reference' and scenario in {'menu_timeout', 'menu_eof_timed'}
        self.assertEqual(case['global_empty_after_teardown'], not source_retains_disabled)
        if source_retains_disabled:
            self.assertEqual(case['after']['mode'], 'disabled')
            self.assertFalse(case['previous_global'])
            self.assertEqual(case['global_after_teardown'],
                {'present': True, 'is_selected_global': True, 'disabled': True, 'id': case['id']})
        else:
            self.assertEqual(case['global_after_teardown'],
                {'present': False, 'is_selected_global': False, 'disabled': None, 'id': None})
        self.assertTrue(case['global_empty'])
        self.assertEqual(case['native_workers_after_teardown'], [])
        self.assertEqual(case['repeat_log'], {'returned_run': False, 'id': None, 'value': None})
        self.assertEqual([row['windows_repeat'] for row in case['repeat_history']], [1])
        return case, driver

    def pair(self, scenario):
        """Collect both independent processes even when one comparison fails.

        Main callers: branch tests. A failed subTest remains a test failure while
        allowing the other SDK's actual evidence to be captured in the same run.
        No absent/failed observation is accepted by subsequent paired assertions.
        """
        result = {}
        for mode in ['reference', 'native']:
            with self.subTest(scenario=scenario, mode=mode):
                result[mode] = self.observe(scenario, mode)
        return result

    def test_hidden_success_and_invalid_token_retry_reuse_host_owned_session(self):
        for scenario in ['success', 'token_retry']:
            pair = self.pair(scenario)
            if len(pair) != 2:
                continue
            for case, driver in pair.values():
                self.assertEqual(case['value'], 'Run')
                self.assertEqual(case['id'], 'previous')
                self.assertTrue(case['previous_global'])
                self.assertTrue(case['after']['session_new'])
                self.assertTrue(case['after']['settings_new'])
                self.assertEqual(driver['tokens'], 2 if scenario == 'token_retry' else 1)
                self.assertEqual(driver['menus'], 2 if scenario == 'token_retry' else 1)

    def test_actual_ctrl_c_and_untimed_menu_ctrl_z_preserve_prior_session(self):
        for scenario in ['interrupt_menu', 'interrupt_token', 'menu_eof']:
            pair = self.pair(scenario)
            if len(pair) != 2:
                continue
            for case, driver in pair.values():
                self.assertEqual(case['error'], 'KeyboardInterrupt')
                self.assertEqual(case['message'], '')
                self.assertIsNone(case['cause'])
                self.assertEqual(case['after'], case['before'])
                self.assertTrue(case['credential_files_unchanged'])
                self.assertTrue(case['previous_global'])
                self.assertEqual(case['previous_log'], {'returned_run': False, 'id': None, 'value': None})
                self.assertTrue(driver['ctrl_z_sent'] if scenario == 'menu_eof' else driver['ctrl_c_sent'])

    def test_timed_menu_deadline_and_ctrl_z_select_disabled_without_credentials(self):
        for scenario in ['menu_timeout', 'menu_eof_timed']:
            pair = self.pair(scenario)
            if len(pair) != 2:
                continue
            for case, driver in pair.values():
                self.assertEqual(case['value'], 'Run')
                self.assertEqual(case['after']['mode'], 'disabled')
                self.assertTrue(case['after']['session_old'])
                self.assertTrue(case['after']['settings_old'])
                self.assertTrue(case['credential_files_unchanged'])
                self.assertEqual(driver['tokens'], 0)
            if scenario == 'menu_eof_timed':
                self.assertEqual(pair['reference'][1]['menus'], 2)
                self.assertEqual(pair['native'][1]['menus'], 1)

    def test_hidden_ctrl_z_records_source_reprompt_and_native_cancellation(self):
        pair = self.pair('token_eof')
        if len(pair) != 2:
            return
        for case, driver in pair.values():
            self.assertEqual(case['error'], 'KeyboardInterrupt')
            self.assertEqual(case['after'], case['before'])
            self.assertTrue(case['credential_files_unchanged'])
            self.assertTrue(case['previous_global'])
            self.assertTrue(driver['ctrl_z_sent'])
            self.assertEqual(driver['tokens'], 1)
        self.assertEqual(pair['reference'][1]['menus'], 2)
        self.assertTrue(pair['reference'][1]['ctrl_c_sent'])
        self.assertEqual(pair['native'][1]['menus'], 1)
        self.assertFalse(pair['native'][1]['ctrl_c_sent'])

    def test_hidden_deadline_retains_explicit_native_repair(self):
        pair = self.pair('secret_timeout')
        if len(pair) != 2:
            return
        source, source_driver = pair['reference']
        native, native_driver = pair['native']
        self.assertEqual(source['id'], 'previous')
        self.assertTrue(source['after']['session_new'])
        self.assertTrue(source_driver['delayed_token_sent'])
        self.assertEqual(native['after']['mode'], 'disabled')
        self.assertTrue(native['after']['session_old'])
        self.assertTrue(native['credential_files_unchanged'])
        self.assertFalse(native_driver['delayed_token_sent'])
        # The raw time remains recorded; allow constructor and scheduling cost
        # while requiring a bounded return without any delivered secret bytes.
        self.assertLess(native['elapsed_seconds'], 2)

    def test_actual_verified_login_rejection_and_cancel_preserve_old_auth(self):
        pair = self.pair('login_rejection')
        if len(pair) != 2:
            return
        for case, driver in pair.values():
            self.assertEqual(case['error'], 'KeyboardInterrupt')
            self.assertEqual(case['after'], case['before'])
            self.assertTrue(case['credential_files_unchanged'])
            self.assertEqual(driver['tokens'], 2)
            self.assertEqual(driver['menus'], 0)
            rejected = [row for row in case['requests'] if row.get('response_status') == 401]
            self.assertTrue(rejected)
            self.assertTrue(all(row['credential_new'] for row in rejected))


class WindowsConsoleMetadataContracts(unittest.TestCase):
    """Check actual built-in/file provenance on every installed platform."""

    def test_builtin_module_without_file_records_exact_import_origin(self):
        self.assertFalse(hasattr(sys, '__file__'))
        self.assertEqual(module_source_evidence(sys), {'origin': 'built-in', 'sha256': None})

    def test_file_module_hashes_its_actual_source_bytes(self):
        self.assertEqual(module_source_evidence(getpass),
            {'origin': getpass.__spec__.origin,
             'sha256': hashlib.sha256(Path(getpass.__file__).read_bytes()).hexdigest()})


if __name__ == '__main__':
    unittest.main()
