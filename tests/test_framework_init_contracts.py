"""Compare integration-switch selection separately from framework internals.

Architecture: exact installed W&B wheel -> isolated genuine SDK entry points ->
mandatory JSON file and raw diagnostics -> strict per-scenario assertions.
Main callers: focused local tests and parent-selected CI. Offline cases use the
actual W&B local core, requiring local socket permission, with no hosted runs.
Missing optional frameworks are named explicitly rather than assumed tested.
"""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]


class FrameworkInitContracts(unittest.TestCase):
    def observe(self, scenario, *, import_path=None):
        """Retain both real process observations before applying comparisons.

        Main callers: each selected init boundary test. Environment credentials
        are omitted; native online branches use MemorySink and reference hosted
        branches stop before service construction. Raw diagnostics survive a
        nonzero exit, missing JSON file or strict comparison failure.
        """
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B 0.30.0 wheel is unavailable')
        reference = reference_runtime()
        output_dir = ROOT / 'test-results'
        output_dir.mkdir(exist_ok=True)
        pair = []
        for mode in ['reference', 'native']:
            suffix = '-' + import_path.replace('.', '_') if import_path else ''
            output = output_dir / f'framework-init-{scenario}{suffix}-{mode}.json'
            output.unlink(missing_ok=True)
            environment = {key: value for key, value in os.environ.items()
                if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            environment.update(PYTHONDONTWRITEBYTECODE='1', WANDB_DATA_DIR='/tmp/backprop-reference-data',
                PYTHONPATH=str(ROOT / 'tests') if mode == 'reference' else str(ROOT / 'sdk'))
            if import_path:
                environment['FRAMEWORK_INIT_IMPORT'] = import_path
            python = reference.python if mode == 'reference' else sys.executable
            result = run_observation([python, '-B', str(ROOT / 'tests/framework_init_probe.py'),
                mode, scenario], output, cwd=tempfile.gettempdir(), env=environment, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            self.assertTrue(output.is_file())
            pair.append(json.loads(output.read_text()))
        self.assertEqual(pair[0]['init_signature'], pair[1]['init_signature'])
        return pair[0]['portable'], pair[1]['portable']

    def test_mode_default_and_valid_constructor_assignment_values(self):
        expected, actual = self.observe('settings')
        self.assertEqual(expected['default'], actual['default'])
        self.assertEqual(expected['valid'], actual['valid'])
        self.assertEqual(expected['switch_fields'], actual['switch_fields'])
        self.assertEqual(expected['unknown_init_argument'], actual['unknown_init_argument'])
        # The native signature adds private keyword-only transport controls.
        # Public argument count errors must still reject before creating a run.
        self.assertEqual(expected['too_many_init_arguments'], actual['too_many_init_arguments'])

    def test_mode_rejection_equivalence_and_unchanged_assignment_state(self):
        expected, actual = self.observe('settings')
        self.assertEqual(set(expected['invalid']), set(actual['invalid']))
        # Settings.mode is selected, while the Pydantic-generated validation
        # machinery is target NO. Keep both original errors in the artifact;
        # this explicit native equivalent must reject every same input without
        # altering the existing field. Unknown error kinds/messages still fail.
        for key, reference in expected['invalid'].items():
            native = actual['invalid'][key]
            self.assertEqual(reference['mode_after'], 'offline')
            self.assertEqual(native['mode_after'], 'offline')
            message = 'Settings.mode cannot be None' if key == 'None' else f'Invalid Settings.mode: {key}'
            for stage in ['construct', 'assign']:
                self.assertEqual(reference[stage]['error'], 'ValidationError')
                self.assertIn('type=literal_error', reference[stage]['message'])
                self.assertEqual(native[stage], {'error': 'UsageError', 'message': message})

    def test_disabled_integration_switches_and_reinit(self):
        self.assertEqual(*self.observe('disabled'))

    def test_service_branch_dispatch_and_tensorboard_precedence(self):
        self.assertEqual(*self.observe('dispatch'))

    def test_real_offline_reinit_mode_precedence_and_disabled_transition(self):
        self.assertEqual(*self.observe('offline'))

    def test_adapter_failure_precedes_global_binding_and_durable_run_start(self):
        self.assertEqual(*self.observe('adapter_failure'))

    def test_failed_adapter_setup_preserves_previous_run_under_each_reinit_policy(self):
        self.assertEqual(*self.observe('adapter_failure_previous'))

    def test_real_reference_offline_history_reader_without_tensorboard(self):
        """Catch shared transaction reader regressions with genuine metrics.

        Main caller: ordinary focused init tests, requiring only pinned W&B.
        Why this exists: optional writer skips must not hide the helper's bytes
        interface or reference service flush boundary. Capture remains exact.
        """
        expected, actual = self.observe('history_reader')
        self.assertEqual(expected, actual)
        self.assertEqual(expected, {'metrics': [
            {'history_reader/loss': 1.25, 'count': 3},
            {'history_reader/loss': 0.5, 'count': 4}], 'global_empty': True})
        capture = ROOT / 'test-results/framework-init-history_reader-reference.wandb'
        self.assertTrue(capture.is_file())
        self.assertEqual(capture.read_bytes()[:7], b':W&B\xe1\xbe\x00')

    def test_real_local_metadata_config_and_positional_parameter_matrix(self):
        self.assertEqual(*self.observe('parameters'))

    def test_omitted_metadata_and_actual_project_inference(self):
        expected, actual = self.observe('defaults')
        self.assertEqual(set(expected), set(actual))
        self.assertEqual(len(expected), 9)
        for name in expected:
            if name.endswith('git_privacy'):
                base = {'entity': 'dummy' if name.startswith('disabled') else '',
                        'name': 'dummy-' + name if name.startswith('disabled') else None,
                        'global_empty': True, 'global_identity': True,
                        'input_entity': None, 'input_name': None, 'input_project': None,
                        'input_root_is_requested': True,
                        'root_is_requested': name.startswith('offline'),
                        'root_is_system_temp': name.startswith('disabled')}
                # Pinned project inference reads Git despite disable_git=True.
                # Keep both raw outcomes; native honors that explicit privacy
                # request before inspecting local repository identity.
                self.assertEqual(expected[name], {**base, 'project': 'inference-repository'})
                self.assertEqual(actual[name], {**base, 'project': 'dummy' if name.startswith('disabled') else 'uncategorized'})
            else:
                self.assertEqual(expected[name], actual[name], name)

    def test_resume_boolean_branch_and_active_must_interactions(self):
        self.assertEqual(*self.observe('interactions'))

    def test_independent_supported_init_imports(self):
        for import_path in ['wandb', 'wandb.sdk']:
            with self.subTest(import_path=import_path):
                self.assertEqual(*self.observe('imports', import_path=import_path))

    def test_actual_save_code_artifact_and_disable_precedence(self):
        expected, actual = self.observe('save_code')
        self.assertEqual(set(expected), set(actual))
        self.assertEqual(len(expected), 6)
        manifest = [{'root_matches': True, 'manifest_names': ['framework-fixture.py']}]
        for name in expected:
            source, native = expected[name], actual[name]
            if name == 'offline:True:True':
                # Pinned explicit code_dir logging ignores disable_code=True.
                # Preserve the original artifact result and the native privacy
                # repair as separate exact observations, without normalization.
                self.assertEqual(source, {'save_code': True, 'disable_code': True,
                                         'calls': manifest, 'global_empty': True})
                self.assertEqual(native, {'save_code': True, 'disable_code': True,
                                         'calls': [], 'global_empty': True})
            else:
                self.assertEqual(source, native)
                self.assertEqual(native['calls'], manifest if name == 'offline:True:False' else [])
            self.assertTrue(native['global_empty'])

    def test_allow_val_change_explicit_repair_retains_original_source_errors_and_state(self):
        """Check an intentional repair instead of pretending the SDKs agree.

        Main caller: focused init review. Pinned init declares this argument
        but never applies it; native honors it and mutates supplied Settings.
        Pinned config also ignores the True Settings field. Rejected native
        edits retain the exact reference ConfigErrors; enabled native edits
        prove the repair reaches real config behavior, not just field storage.
        """
        expected, actual = self.observe('allow_val_change')
        false_control = expected['offline:False:False']
        for mode in ['disabled', 'offline']:
            for initial, argument in [(False, True), (True, False), (False, False), (True, None)]:
                key = f'{mode}:{initial}:{argument}'
                source, native = expected[key], actual[key]
                repaired = initial if argument is None else argument
                self.assertEqual(source['input_allow_val_change'], initial)
                self.assertEqual(source['run_allow_val_change'], initial)
                self.assertEqual(native['input_allow_val_change'], repaired)
                self.assertEqual(native['run_allow_val_change'], repaired)
                if initial == repaired and (mode == 'disabled' or not initial):
                    self.assertEqual(source, native)
                if mode == 'offline':
                    for observed, allowed in [(source, False), (native, repaired)]:
                        for edit in ['numeric_edit', 'nested_edit']:
                            if allowed:
                                self.assertEqual(observed[edit], {'value': None})
                            else:
                                self.assertEqual(observed[edit], false_control[edit])
                                self.assertEqual(observed[edit]['error'], 'ConfigError')
                        self.assertEqual(observed['config_after'],
                            {'value': 2, 'nested': {'depth': 3}} if allowed else
                            {'value': 1, 'nested': {'depth': 2}})

    @unittest.skipUnless(importlib.util.find_spec('tensorboard'),
                         'Actual init TensorBoard writer contract requires installed tensorboard')
    def test_actual_tensorboard_init_runtime(self):
        expected, actual = self.observe('tensorboard_runtime')
        self.assertEqual(expected, actual)
        for key, case in actual.items():
            enabled = case['mode'] != 'disabled' and case.get('reenabled', case['sync_tensorboard'])
            self.assertEqual(case['values'], [0.75] if enabled else [])
            self.assertEqual(case['global_steps'], [3] if enabled else [])
            self.assertTrue(case['real_event_files'])
        self.assertFalse(actual['offline:return_previous_enable']['sync_tensorboard'])
        self.assertTrue(actual['offline:return_previous_enable']['returned_previous'])

    @unittest.skipUnless(importlib.util.find_spec('gymnasium') or importlib.util.find_spec('gym'),
                         'Actual init Gym recorder contract requires installed gymnasium or gym')
    def test_actual_gym_init_runtime(self):
        expected, actual = self.observe('gym_runtime')
        self.assertEqual(expected, actual)
        for key, case in actual['cases'].items():
            self.assertEqual(case['patched'], key == 'offline:True')
            self.assertTrue(case['same_hook_after_repeated_init'])


if __name__ == '__main__':
    unittest.main()
