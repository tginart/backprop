"""Compare genuine init HTTP lifecycles without a hosted service or sink fake.

Architecture: independently selected Python -> real SDK/HTTP implementation ->
strict localhost TLS protocol -> mandatory JSON and untouched diagnostics ->
stage-specific public state, provider errors and repeat-session assertions.
Main callers: focused contracts CI. Why this exists: auth-selection sentinels
cannot verify real HTTP errors, deadlines, retry attempts or late cleanup.
This finite slice does not establish every init option or hosted server behavior.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'c' * 40
NO_VALUE = {'returned_run': False, 'id': None, 'value': None}


def transport_filename(scenario, import_path, mode, *, direct=False):
    """Encode filename components while preserving the original test scenario.

    Main callers: observe and the artifact-packaging regression. Why this
    exists: artifact uploads reject colon and other filesystem punctuation.
    Percent encoding also escapes literal percent signs, so distinct logical
    scenarios keep distinct filenames and raw diagnostic sidecars stay paired.
    """
    parts = [quote(value, safe='') for value in (scenario, import_path, mode)]
    suffix = '-direct' if direct else ''
    return f'framework-init-transport-{parts[0]}-{parts[1]}{suffix}-{parts[2]}.json'


class FrameworkInitTransportContracts(unittest.TestCase):
    def test_artifact_filenames_preserve_logical_scenarios_and_raw_sidecars(self):
        """Verify actual result/diagnostic packaging without starting an SDK.

        Main caller: focused CI correction. The tiny child writes the original
        scenario to JSON and emits exact raw diagnostics through the existing
        process boundary. No transport/lifecycle assertion is substituted here.
        """
        cases = ['publish:403', 'read:403', 'publish_echo:403', 'auth:401', 'auth:403',
                 'auth_success', 'publish:500,403', 'publish:403:finish_previous',
                 'publish:403:create_new', 'publish:403:return_previous', 'blocked_publish',
                 'auth%3A401', 'auth-401', 'punctuation:"<>|*?\\/\r\n']
        names = [transport_filename(scenario, 'top', 'native') for scenario in cases]
        self.assertEqual(len(names), len(set(names)))
        forbidden = set(':"<>|*?\\/\r\n')
        for name in names:
            self.assertFalse(forbidden.intersection(name), name)
        script = ('import json,pathlib,sys; '
                  'pathlib.Path(sys.argv[2]).write_text(json.dumps({"scenario":sys.argv[1]})); '
                  'sys.stdout.buffer.write(b"original stdout\\n"); '
                  'sys.stderr.buffer.write(b"original stderr\\n")')
        with tempfile.TemporaryDirectory(prefix='init-artifact-packaging-') as directory:
            for scenario in ('auth:401', 'publish:500,403'):
                output = Path(directory) / transport_filename(scenario, 'top', 'native')
                result = run_observation([sys.executable, '-c', script, scenario], output,
                                         cwd=directory, env=os.environ.copy(), timeout=10)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(output.read_text()), {'scenario': scenario})
                self.assertEqual(output.with_suffix('.stdout.log').read_bytes(), b'original stdout\n')
                self.assertEqual(output.with_suffix('.stderr.log').read_bytes(), b'original stderr\n')
            for path in Path(directory).iterdir():
                self.assertFalse(forbidden.intersection(path.name), path.name)

    def test_explicit_reference_executable_preserves_symlink_path(self):
        """A reference virtualenv's executable path must survive selection.

        Main caller: interpreter-isolation regression. The temporary symlink
        represents an existing executable without creating/changing an env.
        Replace only the metadata subprocess here, since a copied symlink has
        no virtualenv config; real SDK comparisons separately verify imports.
        """
        with tempfile.TemporaryDirectory(prefix='reference-python-link-') as directory:
            executable = Path(directory) / 'python'
            executable.symlink_to(sys.executable)
            with patch.dict(os.environ, {'BACKPROP_REFERENCE_PYTHON': str(executable)}), \
                    patch('reference_runtime._resolve') as resolve:
                reference_runtime()
            resolve.assert_called_once_with(str(executable))

    def observe(self, scenario, *, modes=('reference', 'native'), import_path='top', direct=False):
        """Keep each interpreter's real imports and immutable raw observations.

        Main callers: selected failure/success cases below. An explicitly
        configured reference interpreter is mandatory; a local optional wheel
        absence can skip only when no separate interpreter was configured.
        No reference dependency directory enters the native process path.
        """
        if 'reference' in modes and not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 reference interpreter is unavailable')
        reference = reference_runtime() if 'reference' in modes else None
        observations = []
        for mode in modes:
            output = ROOT / 'test-results' / transport_filename(scenario, import_path, mode, direct=direct)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            paths = [ROOT / 'tests'] if mode == 'reference' else [ROOT / 'sdk', ROOT / 'tests']
            env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), PYTHONDONTWRITEBYTECODE='1',
                       WANDB_DATA_DIR='/tmp/backprop-reference-data',
                       FRAMEWORK_INIT_TRANSPORT_IMPORT=import_path,
                       FRAMEWORK_INIT_DIRECT_RUN='1' if direct else '0')
            python = reference.python if mode == 'reference' else sys.executable
            result = run_observation([python, '-B', str(ROOT / 'tests/framework_init_transport_probe.py'),
                                      mode, scenario], output, cwd=tempfile.gettempdir(), env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            data = json.loads(output.read_text())
            self.assertEqual(data['sdk'], mode)
            self.assertEqual(data['import'], import_path)
            self.assertEqual(data['scenario'], scenario)
            if mode == 'reference':
                self.assertEqual(data['runtime']['sdk_version'], '0.30.0')
                self.assertNotIn(str(ROOT / 'sdk'), data['runtime']['module_file'])
                self.assertEqual(data['runtime']['source_hashes']['sdk/wandb_init.py'],
                                 '1f38fe5185742bb3609031b4abc4493bb4e5b9ff442607b05825cef8e83017ab')
                self.assertEqual(data['runtime']['source_hashes']['sdk/wandb_login.py'],
                                 'd8611b98f98fc590bf4984f8c374c0c6689062bf00b0251ca043a347e9595e06')
            else:
                self.assertTrue(data['runtime']['module_file'].startswith(str(ROOT / 'sdk')))
            observation = data['observations']
            self.assertEqual(observation['unknown_requests'], [])
            self.assertEqual(observation['repeat_log'], NO_VALUE)
            self.assertTrue(observation['repeat_global_finished'])
            observations.append(observation)
        return observations

    def assert_failure_cleanup(self, case):
        """Check ownership, rather than unrelated process-global OTEL threads."""
        self.assertIsNone(case['global_run'])
        self.assertEqual(case['open_run_log_handlers'], [])
        for name in case['threads_after_failure']:
            self.assertFalse(name.startswith('backprop-') and name != 'backprop-init',
                             case['threads_after_failure'])
        self.assertNotIn('backprop-init', case['threads_after_teardown'])

    def assert_native_recovery(self, case):
        self.assertEqual(case['repeat_online_log'], NO_VALUE)
        self.assertTrue(case['repeat_online_finished'])
        accepted = [request for request in case['requests']
                    if '/publishRecords' in request['path'] and request.get('response_status') == 200]
        self.assertGreaterEqual(len(accepted), 2)
        self.assertTrue(any('transport_recovered' in json.dumps(record) for record in case['records']))

    def test_actual_publish_denial_preserves_provider_cause_and_pending_journal(self):
        source, native = self.observe('publish:403')
        self.assertEqual(source['error'], native['error'])
        self.assertEqual(source['message'], 'fixture rejected init')
        self.assertEqual(source['causes'], [{'error': 'CommError', 'message': 'fixture rejected init', 'status': None}])
        self.assertEqual([cause['error'] for cause in native['causes']], ['CommError', 'BackpropError', 'FoundryError'])
        self.assertEqual(native['causes'][-1]['status'], 403)
        self.assertIn('Foundry POST failed (HTTP 403)', native['message'])
        self.assertIn('Retry run.flush() or backprop sync.', native['message'])
        self.assertNotIn(TOKEN, native['message'])
        self.assertEqual(len(native['native_journals']), 1)
        journal = native['native_journals'][0]
        self.assertEqual([event['type'] for event in journal['events']], ['run_start', 'file', 'run_metadata'])
        self.assertIsNone(journal['ack'])
        for case in (source, native):
            self.assert_failure_cleanup(case)
            self.assertEqual([request['response_status'] for request in case['requests']
                              if request.get('response_status', 0) >= 400], [403])
        self.assert_native_recovery(native)

    def test_actual_read_denial_has_same_public_family_without_persisting_a_start(self):
        (native,) = self.observe('read:403', modes=('native',))
        self.assertEqual(native['error'], 'CommError')
        self.assertEqual([cause['error'] for cause in native['causes']], ['CommError', 'FoundryError'])
        self.assertEqual(native['causes'][-1]['status'], 403)
        self.assertEqual(native['native_journals'], [])
        self.assert_failure_cleanup(native)
        self.assert_native_recovery(native)

    def test_transport_diagnostic_translation_does_not_reintroduce_echoed_token(self):
        (native,) = self.observe('publish_echo:403', modes=('native',))
        self.assertEqual(native['error'], 'CommError')
        self.assertEqual(native['causes'][-1]['status'], 403)
        for cause in native['causes']:
            self.assertNotIn(TOKEN, cause['message'])
            self.assertIn('fixture echoed synthetic key [redacted]', cause['message'])
        self.assert_failure_cleanup(native)
        self.assert_native_recovery(native)

    def test_real_verified_login_rejection_does_not_publish_auth(self):
        for status in (401, 403):
            with self.subTest(status=status):
                source, native = self.observe(f'auth:{status}')
                for case in (source, native):
                    self.assertEqual(case['error'], 'AuthenticationError')
                    self.assertEqual(case['auth_before'], case['auth_after'])
                    self.assertEqual(case['auth_after']['key'], 'b' * 40)
                    self.assertEqual(case['retained_key_before_recovery'], 'b' * 40)
                    self.assertEqual(case['supplied_key_after_login'], 'b' * 40)
                    self.assertFalse(case['credential_file_exists'])
                    self.assertFalse(case['netrc_exists'])
                    self.assertEqual(case['fresh_setup_key'], 'e' * 40)
                    self.assert_failure_cleanup(case)
                # The native fixture then performs an actual successful online
                # recovery with explicit c40. That later init selection updates
                # the process-owned setup handle, independently of the rejected
                # login above. Source recovery here is offline only; its pinned
                # online-selection publication is paired by the terminal probe.
                self.assertNotIn('retained_key_after_online_recovery', source)
                self.assertEqual(native['retained_key_after_online_recovery'], TOKEN)
                for case, key in ((source, 'b' * 40), (native, TOKEN)):
                    self.assertEqual(case['retained_key_before_teardown'], key)
                    self.assertEqual(case['retained_key_after_teardown'], key)
                    self.assertEqual(case['retained_key_after_fresh_setup'], key)
                self.assertEqual(source['message'],
                    'Failed to authenticate with ' + source['auth_before']['host'] + ': fixture rejected auth')
                self.assertEqual(native['message'], 'Foundry credentials could not be verified')
                self.assertEqual(native['causes'][-1]['status'], status)
                self.assert_native_recovery(native)

    def test_successful_auth_updates_retained_setup_only_after_verification(self):
        source, native = self.observe('auth_success')
        for case in (source, native):
            self.assertTrue(case['value'])
            self.assertEqual(case['auth_before']['key'], 'b' * 40)
            self.assertEqual(case['auth_after']['key'], TOKEN)
            self.assertEqual(case['supplied_key_after_login'], 'b' * 40)
            self.assertEqual(case['second_failed_login']['error'], 'AuthenticationError')
            for field in ('retained_key_after_failed_login', 'retained_key_after_teardown',
                          'retained_key_after_fresh_setup'):
                self.assertEqual(case[field], TOKEN)
            self.assertEqual(case['fresh_setup_key'], 'e' * 40)
        self.assertTrue(native['credential_file_exists'])
        self.assertEqual(native['auth_after']['environment']['BACKPROP_TOKEN'], TOKEN)
        self.assertFalse(source['credential_file_exists'])
        self.assertEqual(source['auth_after']['environment'], source['auth_before']['environment'])
        self.assert_native_recovery(native)

    def test_actual_transient_then_terminal_publication_has_exact_attempts(self):
        source, native = self.observe('publish:500,403')
        for case in (source, native):
            failures = [request for request in case['requests'] if request.get('response_status', 0) >= 400]
            self.assertEqual([request['response_status'] for request in failures], [500, 403])
            self.assertEqual(failures[0]['body'], failures[1]['body'])
            self.assertEqual(case['error'], 'CommError')
            self.assert_failure_cleanup(case)
        native_failures = [request for request in native['requests'] if request.get('response_status', 0) >= 400]
        self.assertGreaterEqual(native_failures[1]['received_at'] - native_failures[0]['received_at'], .9)
        self.assertEqual(native['causes'][-1]['status'], 403)
        self.assert_native_recovery(native)

    def test_reinit_backend_failure_preserves_each_measured_old_handle_policy(self):
        for policy in ('finish_previous', 'create_new', 'return_previous'):
            with self.subTest(policy=policy):
                source, native = self.observe(f'publish:403:{policy}')
                for case in (source, native):
                    if policy == 'finish_previous':
                        self.assertEqual(case['error'], 'CommError')
                        self.assertFalse(case['previous_global'])
                        self.assertIsNone(case['global_run'])
                    elif policy == 'create_new':
                        self.assertEqual(case['error'], 'CommError')
                        self.assertTrue(case['previous_global'])
                        self.assertEqual(case['previous_log'], NO_VALUE)
                    else:
                        self.assertTrue(case['returned_run'])
                        self.assertEqual(case['id'], case['global_run'])
                        self.assertTrue(case['previous_global'])
                        self.assertEqual(case['previous_log'], NO_VALUE)
                        self.assertEqual([request for request in case['requests']
                                          if request['operation'] == 'UpsertBucket' or '/publishRecords' in request['path']], [])
                if policy == 'finish_previous':
                    self.assertEqual(source['previous_log']['error'], 'UsageError')
                    self.assertIn('is finished. The call to `log` will be ignored.', source['previous_log']['message'])
                    self.assertEqual(native['previous_log'], source['previous_log'])

    def test_actual_deadline_and_late_response_never_bind_failed_run(self):
        source, native = self.observe('blocked_publish')
        for case in (source, native):
            self.assertEqual(case['error'], 'CommError')
            self.assertTrue(case['message'].startswith('Timed out initializing run: '))
            self.assertTrue(case['message'].endswith(
                'Consider increasing the init_timeout setting:\n  wandb.init(settings=wandb.Settings(init_timeout=...))'))
            self.assertIsNone(case['global_run_after_late_response'])
            self.assertGreaterEqual(case['init_elapsed_seconds'], .20)
            self.assertLess(case['init_elapsed_seconds'], 3)
            self.assert_failure_cleanup(case)
        self.assertEqual([cause['error'] for cause in native['causes']], ['CommError', 'TimeoutError'])
        self.assertNotIn('backprop-init', native['threads_after_late_response'])
        self.assertFalse(any(name.startswith('backprop-')
                             for name in native['threads_after_late_response']))
        self.assert_native_recovery(native)

    def test_direct_run_retains_timeout_error_from_actual_foundry_request(self):
        (native,) = self.observe('blocked_publish', modes=('native',), direct=True)
        self.assertEqual(native['error'], 'TimeoutError')
        self.assertEqual([cause['error'] for cause in native['causes']], ['TimeoutError'])
        self.assert_failure_cleanup(native)
        self.assert_native_recovery(native)

    def test_independent_supported_aliases_preserve_real_publication_failure(self):
        for import_path in ('wandb', 'sdk'):
            with self.subTest(import_path=import_path):
                source, native = self.observe('publish:403', import_path=import_path)
                self.assertEqual(source['error'], native['error'])
                self.assertEqual(native['error'], 'CommError')
                self.assertEqual(source['message'], 'fixture rejected init')
                self.assertEqual(native['causes'][-1]['status'], 403)
                self.assert_failure_cleanup(source)
                self.assert_failure_cleanup(native)
                self.assert_native_recovery(native)


if __name__ == '__main__':
    unittest.main()
