"""Check genuine init auth selection before active-run lifecycle changes.

Architecture: two isolated SDK processes -> raw observations and diagnostics ->
explicit per-call contracts. Main caller: focused local and optional CI suites.
Unavailable hosted branches are labelled rather than treated as live services.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from framework_gap_process import run_observation
from reference_runtime import reference_runtime

ROOT = Path(__file__).resolve().parents[1]


class FrameworkInitAuthContracts(unittest.TestCase):
    def observe(self, scenario):
        """Save mandatory result files and original diagnostics before checks."""
        if not os.environ.get('BACKPROP_REFERENCE_PYTHON'):
            try:
                importlib.metadata.distribution('wandb')
            except importlib.metadata.PackageNotFoundError:
                self.skipTest('Pinned W&B0.30.0 wheel is required')
        reference = reference_runtime()
        pair = []
        for sdk in ['reference', 'native']:
            output = ROOT / f'test-results/framework-init-auth-{scenario}-{sdk}.json'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WANDB_', 'BACKPROP_', 'FOUNDRY_'))}
            env.update(PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(ROOT / 'tests') if sdk == 'reference' else str(ROOT / 'sdk'))
            python = reference.python if sdk == 'reference' else sys.executable
            result = run_observation([python, '-B', str(ROOT / 'tests/framework_init_auth_probe.py'),
                                      sdk, scenario], output, cwd=tempfile.gettempdir(), env=env, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            pair.append(json.loads(output.read_text())['observations'])
        return pair

    def test_failing_auth_precedes_all_reinit_policies(self):
        expected, actual = self.observe('failure')
        self.assertEqual(expected, actual)
        for name, case in actual.items():
            self.assertEqual(case['result'], {'error': name.split(':')[1],
                                             'message': 'fixture rejected auth'})
            self.assertEqual(case['calls'], ['prompt'])
            self.assertTrue(case['global_previous'])
            self.assertEqual(case['previous_log'], {'value': None})
            self.assertEqual(case['credential_environment'], {})

    def test_init_credential_source_relogin_and_force_selection(self):
        expected, actual = self.observe('selection')
        self.assertEqual(set(expected), set(actual))
        self.assertEqual(len(expected), 36)
        # Keep original full backend outcomes in the artifacts. Pinned hosted
        # construction stops at a declared sentinel; native has a real Run
        # backed only by the inert unavailable-store boundary fixture.
        for name in expected:
            if name.startswith(('host_scope:', 'init_relogin:')):
                # Actual pinned init reuses an old-origin session or ignores
                # a directly supplied relogin setting. Keep those original
                # outcomes; native honors origin ownership and explicit relogin.
                self.assertEqual(expected[name]['chosen_source'], 'setup')
                self.assertEqual(expected[name]['prompt_calls'], [])
                self.assertEqual(actual[name]['chosen_source'], 'prompt')
                _, setup_force, override = name.split(':')
                force = setup_force == 'True' if override == 'None' else override == 'True'
                self.assertEqual(actual[name]['prompt_calls'], [{'force': force}])
                host = 'https://different-init.invalid' if name.startswith('host_scope:') else 'https://framework-init.invalid'
                self.assertEqual(actual[name]['selected'], {
                    'token': 'd' * 40, 'run_api_key': 'd' * 40, 'mode': 'online',
                    'host': host})
                self.assertEqual(expected[name]['selected'], {
                    'token': 'b' * 40, 'run_api_key': 'b' * 40, 'mode': 'online',
                    'host': host})
            else:
                self.assertEqual(expected[name]['chosen_source'], actual[name]['chosen_source'], name)
                self.assertEqual(expected[name]['prompt_calls'], actual[name]['prompt_calls'], name)
                self.assertEqual(expected[name]['selected'], actual[name]['selected'], name)
            self.assertEqual(expected[name]['raw_result'],
                             {'error': 'RuntimeError', 'message': 'declared hosted backend boundary'})
            self.assertEqual(actual[name]['raw_result'], {'value': False})

    def test_actual_selected_session_precedes_later_environment_until_teardown(self):
        expected, actual = self.observe('session')
        self.assertEqual(set(expected), {'environment', 'explicit', 'prompt'})
        self.assertEqual(set(expected), set(actual))
        for first_source in expected:
            source, native = expected[first_source], actual[first_source]
            self.assertEqual(source['calls'], native['calls'])
            self.assertEqual(len(source['runs']), 3)
            self.assertEqual(len(native['runs']), 3)
            for index, (left, right) in enumerate(zip(source['runs'], native['runs'])):
                self.assertEqual(left['selected'], right['selected'])
                self.assertEqual(left['chosen_source'], first_source if index < 2 else 'setup')
                self.assertEqual(left['chosen_source'], right['chosen_source'])
                self.assertEqual(left['raw_result'],
                    {'error': 'RuntimeError', 'message': 'declared hosted backend boundary'})
                self.assertEqual(right['raw_result'], {'value': False})


if __name__ == '__main__':
    unittest.main()
