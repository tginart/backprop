"""Verify public Api auth with real clients, cache files and terminal input.

Architecture: separate pinned/native processes -> localhost TLS verifier ->
actual credential selection or POSIX PTY -> public state and redacted evidence.
Main callers: core CI and focused constructor verification. Windows console
input has its own framework tests and is never inferred from this PTY suite.
"""
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

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]
TOKEN = b'b' * 40


def terminal(command, output, env, scenario, mode):
    """Drive only observed prompts, preserving bytes and bounded cancellation.

    Main caller: observe for interactive cases. Separate provider menus select
    the existing-account route; real token input must remain hidden. No prompt
    function, auth object or service client is patched.
    """
    import pty
    master, slave = pty.openpty()
    transcript = bytearray()
    child = subprocess.Popen([*command, str(output)], cwd=tempfile.gettempdir(), env=env,
                             stdin=slave, stderr=slave, stdout=subprocess.PIPE)
    os.close(slave)
    menus = tokens = 0
    stdout = b''
    try:
        deadline = time.monotonic() + 35
        while child.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('Actual Api terminal probe did not finish')
            if select.select([master], [], [], .02)[0]:
                try:
                    data = os.read(master, 65536)
                    if not data: break
                    transcript.extend(data)
                except OSError:
                    break
            menu = b'Enter your choice:' if mode == 'reference' else b'Continue offline:'
            count = transcript.count(menu)
            if count > menus:
                menus = count
                if scenario != 'prompt_timeout':
                    os.write(master, b'2\n' if mode == 'reference' else b'1\n')
            secret = b'Paste your API key and hit enter:' if mode == 'reference' else b'token (hidden):'
            count = transcript.count(secret)
            if count > tokens:
                tokens = count
                if scenario == 'prompt_interrupt': os.kill(child.pid, signal.SIGINT)
                elif scenario == 'prompt_eof': os.write(master, b'\x04')
                elif scenario != 'prompt_timeout':
                    os.write(master, b'invalid token\n' if scenario == 'prompt_retry' and tokens == 1 else TOKEN + b'\n')
        stdout, _ = child.communicate(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            stdout, _ = child.communicate()
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 65536)
                if not data: break
                transcript.extend(data)
            except OSError:
                break
        os.close(master)
        output.with_suffix('.stdout.log').write_bytes(stdout)
        output.with_suffix('.stderr.log').write_bytes(transcript)
        output.with_suffix('.driver.json').write_text(json.dumps({'menus': menus, 'tokens': tokens,
            'returncode': child.returncode}, indent=2) + '\n')
    return child.returncode


class PublicApiAuthContracts(unittest.TestCase):
    def observe(self, scenario, alias='root'):
        """Retain real-client evidence and reject unexpected traffic or secrets.

        Main callers: the constructor contract methods below. Separate SDK
        processes prevent one implementation's cached session affecting the
        other; terminal cases exercise actual input instead of replacing it.
        """
        reference = reference_runtime()
        pair = []
        for mode in ['reference', 'native']:
            output = ROOT / 'test-results' / f'public-api-auth-{scenario}-{alias}-{mode}.json'
            output.parent.mkdir(exist_ok=True)
            output.unlink(missing_ok=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            env.update(PYTHONDONTWRITEBYTECODE='1', WANDB_DATA_DIR='/tmp/backprop-reference-data', TERM='xterm',
                       PYTHONPATH=os.pathsep.join(map(str, [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests'])))
            command = [reference.python if mode == 'reference' else sys.executable, '-B',
                       str(ROOT / 'tests/public_api_auth_probe.py'), mode, scenario, alias]
            if scenario.startswith('prompt'):
                code = terminal(command, output, env, scenario, mode)
            else:
                # The reference constructor starts and tears down wandb-core;
                # that service handshake is outside its requested HTTP timeout.
                # Keep the native process deadline tight and separately bounded.
                process_timeout = 90 if mode == 'reference' else 35
                code = run_observation(command, output, cwd=tempfile.gettempdir(), env=env, timeout=process_timeout).returncode
            self.assertEqual(code, 0, output.with_suffix('.stderr.log').read_text())
            record = json.loads(output.read_text())
            case = record['observation']
            self.assertEqual(case['unknown'], [])
            self.assertEqual(case['native_workers_after_teardown'], [])
            for path in [output, output.with_suffix('.stdout.log'), output.with_suffix('.stderr.log')]:
                for char in [b'a', b'b', b'c']:
                    self.assertNotIn(char * 40, path.read_bytes(), str(path))
            pair.append(case)
        return pair

    def test_actual_environment_cache_and_settings_credentials_across_aliases(self):
        for alias in ['root', 'public', 'module']:
            for scenario, token in [('environment', 'new'), ('cache', 'old'), ('settings', 'old')]:
                with self.subTest(alias=alias, scenario=scenario):
                    source, native = self.observe(scenario, alias)
                    self.assertEqual(source['result'], {'value': 'Api', 'token': token, 'project': 'api-auth'})
                    self.assertEqual(native['result'], source['result'])
                    self.assertEqual(native['after'], source['after'])
                    self.assertTrue(source['cache_unchanged'])
                    self.assertTrue(native['cache_unchanged'])

    def test_api_selection_ignores_init_relogin_and_empty_explicit_key_uses_session(self):
        for scenario, token in [('environment_relogin', 'new'), ('session_relogin', 'old'),
                                ('empty_key', 'old'), ('explicit_session', 'explicit')]:
            with self.subTest(scenario=scenario):
                source, native = self.observe(scenario)
                self.assertEqual(source['result'], {'value': 'Api', 'token': token, 'project': 'api-auth'})
                self.assertEqual(native['result'], source['result'])
                self.assertEqual(native['after'], source['after'])
                self.assertEqual(native['cache_unchanged'], source['cache_unchanged'])

    def test_rejected_credentials_raise_and_explicit_failure_preserves_session(self):
        for scenario in ['environment_rejected', 'cache_rejected', 'failure_recovery']:
            with self.subTest(scenario=scenario):
                source, native = self.observe(scenario)
                self.assertEqual(source['result']['error'], 'AuthenticationError')
                self.assertEqual(native['result']['error'], source['result']['error'])
                self.assertTrue(native['cache_unchanged'])
                self.assertEqual(native['after'], native['before'])
                if scenario == 'failure_recovery':
                    self.assertEqual(native['recovery'], source['recovery'])
                    self.assertEqual(native['recovery']['token'], 'old')

    def test_actual_timeout_boundaries_match_without_an_unbounded_zero_wait(self):
        for scenario in ['timeout_zero', 'timeout_negative', 'timeout_true']:
            with self.subTest(scenario=scenario):
                source, native = self.observe(scenario)
                if scenario == 'timeout_true':
                    self.assertEqual(source['result'], {'value': 'Api', 'token': 'new', 'project': 'api-auth'})
                    self.assertEqual(native['result'], source['result'])
                else:
                    self.assertEqual(source['result']['error'], 'AuthenticationError')
                    self.assertEqual(native['result']['error'], source['result']['error'])
                    self.assertEqual(native['after'], native['before'])
                    self.assertEqual(native['requests'], [])

    @unittest.skipUnless(os.name == 'posix', 'Actual POSIX terminal required')
    def test_real_inspection_prompt_retry_and_aliases(self):
        for alias in ['root', 'public', 'module']:
            for scenario in ['prompt', 'prompt_retry']:
                with self.subTest(alias=alias, scenario=scenario):
                    source, native = self.observe(scenario, alias)
                    self.assertEqual(source['result'], {'value': 'Api', 'token': 'new', 'project': 'api-auth'})
                    self.assertEqual(native['result'], source['result'])
                    self.assertEqual(native['after'], source['after'])
                    self.assertTrue(source['cache_exists'])
                    self.assertTrue(native['cache_exists'])

    @unittest.skipUnless(os.name == 'posix', 'Actual POSIX terminal required')
    def test_real_inspection_timeout_and_cancellation_do_not_select_local_mode(self):
        for scenario, error in [('prompt_timeout', 'TimeoutError'), ('prompt_interrupt', 'KeyboardInterrupt'),
                                ('prompt_eof', 'KeyboardInterrupt')]:
            with self.subTest(scenario=scenario):
                source, native = self.observe(scenario)
                self.assertEqual(source['result']['error'], error)
                self.assertEqual(native['result']['error'], error)
                self.assertEqual(source['before'], source['after'])
                self.assertEqual(native['before'], native['after'])
                self.assertFalse(native['cache_exists'])


if __name__ == '__main__':
    unittest.main()
